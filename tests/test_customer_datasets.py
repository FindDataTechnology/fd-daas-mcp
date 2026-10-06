"""Customer-dataset tools (wire-customer-local-data, task group 1).

Covers the frozen contract §2: atomic full-snapshot ingest, row anchors
(pk-first, row-hash fallback), the correction overlay (update/delete/insert,
replay on re-sync, dangling marks, revert), the row cap, soft-reference
metadata rows and dataset deletion. Tools are exercised through
``registry.build()`` so the AST discovery + namespacing path is under test too.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_DAAS_MCP = Path(__file__).resolve().parents[1] / "daas-mcp"
sys.path.insert(0, str(_DAAS_MCP))
from daas_database import get_database  # noqa: E402
sys.path.remove(str(_DAAS_MCP))

from daas.fd_daas_mcp import registry  # noqa: E402

COLS = [
    {"name": "id", "type": "INTEGER"},
    {"name": "name", "type": "TEXT"},
    {"name": "amount", "type": "REAL"},
]

ROWS = [
    {"id": 1, "name": "甲", "amount": 10.5},
    {"id": 2, "name": "乙", "amount": 20.0},
    {"id": 3, "name": "丙", "amount": 30.25},
]


def _t() -> dict:
    # the group is profile-gated (`cell`) — build under it, exactly as the
    # wire cell's merged server does; keyed by the NAMESPACED wire-facing name
    # (group prefix + tool name = the contract's customer_dataset_* form)
    return {
        registry.namespaced(g, n): fn
        for g, n, fn in registry.build(profile="cell")
    }


def _seed(key: str, rows: list, pk: list | None = None, columns: list | None = None) -> dict:
    t = _t()
    begun = t["customer_dataset_ingest_begin"](
        dataset_key=key, columns=columns or COLS, pk_columns=pk
    )
    assert "error" not in begun, begun
    chunked = t["customer_dataset_ingest_chunk"](
        ingest_id=begun["ingest_id"], rows=rows, start_index=0
    )
    assert "error" not in chunked, chunked
    committed = t["customer_dataset_ingest_commit"](ingest_id=begun["ingest_id"])
    assert "error" not in committed, committed
    return committed


def _rows_by_name(key: str, **kw) -> dict:
    got = _t()["customer_dataset_preview"](dataset_key=key, **kw)
    assert "error" not in got, got
    return {r["name"]: r for r in got["rows"]}


# ── ingest ───────────────────────────────────────────────────────────


def test_commit_and_preview_hash_anchor():
    committed = _seed("sales_a", ROWS)
    assert committed["rows"] == 3
    assert committed["size_bytes"] > 0

    got = _t()["customer_dataset_preview"](dataset_key="sales_a")
    assert got["total"] == 3
    assert [c["name"] for c in got["columns"]] == ["id", "name", "amount"]
    first = got["rows"][0]
    assert first["__row_id"].startswith("hash:")
    assert first["name"] == "甲"

    meta = _t()["customer_dataset_overview"](dataset_key="sales_a")
    assert meta["rows"] == 3 and meta["pk_columns"] == []


def test_pk_anchor_uses_declared_key():
    _seed("sales_b", ROWS, pk=["id"])
    got = _t()["customer_dataset_preview"](dataset_key="sales_b")
    ids = {r["id"]: r["__row_id"] for r in got["rows"]}
    assert ids[1] == "pk:[1]" and ids[2] == "pk:[2]"


def test_begin_supersedes_open_ingest_and_abort_keeps_snapshot():
    _seed("sales_c", ROWS)
    t = _t()
    first = t["customer_dataset_ingest_begin"](dataset_key="sales_c", columns=COLS)
    second = t["customer_dataset_ingest_begin"](dataset_key="sales_c", columns=COLS)
    assert "error" not in second
    # the superseded ingest cannot accept more chunks
    refused = t["customer_dataset_ingest_chunk"](
        ingest_id=first["ingest_id"], rows=[ROWS[0]], start_index=0
    )
    assert "error" in refused
    aborted = t["customer_dataset_ingest_abort"](ingest_id=second["ingest_id"])
    assert aborted == {"aborted": True}
    # old snapshot untouched, staging table dropped
    assert _t()["customer_dataset_preview"](dataset_key="sales_c")["total"] == 3
    assert (
        get_database()
        .engine.raw_connection()
        .cursor()
        .execute("SELECT name FROM sqlite_master WHERE name='cust_sales_c__staging'")
        .fetchone()
        is None
    )


def test_chunk_start_index_guard():
    t = _t()
    begun = t["customer_dataset_ingest_begin"](dataset_key="sales_d", columns=COLS)
    t["customer_dataset_ingest_chunk"](ingest_id=begun["ingest_id"], rows=ROWS[:2], start_index=0)
    bad = t["customer_dataset_ingest_chunk"](
        ingest_id=begun["ingest_id"], rows=ROWS[2:], start_index=99
    )
    assert "error" in bad and "start_index" in bad["error"]
    t["customer_dataset_ingest_abort"](ingest_id=begun["ingest_id"])


def test_row_cap_rejects_and_keeps_old_snapshot(monkeypatch):
    _seed("sales_e", ROWS[:2])
    monkeypatch.setenv("CUSTOMER_DATASET_MAX_ROWS", "2")
    t = _t()
    begun = t["customer_dataset_ingest_begin"](dataset_key="sales_e", columns=COLS)
    t["customer_dataset_ingest_chunk"](ingest_id=begun["ingest_id"], rows=ROWS, start_index=0)
    refused = t["customer_dataset_ingest_commit"](ingest_id=begun["ingest_id"])
    assert "error" in refused and "上限" in refused["error"]
    assert _t()["customer_dataset_preview"](dataset_key="sales_e")["total"] == 2


def test_chinese_column_names():
    cols = [{"name": "地区", "type": "TEXT"}, {"name": "销量", "type": "INTEGER"}]
    rows = [{"地区": "上海", "销量": 7}, {"地区": "北京", "销量": 9}]
    _seed("cn_cols", rows, columns=cols)
    got = _t()["customer_dataset_preview"](dataset_key="cn_cols")
    assert got["total"] == 2
    assert {r["地区"] for r in got["rows"]} == {"上海", "北京"}


# ── corrections ──────────────────────────────────────────────────────


def test_update_correction_applies_replays_and_keeps_base():
    _seed("corr_a", ROWS)
    anchor = _rows_by_name("corr_a")["乙"]["__row_id"]
    added = _t()["customer_dataset_correction_add"](
        dataset_key="corr_a", op="update", anchor=anchor, values={"amount": 99.5}, actor="alice"
    )
    assert "error" not in added

    rows = _rows_by_name("corr_a")
    assert rows["乙"]["amount"] == 99.5
    # raw snapshot keeps the original value
    base_rows = _rows_by_name("corr_a", include_corrected=False)
    assert base_rows["乙"]["amount"] == 20.0

    # re-sync replays the correction
    _seed("corr_a", ROWS)
    rows2 = _rows_by_name("corr_a")
    assert rows2["乙"]["amount"] == 99.5
    base2 = _rows_by_name("corr_a", include_corrected=False)
    assert base2["乙"]["amount"] == 20.0

    history = _t()["customer_dataset_correction_list"](dataset_key="corr_a")
    entry = history["corrections"][0]
    assert entry["op"] == "update" and entry["actor"] == "alice"
    assert entry["old_values"]["amount"] == 20.0 and entry["dangling_at"] is None


def test_correction_dangles_then_recovers():
    _seed("corr_b", ROWS)
    anchor = _rows_by_name("corr_b")["丙"]["__row_id"]
    t = _t()
    t["customer_dataset_correction_add"](
        dataset_key="corr_b", op="update", anchor=anchor, values={"amount": 1.0}, actor="bob"
    )
    # re-sync without row 丙 → the correction dangles, is kept and listed
    _seed("corr_b", ROWS[:2])
    dangling = t["customer_dataset_correction_dangling"](dataset_key="corr_b")
    assert len(dangling["corrections"]) == 1
    assert dangling["corrections"][0]["dangling_at"] is not None
    history = t["customer_dataset_correction_list"](dataset_key="corr_b")
    assert len(history["corrections"]) == 1  # kept, not dropped
    # re-sync with the row back → dangling cleared and value re-applied
    _seed("corr_b", ROWS)
    assert t["customer_dataset_correction_dangling"](dataset_key="corr_b")["corrections"] == []
    assert _rows_by_name("corr_b")["丙"]["amount"] == 1.0


def test_delete_and_insert_corrections():
    _seed("corr_c", ROWS)
    t = _t()
    anchor = _rows_by_name("corr_c")["甲"]["__row_id"]
    deleted = t["customer_dataset_correction_add"](dataset_key="corr_c", op="delete", anchor=anchor)
    assert "error" not in deleted
    assert _t()["customer_dataset_preview"](dataset_key="corr_c")["total"] == 2

    inserted = t["customer_dataset_correction_add"](
        dataset_key="corr_c", op="insert", values={"id": 9, "name": "新", "amount": 5.0}
    )
    assert "error" not in inserted
    rows = _rows_by_name("corr_c")
    assert rows["新"]["__row_id"].startswith("ins:")
    assert _t()["customer_dataset_preview"](dataset_key="corr_c")["total"] == 3
    # editing an inserted row targets its ins: anchor
    upd = t["customer_dataset_correction_add"](
        dataset_key="corr_c", op="update", anchor=rows["新"]["__row_id"], values={"amount": 6.0}
    )
    assert "error" not in upd, upd
    assert _rows_by_name("corr_c")["新"]["amount"] == 6.0
    # delete + insert + follow-up update all survive a re-sync
    _seed("corr_c", ROWS)
    assert _t()["customer_dataset_preview"](dataset_key="corr_c")["total"] == 3
    assert _rows_by_name("corr_c")["新"]["amount"] == 6.0


def test_revert_restores_snapshot_value():
    _seed("corr_d", ROWS)
    t = _t()
    anchor = _rows_by_name("corr_d")["甲"]["__row_id"]
    added = t["customer_dataset_correction_add"](
        dataset_key="corr_d", op="update", anchor=anchor, values={"amount": 777.0}
    )
    assert _rows_by_name("corr_d")["甲"]["amount"] == 777.0
    reverted = t["customer_dataset_correction_revert"](
        dataset_key="corr_d", correction_id=added["correction_id"], actor="carol"
    )
    assert reverted == {"reverted": True}
    assert _rows_by_name("corr_d")["甲"]["amount"] == 10.5
    history = t["customer_dataset_correction_list"](
        dataset_key="corr_d", include_reverted=True
    )
    assert history["corrections"][0]["reverted_at"] is not None


# ── softrefs / lifecycle / surface ───────────────────────────────────


def test_softref_metadata_rows_created_and_deleted():
    _seed("refs_a", ROWS)
    conn = get_database().engine.raw_connection()
    try:
        cur = conn.cursor()
        assert cur.execute("SELECT COUNT(*) FROM sources WHERE name='cust_refs_a'").fetchone()[0] == 1
        assert cur.execute("SELECT COUNT(*) FROM datasources WHERE name='cust_refs_a'").fetchone()[0] == 1
    finally:
        conn.close()
    assert _t()["customer_dataset_delete"](dataset_key="refs_a", actor="dave") == {"deleted": True}
    conn = get_database().engine.raw_connection()
    try:
        cur = conn.cursor()
        assert cur.execute("SELECT COUNT(*) FROM sources WHERE name='cust_refs_a'").fetchone()[0] == 0
        assert cur.execute("SELECT COUNT(*) FROM datasources WHERE name='cust_refs_a'").fetchone()[0] == 0
        # data tables gone, audit ingest row kept
        assert cur.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'cust_refs_a%'"
        ).fetchone()[0] == 0
        statuses = [
            r[0]
            for r in cur.execute(
                "SELECT status FROM customer_dataset_ingests WHERE dataset_key='refs_a'"
            ).fetchall()
        ]
        assert "deleted" in statuses and "committed" in statuses
    finally:
        conn.close()
    assert "error" in _t()["customer_dataset_preview"](dataset_key="refs_a")
    assert "error" in _t()["customer_dataset_overview"](dataset_key="refs_a")


def test_validation_errors():
    t = _t()
    bad_key = t["customer_dataset_ingest_begin"](dataset_key="Bad-Key", columns=COLS)
    assert "error" in bad_key
    bad_type = t["customer_dataset_ingest_begin"](
        dataset_key="ok_key", columns=[{"name": "a", "type": "BLOB"}]
    )
    assert "error" in bad_type
    reserved = t["customer_dataset_ingest_begin"](
        dataset_key="ok_key", columns=[{"name": "__row_hash", "type": "TEXT"}]
    )
    assert "error" in reserved
    _seed("ok_key2", ROWS)
    unknown_col = t["customer_dataset_correction_add"](
        dataset_key="ok_key2", op="update", anchor="hash:deadbeef", values={"nope": 1}
    )
    assert "error" in unknown_col


def test_registry_surface_and_namespacing():
    names = {n for _, n, _ in registry.build(profile="cell")}
    expected = {
        "ingest_begin", "ingest_chunk", "ingest_commit", "ingest_abort",
        "overview", "preview", "correction_add", "correction_list",
        "correction_dangling", "correction_revert", "delete",
    }
    assert expected <= names
    assert registry.namespaced("customer_dataset", "preview") == "customer_dataset_preview"
    # plain `delete` legitimately joins the whitelisted generic-name
    # collisions (create/list/get/update/delete); nothing else may collide
    new_collisions = set(registry.collisions()) - {"create", "list", "get", "update", "delete"}
    assert not expected & new_collisions


def test_indicator_rule_reads_customer_table():
    """The point of the engine contract: a materialized customer dataset feeds
    the existing analysis chain with zero special-casing — an indicator rule
    (datasource = the dataset's soft-ref row) computes over its table and the
    run lands in `observations`."""
    cols = [{"name": "date", "type": "TEXT"}, {"name": "value", "type": "REAL"}]
    rows = [
        {"date": "2026-01-01", "value": 1.0},
        {"date": "2026-01-02", "value": 2.0},
        {"date": "2026-01-03", "value": 4.0},
    ]
    _seed("ts_a", rows, columns=cols)
    t = _t()
    created = t["daas_create_indicator"](
        name="zz_cust_ts_a_level",
        datasource="cust_ts_a",
        source_table="cust_ts_a",
        date_column="date",
        value_column="value",
        op="level",
    )
    assert "error" not in created, created
    ran = t["daas_run_indicator"](name="zz_cust_ts_a_level")
    assert "error" not in ran, ran

    conn = get_database().engine.raw_connection()
    try:
        n = conn.cursor().execute(
            "SELECT COUNT(*) FROM observations WHERE indicator='zz_cust_ts_a_level'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 3


def test_correction_feeds_the_rule_chain():
    """Corrections change what the analysis chain reads: an update on the
    effective table shows up in a fresh indicator run."""
    cols = [{"name": "date", "type": "TEXT"}, {"name": "value", "type": "REAL"}]
    _seed(
        "ts_b",
        [
            {"date": "2026-01-01", "value": 1.0},
            {"date": "2026-01-02", "value": 2.0},
        ],
        columns=cols,
    )
    anchor = _t()["customer_dataset_preview"](dataset_key="ts_b")["rows"][1]["__row_id"]
    _t()["customer_dataset_correction_add"](
        dataset_key="ts_b", op="update", anchor=anchor, values={"value": 100.0}, actor="erin"
    )
    t = _t()
    t["daas_create_indicator"](
        name="zz_cust_ts_b_level",
        datasource="cust_ts_b",
        source_table="cust_ts_b",
        date_column="date",
        value_column="value",
        op="level",
    )
    ran = t["daas_run_indicator"](name="zz_cust_ts_b_level")
    assert "error" not in ran, ran
    conn = get_database().engine.raw_connection()
    try:
        cur = conn.cursor()
        corrected = cur.execute(
            "SELECT value FROM observations WHERE indicator='zz_cust_ts_b_level' AND date='2026-01-02'"
        ).fetchone()
        base = cur.execute(
            "SELECT value FROM cust_ts_b__base WHERE date='2026-01-02'"
        ).fetchone()
    finally:
        conn.close()
    assert float(corrected[0]) == 100.0  # analysis sees the correction
    assert float(base[0]) == 2.0  # raw snapshot keeps the source value