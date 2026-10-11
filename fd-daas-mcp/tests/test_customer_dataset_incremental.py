"""customer-dataset incremental ingest — merge commit semantics
(wire-connector-incremental-sync §1, engine side).

``ingest_begin(mode="merge")`` + ``ingest_commit`` = upsert the staged rows
into the base by the dataset's primary key, for connector watermark sync.
Covers the required behaviors:

- upsert: new pk rows inserted, existing pk rows updated (whole row, value-
  and order-independent), rows absent from the window kept (source-side
  deletes never propagate), single and composite pk;
- gates: merge without a declared pk rejects, unknown mode values reject,
  NULL pk rows reject the whole batch (base untouched);
- row cap judged on the POST-merge total — over cap rolls the entire
  transaction back (base unchanged, ingest not committed), in-cap updates
  of existing rows pass;
- corrections stay alive across merges (replayed onto the merged table);
- provenance: ``dataset.incremental.merged`` lands in the WAL in the same
  transaction, with rows = post-merge total and incoming = staged rows;
- first-ever commit with mode=merge degenerates to the snapshot swap;
- old databases (ingests table built without the ``mode`` column) upgrade
  lazily on first use and keep exact replace semantics.

Isolation: each test points ``DAAS_DATABASE_URL`` at its own ``tmp_path``
database (same pattern as test_cell_event_wal.py — every registry rebuild
re-imports the group modules, so the first ``get_database()`` call binds to
this URL; raw assertions go through the loaded tools' own module globals).
"""
from __future__ import annotations

import sqlite3

import pytest

import conftest
from conftest import engine_db_url, reset_pg_schema
from daas.fd_daas_mcp import registry

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


@pytest.fixture(autouse=True)
def _fresh_engine_db(tmp_path, monkeypatch):
    """Per-test engine database under tmp_path (see module docstring). On the
    postgres leg: the shared one-shot PG test database with the schema dropped
    and recreated — the isolation equivalent of a fresh sqlite file."""
    monkeypatch.setenv("DAAS_DATABASE_URL", engine_db_url(f"{tmp_path}/incr_engine.db"))
    reset_pg_schema()
    yield


def _t() -> dict:
    return {
        registry.namespaced(g, n): fn
        for g, n, fn in registry.build(profile="cell")
    }


def _ingest(key: str, rows: list, mode: str = "replace", pk: list | None = None,
            columns: list | None = None) -> dict:
    t = _t()
    begun = t["customer_dataset_ingest_begin"](
        dataset_key=key, columns=columns or COLS, pk_columns=pk, mode=mode
    )
    assert "error" not in begun, begun
    assert begun["mode"] == mode
    if rows:
        chunked = t["customer_dataset_ingest_chunk"](
            ingest_id=begun["ingest_id"], rows=rows, start_index=0
        )
        assert "error" not in chunked, chunked
    committed = t["customer_dataset_ingest_commit"](ingest_id=begun["ingest_id"])
    assert "error" not in committed, committed
    return committed


def _tools_module():
    """The registry-loaded customer_dataset_tools module (via a tool's globals)."""
    return _t()["customer_dataset_preview"].__globals__


def _raw(sql: str, params: tuple = ()):
    return _tools_module()["_raw_query_all"](sql, params)


def _rows_by_id(key: str, include_corrected: bool = True) -> dict:
    got = _t()["customer_dataset_preview"](dataset_key=key, include_corrected=include_corrected)
    assert "error" not in got, got
    return {r["id"]: r for r in got["rows"]}


# ── upsert semantics ─────────────────────────────────────────────────


def test_merge_updates_inserts_and_keeps_unmentioned_rows():
    _ingest("mrg_a", ROWS, pk=["id"])
    # a shuffled incremental window: three pk updates + one brand-new pk —
    # matching is by primary key, never by row order
    window = [
        {"id": 3, "name": "丙新", "amount": 33.0},
        {"id": 9, "name": "丁", "amount": 90.0},
        {"id": 1, "name": "甲新", "amount": 11.0},
        {"id": 2, "name": "乙新", "amount": 21.0},
    ]
    committed = _ingest("mrg_a", window, mode="merge", pk=["id"])
    assert committed["rows"] == 4  # post-merge total

    rows = _rows_by_id("mrg_a")
    assert rows[1]["name"] == "甲新" and rows[1]["amount"] == 11.0
    assert rows[2]["name"] == "乙新" and rows[2]["amount"] == 21.0
    assert rows[3]["name"] == "丙新" and rows[3]["amount"] == 33.0
    assert rows[9]["name"] == "丁"
    # the merged values live in the base snapshot too (raw read)
    base = _rows_by_id("mrg_a", include_corrected=False)
    assert base[1]["name"] == "甲新" and set(base) == {1, 2, 3, 9}
    # metadata follows the merged total; the spent staging window is gone
    overview = _t()["customer_dataset_overview"](dataset_key="mrg_a")
    assert overview["rows"] == 4
    assert _raw("SELECT name FROM sqlite_master WHERE name='cust_mrg_a__staging'") == []


