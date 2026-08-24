"""DingTalk (钉钉) custom-robot webhook notifier.

https://open.dingtalk.com/document/robots/custom-robot-access
Security: the webhook URL carries an `access_token`; an optional `secret`
enables HMAC-SHA256 signing (`&timestamp=<ms>&sign=<base64(hmac-sha256(
secret, ts + "\\n" + secret))>` appended to the URL).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse

import httpx

from .base import Notifier


class DingTalkNotifier(Notifier):
    name = "dingtalk"
    env_keys = ("ALERTS_DINGTALK_WEBHOOK_URL",)

    def _url(self, ctx: dict) -> str | None:
        override = (ctx.get("channel_overrides") or {}).get("dingtalk", {})
        if isinstance(override, dict) and override.get("webhook_url"):
            return str(override["webhook_url"])
        return self._env("ALERTS_DINGTALK_WEBHOOK_URL")

    def _sign(self, secret: str, timestamp_ms: int) -> str:
        string_to_sign = f"{timestamp_ms}\n{secret}"
        digest = hmac.new(
            secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        return urllib.parse.quote_plus(base64.b64encode(digest))

    def send(self, message: str, ctx: dict) -> dict:
        url = self._url(ctx)
        if not url:
            return {"ok": False, "error": "not configured", "missing_keys": ["ALERTS_DINGTALK_WEBHOOK_URL"]}
        secret = self._env("ALERTS_DINGTALK_SECRET")
        if secret:
            ts = int(time.time() * 1000)
            url = f"{url}&timestamp={ts}&sign={self._sign(secret, ts)}"
        # DingTalk text cap ~20000 chars; truncate defensively.
        text = message if len(message) <= 20000 else message[:19996] + " …"
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
