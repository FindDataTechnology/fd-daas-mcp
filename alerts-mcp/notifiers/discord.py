"""Discord incoming-webhook notifier."""
from __future__ import annotations

import httpx

from .base import Notifier


class DiscordNotifier(Notifier):
    name = "discord"
    env_keys = ("ALERTS_DISCORD_WEBHOOK_URL",)

    def _url(self, ctx: dict) -> str | None:
        override = (ctx.get("channel_overrides") or {}).get("discord", {})
        if isinstance(override, dict) and override.get("webhook_url"):
            return str(override["webhook_url"])
        return self._env("ALERTS_DISCORD_WEBHOOK_URL")

    def send(self, message: str, ctx: dict) -> dict:
        url = self._url(ctx)
        if not url:
            return {"ok": False, "error": "not configured", "missing_keys": ["ALERTS_DISCORD_WEBHOOK_URL"]}
        # Discord caps embeds; plain content allows 2000 chars. Truncate to be safe.
        text = message if len(message) <= 2000 else message[:1996] + " …"
        try:
            resp = httpx.post(url, json={"content": text}, timeout=15)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        # Discord returns 204 No Content on success.
        if resp.status_code in (200, 204):
            return {"ok": True}
        return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
