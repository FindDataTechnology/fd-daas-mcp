"""Rule engine for alerts-mcp.

Loads a rule's recent series, evaluates its condition against the DSL, applies
the fire mode (`every_match` / `on_change`) + cooldown, and dispatches to the
rule's channels. One channel failure does not abort the dispatch.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from alert_database import AlertDatabase, AlertError, get_db
from messaging import render_message
from notifiers import registry
from notifiers.registry import send as _channel_send

#: How many recent values to load for evaluation. Covers typical windows
#: (sma-20, pct_change-10, …). Conditions referencing larger windows will see
#: `value(n)`/`avg(n)` return None for out-of-range n, which the DSL treats as
#: "not enough data" → the comparison short-circuits to False.
MAX_LOOKBACK = 30


def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _load_series(db: AlertDatabase, rule) -> tuple[list[float], list]:
    """Return (values_newest_first, raw_rows) for the rule's series."""
    rows = db.get_series_latest(
        rule.source_table,
        rule.series_filter_json,
        rule.date_column,
        rule.value_column,
        limit=MAX_LOOKBACK,
    )
    # rows are newest-first already (ORDER BY date DESC).
    values = []
    for r in rows:
        f = _to_float(r.get("value"))
        if f is not None:
            values.append(f)
    return values, rows


def _normalize_channels(channels_json) -> tuple[list[str], dict]:
    """channels_json is either ["telegram","slack"] or
    {"telegram":{"chat_id":"…"},"slack":{}}. Return (names, overrides_dict)."""
    if isinstance(channels_json, list):
        return [str(c) for c in channels_json], {}
    if isinstance(channels_json, dict):
        names = list(channels_json.keys())
        overrides = {k: v for k, v in channels_json.items() if isinstance(v, dict)}
        return [str(n) for n in names], overrides
    raise AlertError("channels must be a list of names or a {name: override} object")


def evaluate_rule(rule) -> tuple[bool, dict]:
    """Evaluate `rule`'s condition against its latest series.

    Returns (matches, ctx) where ctx carries the values for message rendering.
    Does NOT dispatch. Raises AlertError on a bad series.
    """
    import expressions as E

    db = get_db()
    values, rows = _load_series(db, rule)
    if not values:
        return False, {"latest": None, "prev": None, "rule_name": rule.name}
    latest = values[0]
    prev = values[1] if len(values) > 1 else None
    series_filter = rule.series_filter_json or {}
    source = series_filter.get("source", rule.source_table)
    indicator = series_filter.get("indicator", rule.value_column)
    latest_date = rows[0].get("date") if rows else None
    pct_change = ""
    if prev is not None and prev != 0:
        pct_change = (latest - prev) / prev
    ctx = {
        "latest": latest,
        "prev": prev,
        "series": values,
        "rule_name": rule.name,
        "source": source,
        "indicator": indicator,
        "date": latest_date,
        "value": latest,
        "pct_change": pct_change,
    }
    try:
        matches = E.evaluate(rule.condition, ctx)
    except E.ExpressionError as e:
        raise AlertError(f"condition error: {e}")
    return matches, ctx


def _cooldown_allows(rule, now: datetime) -> bool:
    if rule.last_fired_at is None:
        return True
    last = rule.last_fired_at
    # SQLite stores datetimes naive; normalize both to aware UTC before subtracting.
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    elapsed = (now - last).total_seconds()
    return elapsed >= rule.cooldown_seconds


def run_rule(name: str) -> dict:
    """Evaluate one rule; dispatch (and record an event) if it fires.

    Returns a JSON-serializable summary. Never raises on dispatch/transport
    errors — records them in `channels_results`.
    """
    db = get_db()
    rule = db.get_rule_row(name)
    if rule is None:
        return {"error": f"rule not found: {name}"}
    if not rule.enabled:
        return {"error": f"rule disabled: {name}"}
    try:
        matches, ctx = evaluate_rule(rule)
    except AlertError as e:
        return {"rule": name, "error": str(e)}

    now = datetime.now(timezone.utc)
    latest = ctx.get("latest")

    if not matches:
        # False evaluation: update last_state (so on_change can detect the next
        # transition) but insert no event.
        db.update_state(rule.id, False, latest)
        return {"rule": name, "fired": False, "latest": latest, "reason": "condition false"}

    # Condition is true. Decide whether to fire per fire_mode + cooldown.
    names, overrides = _normalize_channels(rule.channels_json)
    if rule.fire_mode == "on_change":
        if rule.last_state is True:
            # Already in the true state — on_change does not refire.
            return {"rule": name, "fired": False, "latest": latest, "reason": "on_change already true"}
        # last_state is None or False → fire (false→true transition, incl. first eval).
    else:  # every_match
        if not _cooldown_allows(rule, now):
            return {"rule": name, "fired": False, "latest": latest, "reason": "cooldown"}

    # Fire: render message + fan out to channels (one failure does not abort).
    render_ctx = dict(ctx)
    render_ctx.setdefault("rule_name", rule.name)
    message = render_message(rule.message_template, render_ctx)
    channel_ctx = {"channel_overrides": overrides, "rule": rule.name, "latest": latest}
    results = []
    for ch in names:
        # try/except per channel — never let one channel crash the dispatch.
        try:
            res = _channel_send(ch, message, channel_ctx)
        except Exception as e:
            res = {"ok": False, "channel": ch, "error": f"{type(e).__name__}: {e}"}
        results.append(res)

    db.record_firing(
        rule_id=rule.id,
        value_json={"latest": latest, "prev": ctx.get("prev"), "date": ctx.get("date")},
        message_rendered=message,
        channels_results_json=results,
        new_state=True,
        latest_value=latest,
    )
    fired_ok = [r for r in results if r.get("ok")]
    return {
        "rule": name,
        "fired": True,
        "latest": latest,
        "message": message,
        "channels": results,
        "ok_channels": len(fired_ok),
        "total_channels": len(results),
    }


def run_all() -> dict:
    """Evaluate every enabled rule once. Sequential; per-rule try/except."""
    db = get_db()
    rules = db.list_rules()
    results = []
    for r in rules:
        if not r.get("enabled"):
            continue
        try:
            summary = run_rule(r["name"])
        except Exception as e:
            summary = {"rule": r["name"], "error": f"{type(e).__name__}: {e}"}
        results.append(summary)
    fired = sum(1 for s in results if s.get("fired"))
    errors = sum(1 for s in results if s.get("error"))
    return {"total": len(results), "fired": fired, "errors": errors, "rules": results}
