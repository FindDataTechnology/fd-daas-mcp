"""Feishu (飞书 / Lark) custom-bot webhook notifier.

https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot
Security: an optional `secret` enables signing — `sign = base64(
hmac-sha256(key=ts + "\\n" + secret, msg=""))` sent as `timestamp` + `sign`
fields in the JSON body.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time

import httpx

from .base import Notifier


class FeishuNotifier(Notifier):
    name = "feishu"
    env_keys = ("ALERTS_FEISHU_WEBHOOK_URL",)

    def _url(self, ctx: dict) -> str | None:
        override = (ctx.get("channel_overrides") or {}).get("feishu", {})
        if isinstance(override, dict) and override.get("webhook_url"):
            return str(override["webhook_url"])
        return self._env("ALERTS_FEISHU_WEBHOOK_URL")

    def _sign(self, secret: str, timestamp_s: int) -> str:
        string_to_sign = f"{timestamp_s}\n{secret}"
        digest = hmac.new(
            string_to_sign.encode("utf-8"),
            b"",
            digestmod=hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("utf-8")

    def send(self, message: str, ctx: dict) -> dict:
        url = self._url(ctx)
        if not url:
            return {"ok": False, "error": "not configured", "missing_keys": ["ALERTS_FEISHU_WEBHOOK_URL"]}
        body: dict = {"msg_type": "text", "content": {"text": message}}
        secret = self._env("ALERTS_FEISHU_SECRET")
        if secret:
            ts = int(time.time())
            body["timestamp"] = str(ts)
            body["sign"] = self._sign(secret, ts)
        try:
            resp = httpx.post(url, json=body, timeout=15)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if resp.status_code != 200:
            return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        data = resp.json()
        # Feishu returns {"code":0,"msg":"success"} or {"StatusCode":0}.
        code = data.get("code", data.get("StatusCode"))
        if code not in (0, None):
            return {"ok": False, "error": f"code {code}: {data.get('msg', data.get('StatusMessage'))}"}
        return {"ok": True}
