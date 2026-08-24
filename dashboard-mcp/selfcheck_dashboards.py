"""Offline self-check for the dashboard registry: temp DB, temp dash dir, no network.

Exercises register (insert + upsert) -> get -> search -> update -> list -> delete,
asserting index.html + daas.md reflect each state, and that the relative-URL
resolver points at the repo-root DB.

Run: uv run --directory mcp/dashboard-mcp python selfcheck_dashboards.py
   (or: mcp/dashboard-mcp/.venv/bin/python selfcheck_dashboards.py)
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dashboard_database as dd  # noqa: E402

# Redirect DB + dash dir BEFORE first instantiation so the singleton picks
# them up and we don't touch the real daas.db or real index.html/daas.md.
_TMP_DB = Path(tempfile.mkdtemp()) / "daas.db"
dd._get_db_url = lambda: f"sqlite:///{_TMP_DB}"
_TMP_DASH = Path(tempfile.mkdtemp())
dd._DASH_DIR = _TMP_DASH

from dashboard_database import DashboardDatabase  # noqa: E402

db = DashboardDatabase()


def ok(cond, msg):
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)
    print(f"  ok: {msg}")


print("== dashboard-registry self-check ==")

# 1. URL resolution
ok(dd._resolve_url("sqlite:///daas.db").endswith("daas.db"),
   "relative sqlite URL resolves under repo root")
ok(dd._resolve_url("sqlite:////abs/path.db") == "sqlite:////abs/path.db",
   "absolute sqlite URL passed through unchanged")
ok(dd._resolve_url("sqlite:///:memory:") == "sqlite:///:memory:",
   ":memory: passed through")

# 2. register (insert)
r = db.register(
    "byd-daily", "比亚迪日行情", "BYD daily OHLCV + SMA5",
    '["scraw_byd_daily"]', "daily 04:30",
    "mcp/dashboard-mcp/dashboards/byd-daily.html", "file:///tmp/byd-daily.html",
)
ok(r.get("action") == "inserted" and r["slug"] == "byd-daily", "register inserts a row")
ok(r["source_tables"] == ["scraw_byd_daily"], "register parses source_tables JSON")
ok((_TMP_DASH / "index.html").exists(), "index.html generated on register")
ok((_TMP_DASH / "daas.md").exists(), "daas.md generated on register")
ok("byd-daily.html" in (_TMP_DASH / "index.html").read_text(), "index.html lists the dashboard")
ok("比亚迪日行情" in (_TMP_DASH / "daas.md").read_text(), "daas.md lists the dashboard name")

# 3. get
g = db.get("byd-daily")
ok(g["name"] == "比亚迪日行情", "get returns the name")
ok(g["file_url"] == "file:///tmp/byd-daily.html", "get returns file_url")
ok("error" in db.get("nope"), "get missing -> error")

# 4. search
ok(len(db.search("比亚迪")) == 1, "search matches name")
ok(len(db.search("scraw_byd_daily")) == 1, "search matches source_tables")
ok(len(db.search("BYD")) == 1, "search is case-insensitive")
ok(len(db.search("zzz-not-found")) == 0, "search no-match -> empty")

# 5. register (upsert) — re-register same slug updates, no duplicate
r2 = db.register(
    "byd-daily", "比亚迪日行情 v2", "updated intro",
    '["scraw_byd_daily"]', "daily 04:30",
    "mcp/dashboard-mcp/dashboards/byd-daily.html", "file:///tmp/byd-daily.html",
)
ok(r2["action"] == "updated", "re-register upserts (action=updated)")
lst = db.list_all()
ok(len(lst) == 1, "upsert does not duplicate (list len=1)")
ok(lst[0]["name"] == "比亚迪日行情 v2", "upsert updated the name")
ok(_TMP_DASH.joinpath("daas.md").read_text().count("[byd-daily.html](byd-daily.html)") == 1,
   "daas.md has exactly one row for the slug after upsert")

# 6. update (patch)
u = db.update("byd-daily", intro="new intro", refresh_cadence="static snapshot")
ok(u["intro"] == "new intro" and u["refresh_cadence"] == "static snapshot", "update patches fields")
ok(u["name"] == "比亚迪日行情 v2", "update leaves unpassed fields unchanged")

# 7. delete
d = db.delete("byd-daily")
ok(d.get("deleted") == "byd-daily", "delete returns deleted slug")
ok("byd-daily.html" not in (_TMP_DASH / "index.html").read_text(),
   "index.html no longer lists the deleted dashboard")
ok("byd-daily" not in (_TMP_DASH / "daas.md").read_text(),
   "daas.md no longer lists the deleted dashboard")
ok("error" in db.get("byd-daily"), "get after delete -> error")

# 8. slug validation
bad = db.register("bad slug!", "x", "x", "[]", "x", "x", "file:///x")
ok("error" in bad, "invalid slug rejected")

print("\nALL CHECKS PASSED")
