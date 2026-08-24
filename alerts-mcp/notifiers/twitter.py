"""Twitter/X notifier via OAuth 1.0a user context (HMAC-SHA1 signing).

Hand-rolled over stdlib `hmac`/`hashlib`/`urllib.parse` + `httpx` — no new
dependency. Posts a status update to the v1.1 endpoint using the four
`ALERTS_TWITTER_*` secrets (consumer key/secret + access token/secret).

`sign(method, url, params, consumer_secret, token_secret)` is exposed at module
level so it can be unit-tested against the RFC 5849 signature vector without a
network call.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
import urllib.parse

import httpx

from .base import Notifier

#: v1.1 tweet endpoint (OAuth 1.0a user context; tweet.write scope).
_STATUSES_UPDATE = "https://api.twitter.com/1.1/statuses/update.json"


def percent_encode(s: str) -> str:
    """RFC 5849 §3.6: encode everything except unreserved `A-Za-z0-9-._~`."""
    return urllib.parse.quote(s, safe="~")


def sign(
    method: str,
    url: str,
    params: list[tuple[str, str]],
    consumer_secret: str,
    token_secret: str,
) -> str:
    """Compute the OAuth 1.0a HMAC-SHA1 signature (base64) for a request.

    `params` is the full list of (key, value) pairs to sign — query params, body
    params (for form-encoded bodies), and the oauth_* params (every param except
    `oauth_signature` itself). Duplicates are preserved; sorting is by
    (percent-encoded key, percent-encoded value).
    """
    # Normalize parameters: encode each key+value, sort, join with &.
    encoded = [(percent_encode(k), percent_encode(v)) for k, v in params]
    encoded.sort()
    normalized = "&".join(f"{k}={v}" for k, v in encoded)
    # Base string: METHOD & encode(url) & encode(normalized_params).
    base_string = (
        f"{method.upper()}&{percent_encode(url)}&{percent_encode(normalized)}"
    )
    signing_key = f"{percent_encode(consumer_secret)}&{percent_encode(token_secret)}"
    digest = hmac.new(
        signing_key.encode("utf-8"),
        base_string.encode("utf-8"),
        digestmod=hashlib.sha1,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def build_auth_header(
    method: str,
    url: str,
    body_params: list[tuple[str, str]],
    consumer_key: str,
    consumer_secret: str,
    token: str,
    token_secret: str,
) -> str:
    """Build the `Authorization: OAuth ...` header for a signed request."""
    oauth_params = [
        ("oauth_consumer_key", consumer_key),
        ("oauth_nonce", secrets.token_hex(16)),
        ("oauth_signature_method", "HMAC-SHA1"),
        ("oauth_timestamp", str(int(time.time()))),
        ("oauth_token", token),
        ("oauth_version", "1.0"),
    ]
    all_params = oauth_params + body_params
    signature = sign(method, url, all_params, consumer_secret, token_secret)
    auth_parts = [
        (k, v) for k, v in oauth_params
    ] + [("oauth_signature", signature)]
    # Header values are percent-encoded and quoted.
    inner = ", ".join(f'{percent_encode(k)}="{percent_encode(v)}"' for k, v in auth_parts)
    return f"OAuth {inner}"


class TwitterNotifier(Notifier):
    name = "twitter"
    env_keys = (
        "ALERTS_TWITTER_CONSUMER_KEY",
        "ALERTS_TWITTER_CONSUMER_SECRET",
        "ALERTS_TWITTER_ACCESS_TOKEN",
        "ALERTS_TWITTER_ACCESS_TOKEN_SECRET",
    )

    def send(self, message: str, ctx: dict) -> dict:
        ck = self._env("ALERTS_TWITTER_CONSUMER_KEY")
        cs = self._env("ALERTS_TWITTER_CONSUMER_SECRET")
        at = self._env("ALERTS_TWITTER_ACCESS_TOKEN")
        ats = self._env("ALERTS_TWITTER_ACCESS_TOKEN_SECRET")
        if not (ck and cs and at and ats):
            return {"ok": False, "error": "not configured", "missing_keys": self.missing_keys()}
        # Twitter caps a tweet at 280 chars; truncate to keep it postable.
        status = message if len(message) <= 280 else message[:277] + "…"
        body_params = [("status", status)]
        auth = build_auth_header(
            "POST", _STATUSES_UPDATE, body_params, ck, cs, at, ats
        )
        try:
            resp = httpx.post(
                _STATUSES_UPDATE,
                data=urllib.parse.urlencode(body_params),
                headers={
                    "Authorization": auth,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=20,
            )
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if resp.status_code in (200, 201):
            return {"ok": True}
        return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
