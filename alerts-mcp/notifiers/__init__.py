"""Notifier plugin layer for alerts-mcp.

A `Notifier` is an outbound channel adapter (Telegram, Discord, Slack, Twitter,
DingTalk, Feishu, 企业微信). Each adapter reads its credentials from root `.env`
under `ALERTS_*` prefixes, reports `is_configured()`, and implements
`send(message, ctx) -> {"ok": bool, "error": str?}`.

`registry.send(channel, message, ctx)` looks up the adapter by name and never
raises — missing/unconfigured channels return `{"ok": False, "error": ...}` so
a dispatch fan-out records the failure in `alert_events.channels_results_json`
instead of aborting the whole send.

New channel = one file under notifiers/ + one line in registry.REGISTRY.
"""
from .base import Notifier
from .registry import REGISTRY, list_channels, send

__all__ = ["Notifier", "REGISTRY", "list_channels", "send"]
