"""企业微信 (WeCom / WeChat Work) group-robot webhook notifier.

https://developer.work.weixin.qq.com/document/path/91770
Security: the webhook URL carries a `key` query param (the secret). WeCom group
robots do not use HMAC request signing for outbound messages — the URL key IS
the credential (optionally combined with keyword/IP-allowlist settings on the
bot). `ALERTS_WECOM_SECRET` is reserved for a future signing mode but is not
required and not currently used by `send`.
"""
from __future__ import annotations

import httpx

from .base import Notifier


class WeComNotifier(Notifier):
    name = "wecowork"
    env_keys = ("ALERTS_WECOM_WEBHOOK_URL",)

    def _url(self, ctx: dict) -> str | None:
        override = (ctx.get("channel_overrides") or {}).get("wecowork", {})
        if isinstance(override, dict) and override.get("webhook_url"):
            return str(override["webhook_url"])
        return self._env("ALERTS_WECOM_WEBHOOK_URL")

    def send(self, message: str, ctx: dict) -> dict:
        url = self._url(ctx)
        if not url:
            return {"ok": False, "error": "not configured", "missing_keys": ["ALERTS_WECOM_WEBHOOK_URL"]}
        # WeCom text cap 4096 bytes; truncate defensively.
        text = message if len(message) <= 2000 else message[:1996] + " …"
        try:
            resp = httpx.post(
                url, json={"msgtype": "text", "text": {"content": text}}, timeout=15
            )
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if resp.status_code != 200:
            return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        data = resp.json()
        if data.get("errcode") not in (0, None):
            return {"ok": False, "error": f"errcode {data.get('errcode')}: {data.get('errmsg')}"}
        return {"ok": True}
