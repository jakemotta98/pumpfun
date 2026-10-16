# Pump.fun post-migration screener (phase 1: log only)

Watches every Pump.fun token that graduates to PumpSwap, records its on-chain state at 15 minutes, 1 hour, 4 hours and 24 hours after graduation, and tracks its price for 9 days. It does not trade. The rules it follows are fixed in `PREREGISTRATION.md`.

## Setup (about 30 minutes, all in a browser)

### 1. Rent the server
1. Sign up at DigitalOcean (or any provider offering Ubuntu servers with a browser console).
2. Create a Droplet: **Ubuntu 24.04**, the cheapest regular plan (1 GB RAM is enough; around US$6/month, check the current price), any region.
3. Choose a root password when asked (or an SSH key if you know how). Write it down.

### 2. Get your Helius key
1. Sign up at helius.dev (free plan).
2. In the dashboard, copy your **API key**.

### 3. Make your Telegram bot
1. In Telegram, search for **@BotFather** and open it.
2. Send `/newbot`, pick any name, then a username ending in `bot`.
3. Copy the **token** it gives you (looks like `123456:ABC...`).

### 4. Install
1. In DigitalOcean, open your Droplet and click **Console** (it opens a terminal in your browser, logged in as root).
2. Paste these three lines:
   ```
   apt-get update && apt-get install -y git
   git clone https://github.com/jakemotta98/pumpfun /opt/pumpscreener
   bash /opt/pumpscreener/install.sh
   ```
3. When asked, paste the Helius key, then the Telegram token (nothing shows as you paste; that is deliberate).
4. Send your bot any message in Telegram, press Enter, then type the 4-digit code the bot sends you.
5. The installer runs a live check (takes up to 4 minutes). If everything says OK, it starts the screener and you get a "Screener started" message on Telegram.
6. If anything says FAIL, copy the whole output and send it to Claude. Nothing has been started.

## Day to day
- Every morning at 8am Perth time: a summary on Telegram.
- Telegram commands: `/status`, `/report`.
- Install an update from Claude: open the Console and run `bash /opt/pumpscreener/update.sh`.
- Live log: `journalctl -u pumpscreener -f` (Ctrl+C to exit).
- Export the data for analysis: `sudo -u screener /opt/pumpscreener/.venv/bin/python -m screener export /opt/pumpscreener/data/export.csv`

## What it costs to run
- Server: about US$6/month.
- Helius: free plan to start. The daily summary shows call counts; if the Helius dashboard shows credits running out, upgrade to the Developer plan (US$49/month).
- Dexscreener: free.

## Safety
- No wallet, no keys to any wallet, no trading code in this phase.
- The code is owned by root and the service runs as a locked-down user that can only write to `data/`.
- Your Helius key and Telegram token live in `/opt/pumpscreener/.env`, readable only by the service.
