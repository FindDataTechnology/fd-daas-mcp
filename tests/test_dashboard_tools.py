"""dashboard_tools tests: register/get/list/search/update/delete round-trip,
index.html + daas.md regeneration on register/delete, and query_table reading
rows from the throwaway DB with limit/offset pagination.

Loads dashboard-mcp/server.py under a unique module name (cron's test already
occupied `sys.modules["server"]`) via importlib - mirroring the consolidation
registry's per-group unique load. `_DASH_DIR` (where index.html/daas.md are
written) is monkeypatched to a per-test tmp_path so regeneration is exercised
for real without writing to the repo. The DashboardDatabase singleton binds to
the throwaway DAAS_DATABASE_URL set by conftest.

Convention: dashboards this module creates are prefixed `zz_test_` and torn down
by `_cleanup_dashboards()` in every test.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_DASH_MCP = Path(__file__).resolve().parents[1] / "dashboard-mcp"
sys.path.insert(0, str(_DASH_MCP))
import dashboard_database  # noqa: E402
from models import Dashboard  # noqa: E402

# Load dashboard server.py uniquely (avoids the `server` name collision with
# cron's already-imported server). Runs `_init_db()` (create_all on throwaway).
_spec = importlib.util.spec_from_file_location(
    "dashboard_test_server", _DASH_MCP / "server.py"
)
dashboard_server = importlib.util.module_from_spec(_spec)
sys.modules["dashboard_test_server"] = dashboard_server
_spec.loader.exec_module(dashboard_server)


def _cleanup_dashboards() -> None:
    sess = dashboard_database.DashboardDatabase()._session()
    try:
        sess.query(Dashboard).filter(Dashboard.slug.like("zz_test_%")).delete()
        sess.commit()
    finally:
        sess.close()


def _register(slug="zz_test_dash", **kw):
    """Thin helper around the register tool with sane defaults."""
    return json.loads(
        dashboard_server.register(
            slug=kw.get("slug", slug),
            name=kw.get("name", "ZZ Test"),
            intro=kw.get("intro", "zz intro"),
            source_tables=kw.get("source_tables", ["observations"]),
            refresh_cadence=kw.get("refresh_cadence", "daily"),
            file_path=kw.get("file_path", "zz.html"),
            file_url=kw.get("file_url", "zz.html"),
        )
    )


def test_dashboard_register_get_list_search_update_delete(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_database, "_DASH_DIR", tmp_path)
    _cleanup_dashboards()

    res = _register(slug="zz_test_dash", intro="zz intro")
    assert res.get("action") == "inserted", res
    assert res["slug"] == "zz_test_dash"
    assert res["name"] == "ZZ Test"

    got = json.loads(dashboard_server.get("zz_test_dash"))
    assert got.get("slug") == "zz_test_dash"
    assert got.get("refresh_cadence") == "daily"

    listed = json.loads(dashboard_server.list())
    assert "zz_test_dash" in [d["slug"] for d in listed]

    matches = json.loads(dashboard_server.search("zz intro"))
    assert "zz_test_dash" in [m["slug"] for m in matches]
    assert json.loads(dashboard_server.search("no-such-keyword")) == []

    upd = json.loads(
        dashboard_server.update("zz_test_dash", intro="updated intro")
    )
    assert upd.get("intro") == "updated intro"
    # Re-registering the same slug upserts (action=updated), not inserts.
    again = _register(slug="zz_test_dash", intro="zz intro")
    assert again.get("action") == "updated", again

    dele = json.loads(dashboard_server.delete("zz_test_dash"))
    assert dele == {"deleted": "zz_test_dash"}
    assert "error" in json.loads(dashboard_server.get("zz_test_dash"))
    _cleanup_dashboards()


def test_register_and_delete_regenerate_index(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_database, "_DASH_DIR", tmp_path)
    _cleanup_dashboards()

    _register(slug="zz_test_regen", name="ZZ Regen")
    index = tmp_path / "index.html"
    daas_md = tmp_path / "daas.md"
    assert index.exists() and daas_md.exists()
    assert "zz_test_regen" in index.read_text(encoding="utf-8")
    assert "zz_test_regen" in daas_md.read_text(encoding="utf-8")

    # Deleting regenerates again - the slug is gone from the index.
    json.loads(dashboard_server.delete("zz_test_regen"))
    assert index.exists()  # regenerated, not removed
    assert "zz_test_regen" not in index.read_text(encoding="utf-8")
    _cleanup_dashboards()


def test_query_table_reads_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_database, "_DASH_DIR", tmp_path)
    _cleanup_dashboards()
    _register(slug="zz_test_qt")

    out = json.loads(dashboard_server.query_table("daas", "dashboards", limit=50))
    assert "error" not in out, out
    assert "slug" in out["columns"]
    assert out["total"] >= 1
    assert "zz_test_qt" in [r["slug"] for r in out["rows"]]

    # Pagination: limit=0 returns no rows but total still reflects the count.
    page0 = json.loads(dashboard_server.query_table("daas", "dashboards", limit=0))
    assert page0["rows"] == []
    assert page0["total"] >= 1

    # Missing table surfaces an error (not a crash).
    bad = json.loads(dashboard_server.query_table("daas", "no_such_table"))
    assert "error" in bad
    _cleanup_dashboards()
