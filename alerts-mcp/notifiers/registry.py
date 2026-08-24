"""Channel registry — maps channel names to adapter instances.

`send(channel, message, ctx)` never raises: unknown/unconfigured channels return
`{"ok": False, "error": ...}` so the dispatch fan-out records per-channel
outcomes in `alert_events.channels_results_json`.
"""
from __future__ import annotations

from .base import Notifier
from .discord import DiscordNotifier
from .dingtalk import DingTalkNotifier
from .feishu import FeishuNotifier
from .slack import SlackNotifier
from .telegram import TelegramNotifier
from .twitter import TwitterNotifier
from .wecowork import WeComNotifier


def _build_registry() -> dict[str, Notifier]:
    adapters: list[Notifier] = [
        TelegramNotifier(),
        DiscordNotifier(),
        SlackNotifier(),
        TwitterNotifier(),
        DingTalkNotifier(),
        FeishuNotifier(),
        WeComNotifier(),
    ]
    return {a.name: a for a in adapters}


REGISTRY: dict[str, Notifier] = _build_registry()


def list_channels() -> list[dict]:
    """Return ``[{name, configured, missing_keys}]`` for every adapter.

    Never returns credential values — only the names of missing keys.
    """
    return [a.to_status() for a in REGISTRY.values()]


def send(channel: str, message: str, ctx: dict) -> dict:
    """Send `message` via `channel`. Returns ``{"ok": bool, "channel": str, "error": str?}``.

    Unknown channel → ``{"ok": False, "error": "unknown channel"}``.
    Unconfigured channel → ``{"ok": False, "error": "not configured", "missing_keys": [...]}``.
    Transport error → ``{"ok": False, "error": "<detail>"}``.
    Never raises.
    """
    adapter = REGISTRY.get(channel)
    if adapter is None:
        return {"ok": False, "channel": channel, "error": "unknown channel"}
    if not adapter.is_configured():
        return {
            "ok": False,
            "channel": channel,
            "error": "not configured",
            "missing_keys": adapter.missing_keys(),
        }
    try:
        result = adapter.send(message, ctx)
    except Exception as e:  # never let one channel crash the dispatch
        return {"ok": False, "channel": channel, "error": f"{type(e).__name__}: {e}"}
    if "channel" not in result:
        result["channel"] = channel
    return result
