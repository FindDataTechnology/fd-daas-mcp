"""customer_dataset_window — partitioned/ordered window operator
(wire-window-operator 1.1–1.4).

The engine-side half of the window contract (design D1–D4):

- output = NEW derived dataset: source columns passed through + one column per
  window row (named by ``as``); the row set is unchanged (windows never add or
  drop rows);
- function closed set row_number|rank|dense_rank|lag|lead|sum|avg|min|max|count;
  ranking funcs and lag/lead require ``order_by``; lag/lead ``offset`` defaults
  to 1 and must be 1..10000; sum/avg/min/max require ``column`` (count allows
  none); ``as`` required, unique, and never collides with a source column;
  ``frame ∈ whole_partition|running`` (running requires ``order_by``);
- determinism: the internal ORDER BY appends the stable row key ``__row_hash``
  as the final tie-break, so re-derivation is row-identical; ``__row_hash`` and
  every reserved-prefix column stay out of the output;
- lineage kind=``window`` with the full spec; ``derive_refresh`` replays it;
- the row cap rejects the whole batch and leaves the child's previous state
  untouched.

All expected values below are hand-computed from the seeded rows.

Dual-backend note (task 1.4): the PG leg is not runnable on this checkout —
``tests/conftest.py`` has no ``FD_ENGINE_TEST_BACKEND`` / ``FD_ENGINE_PG_TEST_URL``
selection and ``customer-dataset-mcp/db_dialect.py`` (wire-cell-postgres-storage)
is absent, so ``_tx()``/``_raw_query_*`` are raw sqlite3 (``BEGIN IMMEDIATE``,
``?`` placeholders) and cannot talk to psycopg. The suite therefore carries a
``requires_sqlite`` gate; the SQL itself is standard window syntax and runs
unchanged on both backends once the dialect layer is present.

Isolation: each test points ``DAAS_DATABASE_URL`` at its own ``tmp_path``
database (same pattern as test_customer_data_link.py).
"""
from __future__ import annotations

import os
import pathlib

import pytest

from daas.fd_daas_mcp import registry

# ── dual-backend gate (wire-window-operator 1.4) ─────────────────────
TEST_BACKEND = os.environ.get("FD_ENGINE_TEST_BACKEND", "sqlite").strip().lower()
#: PG 腿需要 backend adapter（wire-cell-postgres-storage）。internal-main 有
#: `customer-dataset-mcp/db_dialect.py`；开源 main 线无 → 该 checkout 才跳过。
_HAS_DIALECT = (
    pathlib.Path(__file__).resolve().parents[1] / "customer-dataset-mcp" / "db_dialect.py"
).exists()
requires_sqlite = pytest.mark.skipif(
    TEST_BACKEND == "postgres" and not _HAS_DIALECT,
    reason=(
        "window execution suite needs the sqlite write path; the PG leg "
        "requires customer-dataset-mcp/db_dialect.py "
        "(wire-cell-postgres-storage), absent on this checkout"
    ),
)

WINDOW_COLS = [
    {"name": "id", "type": "INTEGER"},
    {"name": "region", "type": "TEXT"},
    {"name": "amount", "type": "REAL"},
    {"name": "score", "type": "REAL"},
]

# No ties on the order key (amount): ranking hand-computation is unique.
ROWS_UNIQUE = [
    {"id": 1, "region": "华东", "amount": 10.0, "score": 1.0},
    {"id": 2, "region": "华东", "amount": 30.0, "score": None},
    {"id": 3, "region": "华东", "amount": 20.0, "score": 2.0},
    {"id": 4, "region": "华北", "amount": 5.0, "score": None},
    {"id": 5, "region": "华北", "amount": 15.0, "score": 3.0},
]

# Ties on the order key (amount) — exercises rank/dense_rank semantics and
# the __row_hash tie-break for order-sensitive functions.
TIES_COLS = [
    {"name": "id", "type": "INTEGER"},
    {"name": "region", "type": "TEXT"},
    {"name": "amount", "type": "REAL"},
]
TIES_ROWS = [
    {"id": 1, "region": "A", "amount": 10.0},
    {"id": 2, "region": "A", "amount": 20.0},
    {"id": 3, "region": "A", "amount": 20.0},
    {"id": 4, "region": "A", "amount": 10.0},
    {"id": 5, "region": "B", "amount": 5.0},
]

ORDER_AMOUNT_DESC = [{"column": "amount", "dir": "desc"}]


