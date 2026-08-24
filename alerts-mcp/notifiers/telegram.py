"""Telegram Bot API notifier."""
from __future__ import annotations

import httpx

from .base import Notifier


class TelegramNotifier(Notifier):
    name = "telegram"
    env_keys = ("ALERTS_TELEGRAM_BOT_TOKEN",)

    def _chat_id(self, ctx: dict) -> str | None:
        # Per-rule override in channels_json["telegram"]["chat_id"] wins over env.
        override = (ctx.get("channel_overrides") or {}).get("telegram", {})
        if isinstance(override, dict) and override.get("chat_id"):
            return str(override["chat_id"])
        return self._env("ALERTS_TELEGRAM_CHAT_ID")

    def is_configured(self) -> bool:
        # Need a bot token AND a resolvable chat id (env default or per-rule override).
        if not self._env("ALERTS_TELEGRAM_BOT_TOKEN"):
            return False
        return True

    def missing_keys(self) -> list[str]:
        miss = [k for k in self.env_keys if self._env(k) is None]
        if self._env("ALERTS_TELEGRAM_BOT_TOKEN") and not self._env("ALERTS_TELEGRAM_CHAT_ID"):
            miss.append("ALERTS_TELEGRAM_CHAT_ID")
        return miss

    def send(self, message: str, ctx: dict) -> dict:
        token = self._env("ALERTS_TELEGRAM_BOT_TOKEN")
        chat_id = self._chat_id(ctx)
        if not token:
            return {"ok": False, "error": "not configured", "missing_keys": ["ALERTS_TELEGRAM_BOT_TOKEN"]}
        if not chat_id:
            return {"ok": False, "error": "no chat_id (set ALERTS_TELEGRAM_CHAT_ID or channels override)"}
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        # Telegram caps messages at 4096 chars; truncate the head to keep the rule name.
        text = message if len(message) <= 4096 else message[:4090] + "\n…"
        try:
            resp = httpx.post(url, json={"chat_id": chat_id, "text": text}, timeout=15)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if resp.status_code != 200:
            return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        data = resp.json()
        if not data.get("ok"):
            return {"ok": False, "error": data.get("description", "telegram error")}
        return {"ok": True, "message_id": data.get("result", {}).get("message_id")}
