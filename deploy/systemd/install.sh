#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/home/ubuntu/BeautyBridge_App"

sudo cp "$APP_DIR/deploy/systemd/beautybridge.service" /etc/systemd/system/beautybridge.service
sudo cp "$APP_DIR/deploy/systemd/beautybridge-worker.service" /etc/systemd/system/beautybridge-worker.service
sudo systemctl daemon-reload
sudo systemctl enable beautybridge beautybridge-worker
sudo systemctl restart beautybridge
sudo systemctl restart beautybridge-worker

echo "BeautyBridge web + worker services restarted."
