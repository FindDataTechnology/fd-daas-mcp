"""customer-dataset link — dataset-to-dataset semantic connection
(wire-entity-link §1, engine side).

``link`` = LEFT JOIN materialization between two customer datasets over
(left_column, right_column) into a NEW child dataset. Covers the required
behaviors:

- materialization: every left row kept, hit rows carry the right columns,
  miss rows carry NULL (never a neighbor's value), column plan = left
  verbatim + right renamed on collision (``right_<name>``, then ``_2``
  increments), pk inherited from the left side, lineage kind="link";
- gates: unknown side datasets / drifted join columns reject with the
  offender named, an existing child key rejects (pointing at
  derive_refresh), identical sides reject;
- refresh: ``derive_refresh`` re-joins both sides' latest effective state
  (rows and values update), result carries refreshed=True;
- row cap: an over-cap join rejects the whole transaction (the child keeps
  its previous state — i.e. stays unregistered on first link);
- IS-key semantics: the join walks the EFFECTIVE tables and compares keys
  with SQL ``IS`` — NULL keys compare equal to NULL (design D1), ordinary
  misses stay NULL.

Isolation: each test points ``DAAS_DATABASE_URL`` at its own ``tmp_path``
database (same pattern as test_customer_dataset_incremental.py).
"""
from __future__ import annotations

import pytest

from conftest import dialect_cursor, engine_db_url, reset_pg_schema
from daas.fd_daas_mcp import registry

ORDER_COLS = [
    {"name": "id", "type": "INTEGER"},
    {"name": "customer_id", "type": "INTEGER"},
    {"name": "amount", "type": "REAL"},
]
CUSTOMER_COLS = [
    {"name": "id", "type": "INTEGER"},
    {"name": "name", "type": "TEXT"},
    {"name": "tier", "type": "TEXT"},
]

ORDERS = [
    {"id": 1, "customer_id": 10, "amount": 100.0},
    {"id": 2, "customer_id": 20, "amount": 200.0},
    {"id": 3, "customer_id": 99, "amount": 300.0},  # no matching customer
]
CUSTOMERS = [
    {"id": 10, "name": "甲公司", "tier": "gold"},
    {"id": 20, "name": "乙公司", "tier": "silver"},
]


@pytest.fixture(autouse=True)
def _fresh_engine_db(tmp_path, monkeypatch):
    """Per-test engine database under tmp_path (see module docstring). On the
    postgres leg: the shared one-shot PG test database with the schema dropped
    and recreated — the isolation equivalent of a fresh sqlite file."""
    monkeypatch.setenv("DAAS_DATABASE_URL", engine_db_url(f"{tmp_path}/link_engine.db"))
    reset_pg_schema()
    yield


def _t() -> dict:
    return {
        registry.namespaced(g, n): fn
        for g, n, fn in registry.build(profile="cell")
    }


def _ingest(key: str, rows: list, columns: list, pk: list | None = None) -> None:
    t = _t()
    begun = t["customer_dataset_ingest_begin"](
        dataset_key=key, columns=columns, pk_columns=pk
    )
    assert "error" not in begun, begun
    if rows:
        chunked = t["customer_dataset_ingest_chunk"](
            ingest_id=begun["ingest_id"], rows=rows, start_index=0
        )
        assert "error" not in chunked, chunked
    committed = t["customer_dataset_ingest_commit"](ingest_id=begun["ingest_id"])
    assert "error" not in committed, committed


def _seed_orders_customers() -> None:
    _ingest("lnk_orders", ORDERS, ORDER_COLS, pk=["id"])
    _ingest("lnk_customers", CUSTOMERS, CUSTOMER_COLS, pk=["id"])


def _link(child: str = "lnk_child", **kw) -> dict:
    return _t()["customer_dataset_link"](
        child_key=child,
        left_key=kw.get("left_key", "lnk_orders"),
        left_column=kw.get("left_column", "customer_id"),
        right_key=kw.get("right_key", "lnk_customers"),
        right_column=kw.get("right_column", "id"),
        actor=kw.get("actor", "tester"),
    )