@pytest.fixture(autouse=True)
def _fresh_engine_db(tmp_path, monkeypatch):
    """Per-test engine database under tmp_path (see module docstring)."""
    monkeypatch.setenv("DAAS_DATABASE_URL", f"sqlite:///{tmp_path}/window_engine.db")
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


def _seed(rows: list | None = None, columns: list | None = None) -> str:
    src = _key("wsrc")
    _ingest(src, columns or WINDOW_COLS, rows or ROWS_UNIQUE, pk=["id"])
    return src


def _window(child: str, parent: str, **kwargs) -> dict:
    t = _t()
    args = {
        "child_key": child,
        "parent_key": parent,
        "partition_by": ["region"],
        "order_by": ORDER_AMOUNT_DESC,
        "window": [],
    }
    args.update(kwargs)
    return t["customer_dataset_window"](**args)


def _rows(child: str, sort: list | None = None) -> list[dict]:
    return _t()["customer_dataset_query"](
        dataset_key=child,
        sort=sort or [{"column": "id", "dir": "asc"}],
    )["rows"]


def _overview(child: str) -> dict:
    return _t()["customer_dataset_overview"](dataset_key=child)


def _exists(key: str) -> bool:
    return "error" not in _overview(key)


# ── ranking functions ────────────────────────────────────────────────


@requires_sqlite
def test_row_number_partitioned_by_region():
    """Unique order keys: 华东 30→1, 20→2, 10→3; 华北 15→1, 5→2."""
    src = _seed()
    child = _key("wout")
    r = _window(child, src, window=[{"func": "row_number", "as": "rn"}])
    assert "error" not in r, r
    assert r["kind"] == "window"
    by_id = {x["id"]: x["rn"] for x in _rows(child)}
    assert by_id == {1: 3, 2: 1, 3: 2, 4: 2, 5: 1}


@requires_sqlite
def test_rank_and_dense_rank_keep_ties():
    """Tied amounts (20,20,10,10) → rank 1,1,3,3; dense_rank 1,1,2,2.

    The declared order keys define the ties; the __row_hash tie-break must not
    erase them (otherwise rank would degenerate into row_number).
    """
    src = _seed(TIES_ROWS, TIES_COLS)
    child = _key("wout")
    r = _window(
        child,
        src,
        window=[
            {"func": "row_number", "as": "rn"},
            {"func": "rank", "as": "rk"},
            {"func": "dense_rank", "as": "drk"},
        ],
    )
    assert "error" not in r, r
    rows = {x["id"]: x for x in _rows(child)}
    assert {rows[i]["rk"] for i in (2, 3)} == {1}
    assert {rows[i]["rk"] for i in (1, 4)} == {3}
    assert {rows[i]["drk"] for i in (2, 3)} == {1}
    assert {rows[i]["drk"] for i in (1, 4)} == {2}
    # row_number is a total order: 1..4 over the four A rows.
    assert {rows[i]["rn"] for i in (1, 2, 3, 4)} == {1, 2, 3, 4}
    # The single B row is rank/dense_rank/row_number 1.
    assert (rows[5]["rk"], rows[5]["drk"], rows[5]["rn"]) == (1, 1, 1)


# ── lag / lead ───────────────────────────────────────────────────────


@requires_sqlite
def test_lag_lead_offset_and_boundary():
    """Ordered A amounts are [20,20,10,10]; offset defaults to 1.

    lag1  = [None,20,20,10], lead1 = [20,10,10,None]
    lag2  = [None,None,20,20], lead2 = [10,10,None,None]
    The first row of each partition has no predecessor → NULL (never a
    fabricated neighbor value).
    """
    src = _seed(TIES_ROWS, TIES_COLS)
    child = _key("wout")
    r = _window(
        child,
        src,
        window=[
            {"func": "row_number", "as": "rn"},
            {"func": "lag", "column": "amount", "as": "lag1"},
            {"func": "lead", "column": "amount", "as": "lead1"},
            {"func": "lag", "column": "amount", "offset": 2, "as": "lag2"},
            {"func": "lead", "column": "amount", "offset": 2, "as": "lead2"},
        ],
    )
    assert "error" not in r, r
    a_rows = [x for x in _rows(child, [{"column": "rn", "dir": "asc"}]) if x["region"] == "A"]
    # rn 1..4 inside the partition; order by rn = the window row order.
    a_rows = sorted(a_rows, key=lambda x: x["rn"])
    assert [x["lag1"] for x in a_rows] == [None, 20.0, 20.0, 10.0]
    assert [x["lead1"] for x in a_rows] == [20.0, 10.0, 10.0, None]
    assert [x["lag2"] for x in a_rows] == [None, None, 20.0, 20.0]
    assert [x["lead2"] for x in a_rows] == [10.0, 10.0, None, None]
    b_row = [x for x in _rows(child) if x["region"] == "B"][0]
    assert b_row["lag1"] is None and b_row["lead1"] is None


