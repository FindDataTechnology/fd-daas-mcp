"""One-time backfill: register the existing `us-leaders-trend-monitor` standalone
HTML dashboard into the new `dashboards` table, then regenerate index.html +
daas.md from the DB.

The existing dashboard was built before the registry existed; this script derives
its metadata (name, intro, source tables, refresh cadence, file path/url) and
calls `DashboardDatabase.register` (upsert by slug). Idempotent — re-run updates.

Run: uv run --directory mcp/dashboard-mcp python backfill_dashboards.py [--dry-run]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dashboard_database as dd  # noqa: E402
from dashboard_database import DashboardDatabase  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
SLUG = "us-leaders-trend-monitor"
HTML_REL = f"mcp/dashboard-mcp/dashboards/{SLUG}.html"
HTML_ABS = _REPO_ROOT / HTML_REL


def _introspect_source_tables() -> list:
    """List the `scraw_<sym>_daily` tables backing the us-leaders dashboard
    (plus `observations`) in the canonical DB. Falls back to observations only
    if the DB is unreadable."""
    try:
        import sqlite3
        url = dd._get_db_url()
        path = url.replace("sqlite:///", "")
        conn = sqlite3.connect(path)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'scraw_%_daily' "
            "ORDER BY name"
        ).fetchall()
        conn.close()
        tables = [r[0] for r in rows]
        return tables + ["observations"]
    except Exception as e:
        print(f"  [warn] could not introspect scraw tables ({e}); using fallback list")
        return ["observations"]


def _entity_coverage(source_tables: list) -> list:
    """Extract symbols from `scraw_<sym>_daily` table names, if present."""
    syms = []
    for t in source_tables:
        if t.startswith("scraw_") and t.endswith("_daily"):
            syms.append(t[len("scraw_"):-len("_daily")])
    return syms or None


def build_record() -> dict:
    source_tables = _introspect_source_tables()
    return {
        "slug": SLUG,
        "name": "强势股趋势监控 — Phase 1 MVP",
        "intro": (
            "Livermore×O'Neil 强势股趋势监控看板。展示美股 leadership 候选标的的日行情"
            "（OHLCV）与趋势指标（MA5/10/20、RSI14、20日波动率、20日新高）每日快照，"
            "用于识别处于趋势确立 / 加速阶段的强势股。Phase 1 MVP。"
        ),
        "source_tables": source_tables,
        "entity_coverage": _entity_coverage(source_tables),
        "time_range": None,
        "refresh_cadence": (
            "daily 04:30 fetch / 04:45 indicators (Asia/Shanghai); "
            "rebuild HTML via build_us_leaders_dashboard.py"
        ),
        "chart_config": [
            {
                "type": "candlestick+line",
                "source_table": "scraw_<sym>_daily",
                "x": "date",
                "y": ["open", "high", "low", "close", "ma5", "ma10", "ma20"],
                "filterable": True,
            },
            {
                "type": "line",
                "source_table": "observations",
                "x": "date",
                "y": ["rsi14", "volstd20", "high20"],
                "filterable": True,
            },
        ],
        "file_path": HTML_REL,
        "file_url": f"file://{HTML_ABS.resolve()}",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print the record, don't write")
    args = ap.parse_args()

    rec = build_record()
    print(f"Backfilling dashboard: {rec['slug']}")
    print(f"  name: {rec['name']}")
    print(f"  source_tables: {len(rec['source_tables'])} table(s)")
    print(f"  entity_coverage: {rec['entity_coverage']}")
    print(f"  file_url: {rec['file_url']}")

    if not HTML_ABS.exists():
        print(f"  [warn] HTML file not found at {HTML_ABS} (registering metadata anyway)")

    if args.dry_run:
        print("\n--dry-run: not writing. Record:")
        import json
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        return

    db = DashboardDatabase()
    result = db.register(**rec)
    if "error" in result:
        print(f"\nERROR: {result['error']}")
        sys.exit(1)
    print(f"\nRegistered (action={result.get('action')}). "
          f"index.html + daas.md regenerated from the DB.")

    # Verify
    import json
    lst = db.list_all()
    print(f"list now returns {len(lst)} row(s).")


if __name__ == "__main__":
    main()