def test_merge_does_not_propagate_source_deletes():
    _ingest("mrg_del", ROWS, pk=["id"])
    # the next window only mentions id=1 — ids 2/3 are absent (deleted at the
    # source) and MUST survive on the platform side
    _ingest("mrg_del", [ROWS[0]], mode="merge", pk=["id"])
    rows = _rows_by_id("mrg_del")
    assert set(rows) == {1, 2, 3}
    assert rows[2]["amount"] == 20.0  # untouched, original value


def test_merge_composite_pk_updates_and_inserts():
    cols = [
        {"name": "date", "type": "TEXT"},
        {"name": "region", "type": "TEXT"},
        {"name": "value", "type": "REAL"},
    ]
    rows = [
        {"date": "2026-01-01", "region": "上海", "value": 1.0},
        {"date": "2026-01-01", "region": "北京", "value": 2.0},
    ]
    _ingest("mrg_cpk", rows, pk=["date", "region"], columns=cols)
    window = [
        {"date": "2026-01-01", "region": "上海", "value": 100.0},  # full-pk update
        {"date": "2026-01-02", "region": "上海", "value": 3.0},    # new pk pair
    ]
    committed = _ingest("mrg_cpk", window, mode="merge", pk=["date", "region"], columns=cols)
    assert committed["rows"] == 3
    got = _t()["customer_dataset_preview"](dataset_key="mrg_cpk")
    by_pk = {(r["date"], r["region"]): r["value"] for r in got["rows"]}
    assert by_pk[("2026-01-01", "上海")] == 100.0  # updated
    assert by_pk[("2026-01-01", "北京")] == 2.0    # unmentioned, kept
    assert by_pk[("2026-01-02", "上海")] == 3.0    # inserted


# ── gates: pk required, mode validated, NULL pk rejected ─────────────


def test_merge_without_pk_and_bad_mode_reject_at_begin():
    t = _t()
    no_pk = t["customer_dataset_ingest_begin"](dataset_key="mrg_nopk", columns=COLS, mode="merge")
    assert "error" in no_pk and "主键" in no_pk["error"]
    bad_mode = t["customer_dataset_ingest_begin"](
        dataset_key="mrg_badmode", columns=COLS, pk_columns=["id"], mode="upsert"
    )
    assert "error" in bad_mode and "模式" in bad_mode["error"]
    # both begins failed validation before any write — no dataset registered
    assert "error" in t["customer_dataset_overview"](dataset_key="mrg_nopk")
    assert "error" in t["customer_dataset_overview"](dataset_key="mrg_badmode")


def test_merge_rejects_null_pk_and_leaves_base_untouched():
    _ingest("mrg_null", ROWS, pk=["id"])
    t = _t()
    begun = t["customer_dataset_ingest_begin"](
        dataset_key="mrg_null", columns=COLS, pk_columns=["id"], mode="merge"
    )
    chunked = t["customer_dataset_ingest_chunk"](
        ingest_id=begun["ingest_id"],
        rows=[{"id": 7, "name": "好行", "amount": 1.0}, {"id": None, "name": "坏行", "amount": 2.0}],
        start_index=0,
    )
    assert "error" not in chunked, chunked
    refused = t["customer_dataset_ingest_commit"](ingest_id=begun["ingest_id"])
    assert "error" in refused and "主键" in refused["error"] and "NULL" in refused["error"]
    # whole batch rejected: base untouched, ingest still open
    base = _rows_by_id("mrg_null", include_corrected=False)
    assert set(base) == {1, 2, 3} and base[1]["name"] == "甲"
    status = _raw(
        "SELECT status FROM customer_dataset_ingests WHERE id=?", (begun["ingest_id"],)
    )
    assert status == [("open",)]


