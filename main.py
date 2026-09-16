import csv
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, request, send_file
from openai import OpenAI

from config import (
    ADMIN_API_TOKEN,
    ADMIN_CHAT_ID,
    BRANDS,
    DB_PATH,
    LOCAL_TZ,
    META_APP_SECRET,
    OPENAI_MODEL,
    TELEGRAM_BOT_TOKEN,
    VERIFY_TOKEN,
)
from states import BotState, can_transition

app = Flask(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ai = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


def db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, brand TEXT, sender_id TEXT, role TEXT, content TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS events(message_id TEXT PRIMARY KEY, created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS state(brand TEXT, sender_id TEXT, data TEXT NOT NULL DEFAULT '{}', updated_at DATETIME DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(brand,sender_id));
        CREATE TABLE IF NOT EXISTS appointments(id INTEGER PRIMARY KEY AUTOINCREMENT, brand TEXT, sender_id TEXT, name TEXT, phone TEXT, service_id TEXT, service_name TEXT, appointment_date TEXT, appointment_time TEXT, employee_id TEXT, master_name TEXT, crm_visit_id TEXT, status TEXT, paid INTEGER DEFAULT 0, reminder_sent INTEGER DEFAULT 0, reinvite_sent INTEGER DEFAULT 0, notes TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
        """)


init_db()


def state_get(brand, sender):
    with db() as c:
        row = c.execute("SELECT data FROM state WHERE brand=? AND sender_id=?", (brand, sender)).fetchone()
    if not row:
        return {}
    try:
        data = json.loads(row[0])
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def state_set(brand, sender, **updates):
    current = state_get(brand, sender)
    if "state" in updates:
        from_state = current.get("state") or BotState.START.value
        to_state = updates["state"]
        if from_state != to_state and not can_transition(from_state, to_state):
            # Not blocking on this — the state machine models the expected
            # flow, but a conversational bot can hit edge cases it doesn't
            # cover yet. Log it so unexpected jumps are visible without
            # risking a hard failure in the booking flow.
            logging.warning("Unexpected state transition for %s/%s: %s -> %s", brand, sender, from_state, to_state)
    current.update(updates)
    with db() as c:
        c.execute("INSERT INTO state(brand,sender_id,data) VALUES(?,?,?) ON CONFLICT(brand,sender_id) DO UPDATE SET data=excluded.data,updated_at=CURRENT_TIMESTAMP", (brand, sender, json.dumps(current, ensure_ascii=False)))
    return current


def save_message(brand, sender, role, text):
    with db() as c:
        c.execute("INSERT INTO messages(brand,sender_id,role,content) VALUES(?,?,?,?)", (brand, sender, role, str(text)[:8000]))


def history(brand, sender, limit=14):
    with db() as c:
        rows = c.execute("SELECT role,content FROM messages WHERE brand=? AND sender_id=? ORDER BY id DESC LIMIT ?", (brand, sender, limit)).fetchall()
    return [{"role": r, "content": t} for r, t in reversed(rows)]


def mark_event(mid):
    try:
        with db() as c:
            c.execute("INSERT INTO events(message_id) VALUES(?)", (mid,))
        return True
    except sqlite3.IntegrityError:
        return False


def create_local_appointment(brand, sender, **x):
    with db() as c:
        cur = c.execute("INSERT INTO appointments(brand,sender_id,name,phone,service_id,service_name,appointment_date,appointment_time,employee_id,master_name,crm_visit_id,status,paid,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (brand,sender,x.get("name"),x.get("phone"),x.get("service_id"),x.get("service_name"),x.get("date"),x.get("time"),x.get("employee_id"),x.get("master_name"),x.get("crm_visit_id"),x.get("status","requested"),int(bool(x.get("paid",False))),x.get("notes", "")))
        return cur.lastrowid


def set_paid(appointment_id):
    with db() as c:
        c.execute("UPDATE appointments SET paid=1,status='confirmed' WHERE id=?", (appointment_id,))


def cfg_for(brand):
    cfg = BRANDS.get(brand)
    if not cfg or not cfg.get("enabled"):
        raise KeyError(brand)
    return cfg


def brand_by_page(page_id):
    for brand, cfg in BRANDS.items():
        if cfg.get("enabled") and str(cfg.get("page_id", "")) == str(page_id):
            return brand
    return None


def instagram_send(cfg, recipient, text):
    token = cfg.get("page_access_token")
    if not token:
        raise RuntimeError("Instagram page access token is not configured")
    r = requests.post("https://graph.facebook.com/v21.0/me/messages", params={"access_token": token}, json={"recipient":{"id":str(recipient)},"message":{"text":str(text)[:2000]}}, timeout=15)
    if r.status_code >= 300:
        raise RuntimeError(f"Instagram error {r.status_code}: {r.text[:500]}")


def telegram(cfg, text):
    token = TELEGRAM_BOT_TOKEN
    chat = cfg.get("telegram_chat_id") or ADMIN_CHAT_ID
    if not token or not chat:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id":chat,"text":str(text)[:4000]}, timeout=15)
    except requests.RequestException:
        logging.exception("Telegram send failed")


class BookonAdapter:
    """
    Bookon private integration. Isolated here so the product is not tied to
    one CRM. All actual browser/session work lives in bocrm_playwright.py —
    this class only adapts its generic ok/data responses to what the rest of
    the bot expects (a sorted slot list, or a crm visit id / exception).
    """
    def __init__(self, cfg):
        self.cfg = cfg
        self.crm = cfg.get("crm", {})

    def _client(self):
        from bocrm_playwright import BOCRMManualAdapter
        return BOCRMManualAdapter(
            email=self.crm.get("email", ""),
            password=self.crm.get("password", ""),
            branch_id=self.crm.get("branch_id", ""),
            storage_state_path=self.crm.get("storage_state"),
        )

    def slots(self, service_id, date_str):
        datetime.strptime(date_str, "%Y-%m-%d")
        result = self._client().get_available_slots_sync(service_id, date_str)
        if not result.get("ok"):
            logging.error("Bookon slots error: %s", result.get("message"))
            return []

        masters = self.cfg.get("masters", {})
        lines = []
        for specialist_id, dates in (result.get("data") or {}).items():
            for day, blocks in (dates or {}).items():
                for block in blocks or []:
                    try:
                        start = datetime.fromisoformat(str(block["startTime"]).replace("Z", "+00:00"))
                        end = datetime.fromisoformat(str(block["stopTime"]).replace("Z", "+00:00"))
                        lines.append({
                            "employee_id": str(specialist_id),
                            "master": masters.get(str(specialist_id), str(specialist_id)),
                            "date": day,
                            "time": start.strftime("%H:%M"),
                            "end": end.strftime("%H:%M"),
                        })
                    except Exception:
                        continue
        lines.sort(key=lambda x: (0 if 10 <= int(x["time"][:2]) < 12 else 1, x["time"]))
        limit = max(1, int(self.cfg.get("booking_rules", {}).get("offer_slots_limit", 3)))
        return lines[:limit]

    def book(self, employee_id, service_id, date_str, time_str, name, phone):
        result = self._client().create_visit_sync(employee_id, service_id, date_str, time_str, name, phone)
        if not result.get("ok"):
            raise RuntimeError(result.get("message", "Bookon booking failed"))
        return str(result.get("crm_id") or "")


def crm_type(cfg):
    return str(cfg.get("crm_type") or cfg.get("crm",{}).get("type") or "manual").lower()


def tool_specs():
    return [{"type":"function","function":{"name":"remember_booking","description":"Сохрани уже названные клиентом данные: service_id/date_str/time_str/employee_id/name/phone. Не переспрашивай их позже.","parameters":{"type":"object","properties":{"service_id":{"type":"string"},"date_str":{"type":"string"},"time_str":{"type":"string"},"employee_id":{"type":"string"},"name":{"type":"string"},"phone":{"type":"string"}}}}}, {"type":"function","function":{"name":"get_available_slots","description":"Получи актуальные слоты для service_id на date_str. Не вызывай повторно, если state уже имеет выбранные date/time и нужно только имя/телефон.","parameters":{"type":"object","properties":{"service_id":{"type":"string"},"date_str":{"type":"string"}},"required":["service_id","date_str"]}}}, {"type":"function","function":{"name":"create_visit","description":"Создай запись только когда service_id,date_str,time_str,employee_id,name,phone уже собраны.","parameters":{"type":"object","properties":{"service_id":{"type":"string"},"date_str":{"type":"string"},"time_str":{"type":"string"},"employee_id":{"type":"string"},"name":{"type":"string"},"phone":{"type":"string"}},"required":["service_id","date_str","time_str","employee_id","name","phone"]}}}]


def system_prompt(brand, cfg, state):
    missing = [k for k,v in {"service_id":state.get("service_id"),"date":state.get("date"),"time":state.get("time"),"employee_id":state.get("employee_id"),"name":state.get("name"),"phone":state.get("phone")}.items() if not v]
    services = "\n".join(f"{sid} — {v.get('name',sid)}" for sid,v in cfg.get("services",{}).items()) or "каталог ще не налаштований"
    masters = "\n".join(f"{sid} — {name}" for sid,name in cfg.get("masters",{}).items()) or "майстри ще не налаштовані"
    return f"""Ти — AI-адміністратор {cfg.get('name')}. Відповідай коротко й природно українською. Сьогодні {datetime.now(ZoneInfo(LOCAL_TZ)).strftime('%d.%m.%Y')}.\n\nCRM={crm_type(cfg)}; manual/home_master означає ручну заявку без вигадування вільних слотів.\nSTATE={json.dumps(state,ensure_ascii=False)}\nВІДСУТНІ={missing}\n\nПравила: 1) Не втрачай state. Якщо date/time уже є, після імені/телефону не показуй слоти заново. 2) Нові дані зберігай через remember_booking. 3) create_visit тільки коли всі поля зібрані. 4) Послуга з requires_photo=true потребує фото до запису. 5) Не обіцяй запис до SUCCESS. 6) Якщо CRM впала — зроби manual fallback. 7) Після SUCCESS, якщо є передоплата, попроси її. 8) Не повідомляй адресу/Wi-Fi/телефон до оплати, коли block_address_if_not_paid=true. 9) Пріоритетні години: {cfg.get('priority_hours')}. 10) follow-up через {cfg.get('follow_up_days',21)} днів.\n\nПОСЛУГИ:\n{services}\n\nМАЙСТРИ:\n{masters}\n\nПРАЙС:\n{cfg.get('price_text','')}\n\nПередоплата: {cfg.get('prepayment_amount',0)} грн.\n"""


def process_with_ai(brand, sender, text):
    if not ai:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    cfg = cfg_for(brand)
    save_message(brand, sender, "user", text)
    state = state_get(brand, sender)
    messages = [{"role":"system","content":system_prompt(brand,cfg,state)}] + history(brand,sender,14)
    response = ai.chat.completions.create(model=OPENAI_MODEL,messages=messages,tools=tool_specs(),tool_choice="auto",temperature=0.35,max_tokens=700)
    assistant = response.choices[0].message
    if assistant.tool_calls:
        messages.append(assistant)
        for call in assistant.tool_calls:
            args = json.loads(call.function.arguments or "{}")
            result = handle_tool(brand,sender,cfg,call.function.name,args)
            messages.append({"role":"tool","tool_call_id":call.id,"name":call.function.name,"content":result})
        final = ai.chat.completions.create(model=OPENAI_MODEL,messages=messages,temperature=0.3,max_tokens=700)
        reply = final.choices[0].message.content or ""
    else:
        reply = assistant.content or ""
    st = state_get(brand,sender)
    if cfg.get("block_address_if_not_paid",True) and not st.get("receipt"):
        for secret in [cfg.get("address"),cfg.get("phone"),cfg.get("wifi"),cfg.get("wifi_password"),cfg.get("card_number")]:
            if secret:
                reply = reply.replace(secret,"")
    save_message(brand,sender,"assistant",reply)
    return reply


def handle_tool(brand,sender,cfg,name,args):
    if name == "remember_booking":
        upd = {k:v.strip() for k,v in (("service_id",args.get("service_id")),("date",args.get("date_str")),("time",args.get("time_str")),("employee_id",args.get("employee_id")),("name",args.get("name")),("phone",args.get("phone"))) if v}
        upd["state"] = "COLLECTING"
        state_set(brand,sender,**upd)
        return json.dumps({"status":"REMEMBERED",**upd},ensure_ascii=False)
    if name == "get_available_slots":
        if crm_type(cfg) in {"manual","home_master","none"}:
            return "MANUAL_MODE: не вигадуй слоти; збери бажану дату/час."
        try:
            return json.dumps(BookonAdapter(cfg).slots(args["service_id"],args["date_str"]),ensure_ascii=False)
        except Exception as exc:
            logging.exception("Bookon availability failed")
            return json.dumps({"status":"CRM_ERROR","message":str(exc)},ensure_ascii=False)
    if name == "create_visit":
        service_id=args["service_id"]; service_name=cfg.get("services",{}).get(service_id,{}).get("name",service_id); employee_id=args["employee_id"]; master=cfg.get("masters",{}).get(employee_id,employee_id)
        try:
            if crm_type(cfg) in {"manual","home_master","none"}:
                status="pending_manual_confirmation"; crm_id=""
            else:
                crm_id=BookonAdapter(cfg).book(employee_id,service_id,args["date_str"],args["time_str"],args["name"],args["phone"]); status="awaiting_payment" if cfg.get("prepayment_required") else "confirmed"
            appt=create_local_appointment(brand,sender,name=args["name"],phone=args["phone"],service_id=service_id,service_name=service_name,date=args["date_str"],time=args["time_str"],employee_id=employee_id,master_name=master,crm_visit_id=crm_id,status=status)
            state_set(brand,sender,state="WAITING_PAYMENT" if status=="awaiting_payment" else ("WAITING_ADMIN_CONFIRMATION" if status.startswith("pending") else "BOOKED_CONFIRMED"),appointment_id=appt,**{"service_id":service_id,"date":args["date_str"],"time":args["time_str"],"employee_id":employee_id,"name":args["name"],"phone":args["phone"]})
            telegram(cfg,f"✅ BeautyBridge: {cfg.get('name')}\n{service_name}\n{args['date_str']} {args['time_str']}\n{master}\nКлієнт: {args['name']}\nID: {appt}\nCRM: {crm_id or '-'}")
            return json.dumps({"status":"SUCCESS","appointment_id":appt,"crm_id":crm_id,"service":service_name,"master":master,"payment_required":bool(cfg.get("prepayment_required"))},ensure_ascii=False)
        except Exception as exc:
            logging.exception("Booking failed; manual fallback")
            appt=create_local_appointment(brand,sender,name=args["name"],phone=args["phone"],service_id=service_id,service_name=service_name,date=args["date_str"],time=args["time_str"],employee_id=employee_id,master_name=master,status="pending_manual_confirmation",notes=f"CRM fallback: {exc}")
            state_set(brand,sender,state="WAITING_ADMIN_CONFIRMATION",appointment_id=appt,**{"service_id":service_id,"date":args["date_str"],"time":args["time_str"],"employee_id":employee_id,"name":args["name"],"phone":args["phone"]})
            telegram(cfg,f"⚠️ CRM fallback. Manual appointment {appt}. Причина: {exc}")
            return json.dumps({"status":"MANUAL_FALLBACK","appointment_id":appt,"service":service_name,"master":master},ensure_ascii=False)
    return "UNKNOWN_TOOL"


def admin_required(fn):
    @wraps(fn)
    def wrapped(*args,**kwargs):
        if not ADMIN_API_TOKEN: return jsonify({"error":"ADMIN_API_TOKEN is not configured"}),503
        if not hmac.compare_digest(request.headers.get("X-Admin-Token",""),ADMIN_API_TOKEN): return jsonify({"error":"unauthorized"}),401
        return fn(*args,**kwargs)
    return wrapped


_buffers,_timers,_lock={},{},threading.Lock()


def flush(brand,sender):
    key=f"{brand}:{sender}"
    with _lock:
        parts=_buffers.pop(key,[]); _timers.pop(key,None)
    if not parts:return
    try: instagram_send(cfg_for(brand),sender,process_with_ai(brand,sender," ".join(parts)))
    except Exception as exc: logging.exception("Processing failed"); telegram(cfg_for(brand),f"⚠️ BeautyBridge error {brand}: {exc}")


def buffer_message(brand,sender,text):
    key=f"{brand}:{sender}"
    with _lock:
        _buffers.setdefault(key,[]).append(text)
        if _timers.get(key): _timers[key].cancel()
        t=threading.Timer(1.0,flush,args=(brand,sender)); t.daemon=True; _timers[key]=t; t.start()


def daily_tasks():
    tz=ZoneInfo(LOCAL_TZ); tomorrow=(datetime.now(tz)+timedelta(days=1)).date().isoformat(); today=datetime.now(tz).date()
    with db() as c:
        rows=c.execute("SELECT id,brand,sender_id,appointment_time,service_name,master_name FROM appointments WHERE appointment_date=? AND status IN ('confirmed','pending_manual_confirmation') AND reminder_sent=0",(tomorrow,)).fetchall()
        for appt_id,brand,sender,tm,service,master in rows:
            cfg=cfg_for(brand)
            if cfg.get("page_access_token"):
                try: instagram_send(cfg,sender,f"Нагадуємо про запис завтра о {tm} 💅\n{service}\nМайстер: {master}")
                except Exception: logging.exception("reminder failed")
            c.execute("UPDATE appointments SET reminder_sent=1 WHERE id=?",(appt_id,))
        for brand,cfg in BRANDS.items():
            if not cfg.get("enabled"): continue
            target=(today-timedelta(days=int(cfg.get("follow_up_days",21)))).isoformat()
            rr=c.execute("SELECT DISTINCT sender_id,name FROM appointments WHERE brand=? AND appointment_date=? AND status='confirmed' AND reinvite_sent=0",(brand,target)).fetchall()
            for sender,name in rr:
                if cfg.get("page_access_token"):
                    try: instagram_send(cfg,sender,f"Привіт, {name or ''}! 👋 Минуло {cfg.get('follow_up_days',21)} днів. Запросити вас на наступну процедуру? ✨")
                    except Exception: logging.exception("retention failed")
                c.execute("UPDATE appointments SET reinvite_sent=1 WHERE brand=? AND sender_id=? AND appointment_date=?",(brand,sender,target))


def scheduler():
    while True:
        try:
            if datetime.now(ZoneInfo(LOCAL_TZ)).minute < 10: daily_tasks()
        except Exception: logging.exception("scheduler failed")
        time.sleep(3600)

threading.Thread(target=scheduler,daemon=True).start()


def verify_meta_signature(raw_body, signature_header):
    """Check X-Hub-Signature-256 against META_APP_SECRET (HMAC-SHA256 of the raw body)."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(META_APP_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    provided = signature_header.split("=", 1)[1]
    return hmac.compare_digest(expected, provided)


