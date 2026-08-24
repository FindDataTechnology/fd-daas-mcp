"""Slack incoming-webhook notifier."""
from __future__ import annotations

import httpx

from .base import Notifier


class SlackNotifier(Notifier):
    name = "slack"
    env_keys = ("ALERTS_SLACK_WEBHOOK_URL",)

    def _url(self, ctx: dict) -> str | None:
        override = (ctx.get("channel_overrides") or {}).get("slack", {})
        if isinstance(override, dict) and override.get("webhook_url"):
            return str(override["webhook_url"])
        return self._env("ALERTS_SLACK_WEBHOOK_URL")

    def send(self, message: str, ctx: dict) -> dict:
        url = self._url(ctx)
        if not url:
            return {"ok": False, "error": "not configured", "missing_keys": ["ALERTS_SLACK_WEBHOOK_URL"]}
        try:
            resp = httpx.post(url, json={"text": message}, timeout=15)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if resp.status_code == 200 and resp.text.strip() == "ok":
            return {"ok": True}
        if resp.status_code == 200:
            return {"ok": True}
        return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