# ── aggregates: whole_partition vs running ───────────────────────────


@requires_sqlite
def test_aggregates_whole_partition_and_running():
    """A partition = [20,20,10,10] (ordered by amount desc).

    whole:  sum=60, avg=15, min=10, max=20, count=4
    running: sum [20,40,50,60], avg [20,20,50/3,15], min [20,20,10,10],
             max [20,20,20,20], count [1,2,3,4]
    """
    src = _seed(TIES_ROWS, TIES_COLS)
    child = _key("wout")
    r = _window(
        child,
        src,
        window=[
            {"func": "row_number", "as": "rn"},
            {"func": "sum", "column": "amount", "as": "sum_all"},
            {"func": "avg", "column": "amount", "as": "avg_all"},
            {"func": "min", "column": "amount", "as": "min_all"},
            {"func": "max", "column": "amount", "as": "max_all"},
            {"func": "count", "as": "count_all"},
            {"func": "sum", "column": "amount", "frame": "running", "as": "sum_run"},
            {"func": "avg", "column": "amount", "frame": "running", "as": "avg_run"},
            {"func": "min", "column": "amount", "frame": "running", "as": "min_run"},
            {"func": "max", "column": "amount", "frame": "running", "as": "max_run"},
            {"func": "count", "frame": "running", "as": "count_run"},
        ],
    )
    assert "error" not in r, r
    a_rows = sorted(
        [x for x in _rows(child) if x["region"] == "A"], key=lambda x: x["rn"]
    )
    assert [x["sum_all"] for x in a_rows] == [60.0, 60.0, 60.0, 60.0]
    assert [x["avg_all"] for x in a_rows] == [15.0, 15.0, 15.0, 15.0]
    assert [x["min_all"] for x in a_rows] == [10.0, 10.0, 10.0, 10.0]
    assert [x["max_all"] for x in a_rows] == [20.0, 20.0, 20.0, 20.0]
    assert [x["count_all"] for x in a_rows] == [4, 4, 4, 4]
    assert [x["sum_run"] for x in a_rows] == [20.0, 40.0, 50.0, 60.0]
    assert [x["avg_run"] for x in a_rows] == pytest.approx(
        [20.0, 20.0, 50.0 / 3, 15.0]
    )
    assert [x["min_run"] for x in a_rows] == [20.0, 20.0, 10.0, 10.0]
    assert [x["max_run"] for x in a_rows] == [20.0, 20.0, 20.0, 20.0]
    assert [x["count_run"] for x in a_rows] == [1, 2, 3, 4]


@requires_sqlite
def test_count_star_vs_count_column_with_nulls():
    """count without column counts every row; count(column) skips NULLs.

    华东 3 rows / score present 2 (id2 NULL); 华北 2 rows / score present 1.
    """
    src = _seed()
    child = _key("wout")
    r = _window(
        child,
        src,
        window=[
            {"func": "count", "as": "n_rows"},
            {"func": "count", "column": "score", "as": "n_score"},
        ],
    )
    assert "error" not in r, r
    by_region = {}
    for x in _rows(child):
        by_region.setdefault(x["region"], set()).add((x["n_rows"], x["n_score"]))
    assert by_region == {"华东": {(3, 2)}, "华北": {(2, 1)}}


# ── shape / lineage / determinism ────────────────────────────────────


@requires_sqlite
def test_output_shape_row_count_pk_and_no_reserved_columns():
    src = _seed()
    before = _overview(src)["rows"]
    child = _key("wout")
    r = _window(
        child,
        src,
        window=[
            {"func": "row_number", "as": "rn"},
            {"func": "sum", "column": "amount", "frame": "running", "as": "cum"},
        ],
    )
    assert "error" not in r, r
    assert r["rows"] == before  # windows never change the row set
    cols = _overview(child)
    names = [c["name"] for c in cols["columns"]]
    assert names == ["id", "region", "amount", "score", "rn", "cum"]
    # reserved-prefix columns (incl. __row_hash) never reach the output
    assert all(not n.startswith("__") for n in names)
    assert cols["pk_columns"] == ["id"]  # source pk passes through
    # hand-check one row: 华东 ordered 30,20,10 → id2 cum 30, id3 cum 50
    by_id = {x["id"]: x for x in _rows(child)}
    assert by_id[2]["rn"] == 1 and by_id[2]["cum"] == 30.0
    assert by_id[3]["rn"] == 2 and by_id[3]["cum"] == 50.0
    assert by_id[1]["rn"] == 3 and by_id[1]["cum"] == 60.0


