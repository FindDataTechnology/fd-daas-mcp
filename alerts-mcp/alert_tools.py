"""Tool functions for alerts-mcp.

Plain functions (no FastMCP decorator) so they're callable from `server.py`'s
`@app.tool` wrappers AND from the self-check. Each returns a JSON-serializable
dict; validation errors surface as `{"error": ...}` rather than raising.
"""
from __future__ import annotations

from typing import Optional

from alert_database import AlertError, get_db
from engine import run_rule as _run_rule
from notifiers import list_channels as _list_channels


def list_series() -> dict:
    """Distinct (source, function_name, indicator) from observations + scraw_* tables."""
    return {"series": get_db().list_series()}


def get_series_latest(
    source_table: str,
    date_column: str,
    value_column: str,
    series_filter_json: Optional[dict] = None,
    limit: int = 10,
) -> dict:
    """Latest N rows of a series ordered by date_column desc (alert-scoped inspection)."""
    try:
        rows = get_db().get_series_latest(
            source_table, series_filter_json, date_column, value_column, limit
        )
    except AlertError as e:
        return {"error": str(e)}
    return {"rows": rows}


def create_alert_rule(
    name: str,
    condition: str,
    channels: list | dict,
    source_table: str = "observations",
    series_filter_json: Optional[dict] = None,
    date_column: str = "date",
    value_column: str = "value",
    fire_mode: str = "every_match",
    cooldown_seconds: int = 300,
    message_template: str = "$rule_name: $indicator = $latest",
    enabled: bool = True,
) -> dict:
    """Create a trigger rule over a DB series.

    Args:
      name: unique rule name.
      condition: DSL expression (e.g. "latest > 70", "crosses_above(70)", "pct_change(5) > 0.05").
      channels: list of channel names (e.g. ["telegram","slack"]) OR a
        {name: override} object (e.g. {"telegram": {"chat_id": "123"}}).
      source_table: series table (default `observations`).
      series_filter_json: WHERE key→value pairs (e.g. {"source":"akshare","indicator":"rsi"}).
      date_column / value_column: columns to read.
      fire_mode: "every_match" (subject to cooldown) or "on_change" (false→true).
      cooldown_seconds: min gap between dispatches for `every_match`.
      message_template: `string.Template` body ($latest, $prev, $indicator, …).
      enabled: whether --run-all / cron will evaluate this rule.
    """
    try:
        return get_db().create_rule(
            name=name,
            condition=condition,
            channels=channels,
            source_table=source_table,
            series_filter_json=series_filter_json,
            date_column=date_column,
            value_column=value_column,
            fire_mode=fire_mode,
            cooldown_seconds=cooldown_seconds,
            message_template=message_template,
            enabled=enabled,
        )
    except AlertError as e:
        return {"error": str(e)}


def list_alert_rules() -> dict:
    return {"rules": get_db().list_rules()}


def get_alert_rule(name: str) -> dict:
    row = get_db().get_rule(name)
    return row if row is not None else {"error": f"rule not found: {name}"}


def update_alert_rule(
    name: str,
    condition: Optional[str] = None,
    channels: Optional[list | dict] = None,
    source_table: Optional[str] = None,
    series_filter_json: Optional[dict] = None,
    date_column: Optional[str] = None,
    value_column: Optional[str] = None,
    fire_mode: Optional[str] = None,
    cooldown_seconds: Optional[int] = None,
    message_template: Optional[str] = None,
    enabled: Optional[bool] = None,
) -> dict:
    """Update a rule's fields. Only provided fields change. The name cannot be renamed."""
    fields = {
        "condition": condition,
        "channels_json": channels,
        "source_table": source_table,
        "series_filter_json": series_filter_json,
        "date_column": date_column,
        "value_column": value_column,
        "fire_mode": fire_mode,
        "cooldown_seconds": cooldown_seconds,
        "message_template": message_template,
        "enabled": enabled,
    }
    fields = {k: v for k, v in fields.items() if v is not None}
    try:
        return get_db().update_rule(name, **fields)
    except AlertError as e:
        return {"error": str(e)}


def delete_alert_rule(name: str) -> dict:
    ok = get_db().delete_rule(name)
    return {"deleted": name, "events_cascaded": True} if ok else {"error": f"rule not found: {name}"}


def list_channels() -> dict:
    """Return {name, configured, missing_keys} for each of the 7 channels.

    Never returns credential values — only the names of missing keys.
    """
    return {"channels": _list_channels()}


def run_rule(name: str) -> dict:
    """Ad-hoc: evaluate one rule now and dispatch if it fires (ignores enabled?).

    Respects fire_mode + cooldown like the cron path. Use to test a rule on demand.
    """
    return _run_rule(name)


def list_events(rule_name: Optional[str] = None, limit: int = 50) -> dict:
    """Recent alert_events (optionally for one rule), newest first."""
    return {"events": get_db().list_events(rule_name, limit)}