def _rows_by(key: str, col: str) -> dict:
    got = _t()["customer_dataset_preview"](dataset_key=key)
    assert "error" not in got, got
    return {r[col]: r for r in got["rows"]}


# ── materialization: LEFT JOIN semantics + column plan + lineage ──────


def test_link_materializes_left_join():
    _seed_orders_customers()
    created = _link()
    assert created == {"dataset_key": "lnk_child", "rows": 3, "kind": "link"}

    rows = _rows_by("lnk_child", "id")
    # hit rows carry the right columns
    assert rows[1]["name"] == "甲公司" and rows[1]["tier"] == "gold"
    assert rows[2]["name"] == "乙公司" and rows[2]["tier"] == "silver"
    # miss row keeps its left values and gets NULL right columns — every
    # left row survives, never a neighbor's value
    assert rows[3]["name"] is None and rows[3]["tier"] is None
    assert rows[3]["amount"] == 300.0

    overview = _t()["customer_dataset_overview"](dataset_key="lnk_child")
    assert overview["rows"] == 3
    # column plan: left verbatim (name/type) + right renamed on collision
    assert [(c["name"], c["type"]) for c in overview["columns"]] == [
        ("id", "INTEGER"), ("customer_id", "INTEGER"), ("amount", "REAL"),
        ("right_id", "INTEGER"), ("name", "TEXT"), ("tier", "TEXT"),
    ]
    # pk inherited from the LEFT dataset, and pk anchors work on the child
    assert overview["pk_columns"] == ["id"]
    assert rows[1]["__row_id"] == "pk:[1]"
    # physical schema = exactly the planned columns + __row_hash (the left
    # table's own __row_hash must NOT leak through the join)
    conn = _raw_conn()
    try:
        phys = [r[1] for r in dialect_cursor(conn).execute("PRAGMA table_info(cust_lnk_child)")]
    finally:
        conn.close()
    assert phys == ["id", "customer_id", "amount", "right_id", "name", "tier", "__row_hash"]

    # lineage: kind=link, parent=left, spec carries the right-side tuple
    lin = _t()["customer_dataset_lineage"](dataset_key="lnk_child")
    assert "error" not in lin, lin
    deriv = lin["derivation"][0]
    assert deriv["parent_key"] == "lnk_orders"
    assert deriv["kind"] == "link"
    assert deriv["spec"] == {
        "left_key": "lnk_orders", "left_column": "customer_id",
        "right_key": "lnk_customers", "right_column": "id",
    }
    assert deriv["status"] == "ok"


def test_collision_prefix_and_increment_suffix():
    # left already owns a literal ``right_id`` → the right ``id`` first tries
    # right_id (taken) and lands on right_id_2; ``label`` stays verbatim
    left_cols = [{"name": "id", "type": "INTEGER"}, {"name": "right_id", "type": "TEXT"}]
    right_cols = [{"name": "id", "type": "TEXT"}, {"name": "label", "type": "TEXT"}]
    _ingest("lnk_l2", [{"id": 1, "right_id": "x"}], left_cols, pk=["id"])
    _ingest("lnk_r2", [{"id": "1", "label": "L"}], right_cols, pk=["id"])
    created = _link("lnk_c2", left_key="lnk_l2", left_column="id",
                    right_key="lnk_r2", right_column="id")
    assert created["rows"] == 1
    overview = _t()["customer_dataset_overview"](dataset_key="lnk_c2")
    assert [c["name"] for c in overview["columns"]] == ["id", "right_id", "right_id_2", "label"]
    row = _t()["customer_dataset_preview"](dataset_key="lnk_c2")["rows"][0]
    assert row["right_id"] == "x" and row["right_id_2"] == "1" and row["label"] == "L"


# ── gates: named drift errors, existing child, identical sides ────────