def test_merge_rejects_null_in_any_composite_pk_column():
    cols = [
        {"name": "date", "type": "TEXT"},
        {"name": "region", "type": "TEXT"},
        {"name": "value", "type": "REAL"},
    ]
    _ingest(
        "mrg_cnull",
        [{"date": "2026-01-01", "region": "上海", "value": 1.0}],
        pk=["date", "region"],
        columns=cols,
    )
    t = _t()
    begun = t["customer_dataset_ingest_begin"](
        dataset_key="mrg_cnull", columns=cols, pk_columns=["date", "region"], mode="merge"
    )
    t["customer_dataset_ingest_chunk"](
        ingest_id=begun["ingest_id"],
        # region is NULL → one composite-pk member NULL is already fatal
        rows=[{"date": "2026-01-02", "region": None, "value": 9.0}],
        start_index=0,
    )
    refused = t["customer_dataset_ingest_commit"](ingest_id=begun["ingest_id"])
    assert "error" in refused and "NULL" in refused["error"]
    got = _t()["customer_dataset_preview"](dataset_key="mrg_cnull")
    assert got["total"] == 1  # base untouched


# ── row cap: post-merge total, whole-transaction rollback ────────────


def test_merge_over_cap_rolls_back_whole_transaction(monkeypatch):
    _ingest("mrg_cap", ROWS, pk=["id"])
    monkeypatch.setenv("CUSTOMER_DATASET_MAX_ROWS", "3")
    t = _t()
    begun = t["customer_dataset_ingest_begin"](
        dataset_key="mrg_cap", columns=COLS, pk_columns=["id"], mode="merge"
    )
    t["customer_dataset_ingest_chunk"](
        ingest_id=begun["ingest_id"], rows=[{"id": 8, "name": "戊", "amount": 40.0}], start_index=0
    )
    refused = t["customer_dataset_ingest_commit"](ingest_id=begun["ingest_id"])
    assert "error" in refused and "上限" in refused["error"]
    # zero writes survived: base row count unchanged, ingest not committed
    assert _t()["customer_dataset_preview"](dataset_key="mrg_cap")["total"] == 3
    status = _raw(
        "SELECT status FROM customer_dataset_ingests WHERE id=?", (begun["ingest_id"],)
    )
    assert status == [("open",)]
    # an in-cap window that only updates existing rows passes: the cap judges
    # the POST-merge total (3), not base+incoming (3+1)
    ok = _ingest("mrg_cap", [{"id": 1, "name": "甲改", "amount": 1.0}], mode="merge", pk=["id"])
    assert ok["rows"] == 3
    assert _rows_by_id("mrg_cap")[1]["name"] == "甲改"


# ── corrections replay onto the merged table ─────────────────────────


def test_corrections_survive_and_replay_across_merges():
    _ingest("mrg_corr", ROWS, pk=["id"])
    t = _t()
    added = t["customer_dataset_correction_add"](
        dataset_key="mrg_corr", op="update", anchor="pk:[2]", values={"amount": 999.0}, actor="corinne"
    )
    assert "error" not in added, added
    assert _rows_by_id("mrg_corr")[2]["amount"] == 999.0

    # merge #1 re-supplies the corrected pk row: the base value refreshes to
    # the source's 20.5, and the correction replays on top of the merged table
    first = _ingest("mrg_corr", [{"id": 2, "name": "乙", "amount": 20.5}], mode="merge", pk=["id"])
    assert first["rows"] == 3
    assert first["corrections_replayed"] == 1
    base = _rows_by_id("mrg_corr", include_corrected=False)
    assert base[2]["amount"] == 20.5
    assert _rows_by_id("mrg_corr")[2]["amount"] == 999.0

    # merge #2 with a window that never mentions pk 2 — the correction is
    # still alive and re-applied after every merge
    second = _ingest("mrg_corr", [{"id": 3, "name": "丙", "amount": 30.0}], mode="merge", pk=["id"])
    assert second["rows"] == 3
    assert _rows_by_id("mrg_corr")[2]["amount"] == 999.0
    dangling = t["customer_dataset_correction_dangling"](dataset_key="mrg_corr")
    assert dangling["corrections"] == []


# ── provenance WAL ───────────────────────────────────────────────────


def test_merge_writes_incremental_merged_wal_event():
    _ingest("mrg_wal", ROWS, pk=["id"])
    _ingest(
        "mrg_wal",
        [{"id": 1, "name": "甲一", "amount": 11.0}, {"id": 4, "name": "丁", "amount": 40.0}],
        mode="merge",
        pk=["id"],
    )
    pending = _t()["customer_dataset_provenance_wal_pending"]()
    assert [e["type"] for e in pending["events"]] == [
        "dataset.snapshot.refreshed",     # baseline replace commit
        "dataset.incremental.merged",     # incremental merge commit
    ]
    assert pending["events"][1]["payload"] == {
        "dataset_key": "mrg_wal",
        "rows": 4,       # post-merge total
        "incoming": 2,   # rows that arrived in this window
    }