@requires_sqlite
def test_lineage_kind_and_full_spec():
    src = _seed()
    child = _key("wout")
    order = [{"column": "amount", "dir": "desc"}, {"column": "id", "dir": "asc"}]
    spec_window = [
        {"func": "row_number", "as": "rn"},
        {"func": "sum", "column": "amount", "frame": "running", "as": "cum"},
    ]
    r = _window(child, src, partition_by=["region"], order_by=order, window=spec_window)
    assert "error" not in r, r
    der = _t()["customer_dataset_lineage"](dataset_key=child)["derivation"]
    assert der and der[0]["kind"] == "window"
    assert der[0]["parent_key"] == src
    spec = der[0]["spec"]
    assert spec["partition_by"] == ["region"]
    assert spec["order_by"] == order
    assert spec["tiebreak"] == "__row_hash"
    # normalized window rows carry the resolved frame and lag/lead offset
    assert [w["func"] for w in spec["window"]] == ["row_number", "sum"]
    assert [w["as"] for w in spec["window"]] == ["rn", "cum"]
    assert spec["window"][1]["frame"] == "running"


@requires_sqlite
def test_ties_two_runs_and_refresh_are_row_identical():
    """Tied order keys → two independent materializations and a re-derivation
    all produce the same rows (__row_hash tie-break in effect)."""
    src = _seed(TIES_ROWS, TIES_COLS)
    win = [
        {"func": "row_number", "as": "rn"},
        {"func": "lag", "column": "amount", "as": "prev"},
        {"func": "sum", "column": "amount", "frame": "running", "as": "cum"},
    ]
    first, second = _key("wout"), _key("wout")
    r1 = _window(first, src, window=win)
    r2 = _window(second, src, window=win)
    assert "error" not in r1 and "error" not in r2
    rows_first = _rows(first)
    rows_second = _rows(second)
    assert rows_first == rows_second
    assert all("__row_hash" not in x for x in rows_first)
    # re-derivation through the existing refresh path reproduces identical rows
    refreshed = _t()["customer_dataset_derive_refresh"](child_key=first)
    assert "error" not in refreshed, refreshed
    assert refreshed["refreshed"] is True
    assert _rows(first) == rows_first


@requires_sqlite
def test_refresh_replays_after_parent_change():
    """Re-derivation reads the parent's CURRENT rows and recomputes windows."""
    src = _seed()
    child = _key("wout")
    _window(child, src, window=[{"func": "row_number", "as": "rn"}])
    assert _overview(child)["rows"] == 5
    # 华东 gains a new largest amount → it becomes row_number 1
    _ingest(
        src,
        WINDOW_COLS,
        ROWS_UNIQUE + [{"id": 6, "region": "华东", "amount": 40.0, "score": 9.0}],
        pk=["id"],
    )
    r = _t()["customer_dataset_derive_refresh"](child_key=child)
    assert "error" not in r, r
    assert r["rows"] == 6
    by_id = {x["id"]: x["rn"] for x in _rows(child)}
    assert by_id[6] == 1 and by_id[2] == 2 and by_id[3] == 3 and by_id[1] == 4


@requires_sqlite
def test_capabilities_advertise_window_and_tool_registered():
    assert "customer_dataset_window" in _t()
    src = _seed()
    assert _overview(src)["capabilities"].get("window") is True


@requires_sqlite
def test_window_step_in_chain_after_filter():
    """filter → window chain: row count follows the filtered parent."""
    src = _seed()
    filtered = _key("fout")
    r = _t()["customer_dataset_filter"](
        child_key=filtered,
        parent_key=src,
        filters=[{"column": "region", "op": "eq", "value": "华东"}],
    )
    assert "error" not in r, r
    child = _key("wout")
    r = _window(child, filtered, window=[{"func": "row_number", "as": "rn"}])
    assert "error" not in r, r
    assert r["rows"] == 3
    by_id = {x["id"]: x["rn"] for x in _rows(child)}
    assert by_id == {1: 3, 2: 1, 3: 2}


