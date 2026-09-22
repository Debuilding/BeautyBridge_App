# BeautyBridge systemd deployment

The Flask/Gunicorn web process and the background worker are intentionally
separate. The web service runs with `BEAUTYBRIDGE_ROLE=web`; the worker runs
with `BEAUTYBRIDGE_ROLE=worker`.

On the production VM:

```bash
sudo cp deploy/systemd/beautybridge.service /etc/systemd/system/beautybridge.service
sudo cp deploy/systemd/beautybridge-worker.service /etc/systemd/system/beautybridge-worker.service
sudo systemctl daemon-reload
sudo systemctl enable --now beautybridge
sudo systemctl enable --now beautybridge-worker
sudo systemctl restart beautybridge
sudo systemctl restart beautybridge-worker
```

Check:

```bash
systemctl status beautybridge --no-pager
systemctl status beautybridge-worker --no-pager
curl -fsS http://127.0.0.1:5000/health/live
curl -fsS http://127.0.0.1:5000/health/ready
```

Do not put secrets in these unit files. Runtime secrets remain in the
production `.env` and ignored client configuration.
