"""
MCP Server for alerts-mcp — inspect DB series, evaluate trigger rules, dispatch
to social/chat channels.

Tools (10):
  list_series          — distinct (source,function_name,indicator) in observations + scraw_*
  get_series_latest    — latest N rows of a series (alert-scoped inspection)
  create_alert_rule    — bind a series + condition + channels + message template
  list_alert_rules     — all rules
  get_alert_rule       — one rule by name
  update_alert_rule    — update rule fields (only provided ones)
  delete_alert_rule    — delete a rule (cascades to its events)
  list_channels        — which of the 7 channels are configured (no secrets)
  run_alert_rule       - ad-hoc evaluate + dispatch one rule now
  list_events          — recent alert_events (optionally for one rule)

Cron: `python server.py --run-rule <name>` (one rule) or `--run-all` (every
enabled rule, single tick) runs the engine in-process and exits.

Entry: python3 server.py  (FastMCP, stdio transport)
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Optional

# Unified env: root .env first, then per-MCP .env with override=True
try:
    from dotenv import load_dotenv

    _ROOT = Path(__file__).resolve().parents[2]  # repo root
    load_dotenv(_ROOT / ".env")
    load_dotenv(Path(__file__).resolve().parent / ".env", override=True)
except ImportError:
    pass

# make mcp/models importable
_MODELS = Path(__file__).resolve().parent.parent / "models"
if str(_MODELS) not in sys.path:
    sys.path.insert(0, str(_MODELS))

from fastmcp import FastMCP  # noqa: E402

import alert_tools as A  # noqa: E402
from alert_database import AlertError, get_db  # noqa: E402
from engine import run_all, run_rule as _engine_run_rule  # noqa: E402

logger = logging.getLogger("alerts-mcp")
app = FastMCP(name="alerts-mcp")


# ── series inspection ──────────────────────────────────────────


@app.tool
def list_series() -> dict:
    """Distinct (source, function_name, indicator) from observations + scraw_* tables,
    each with a row count + latest date. Alert-scoped discovery (not a SQL browser)."""
    return A.list_series()


@app.tool
def get_series_latest(
    source_table: str,
    date_column: str,
    value_column: str,
    series_filter_json: Optional[dict] = None,
    limit: int = 10,
) -> dict:
    """Return the latest `limit` rows of a series ordered by date_column desc.

    Args:
      source_table: e.g. "observations" or a scraw_* table.
      date_column / value_column: columns to read.
      series_filter_json: WHERE key→value pairs (e.g. {"indicator":"rsi"}).
      limit: max rows (default 10).
    """
    return A.get_series_latest(source_table, date_column, value_column, series_filter_json, limit)


# ── rule CRUD ──────────────────────────────────────────────────


@app.tool
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

    `condition` is a safe DSL (no eval): `latest > 70`, `crosses_above(70)`,
    `pct_change(5) > 0.05`, `latest > 70 and prev <= 70`, etc. `channels` is a
    list of names (["telegram","slack"]) or a {name: override} object. All
    channel credentials live in root .env under ALERTS_*.
    """
    return A.create_alert_rule(
        name=name, condition=condition, channels=channels,
        source_table=source_table, series_filter_json=series_filter_json,
        date_column=date_column, value_column=value_column,
        fire_mode=fire_mode, cooldown_seconds=cooldown_seconds,
        message_template=message_template, enabled=enabled,
    )


@app.tool
def list_alert_rules() -> dict:
    """Return all alert rules."""
    return A.list_alert_rules()


@app.tool
def get_alert_rule(name: str) -> dict:
    """Return one rule by name."""
    return A.get_alert_rule(name)


@app.tool
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
    return A.update_alert_rule(
        name=name, condition=condition, channels=channels,
        source_table=source_table, series_filter_json=series_filter_json,
        date_column=date_column, value_column=value_column,
        fire_mode=fire_mode, cooldown_seconds=cooldown_seconds,
        message_template=message_template, enabled=enabled,
    )


@app.tool
def delete_alert_rule(name: str) -> dict:
    """Delete a rule. Its alert_events rows are removed via FK CASCADE."""
    return A.delete_alert_rule(name)


# ── channels + dispatch ────────────────────────────────────────


@app.tool
def list_channels() -> dict:
    """Return {name, configured, missing_keys} for each of the 7 channels
    (telegram, discord, slack, twitter, dingtalk, feishu, wecowork).

    Never returns credential values — only the names of missing keys.
    """
    return A.list_channels()


@app.tool
def run_alert_rule(name: str) -> dict:
    """Ad-hoc: evaluate one rule now and dispatch if it fires.

    Respects fire_mode + cooldown like the cron path. Use to test a rule on demand.
    """
    return A.run_rule(name)


@app.tool
def list_events(rule_name: Optional[str] = None, limit: int = 50) -> dict:
    """Recent alert_events (optionally for one rule), newest first."""
    return A.list_events(rule_name, limit)


# ── CLI branch (cron-driven) + entry ───────────────────────────


def _cli_run_rule(name: str) -> int:
    try:
        summary = _engine_run_rule(name)
    except AlertError as e:
        summary = {"error": str(e)}
    print(json.dumps(summary, ensure_ascii=False, default=str))
    return 0 if "error" not in summary else 1


def _cli_run_all() -> int:
    summary = run_all()
    print(json.dumps(summary, ensure_ascii=False, default=str))
    return 0 if not summary.get("errors") else 1


if __name__ == "__main__":
    # Touch the DB so create_all runs before any tool / CLI branch.
    get_db()
    if "--run-rule" in sys.argv:
        i = sys.argv.index("--run-rule")
        if i + 1 >= len(sys.argv):
            print(json.dumps({"error": "--run-rule requires a rule name"}))
            sys.exit(2)
        sys.exit(_cli_run_rule(sys.argv[i + 1]))
    if "--run-all" in sys.argv:
        sys.exit(_cli_run_all())
    app.run(transport="stdio", show_banner=False)