def test_named_errors_for_drift_and_missing_sides():
    _seed_orders_customers()
    t = _t()
    drifted_left = _link(left_column="nope")
    assert "error" in drifted_left and "左列不存在" in drifted_left["error"]
    assert "nope" in drifted_left["error"]
    drifted_right = _link(right_column="nope")
    assert "error" in drifted_right and "右列不存在" in drifted_right["error"]
    assert "nope" in drifted_right["error"]
    missing_left = _link(left_key="lnk_ghost")
    assert "error" in missing_left and "左数据集不存在" in missing_left["error"]
    missing_right = _link(right_key="lnk_ghost")
    assert "error" in missing_right and "右数据集不存在" in missing_right["error"]
    # sides must differ, and the child cannot be either side
    same = _link(right_key="lnk_orders")
    assert "error" in same and "左右数据集不能相同" in same["error"]
    self_child = _link(child="lnk_orders")
    assert "error" in self_child and "相同" in self_child["error"]
    # nothing above registered a child
    assert "error" in t["customer_dataset_overview"](dataset_key="lnk_child")


def test_existing_child_rejects_pointing_at_refresh():
    _seed_orders_customers()
    assert "error" not in _link()
    again = _link()
    assert "error" in again and "derive_refresh" in again["error"]
    # the child keeps its previous state
    assert _t()["customer_dataset_overview"](dataset_key="lnk_child")["rows"] == 3


# ── refresh: re-join the latest effective state of both sides ─────────


def test_refresh_rejoins_latest_data():
    _seed_orders_customers()
    _link()
    # the right side moves on: customer 20 renamed, customer 99 onboarded
    _ingest(
        "lnk_customers",
        [
            {"id": 10, "name": "甲公司", "tier": "gold"},
            {"id": 20, "name": "乙公司改", "tier": "platinum"},
            {"id": 99, "name": "壬公司", "tier": "bronze"},
        ],
        CUSTOMER_COLS,
        pk=["id"],
    )
    refreshed = _t()["customer_dataset_derive_refresh"](child_key="lnk_child", actor="tester")
    assert "error" not in refreshed, refreshed
    assert refreshed == {
        "dataset_key": "lnk_child", "rows": 3, "kind": "link", "refreshed": True,
    }
    rows = _rows_by("lnk_child", "id")
    assert rows[2]["name"] == "乙公司改" and rows[2]["tier"] == "platinum"
    assert rows[3]["name"] == "壬公司" and rows[3]["tier"] == "bronze"  # former miss
    # a left-side edit shows up on refresh too
    _ingest("lnk_orders", ORDERS + [{"id": 4, "customer_id": 10, "amount": 400.0}],
            ORDER_COLS, pk=["id"])
    refreshed2 = _t()["customer_dataset_derive_refresh"](child_key="lnk_child")
    assert refreshed2["rows"] == 4
    assert _rows_by("lnk_child", "id")[4]["name"] == "甲公司"


# ── row cap: whole-batch rejection ────────────────────────────────────


def test_over_cap_rejects_whole_batch(monkeypatch):
    _seed_orders_customers()
    monkeypatch.setenv("CUSTOMER_DATASET_MAX_ROWS", "2")
    refused = _link()
    assert "error" in refused and "上限" in refused["error"]
    # whole batch rejected: the child stays unregistered and tableless
    t = _t()
    assert "error" in t["customer_dataset_overview"](dataset_key="lnk_child")
    conn = _raw_conn()
    try:
        cur = dialect_cursor(conn)
        assert cur.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'cust_lnk_child%'"
        ).fetchone()[0] == 0
    finally:
        conn.close()
    # back under the cap the same link succeeds
    monkeypatch.setenv("CUSTOMER_DATASET_MAX_ROWS", "500000")
    assert "error" not in _link()


def _raw_conn():
    mod = _t()["customer_dataset_preview"].__globals__
    return mod["get_database"]().engine.raw_connection()


# ── effective tables + IS key semantics ───────────────────────────────