@app.get("/webhook")
def verify():
    if request.args.get("hub.verify_token") != VERIFY_TOKEN:return "Forbidden",403
    return request.args.get("hub.challenge", ""),200


@app.post("/webhook")
def webhook():
    if META_APP_SECRET:
        if not verify_meta_signature(request.get_data(), request.headers.get("X-Hub-Signature-256", "")):
            logging.warning("Webhook signature verification failed, rejecting request")
            return "Forbidden", 403
    else:
        logging.warning("META_APP_SECRET is not set - webhook signature is NOT verified")
    payload=request.get_json(silent=True) or {}
    for entry in payload.get("entry",[]):
        entry_page_id=str(entry.get("id") or "")
        for event in entry.get("messaging",[]):
            msg=event.get("message") or {}
            if msg.get("is_echo"):continue
            page_id=str((event.get("recipient") or {}).get("id") or entry_page_id); brand=brand_by_page(page_id); sender=str((event.get("sender") or {}).get("id") or "")
            if not brand or not sender:continue
            mid=f"{brand}:{msg.get('mid') or time.time_ns()}"
            if not mark_event(mid):continue
            cfg=cfg_for(brand); st=state_get(brand,sender); text=msg.get("text","") or ""; attachments=msg.get("attachments") or []
            if any(a.get("type")=="image" for a in attachments):
                if st.get("state")=="WAITING_PAYMENT" and st.get("appointment_id"):
                    set_paid(st["appointment_id"]); state_set(brand,sender,receipt=True,state="BOOKED_CONFIRMED")
                    telegram(cfg,f"💳 Отримано чек. Запис {st['appointment_id']} підтверджено.")
                    info=["Все отримали ❤️"]
                    if cfg.get("address"):info.append(f"Адреса: {cfg['address']} 📍")
                    if cfg.get("phone"):info.append(f"Телефон: {cfg['phone']} 📱")
                    if cfg.get("wifi"):info.append(f"Wi-Fi: {cfg['wifi']}")
                    try:instagram_send(cfg,sender,"\n".join(info))
                    except Exception:pass
                    text=(text+" [чек передоплати отримано]").strip()
                else:
                    state_set(brand,sender,photo=True); text=(text+" [клієнт надіслав фото]").strip()
            if text:buffer_message(brand,sender,text)
    return "OK",200


