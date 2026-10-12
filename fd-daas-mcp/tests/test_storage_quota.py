"""customer-dataset engine — optional cell storage quota
(openspec: wire-cell-storage-quota, engine tasks 1.x).

Env ``FD_CELL_STORAGE_QUOTA_MB`` arms a SECOND size gate on the cell data
root — additive to the CUSTOMER_DATASET_MAX_ROWS row cap, never replacing
it. Coverage:

- parsing: unset / non-numeric / non-positive / fractional → quota off
  (None, never raises); positive integer → MB→bytes;
- PostgreSQL leg, quota armed and exceeded (an empty PG database already
  reports ~8MB via pg_database_size, so a 1MB quota is guaranteed over —
  schema resets do not shrink it): every incremental write path —
  ingest_commit (replace AND merge), write_batch, derive, join_indicators,
  link, derive_refresh — rejects the WHOLE transaction with 「超过上限」 and
  both MB values, and prior data stays intact; revoking the env restores
  every path;
- link's gate is the FIRST statement of the materialization transaction —
  an over-quota link surfaces the quota domain error, not a creation error,
  and leaves no half-built child behind;
- overview: storage_quota_bytes / storage_used_bytes appear ONLY when armed
  AND on PostgreSQL (used > 0, quota exact); otherwise the response keeps
  its ungated shape (both keys absent);
- sqlite leg: the same env changes nothing — ingest succeeds and overview
  keeps its ungated shape (the gate is PG-only by contract; the full-suite
  sqlite run with the env set is the zero-behavior-change proof).

Isolation: each test points ``DAAS_DATABASE_URL`` at its own ``tmp_path``
database (same pattern as test_customer_dataset_incremental.py); the
autouse ``_quota_off`` fixture keeps the env hermetic against a polluted
outer environment.
"""
from __future__ import annotations

import re

import pytest

import conftest
from conftest import engine_db_url, reset_pg_schema
from daas.fd_daas_mcp import registry

COLS = [
    {"name": "id", "type": "INTEGER"},
    {"name": "name", "type": "TEXT"},
]
ROWS = [
    {"id": 1, "name": "甲"},
    {"id": 2, "name": "乙"},
    {"id": 3, "name": "丙"},
]
RIGHT_ROWS = [{"id": 2, "name": "乙对"}]
JOIN_COLS = [
    {"name": "id", "type": "INTEGER"},
    {"name": "month", "type": "TEXT"},
]
JOIN_ROWS = [
    {"id": 1, "month": "2026-03-15"},
    {"id": 2, "month": "2026-04-20"},
]

requires_pg = pytest.mark.skipif(
    conftest.TEST_BACKEND != "postgres",
    reason="存储配额闸仅作用于 PostgreSQL 数据根（sqlite 腿零触达）",
)
requires_sqlite = pytest.mark.skipif(
    conftest.TEST_BACKEND != "sqlite",
    reason="sqlite 腿零行为变化的显式断言（PG 腿走各自的配额用例）",
)


@pytest.fixture(autouse=True)
def _fresh_engine_db(tmp_path, monkeypatch):
    """Per-test engine database under tmp_path (see module docstring). On the
    postgres leg: the shared one-shot PG test database with the schema dropped
    and recreated — the isolation equivalent of a fresh sqlite file."""
    monkeypatch.setenv("DAAS_DATABASE_URL", engine_db_url(f"{tmp_path}/quota_engine.db"))
    reset_pg_schema()
    yield


@pytest.fixture(autouse=True)
def _quota_off(monkeypatch):
    """Quota disarmed unless the test arms it (hermetic against a polluted
    outer environment; monkeypatch reverts each test's own setenv)."""
    monkeypatch.delenv("FD_CELL_STORAGE_QUOTA_MB", raising=False)
    yield


def _t() -> dict:
    return {
        registry.namespaced(g, n): fn
        for g, n, fn in registry.build(profile="cell")
    }


def _tools_module() -> dict:
    """The registry-loaded customer_dataset_tools module (via a tool's globals)."""
    return _t()["customer_dataset_preview"].__globals__


def _stage(key: str, rows: list, mode: str = "replace", pk: list | None = None,
           columns: list | None = None) -> str:
    """begin + chunk (staging is intentionally NOT quota-gated) → ingest_id."""
    t = _t()
    begun = t["customer_dataset_ingest_begin"](
        dataset_key=key, columns=columns or COLS, pk_columns=pk, mode=mode
    )
    assert "error" not in begun, begun
    chunked = t["customer_dataset_ingest_chunk"](
        ingest_id=begun["ingest_id"], rows=rows, start_index=0
    )
    assert "error" not in chunked, chunked
    return begun["ingest_id"]


def _ingest(key: str, rows: list, mode: str = "replace", pk: list | None = None,
            columns: list | None = None) -> dict:
    ingest_id = _stage(key, rows, mode=mode, pk=pk, columns=columns)
    committed = _t()["customer_dataset_ingest_commit"](ingest_id=ingest_id)
    assert "error" not in committed, committed
    return committed