def test_merge_first_commit_without_base_builds_baseline():
    # no prior snapshot — the incremental window degenerates to the plain
    # snapshot swap (merge into nothing = everything is new)
    committed = _ingest("mrg_first", ROWS[:2], mode="merge", pk=["id"])
    assert committed["rows"] == 2
    assert set(_rows_by_id("mrg_first")) == {1, 2}
    pending = _t()["customer_dataset_provenance_wal_pending"]()
    assert [e["type"] for e in pending["events"]] == ["dataset.incremental.merged"]
    assert pending["events"][0]["payload"]["rows"] == 2


# ── old database: lazy mode-column upgrade, replace semantics intact ──


def test_legacy_database_upgrades_lazily_and_replace_stays_replace(tmp_path, monkeypatch):
    # a database built by a pre-mode engine build: the ingests audit table
    # exists WITHOUT the mode column and already carries history rows
    if conftest.TEST_BACKEND == "postgres":
        # same legacy shape, materialized on the PG test database: drop the
        # ORM-built table and recreate the pre-mode one, history row included
        conftest.reset_pg_schema()
        from sqlalchemy import create_engine, text

        eng = create_engine(conftest.PG_TEST_URL)
        with eng.begin() as con:
            for stmt in (
                "CREATE TABLE customer_dataset_ingests ("
                " id VARCHAR(36) NOT NULL,"
                " dataset_key VARCHAR(64) NOT NULL,"
                " status VARCHAR(16) NOT NULL,"
                " row_count INTEGER,"
                " actor VARCHAR(255) NOT NULL,"
                " started_at TIMESTAMP,"
                " finished_at TIMESTAMP,"
                " error TEXT,"
                " PRIMARY KEY (id))",
                "INSERT INTO customer_dataset_ingests"
                " (id, dataset_key, status, row_count, actor, started_at)"
                " VALUES ('legacy-row-1', 'legacy_hist', 'committed', 5, 'old-runner',"
                " '2025-12-01 00:00:00.000000')",
            ):
                con.execute(text(stmt))
        eng.dispose()
        monkeypatch.setenv("DAAS_DATABASE_URL", conftest.PG_TEST_URL)
    else:
        db_path = tmp_path / "legacy_engine.db"
        con = sqlite3.connect(db_path)
        con.execute(
            "CREATE TABLE customer_dataset_ingests ("
            " id VARCHAR(36) NOT NULL,"
            " dataset_key VARCHAR(64) NOT NULL,"
            " status VARCHAR(16) NOT NULL,"
            " row_count INTEGER,"
            " actor VARCHAR(255) NOT NULL,"
            " started_at DATETIME,"
            " finished_at DATETIME,"
            " error TEXT,"
            " PRIMARY KEY (id))"
        )
        con.execute(
            "INSERT INTO customer_dataset_ingests"
            " (id, dataset_key, status, row_count, actor, started_at)"
            " VALUES ('legacy-row-1', 'legacy_hist', 'committed', 5, 'old-runner',"
            " '2025-12-01 00:00:00.000000')"
        )
        con.commit()
        con.close()
        monkeypatch.setenv("DAAS_DATABASE_URL", f"sqlite:///{db_path}")

    t = _t()
    # begin defaults to replace; the lazy ALTER lands inside the begin tx
    begun = t["customer_dataset_ingest_begin"](dataset_key="legacy_ds", columns=COLS, pk_columns=["id"])
    assert "error" not in begun, begun
    assert begun["mode"] == "replace"
    t["customer_dataset_ingest_chunk"](ingest_id=begun["ingest_id"], rows=ROWS[:2], start_index=0)
    committed = t["customer_dataset_ingest_commit"](ingest_id=begun["ingest_id"])
    assert "error" not in committed, committed
    assert committed["rows"] == 2

    # replace semantics unchanged: a second full snapshot REPLACES (rows 1/2
    # vanish), it does not merge
    second = _ingest("legacy_ds", ROWS[2:], pk=["id"])
    assert second["rows"] == 1
    assert set(_rows_by_id("legacy_ds")) == {3}

    # the mode column materialized and pre-existing rows read back 'replace'
    cols = {r[1] for r in _raw("PRAGMA table_info(customer_dataset_ingests)")}
    assert "mode" in cols
    legacy_mode = _raw("SELECT mode FROM customer_dataset_ingests WHERE id='legacy-row-1'")
    assert legacy_mode == [("replace",)]

    # and merge works on the upgraded schema
    merged = _ingest("legacy_ds", [{"id": 3, "name": "丙新", "amount": 31.0}], mode="merge", pk=["id"])
    assert merged["rows"] == 1
    assert _rows_by_id("legacy_ds")[3]["name"] == "丙新"
