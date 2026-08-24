"""Seed the 72 US-leaders indicators for the 强势股趋势监控 Phase 1 MVP.

For each of 12 symbols (SPY, QQQ + 10 mega-caps), create 6 indicator rules
over its `scraw_<sym>_daily` table and run them (full recompute → observations):

    <sym>_ma5       sma          window=5   Close
    <sym>_ma10      sma          window=10  Close
    <sym>_ma20      sma          window=20  Close
    <sym>_rsi14     rsi          window=14  Close
    <sym>_volstd20  rolling_std  window=20  Close   (HV proxy)
    <sym>_high20    rolling_max  window=20  High    (20-day breakout level)

Idempotent: if a rule already exists, skip creation but still run it.
Run from the daas-mcp venv (cwd-agnostic — relative DAAS_DATABASE_URL is
resolved against the repo root by ProcessDatabase._resolve_url):

    uv run --directory mcp/daas-mcp python seed_us_leaders_indicators.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_THIS = Path(__file__).resolve()
_REPO_ROOT = _THIS.parent.parent.parent  # mcp/daas-mcp/ → mcp/ → repo root
try:
    from dotenv import load_dotenv
    load_dotenv(_REPO_ROOT / ".env")
    load_dotenv(_THIS.parent / ".env", override=True)
except ImportError:
    pass

sys.path.insert(0, str(_THIS.parent))

from process_database import ProcessDatabase, ProcessError  # noqa: E402

SYMBOLS = [
    "SPY", "QQQ",
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "AMD", "NFLX",
]

# (metric_suffix, op, params, value_column)
SPECS = [
    ("ma5",      "sma",         {"window": 5},  "Close"),
    ("ma10",     "sma",         {"window": 10}, "Close"),
    ("ma20",     "sma",         {"window": 20}, "Close"),
    ("rsi14",    "rsi",         {"window": 14}, "Close"),
    ("volstd20", "rolling_std", {"window": 20}, "Close"),
    ("high20",   "rolling_max", {"window": 20}, "High"),
]


def main() -> int:
    db = ProcessDatabase()
    created = 0
    existed = 0
    failed: list[str] = []
    run_ok = 0
    run_fail: list[tuple[str, str]] = []

    for sym in SYMBOLS:
        source_table = f"scraw_{sym.lower()}_daily"
        for suffix, op, params, value_col in SPECS:
            name = f"{sym}_{suffix}"
            try:
                db.create_indicator(
                    name=name,
                    datasource="yfinance",
                    source_table=source_table,
                    date_column="date",
                    value_column=value_col,
                    op=op,
                    params=params,
                    function_name=sym,
                    indicator_name=name,
                )
                created += 1
            except ProcessError as e:
                if "already exists" in str(e):
                    existed += 1
                else:
                    failed.append(f"{name}: {e}")
                    continue
            # Run it (full recompute → observations), regardless of created/existed.
            res = db.run_indicator(name)
            if isinstance(res, dict) and res.get("error"):
                run_fail.append((name, str(res["error"])))
            else:
                run_ok += 1

    print("═" * 64)
    print(f"US-leaders indicators seed complete")
    print(f"  symbols:        {len(SYMBOLS)}")
    print(f"  specs/symbol:   {len(SPECS)}")
    print(f"  total rules:    {len(SYMBOLS) * len(SPECS)}")
    print(f"  created:        {created}")
    print(f"  pre-existing:   {existed}")
    print(f"  create failures:{len(failed)}")
    for f in failed:
        print(f"    - {f}")
    print(f"  run ok:         {run_ok}")
    print(f"  run failures:   {len(run_fail)}")
    for name, err in run_fail:
        print(f"    - {name}: {err}")
    print("═" * 64)
    return 0 if not failed and not run_fail else 1


if __name__ == "__main__":
    raise SystemExit(main())