@requires_sqlite
def test_output_column_types_follow_design_d4():
    """count/row_number/rank → INTEGER; sum/min/max follow the source column
    type; avg → REAL; lag/lead follow the source column type."""
    src = _seed()
    child = _key("wout")
    r = _window(
        child,
        src,
        window=[
            {"func": "row_number", "as": "rn"},
            {"func": "rank", "as": "rk"},
            {"func": "count", "as": "n"},
            {"func": "sum", "column": "id", "as": "sum_id"},
            {"func": "avg", "column": "id", "as": "avg_id"},
            {"func": "min", "column": "region", "as": "min_region"},
            {"func": "max", "column": "amount", "as": "max_amount"},
            {"func": "lag", "column": "amount", "as": "prev_amount"},
        ],
    )
    assert "error" not in r, r
    types = {c["name"]: c["type"] for c in _overview(child)["columns"]}
    assert types["rn"] == "INTEGER"
    assert types["rk"] == "INTEGER"
    assert types["n"] == "INTEGER"
    assert types["sum_id"] == "INTEGER"  # sum follows the INTEGER source column
    assert types["avg_id"] == "REAL"
    assert types["min_region"] == "TEXT"
    assert types["max_amount"] == "REAL"
    assert types["prev_amount"] == "REAL"


@requires_sqlite
def test_global_partition_without_partition_by():
    """partition_by empty = one global partition; order_by asc row_number."""
    src = _seed()
    child = _key("wout")
    r = _window(
        child,
        src,
        partition_by=[],
        order_by=[{"column": "id", "dir": "asc"}],
        window=[
            {"func": "row_number", "as": "rn"},
            {"func": "count", "as": "n"},
        ],
    )
    assert "error" not in r, r
    by_id = {x["id"]: x for x in _rows(child)}
    assert by_id[1]["rn"] == 1 and by_id[5]["rn"] == 5
    assert all(x["n"] == 5 for x in by_id.values())


# ── row cap ──────────────────────────────────────────────────────────


@requires_sqlite
def test_row_cap_rejects_whole_batch_without_creating_child(monkeypatch):
    src = _seed()  # seed under the default cap (ingest is capped too)
    monkeypatch.setenv("CUSTOMER_DATASET_MAX_ROWS", "3")
    child = _key("wout")
    r = _window(child, src, window=[{"func": "row_number", "as": "rn"}])
    assert "error" in r and "超过上限" in r["error"], r
    assert not _exists(child), "整批拒绝时不得产生任何数据集"


@requires_sqlite
def test_row_cap_on_refresh_keeps_previous_child_state(monkeypatch):
    src = _seed()
    child = _key("wout")
    r = _window(child, src, window=[{"func": "row_number", "as": "rn"}])
    assert "error" not in r, r
    before = _rows(child)
    monkeypatch.setenv("CUSTOMER_DATASET_MAX_ROWS", "3")
    refreshed = _t()["customer_dataset_derive_refresh"](child_key=child)
    assert "error" in refreshed and "超过上限" in refreshed["error"], refreshed
    assert _rows(child) == before, "超限整批拒绝，子集保持原状"