def _quota_error(result: dict) -> str:
    """Assert the result carries the storage-quota domain error with both MB
    values in the text; return the message."""
    assert result.get("success") is False, result
    err = str(result["error"])
    assert "超过上限" in err, err
    assert re.search(r"存储用量 \d+(?:\.\d+)?MB", err), err  # the used value
    assert "1MB" in err, err  # the armed quota value itself
    return err


class _FakeGateway:
    """Deterministic stand-in for the wire-entrance indicator fetcher (same
    pattern as test_customer_data_engine.py — series content is irrelevant
    here, only that join_indicators reaches its gated transaction)."""

    def resolve_codes(self, codes):
        return {"GDP_MO": 101}

    def resolve_entity(self, name):
        return {"中国": ("country", 1)}.get(name)

    def read_range(self, concept_ids, entity_type, entity_id, start, end):
        return {cid: [] for cid in concept_ids}


def _inject_gateway(monkeypatch, fake) -> None:
    g = _t()["customer_dataset_join_indicators"].__globals__
    monkeypatch.setitem(g, "_gateway_factory", (lambda: fake) if fake is not None else None)


# ── env parsing (both legs) ──────────────────────────────────────────


def test_quota_env_parsing(monkeypatch):
    quota = _tools_module()["_storage_quota_bytes"]
    monkeypatch.delenv("FD_CELL_STORAGE_QUOTA_MB", raising=False)
    assert quota() is None  # unset → off
    monkeypatch.setenv("FD_CELL_STORAGE_QUOTA_MB", "2")
    assert quota() == 2 * 1024 * 1024  # positive integer → MB→bytes
    for bad in ("", "0", "-5", "1.5", "abc"):
        monkeypatch.setenv("FD_CELL_STORAGE_QUOTA_MB", bad)
        assert quota() is None, bad  # unset/非正/非数字 → off, never raises


# ── ① quota armed + exceeded: every incremental write path rejects ───


@requires_pg
def test_over_quota_rejects_every_incremental_write_path(monkeypatch):
    t = _t()
    # seed with the gate OPEN: base + query child + join child + link pair
    _ingest("qta_base", ROWS, pk=["id"])
    derived = t["customer_dataset_derive"](
        child_key="qta_query_child", parent_key="qta_base",
        filters=[{"column": "id", "op": "gte", "value": 2}], actor="tester",
    )
    assert "error" not in derived, derived
    _inject_gateway(monkeypatch, _FakeGateway())
    _ingest("qta_join_src", JOIN_ROWS, pk=["id"], columns=JOIN_COLS)
    joined = t["customer_dataset_join_indicators"](
        child_key="qta_join_child", parent_key="qta_join_src",
        indicators=[{"code": "GDP_MO", "as": "gdp"}], date_column="month",
    )
    assert "error" not in joined, joined
    _ingest("qta_right", RIGHT_ROWS, pk=["id"])
    linked = t["customer_dataset_link"](
        child_key="qta_link_child", left_key="qta_base", left_column="id",
        right_key="qta_right", right_column="id", actor="tester",
    )
    assert "error" not in linked, linked

    before = {
        k: t["customer_dataset_preview"](dataset_key=k)["total"]
        for k in ("qta_base", "qta_query_child", "qta_join_child", "qta_link_child", "qta_right")
    }

    # arm a 1MB quota — the PG test database is already several MB
    # (pg_database_size ≥ ~8MB even right after the schema reset), so the
    # gate is guaranteed over on every path below
    monkeypatch.setenv("FD_CELL_STORAGE_QUOTA_MB", "1")

    # ingest_commit — replace semantics
    _quota_error(t["customer_dataset_ingest_commit"](ingest_id=_stage("qta_base", ROWS, pk=["id"])))
    # ingest_commit — merge semantics (incremental sync window)
    _quota_error(
        t["customer_dataset_ingest_commit"](
            ingest_id=_stage("qta_base", [{"id": 4, "name": "丁"}], mode="merge", pk=["id"])
        )
    )
    # write_batch
    _quota_error(
        t["customer_dataset_write_batch"](
            dataset_key="qta_base",
            ops=[{"op": "update", "anchor": "pk:[1]", "values": {"name": "改"}}],
            actor="tester", channel="agent",
        )
    )
    # derive (new child)
    _quota_error(
        t["customer_dataset_derive"](child_key="qta_derived_over", parent_key="qta_base", actor="tester")
    )
    # join_indicators (new child)
    _quota_error(
        t["customer_dataset_join_indicators"](
            child_key="qta_join_over", parent_key="qta_join_src",
            indicators=[{"code": "GDP_MO", "as": "gdp"}], date_column="month",
        )
    )
    # link (new child)
    _quota_error(
        t["customer_dataset_link"](
            child_key="qta_link_over", left_key="qta_base", left_column="id",
            right_key="qta_right", right_column="id", actor="tester",
        )
    )
    # derive_refresh — the query-kind re-materialization runs its own gated
    # transaction (link/join kinds dispatch into link/join_indicators, gated
    # above)
    _quota_error(t["customer_dataset_derive_refresh"](child_key="qta_query_child"))

    # nothing landed, nothing damaged: every prior dataset keeps its rows
    after = {k: t["customer_dataset_preview"](dataset_key=k)["total"] for k in before}
    assert after == before
    # and none of the over-quota children ever registered
    for key in ("qta_derived_over", "qta_join_over", "qta_link_over"):
        gone = t["customer_dataset_overview"](dataset_key=key)
        assert gone.get("success") is False and "不存在" in gone["error"], gone


