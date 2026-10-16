"""Minimal Telegram bot: send messages, receive commands from the owner's chat only."""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger("telegram")


class Telegram:
    def __init__(self, token: str | None, chat_id: str | None, http: httpx.AsyncClient | None = None):
        self.token = token
        self.chat_id = str(chat_id) if chat_id else None
        self.http = http or httpx.AsyncClient(timeout=40)
        self.offset = 0

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    async def send(self, text: str) -> None:
        if not self.enabled:
            log.info("telegram disabled; message: %s", text)
            return
        for chunk in [text[i:i + 3800] for i in range(0, len(text), 3800)] or [""]:
            try:
                await self.http.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                                     json={"chat_id": self.chat_id, "text": chunk,
                                           "disable_web_page_preview": True})
            except httpx.HTTPError as e:
                log.warning("telegram send failed: %s", e)

    async def commands(self) -> list[str]:
        """Long-poll for new commands. Messages from any other chat are ignored."""
        if not self.enabled:
            return []
        try:
            r = await self.http.get(f"https://api.telegram.org/bot{self.token}/getUpdates",
                                    params={"offset": self.offset, "timeout": 30})
            data = r.json()
        except Exception as e:
            log.warning("telegram poll failed: %s", e)
            return []
        out = []
        for u in data.get("result") or []:
            self.offset = max(self.offset, u["update_id"] + 1)
            msg = u.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != self.chat_id:
                continue
            text = (msg.get("text") or "").strip()
            if text.startswith("/"):
                out.append(text.split()[0].split("@")[0].lower())
        return out