def test_link_reads_effective_tables():
    _seed_orders_customers()
    # a correction re-points order 2's key before the link — the join must
    # walk the EFFECTIVE right/left tables (correction layer applies)
    _t()["customer_dataset_correction_add"](
        dataset_key="lnk_orders", op="update", anchor="pk:[2]",
        values={"customer_id": 10},
    )
    assert "error" not in _link()
    rows = _rows_by("lnk_child", "id")
    assert rows[2]["name"] == "甲公司"  # corrected key hit, not the base's 20


def test_is_null_key_semantics():
    # left keys (1, NULL); right keys (NULL, 7) — SQL IS compares NULL equal
    # to NULL (design D1), so the NULL-keyed left row picks up the NULL-keyed
    # right row; key 1 has no right counterpart and stays NULL-extended
    left_cols = [{"name": "id", "type": "INTEGER"}, {"name": "note", "type": "TEXT"}]
    right_cols = [{"name": "rid", "type": "INTEGER"}, {"name": "val", "type": "TEXT"}]
    _ingest("lnk_null_l", [{"id": 1, "note": "a"}, {"id": None, "note": "n"}], left_cols)
    _ingest("lnk_null_r", [{"rid": None, "val": "nullside"}, {"rid": 7, "val": "seven"}],
            right_cols)
    created = _link("lnk_null_c", left_key="lnk_null_l", left_column="id",
                    right_key="lnk_null_r", right_column="rid")
    assert created["rows"] == 2
    rows = _rows_by("lnk_null_c", "note")
    assert rows["n"]["val"] == "nullside"  # NULL IS NULL → matched
    assert rows["a"]["val"] is None        # plain miss → NULL, never a neighbor


def test_sql_side_row_hash_matches_canonical_form():
    # the CREATE-AS path computes __row_hash through a per-connection scalar
    # function; it must equal the canonical Python _row_hash so hash anchors
    # (no-pk datasets) behave identically whether a row arrived by ingest or
    # by link — and corrections anchored on a link child actually hit
    cols = [{"name": "k", "type": "INTEGER"}, {"name": "v", "type": "REAL"}]
    row = {"k": 1, "v": 20.0}  # whole float — _norm collapses it to int 20
    _ingest("lnk_h_l", [row], cols)
    _ingest("lnk_h_r", [{"k": 1, "w": "x"}],
            [{"name": "k", "type": "INTEGER"}, {"name": "w", "type": "TEXT"}])
    assert "error" not in _link("lnk_h_c", left_key="lnk_h_l", left_column="k",
                                right_key="lnk_h_r", right_column="k")
    # a plain ingested dataset with the SAME logical row shape (k, v, w) —
    # the link child's columns are k, v, right_k, w
    _ingest("lnk_h_plain", [{"k": 1, "v": 20.0, "right_k": 1, "w": "x"}],
            [{"name": "k", "type": "INTEGER"}, {"name": "v", "type": "REAL"},
             {"name": "right_k", "type": "INTEGER"}, {"name": "w", "type": "TEXT"}])
    conn = _raw_conn()
    try:
        cur = conn.cursor()
        linked = cur.execute('SELECT "__row_hash" FROM cust_lnk_h_c WHERE k=1').fetchone()
        plain = cur.execute('SELECT "__row_hash" FROM cust_lnk_h_plain WHERE k=1').fetchone()
    finally:
        conn.close()
    assert linked == plain  # SQL-side hash == canonical Python hash
    # and a hash-anchored correction on the no-pk link child lands
    anchor = _t()["customer_dataset_preview"](dataset_key="lnk_h_c")["rows"][0]["__row_id"]
    assert anchor.startswith("hash:")
    edited = _t()["customer_dataset_correction_add"](
        dataset_key="lnk_h_c", op="update", anchor=anchor, values={"v": 99.0}, actor="h"
    )
    assert "error" not in edited, edited
    assert _t()["customer_dataset_preview"](dataset_key="lnk_h_c")["rows"][0]["v"] == 99.0