@app.get("/health")
def health():
    return jsonify({"status":"ok","ai_configured":bool(ai),"brands":[k for k,v in BRANDS.items() if v.get("enabled")],"admin_configured":bool(ADMIN_API_TOKEN)})


@app.get("/admin/config")
@admin_required
def admin_config():
    return jsonify({k:{"name":v.get("name"),"crm_type":crm_type(v),"services":len(v.get("services",{})),"masters":len(v.get("masters",{})),"priority_hours":v.get("priority_hours",[]),"prepayment_required":v.get("prepayment_required",False),"follow_up_days":v.get("follow_up_days",21),"instagram_connected":bool(v.get("page_id") and v.get("page_access_token"))} for k,v in BRANDS.items()})


@app.get("/admin/export.csv")
@admin_required
def export_csv():
    brand=request.args.get("brand") or None; path=os.path.join("data",f"appointments_{brand or 'all'}.csv")
    with db() as c:
        q="SELECT id,appointment_date,appointment_time,name,phone,service_name,master_name,status,paid,notes,created_at FROM appointments"; p=[]
        if brand:q+=" WHERE brand=?";p.append(brand)
        q+=" ORDER BY appointment_date,appointment_time,id"; rows=c.execute(q,p).fetchall()
    os.makedirs("data",exist_ok=True)
    with open(path,"w",newline="",encoding="utf-8-sig") as f:
        w=csv.writer(f);w.writerow(["ID","Date","Time","Client","Phone","Service","Master","Status","Paid","Notes","Created"]);w.writerows(rows)
    return send_file(path,mimetype="text/csv",as_attachment=True,download_name=os.path.basename(path))


if __name__ == "__main__":
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","5000")))
