#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu server. Run as root:  bash /opt/pumpscreener/install.sh
set -euo pipefail

APP=/opt/pumpscreener
cd "$APP"

echo "== Installing system packages =="
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv python3-pip git ufw chrony curl jq >/dev/null
timedatectl set-ntp true || true

echo "== Firewall: allow SSH only =="
SSH_PORT=$( (sshd -T 2>/dev/null || true) | awk '/^port /{print $2; exit}')
SSH_PORT=${SSH_PORT:-22}
ufw allow "${SSH_PORT}/tcp" >/dev/null
ufw --force enable >/dev/null
echo "Firewall on; SSH port ${SSH_PORT} left open."

echo "== Creating service user =="
id screener >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin screener

echo "== Python environment =="
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

if [ ! -f .env ]; then
  echo
  echo "== Keys (paste each one, then press Enter) =="
  read -rs -p "Helius API key (hidden as you paste): " HELIUS; echo
  read -rs -p "Telegram bot token from @BotFather (hidden): " TGTOKEN; echo
  echo
  echo "Now open Telegram, find your new bot, and send it any message (e.g. hi)."
  read -r -p "Press Enter once you have sent it a message... " _
  CHAT_ID=$(curl -s --config <(printf 'url = "https://api.telegram.org/bot%s/getUpdates"' "$TGTOKEN") \
    | jq -r '[.result[].message.chat.id] | last // empty')
  if [ -z "$CHAT_ID" ]; then
    read -r -p "Could not detect your chat id automatically. Paste it here: " CHAT_ID
  fi
  CODE=$((RANDOM % 9000 + 1000))
  curl -s --config <(printf 'url = "https://api.telegram.org/bot%s/sendMessage"' "$TGTOKEN") \
    --data-urlencode "chat_id=${CHAT_ID}" --data-urlencode "text=Screener setup code: ${CODE}" >/dev/null
  read -r -p "Type the 4-digit code the bot just sent you on Telegram: " ENTERED
  if [ "$ENTERED" != "$CODE" ]; then
    echo "Code did not match. Make sure YOU sent the bot a message, then run install.sh again."
    exit 1
  fi
  echo "Telegram confirmed."
  cat > .env <<EOF
HELIUS_API_KEY=${HELIUS}
TELEGRAM_BOT_TOKEN=${TGTOKEN}
TELEGRAM_CHAT_ID=${CHAT_ID}
TIMEZONE=Australia/Perth
REPORT_HOUR=8
EOF
fi

mkdir -p data
# Code stays owned by root (the service cannot modify itself); only data/ and .env belong to the service user
chown -R root:root "$APP"
chown -R screener:screener data
chown screener:screener .env
chmod 600 .env

echo
echo "== Checking everything works against live data =="
if ! sudo -u screener .venv/bin/python -m screener check; then
  echo
  echo "Some checks failed. Copy everything above and send it to Claude. The bot has NOT been started."
  exit 1
fi

echo "== Starting the screener as a service =="
cp pumpscreener.service /etc/systemd/system/pumpscreener.service
systemctl daemon-reload
systemctl enable --now pumpscreener
sleep 3
systemctl --no-pager status pumpscreener | head -5
echo
echo "Done. You should get a 'Screener started' message on Telegram."
echo "Useful commands:  journalctl -u pumpscreener -f                                   (live log)"
echo "                  bash $APP/update.sh                                            (install an update)"
echo "                  sudo -u screener $APP/.venv/bin/python -m screener report      (summary now)"
