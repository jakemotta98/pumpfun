#!/usr/bin/env bash
# Pull the latest code and restart. Run as root:  bash /opt/pumpscreener/update.sh
set -euo pipefail
cd /opt/pumpscreener
git pull --ff-only
.venv/bin/pip install -q -r requirements.txt
chown -R root:root /opt/pumpscreener
chown -R screener:screener data
chown screener:screener .env
chmod 600 .env
cp pumpscreener.service /etc/systemd/system/pumpscreener.service
systemctl daemon-reload
sudo -u screener .venv/bin/python -m screener check || { echo "Check failed; service NOT restarted."; exit 1; }
systemctl restart pumpscreener
echo "Updated and restarted."