# ── ② revoking the env restores every path ───────────────────────────


@requires_pg
def test_quota_revocation_restores_all_paths(monkeypatch):
    t = _t()
    _ingest("qtr_base", ROWS, pk=["id"])

    monkeypatch.setenv("FD_CELL_STORAGE_QUOTA_MB", "1")
    _quota_error(t["customer_dataset_ingest_commit"](ingest_id=_stage("qtr_base", ROWS, pk=["id"])))
    _quota_error(
        t["customer_dataset_write_batch"](
            dataset_key="qtr_base",
            ops=[{"op": "update", "anchor": "pk:[1]", "values": {"name": "改"}}],
            actor="tester",
        )
    )

    # revoke — same process, same data root: every path behaves as before
    monkeypatch.delenv("FD_CELL_STORAGE_QUOTA_MB")
    merged = t["customer_dataset_ingest_commit"](
        ingest_id=_stage("qtr_base", [{"id": 4, "name": "丁"}], mode="merge", pk=["id"])
    )
    assert "error" not in merged, merged
    assert merged["rows"] == 4
    written = t["customer_dataset_write_batch"](
        dataset_key="qtr_base",
        ops=[{"op": "update", "anchor": "pk:[1]", "values": {"name": "改"}}],
        actor="tester",
    )
    assert "error" not in written, written
    derived = t["customer_dataset_derive"](child_key="qtr_child", parent_key="qtr_base", actor="tester")
    assert "error" not in derived, derived
    _ingest("qtr_right", RIGHT_ROWS, pk=["id"])
    linked = t["customer_dataset_link"](
        child_key="qtr_link", left_key="qtr_base", left_column="id",
        right_key="qtr_right", right_column="id", actor="tester",
    )
    assert "error" not in linked, linked


# ── ③ overview observability ─────────────────────────────────────────


@requires_pg
def test_overview_storage_fields_only_when_armed(monkeypatch):
    _ingest("qov_base", ROWS, pk=["id"])
    t = _t()
    off = t["customer_dataset_overview"](dataset_key="qov_base")
    assert "error" not in off, off
    # ungated response shape: exactly the legacy keys, neither storage field
    assert set(off) == {
        "key", "name", "columns", "pk_columns", "rows", "size_bytes",
        "last_commit_at", "capabilities",
    }
    assert "storage_quota_bytes" not in off
    assert "storage_used_bytes" not in off

    monkeypatch.setenv("FD_CELL_STORAGE_QUOTA_MB", "1")
    on = t["customer_dataset_overview"](dataset_key="qov_base")
    assert "error" not in on, on
    assert on["storage_quota_bytes"] == 1 * 1024 * 1024
    assert isinstance(on["storage_used_bytes"], int)
    assert on["storage_used_bytes"] > 0


# ── ④ link's gate is the FIRST statement of its transaction ──────────


@requires_pg
def test_link_reports_quota_error_before_any_ddl(monkeypatch):
    """Over quota, link surfaces the storage-quota domain error — not a
    table-creation/registration error — proving the check runs at the
    transaction head, before any DDL; and the rolled-back transaction leaves
    no child metadata and no physical tables behind."""
    _ingest("qlk_base", ROWS, pk=["id"])
    _ingest("qlk_right", RIGHT_ROWS, pk=["id"])
    monkeypatch.setenv("FD_CELL_STORAGE_QUOTA_MB", "1")
    t = _t()
    _quota_error(
        t["customer_dataset_link"](
            child_key="qlk_child", left_key="qlk_base", left_column="id",
            right_key="qlk_right", right_column="id", actor="tester",
        )
    )
    gone = t["customer_dataset_overview"](dataset_key="qlk_child")
    assert gone.get("success") is False and "不存在" in gone["error"], gone
    # no half-built physical tables linger (whole transaction rolled back)
    leftover = _tools_module()["_raw_query_all"](
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'cust_qlk_child%'"
    )
    assert leftover == []


# ── sqlite leg: the env changes nothing ──────────────────────────────


@requires_sqlite
def test_sqlite_leg_untouched_by_quota_env(monkeypatch):
    monkeypatch.setenv("FD_CELL_STORAGE_QUOTA_MB", "1")
    committed = _ingest("qsl_base", ROWS, pk=["id"])
    assert committed["rows"] == 3  # ingest fully succeeds under the env
    merged = _ingest("qsl_base", [{"id": 4, "name": "丁"}], mode="merge", pk=["id"])
    assert merged["rows"] == 4
    t = _t()
    got = t["customer_dataset_overview"](dataset_key="qsl_base")
    assert "error" not in got, got
    # the two storage fields are PG-only: the sqlite response keeps its
    # ungated shape
    assert "storage_quota_bytes" not in got
    assert "storage_used_bytes" not in got
