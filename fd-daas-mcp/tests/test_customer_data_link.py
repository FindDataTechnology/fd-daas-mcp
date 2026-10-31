"""customer_dataset_link — LEFT-JOIN materialization (wire-transform-pipelines
§1 engine side / wire-entity-link, design D4).

The engine-side half of the entity-relationship materialization contract: the
wire caller (fd-wire entities materialize endpoint) is FROZEN — it sends
exactly ``child_key / left_key / left_column / right_key / right_column /
actor / channel`` (see fd_wire_api/entities/routes.py). These tests pin that
argument shape verbatim plus the required behaviors:

- hits and misses coexist: 5 left rows / 3 right rows / 2 key hits → child
  has 5 rows, hit rows carry right values, miss rows have NULL right columns;
- the deterministic collision rule: a right column whose name collides with a
  left column surfaces as ``right_<name>``; free names are kept verbatim
  (mirrors the wire-side LINKED_COLUMNS snapshot);
- composite keys (lists) match only on full tuples;
- unknown key column → honest structural error, NO partial child (no
  metadata row, no lineage row);
- replay via derive_refresh rebuilds the JOIN from CURRENT left/right
  contents: right gaining rows grows hit count (and row count through
  standard LEFT-JOIN duplication); a deleted source is an honest
  source-missing error and the child keeps its previous state;
- lineage row kind="link" with the right key + both key-column lists;
  provenance WAL follows the derive/join convention (dataset.derived,
  kind=link);
- registration: the tool is in the cell registry, capabilities advertise the
  link flag, and the left parent's delete guard covers the link child.

Isolation: each test points ``DAAS_DATABASE_URL`` at its own ``tmp_path``
database (same pattern as test_customer_data_quality.py).
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from daas.fd_daas_mcp import registry

# wire ORDER_COLUMNS / CUSTOMER_COLUMNS snapshot (entities/routes contract)
ORDERS_COLS = [
    {"name": "order_id", "type": "TEXT"},
    {"name": "customer_id", "type": "TEXT"},
    {"name": "amount", "type": "REAL"},
]
CUSTOMERS_COLS = [
    {"name": "customer_id", "type": "TEXT"},
    {"name": "name", "type": "TEXT"},
]

ORDERS_ROWS = [
    {"order_id": "o1", "customer_id": "c1", "amount": 10.5},
    {"order_id": "o2", "customer_id": "c1", "amount": 20.0},
    {"order_id": "o3", "customer_id": "c9", "amount": 5.0},
    {"order_id": "o4", "customer_id": "c9", "amount": 7.5},
    {"order_id": "o5", "customer_id": "c7", "amount": 1.0},
]
CUSTOMERS_ROWS = [
    {"customer_id": "c1", "name": "甲"},
    {"customer_id": "c2", "name": "乙"},
    {"customer_id": "c3", "name": "丙"},
]

#: engine LEFT JOIN output columns — right customer_id collides → right_
#: prefix; name is free → kept (the wire-side backfill expects this shape)
LINKED_COLUMN_NAMES = ["order_id", "customer_id", "amount", "right_customer_id", "name"]


@pytest.fixture(autouse=True)
def _fresh_engine_db(tmp_path, monkeypatch):
    """Per-test engine database under tmp_path (see module docstring)."""
    monkeypatch.setenv("DAAS_DATABASE_URL", f"sqlite:///{tmp_path}/link_engine.db")
    yield


def _t() -> dict:
    return {
        registry.namespaced(g, n): fn
        for g, n, fn in registry.build(profile="cell")
    }


def _key(prefix: str) -> str:
    return f"{prefix}_{os.urandom(4).hex()}"


def _ingest(key: str, columns: list, rows: list, pk: list | None = None) -> None:
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


def _seed_orders_customers() -> tuple[str, str]:
    left, right = _key("orders"), _key("customers")
    _ingest(left, ORDERS_COLS, ORDERS_ROWS, pk=["order_id"])
    _ingest(right, CUSTOMERS_COLS, CUSTOMERS_ROWS, pk=["customer_id"])
    return left, right


def _link(child: str, left: str, right: str, **overrides) -> dict:
    t = _t()
    args = {
        "child_key": child,
        "left_key": left,
        "left_column": "customer_id",
        "right_key": right,
        "right_column": "customer_id",
        "actor": "customer:cust-001",
        "channel": "console",
    }
    args.update(overrides)
    return t["customer_dataset_link"](**args)


def _rows_sorted(child: str, by: str = "order_id") -> list[dict]:
    return _t()["customer_dataset_query"](
        dataset_key=child, sort=[{"column": by, "dir": "asc"}]
    )["rows"]


def _db_path() -> Path:
    return Path(os.environ["DAAS_DATABASE_URL"][len("sqlite:///"):])


# ── hits / misses / collision rule ───────────────────────────────────


def test_hits_misses_and_collision_prefix():
    left, right = _seed_orders_customers()
    child = _key("linked")
    r = _link(child, left, right)
    assert "error" not in r, r
    assert r == {"dataset_key": child, "rows": 5, "kind": "link"}

    # three-tier layout + registration path identical to derive/join
    ov = _t()["customer_dataset_overview"](dataset_key=child)
    assert "error" not in ov, ov
    assert ov["rows"] == 5
    assert [c["name"] for c in ov["columns"]] == LINKED_COLUMN_NAMES
    assert ov["pk_columns"] == ["order_id"]  # inherits the LEFT pk declaration

    rows = _rows_sorted(child)
    assert len(rows) == 5  # left rows fully preserved
    assert rows[0]["name"] == "甲" and rows[0]["right_customer_id"] == "c1"  # o1 hit
    assert rows[1]["name"] == "甲" and rows[1]["right_customer_id"] == "c1"  # o2 hit
    assert rows[2]["name"] is None and rows[2]["right_customer_id"] is None  # o3 miss
    assert rows[3]["name"] is None and rows[3]["right_customer_id"] is None  # o4 miss
    assert rows[4]["name"] is None and rows[4]["right_customer_id"] is None  # o5 miss
    # left columns pass through untouched
    assert rows[0]["amount"] == 10.5 and rows[4]["order_id"] == "o5"


def test_composite_key_full_tuple_match():
    left, right = _key("lc"), _key("rc")
    _ingest(
        left,
        [{"name": "region", "type": "TEXT"}, {"name": "code", "type": "TEXT"}, {"name": "val", "type": "INTEGER"}],
        [
            {"region": "east", "code": "1", "val": 1},
            {"region": "east", "code": "2", "val": 2},
            {"region": "west", "code": "1", "val": 3},
        ],
    )
    _ingest(
        right,
        [{"name": "region", "type": "TEXT"}, {"name": "code", "type": "TEXT"}, {"name": "label", "type": "TEXT"}],
        [
            {"region": "east", "code": "1", "label": "E1"},
            {"region": "east", "code": "2", "label": "E2"},
            {"region": "north", "code": "1", "label": "N1"},
        ],
    )
    child = _key("lcc")
    r = _link(
        child, left, right,
        left_column=["region", "code"],  # composite (list form)
        right_column=["region", "code"],
    )
    assert "error" not in r, r
    assert r["rows"] == 3
    rows = _rows_sorted(child, by="val")
    assert rows[0]["label"] == "E1"
    assert rows[1]["label"] == "E2"
    # west/1 must NOT match north/1 — composite keys require the full tuple
    assert rows[2]["label"] is None
    assert rows[2]["region"] == "west" and rows[2]["code"] == "1"


# ── honest structural rejections, zero partial child ─────────────────


def test_right_key_column_missing_rejected_no_child():
    left, right = _seed_orders_customers()
    child = _key("linked")
    r = _link(child, left, right, right_column="no_such_column")
    assert r.get("success") is False
    assert "右数据集连接列不存在" in r["error"] and "no_such_column" in r["error"]

    # NO partial child: no metadata row (overview errors), no lineage row
    t = _t()
    gone = t["customer_dataset_overview"](dataset_key=child)
    assert "数据集不存在" in gone["error"]
    lin = _raw_lineage(child)
    assert lin == []


def test_left_key_column_and_unknown_dataset_rejected():
    left, right = _seed_orders_customers()
    t = _t()
    bad_left_col = _link(_key("l1"), left, right, left_column="ghost")
    assert "左数据集连接列不存在" in bad_left_col["error"]

    mismatch = _link(
        _key("l2"), left, right, left_column=["customer_id", "order_id"], right_column=["customer_id"]
    )
    assert "连接列数不匹配" in mismatch["error"]

    no_right = t["customer_dataset_link"](
        child_key=_key("l3"), left_key=left, left_column="customer_id",
        right_key="ghost_right", right_column="customer_id",
        actor="a", channel="console",
    )
    assert "右数据集不存在" in no_right["error"]

    dup_child = _link(left, left, right)  # child == left
    assert "不能与源数据集相同" in dup_child["error"]


def test_existing_child_rejected_pointing_at_refresh():
    left, right = _seed_orders_customers()
    child = _key("linked")
    assert "error" not in _link(child, left, right)
    again = _link(child, left, right)
    assert "数据集已存在" in again["error"] and "derive_refresh" in again["error"]


# ── lineage + WAL conventions ────────────────────────────────────────


def _raw_lineage(child: str) -> list[tuple]:
    conn = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT dataset_key, parent_key, kind, spec_json FROM customer_dataset_lineage"
            " WHERE dataset_key=?",
            (child,),
        ).fetchall()
    finally:
        conn.close()


def test_lineage_row_kind_link_and_wal_event():
    left, right = _seed_orders_customers()
    child = _key("linked")
    assert "error" not in _link(child, left, right)

    lin = _t()["customer_dataset_lineage"](dataset_key=child)
    assert len(lin["derivation"]) == 1
    d = lin["derivation"][0]
    assert d["parent_key"] == left  # lineage parent = LEFT dataset
    assert d["kind"] == "link"
    assert d["status"] == "ok"
    assert d["spec"] == {
        "right_key": right,
        "left_columns": ["customer_id"],
        "right_columns": ["customer_id"],
    }
    # the left parent's children listing includes the link child
    parent_lin = _t()["customer_dataset_lineage"](dataset_key=left)
    assert [c["dataset_key"] for c in parent_lin["children"]] == [child]

    # provenance WAL follows the derive/join convention (dataset.derived)
    conn = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True)
    try:
        events = conn.execute(
            "SELECT type, payload FROM cell_event_wal WHERE payload LIKE ?",
            (f'%"dataset_key": "{child}"%',),
        ).fetchall()
    finally:
        conn.close()
    derived = [(t, json.loads(p)) for t, p in events if t == "dataset.derived"]
    assert derived and derived[0][1]["kind"] == "link"
    assert derived[0][1]["parent_key"] == left


def test_left_delete_blocked_while_link_child_alive():
    left, right = _seed_orders_customers()
    child = _key("linked")
    assert "error" not in _link(child, left, right)
    blocked = _t()["customer_dataset_delete"](dataset_key=left, actor="test")
    assert "存在活跃派生子集" in blocked["error"] and child in blocked["error"]


# ── replay through derive_refresh ────────────────────────────────────


def test_replay_after_right_gains_rows():
    left, right = _seed_orders_customers()
    child = _key("linked")
    assert "error" not in _link(child, left, right)
    assert _t()["customer_dataset_overview"](dataset_key=child)["rows"] == 5

    # right gains two rows: c9 (previously unmatched on the left side) and a
    # second c1 (standard LEFT JOIN → the matching left row duplicates)
    t = _t()
    grew = t["customer_dataset_write_batch"](
        dataset_key=right,
        ops=[
            {"op": "insert", "values": {"customer_id": "c9", "name": "丁"}},
            {"op": "insert", "values": {"customer_id": "c1", "name": "甲2"}},
        ],
        actor="test",
    )
    assert "error" not in grew, grew

    refreshed = t["customer_dataset_derive_refresh"](child_key=child, actor="test")
    assert "error" not in refreshed, refreshed
    assert refreshed["kind"] == "link" and refreshed["refreshed"] is True
    # 5 → 7: the two c9 left rows start hitting, and BOTH c1 left rows now
    # match twice (standard LEFT-JOIN duplication on the duplicate right key)
    assert refreshed["rows"] == 7

    rows = _rows_sorted(child)
    names = [r["name"] for r in rows]
    assert names.count("丁") == 2  # both c9 left rows now hit
    assert names.count("甲") == 2 and names.count("甲2") == 2  # c1 duplicated
    assert names.count(None) == 1  # c7 still unmatched


def test_replay_after_right_deleted_source_missing_child_intact():
    left, right = _seed_orders_customers()
    child = _key("linked")
    assert "error" not in _link(child, left, right)

    # right is NOT the lineage parent → deletion is allowed (no guard)
    deleted = _t()["customer_dataset_delete"](dataset_key=right, actor="test")
    assert "error" not in deleted, deleted

    r = _t()["customer_dataset_derive_refresh"](child_key=child, actor="test")
    assert r.get("success") is False
    assert "右数据集不存在" in r["error"]

    # child keeps its previous state — no fabricated empty result
    rows = _rows_sorted(child)
    assert len(rows) == 5 and rows[0]["name"] == "甲"


def test_replay_after_left_missing_source_missing_child_intact():
    left, right = _seed_orders_customers()
    child = _key("linked")
    assert "error" not in _link(child, left, right)

    # The live guard blocks deleting the left parent while the child exists;
    # simulate the drifted state directly (metadata + tables gone, lineage
    # kept) — exactly the drift the replay path must report honestly.
    conn = sqlite3.connect(str(_db_path()))
    try:
        conn.execute("DELETE FROM customer_datasets WHERE key=?", (left,))
        for suffix in ("", "__base", "__staging"):
            conn.execute(f"DROP TABLE IF EXISTS cust_{left}{suffix}")
        conn.commit()
    finally:
        conn.close()

    r = _t()["customer_dataset_derive_refresh"](child_key=child, actor="test")
    assert r.get("success") is False
    assert "左数据集不存在" in r["error"]

    rows = _rows_sorted(child)
    assert len(rows) == 5 and rows[0]["name"] == "甲"  # child untouched


# ── registration / capabilities / frozen wire payload ────────────────


def test_registered_and_capability_flag_advertised():
    t = _t()
    assert callable(t.get("customer_dataset_link"))
    left, _right = _seed_orders_customers()
    ov = t["customer_dataset_overview"](dataset_key=left)
    assert ov["capabilities"].get("link") is True


def test_wire_contract_payload_verbatim():
    """The wire entities materialize endpoint is a FROZEN caller — this is
    its exact argument dict (fd_wire_api/entities/routes.py materialize_
    relationship). The engine adapts to the caller, not vice versa."""
    left, right = _seed_orders_customers()
    child = _key("linked")
    t = _t()
    result = t["customer_dataset_link"](
        child_key=child,
        left_key=left,
        left_column="customer_id",
        right_key=right,
        right_column="customer_id",
        actor="customer:cust-001",
        channel="console",
    )
    assert "error" not in result, result
    # the wire _register_derived_child consumes rows; dataset_key matches
    assert result["rows"] == 5 and result["dataset_key"] == child


# ── m:n bridge expansion (wire-entity-mn-materialize) ────────────────


JUNCTION_COLS = [
    {"name": "order_id", "type": "TEXT"},
    {"name": "customer_id", "type": "TEXT"},
]


def _seed_junction(rows: list) -> str:
    key = _key("order_customers")
    _ingest(key, JUNCTION_COLS, rows)
    return key


def _bridge_args(junction: str, **overrides) -> dict:
    # 桥展开的连接列口径：orders.order_id ↔ 桥.order_id、customers.customer_id
    # ↔ 桥.customer_id（_link 默认 left_column=customer_id 是 n:1 用例口径）
    args = {
        "left_column": "order_id",
        "junction_key": junction,
        "junction_left_column": "order_id",
        "junction_right_column": "customer_id",
    }
    args.update(overrides)
    return args


def test_bridge_expansion_pairs_and_orphans():
    """桥行驱动 INNER 对表：一条有效桥行一行结果；孤儿桥丢弃并计数如实
    上报；中间集列不进输出；子集主键 = 两端主键成对键（design D1）。"""
    left, right = _seed_orders_customers()
    junction = _seed_junction([
        {"order_id": "o1", "customer_id": "c1"},
        {"order_id": "o2", "customer_id": "c2"},
        {"order_id": "o3", "customer_id": "c3"},
        {"order_id": "o4", "customer_id": "c1"},
        {"order_id": "oX", "customer_id": "c1"},  # 孤儿：左端失存
        {"order_id": "o1", "customer_id": "cX"},  # 孤儿：右端失存
    ])
    child = _key("pairs")
    r = _link(child, left, right, **_bridge_args(junction))
    assert "error" not in r, r
    # 6 桥行 → 4 对（两个孤儿被丢弃、如实计数）
    assert r == {"dataset_key": child, "rows": 4, "kind": "link", "orphan_dropped": 2}

    ov = _t()["customer_dataset_overview"](dataset_key=child)
    assert "error" not in ov, ov
    assert [c["name"] for c in ov["columns"]] == LINKED_COLUMN_NAMES  # 桥列不进输出
    assert ov["pk_columns"] == ["order_id", "right_customer_id"]  # 成对键（撞名走输出名）

    rows = _rows_sorted(child)
    assert len(rows) == 4
    by_order = {r["order_id"]: r for r in rows}
    assert by_order["o1"]["name"] == "甲" and by_order["o1"]["right_customer_id"] == "c1"
    assert by_order["o2"]["name"] == "乙" and by_order["o2"]["right_customer_id"] == "c2"
    assert by_order["o3"]["name"] == "丙" and by_order["o3"]["right_customer_id"] == "c3"
    assert by_order["o4"]["name"] == "甲" and by_order["o4"]["right_customer_id"] == "c1"
    # o5 无桥 → 不入结果（不得产出全 NULL 伪行）
    assert "o5" not in by_order


def test_bridge_replay_after_junction_gains_rows():
    """derive_refresh 按血缘 spec（含 junction 三要素）重连最新桥。"""
    left, right = _seed_orders_customers()
    junction = _seed_junction([{"order_id": "o1", "customer_id": "c1"}])
    child = _key("pairs")
    assert "error" not in _link(child, left, right, **_bridge_args(junction))
    assert _t()["customer_dataset_overview"](dataset_key=child)["rows"] == 1

    # 桥表新增一条有效桥 + 一条孤儿桥
    grew = _t()["customer_dataset_write_batch"](
        dataset_key=junction,
        ops=[
            {"op": "insert", "values": {"order_id": "o2", "customer_id": "c2"}},
            {"op": "insert", "values": {"order_id": "oZ", "customer_id": "c1"}},
        ],
        actor="test",
    )
    assert "error" not in grew, grew

    refreshed = _t()["customer_dataset_derive_refresh"](child_key=child, actor="test")
    assert "error" not in refreshed, refreshed
    assert refreshed["kind"] == "link" and refreshed["refreshed"] is True
    assert refreshed["rows"] == 2
    assert refreshed["orphan_dropped"] == 1


def test_bridge_partial_args_rejected():
    """三要素必须同给同缺（half-given 如实拒）。"""
    left, right = _seed_orders_customers()
    r = _link(_key("pairs"), left, right, junction_key="jx")
    assert "error" in r and "同时提供" in r["error"]


def test_bridge_unknown_junction_column_rejected():
    """桥列不在中间集列快照 → 结构性错误，无半写。"""
    left, right = _seed_orders_customers()
    junction = _seed_junction([{"order_id": "o1", "customer_id": "c1"}])
    r = _link(
        _key("pairs"), left, right,
        **_bridge_args(junction, junction_left_column="ghost"),
    )
    assert "error" in r and "ghost" in r["error"]
    # 错误路径不落子集：桥表仍只有播种时的那一行（无半写佐证）
    bridge_rows = _t()["customer_dataset_query"](dataset_key=junction)["rows"]
    assert len(bridge_rows) == 1


def test_bridge_row_cap_rejects_whole_batch(monkeypatch):
    """桥模式走同一 _materialize_child_tx 行数闸：超限整批拒绝、子集不登记。"""
    left, right = _seed_orders_customers()
    junction = _seed_junction([
        {"order_id": "o1", "customer_id": "c1"},
        {"order_id": "o2", "customer_id": "c2"},
        {"order_id": "o3", "customer_id": "c3"},
        {"order_id": "o4", "customer_id": "c1"},
    ])
    # 播种后再收闸（_max_rows 调用时读 env——播种不受影响）
    monkeypatch.setenv("CUSTOMER_DATASET_MAX_ROWS", "3")
    child = _key("pairs")
    r = _link(child, left, right, **_bridge_args(junction))
    assert "error" in r
    assert "超过上限" in r["error"] and "整批拒绝" in r["error"]
    ov = _t()["customer_dataset_overview"](dataset_key=child)
    assert "error" in ov  # 子集未登记（零半写）