# ── validation matrix ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "kwargs,needle",
    [
        # window rows
        ({"window": []}, "非空数组"),
        ({"window": [{"func": "row_number"}]}, "as"),
        ({"window": [{"func": "median", "as": "x"}]}, "非法窗口函数"),
        (
            {"window": [{"func": "count", "as": f"c{i}"} for i in range(21)]},
            "上限",
        ),
        # order_by required by ranking funcs and lag/lead
        ({"order_by": [], "window": [{"func": "row_number", "as": "rn"}]}, "需要 order_by"),
        ({"order_by": [], "window": [{"func": "rank", "as": "rk"}]}, "需要 order_by"),
        ({"order_by": [], "window": [{"func": "dense_rank", "as": "drk"}]}, "需要 order_by"),
        (
            {"order_by": [], "window": [{"func": "lag", "column": "amount", "as": "x"}]},
            "需要 order_by",
        ),
        (
            {"order_by": [], "window": [{"func": "lead", "column": "amount", "as": "x"}]},
            "需要 order_by",
        ),
        # lag/lead offset bounds
        (
            {"window": [{"func": "lag", "column": "amount", "offset": 0, "as": "x"}]},
            "1..10000",
        ),
        (
            {"window": [{"func": "lead", "column": "amount", "offset": 10001, "as": "x"}]},
            "1..10000",
        ),
        (
            {"window": [{"func": "lag", "column": "amount", "offset": "2", "as": "x"}]},
            "必须为整数",
        ),
        # aggregate column rules
        ({"window": [{"func": "sum", "as": "x"}]}, "column 不存在"),
        ({"window": [{"func": "sum", "column": "nope", "as": "x"}]}, "column 不存在"),
        ({"window": [{"func": "avg", "column": "region", "as": "x"}]}, "TEXT"),
        ({"window": [{"func": "sum", "column": "region", "as": "x"}]}, "TEXT"),
        # `as` discipline
        (
            {"window": [{"func": "count", "as": "x"}, {"func": "count", "as": "x"}]},
            "重复",
        ),
        ({"window": [{"func": "count", "as": "amount"}]}, "冲突"),
        ({"window": [{"func": "count", "as": "__row_hash"}]}, "保留前缀"),
        # partition_by
        ({"partition_by": ["nope"], "window": [{"func": "count", "as": "x"}]}, "分区列不存在"),
        ({"partition_by": ["region"] * 11, "window": [{"func": "count", "as": "x"}]}, "上限"),
        ({"partition_by": ["region", "region"], "window": [{"func": "count", "as": "x"}]}, "分区列重复"),
        # order_by
        ({"order_by": [{"column": "nope"}], "window": [{"func": "count", "as": "x"}]}, "排序列不存在"),
        ({"order_by": [{"column": "amount", "dir": "up"}], "window": [{"func": "count", "as": "x"}]}, "非法排序方向"),
        ({"order_by": [{"column": "amount"}] * 11, "window": [{"func": "count", "as": "x"}]}, "上限"),
        # frame closed set
        (
            {"window": [{"func": "sum", "column": "amount", "frame": "custom", "as": "x"}]},
            "非法 frame",
        ),
        (
            {
                "order_by": [],
                "window": [{"func": "sum", "column": "amount", "frame": "running", "as": "x"}],
            },
            "running 需要 order_by",
        ),
        # func-specific parameter rejection (no silent drops)
        ({"window": [{"func": "row_number", "column": "amount", "as": "x"}]}, "不接受 column"),
        (
            {"window": [{"func": "sum", "column": "amount", "offset": 1, "as": "x"}]},
            "不接受 offset",
        ),
    ],
)
@requires_sqlite
def test_validation_errors_reject_without_dataset(kwargs, needle):
    src = _seed()
    child = _key("wout")
    r = _window(child, src, **kwargs)
    assert "error" in r, r
    assert needle in r["error"], r["error"]
    assert not _exists(child), "校验拒绝时不得产生任何数据集"


@requires_sqlite
def test_child_key_cannot_equal_parent():
    src = _seed()
    r = _window(src, src, window=[{"func": "count", "as": "x"}])
    assert "error" in r and "不能与源数据集相同" in r["error"]


@requires_sqlite
def test_existing_child_rejects():
    src = _seed()
    child = _key("wout")
    first = _window(child, src, window=[{"func": "count", "as": "x"}])
    assert "error" not in first, first
    again = _window(child, src, window=[{"func": "count", "as": "x"}])
    assert "error" in again and "已存在" in again["error"]


# ── PG leg (task 1.4) ────────────────────────────────────────────────


@requires_sqlite
def test_window_sql_is_standard_dialect_neutral():
    """The engine builds the window SQL itself from validated identifiers —
    standard ``OVER (PARTITION BY … ORDER BY …)`` syntax, no dialect
    functions, no bound params, and no sql-step fail-closed guard (design
    D4: window computation is a pure read on the PG data root)."""
    import inspect

    fn = _t()["customer_dataset_window"]
    # the registry evicts group modules after loading, so reach the service
    # class through the tool function's own globals
    svc = fn.__globals__["CustomerDatasetService"]
    window_src = inspect.getsource(svc.window_rows)
    plan_src = inspect.getsource(svc._window_plan)
    for token in ("PARTITION BY", "ORDER BY", "ROWS BETWEEN UNBOUNDED PRECEDING"):
        assert token in plan_src
    # window is not routed through the sql-step guard (that guard belongs to
    # customer_dataset_sql only)
    assert "_validate_sql_text" not in window_src
    assert "_run_sql_readonly" not in window_src
    # no sqlite-only SQL fragments in the constructed expressions
    for banned in ("GROUP_CONCAT", "PRAGMA", "rowid"):
        assert banned not in plan_src
