#!/usr/bin/env bash
# One-time: let the server push daily data to your private pumpfun-data repo. Run as root:
#   bash /opt/pumpscreener/setup-publish.sh
set -euo pipefail
APP=/opt/pumpscreener
REPO_SSH="git@github.com:jakemotta98/pumpfun-data.git"
cd "$APP"
command -v ssh-keygen >/dev/null && command -v git >/dev/null || { echo "ssh-keygen or git missing - tell Claude"; exit 1; }

KEY=data/deploy_key
if [ ! -f "$KEY" ]; then
  sudo -u screener ssh-keygen -t ed25519 -N "" -C "pumpscreener-data" -f "$KEY" >/dev/null
fi
chown screener:screener "$KEY" "$KEY.pub"; chmod 600 "$KEY"

echo
echo "=== Copy the ONE line below (starts with ssh-ed25519) ==="
echo
cat "$KEY.pub"
echo
echo "Then in your browser:"
echo "  1. Open https://github.com/jakemotta98/pumpfun-data/settings/keys"
echo "  2. Click 'Add deploy key'"
echo "  3. Title: pumpscreener server"
echo "  4. Key: paste the line"
echo "  5. Tick 'Allow write access'"
echo "  6. Click 'Add key'"
echo
read -r -p "Press Enter once the key is added... " _

grep -q '^PUBLISH_REPO=' .env || echo "PUBLISH_REPO=${REPO_SSH}" >> .env
chown screener:screener .env; chmod 600 .env

echo "Testing a first publish..."
if sudo -u screener .venv/bin/python -m screener publish; then
  systemctl restart pumpscreener
  echo "Done. The screener will publish after each daily summary and every 6 hours. /publish on Telegram does it now."
else
  echo "Publish failed. Copy the output above and send it to Claude."
  exit 1
fi
