"""
Regression test: get_available_slots() must never offer a time block that is
shorter than the requested service's own duration. Found in production logs
for service 543063 (duration=120) on 2026-09-21, where the raw Bookon
response included 10:00-10:30, 15:35-15:45 and 15:40-15:45 - none of which
can actually fit a 120-minute service, yet all three were being offered to
the client. check_slot() (the final pre-booking recheck) already filtered
these correctly, so the failure only showed up as a confusing "slot no
longer available" right after the client picked one of these bad options -
not because someone else took it, but because it was never valid.

Block times below are UTC ("Z"); _parse_slot converts them to Europe/Kyiv
(UTC+3 in September), so a 14:00Z block appears as 17:00 in the Slot.
"""

from crm.bookon import BookonAdapter


CFG = {
    "crm": {"branch_id": "9970"},
    "local_tz": "Europe/Kyiv",
    "masters": {"36645": "Світлана"},
    "services": {"543063": {"name": "Манікюр комплекс гель", "duration": 120}},
    "booking_rules": {"offer_slots_limit": 3},
}


def _block(start, end):
    return {"startTime": f"2026-09-21T{start}:00Z", "stopTime": f"2026-09-21T{end}:00Z"}


def test_duration_filter_keeps_only_long_enough_blocks(monkeypatch):
    adapter = BookonAdapter(CFG)
    raw = {
        "36645": {
            "2026-09-21": [
                _block("07:00", "07:30"),  # 30 min UTC - too short for 120-min service
                _block("12:35", "12:45"),  # 10 min - too short
                _block("12:40", "12:45"),  # 5 min - too short
                _block("14:00", "16:15"),  # 135 min - long enough, should survive
            ]
        }
    }
    monkeypatch.setattr(adapter, "_raw_response", lambda service_id, date_str: raw)

    slots = adapter.get_available_slots("543063", "2026-09-21")

    assert len(slots) == 1, f"expected only the long-enough block to survive, got {slots}"
    assert slots[0].start == "17:00"  # 14:00Z -> Europe/Kyiv (+3)
    assert slots[0].end == "19:15"


def test_unknown_service_duration_does_not_filter(monkeypatch):
    """If the service isn't in config at all, fail open (keep old behavior)
    rather than silently hiding every slot for a misconfigured service."""
    adapter = BookonAdapter(CFG)
    raw = {"36645": {"2026-09-21": [_block("07:00", "07:15")]}}
    monkeypatch.setattr(adapter, "_raw_response", lambda service_id, date_str: raw)

    slots = adapter.get_available_slots("999999-unknown", "2026-09-21")

    assert len(slots) == 1
