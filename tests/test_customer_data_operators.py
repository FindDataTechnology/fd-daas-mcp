"""确定性算子 + SQL 步骤（wire-transform-operators 1.1–1.5）。

四个算子（filter / project / aggregate_derive / union）与 SQL 步骤
（sql / sql_preview）在引擎侧的契约，以及它们与既有派生的**同源纪律**：

- 输出一律为派生数据集，血缘 kind = 算子名、spec 存参数、``derive_refresh``
  可重放（重放结果与首次一致）；
- 行数超上限整批拒绝且子集保持原状（不静默截断）；
- union 同名列类型不兼容如实 422（不静默强转），INTEGER+REAL 取宽；
- SQL 三层护栏：文本层（单语句/只读起始/关键字与函数黑名单）、授权回调
  （非声明表的 READ 与一切写动作 DENY）、只读连接（query_only）。护栏全拒
  时**不产生任何数据集**；试运行绝不创建/刷新数据集。
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

_DAAS_MCP = Path(__file__).resolve().parents[1] / "daas-mcp"
sys.path.insert(0, str(_DAAS_MCP))
from daas_database import get_database  # noqa: E402

sys.path.remove(str(_DAAS_MCP))

from daas.fd_daas_mcp import registry  # noqa: E402

_COLS = [
    {"name": "id", "type": "INTEGER"},
    {"name": "region", "type": "TEXT"},
    {"name": "amount", "type": "REAL"},
    {"name": "note", "type": "TEXT"},
]

_ROWS = [
    {"id": 1, "region": "华东", "amount": 10.5, "note": "a1"},
    {"id": 2, "region": "华东", "amount": 20.0, "note": "a2"},
    {"id": 3, "region": "华北", "amount": 30.25, "note": "b1"},
    {"id": 4, "region": "华北", "amount": None, "note": None},
    {"id": 5, "region": "华南", "amount": 5.0, "note": "c9"},
]


def _t() -> dict:
    return {
        registry.namespaced(g, n): fn
        for g, n, fn in registry.build(profile="cell")
    }


def _key(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _seed(key: str, rows: list | None = None, columns: list | None = None, pk=None) -> None:
    t = _t()
    begun = t["customer_dataset_ingest_begin"](
        dataset_key=key, columns=columns or _COLS, pk_columns=pk if pk is not None else ["id"]
    )
    assert "error" not in begun, begun
    chunked = t["customer_dataset_ingest_chunk"](
        ingest_id=begun["ingest_id"],
        rows=rows if rows is not None else _ROWS,
        start_index=0,
    )
    assert "error" not in chunked, chunked
    committed = t["customer_dataset_ingest_commit"](ingest_id=begun["ingest_id"])
    assert "error" not in committed, committed


def _query(key: str, **kw) -> dict:
    return _t()["customer_dataset_query"](dataset_key=key, **kw)


def _exists(key: str) -> bool:
    r = _t()["customer_dataset_overview"](dataset_key=key)
    return "error" not in r


def _lineage(key: str) -> dict:
    return _t()["customer_dataset_lineage"](dataset_key=key)


# ── 1.1 filter / project ─────────────────────────────────────────────


class TestFilter:
    def test_filter_materializes_and_lineage_kind(self):
        src = _key("fsrc")
        _seed(src)
        child = _key("fout")
        r = _t()["customer_dataset_filter"](
            child_key=child,
            parent_key=src,
            filters=[{"column": "region", "op": "eq", "value": "华东"}],
        )
        assert "error" not in r, r
        assert r["rows"] == 2 and r["kind"] == "filter"
        rows = _query(child)["rows"]
        assert [x["id"] for x in rows] == [1, 2]
        # 列继承 + 主键继承
        assert [c["name"] for c in _t()["customer_dataset_overview"](dataset_key=child)["columns"]] == [
            c["name"] for c in _COLS
        ]
        der = _lineage(child)["derivation"]
        assert der and der[0]["kind"] == "filter"
        assert der[0]["spec"]["filters"] == [
            {"column": "region", "op": "eq", "value": "华东"}
        ]

    def test_contains_and_startswith_operators(self):
        src = _key("fsrc")
        _seed(src)
        child = _key("fout")
        r = _t()["customer_dataset_filter"](
            child_key=child,
            parent_key=src,
            filters=[{"column": "note", "op": "startswith", "value": "a"}],
        )
        assert "error" not in r, r
        assert r["rows"] == 2

        child2 = _key("fout")
        r2 = _t()["customer_dataset_filter"](
            child_key=child2,
            parent_key=src,
            filters=[{"column": "note", "op": "contains", "value": "9"}],
        )
        assert "error" not in r2, r2
        assert r2["rows"] == 1

    def test_contains_treats_wildcards_literally(self):
        """contains 的 % 是字面量（否则会退化成任意匹配——脏查询）。"""
        src = _key("fsrc")
        _seed(
            src,
            rows=[
                {"id": 1, "region": "华东", "amount": 1.0, "note": "50% off"},
                {"id": 2, "region": "华东", "amount": 2.0, "note": "50 off"},
            ],
        )
        child = _key("fout")
        r = _t()["customer_dataset_filter"](
            child_key=child,
            parent_key=src,
            filters=[{"column": "note", "op": "contains", "value": "50%"}],
        )
        assert "error" not in r, r
        assert r["rows"] == 1, _query(child)["rows"]

    def test_sort_and_limit(self):
        src = _key("fsrc")
        _seed(src)
        child = _key("fout")
        r = _t()["customer_dataset_filter"](
            child_key=child,
            parent_key=src,
            sort=[{"column": "id", "dir": "desc"}],
            limit=2,
        )
        assert "error" not in r, r
        assert [x["id"] for x in _query(child)["rows"]] == [5, 4]

    def test_refresh_replays_and_updates_rows(self):
        src = _key("fsrc")
        _seed(src)
        child = _key("fout")
        _t()["customer_dataset_filter"](
            child_key=child,
            parent_key=src,
            filters=[{"column": "region", "op": "eq", "value": "华东"}],
        )
        assert _t()["customer_dataset_overview"](dataset_key=child)["rows"] == 2
        # 父集新增一行华东 → 重放后子集更新
        _seed(
            src,
            rows=_ROWS
            + [{"id": 6, "region": "华东", "amount": 1.0, "note": "z"}],
        )
        r = _t()["customer_dataset_derive_refresh"](child_key=child)
        assert "error" not in r, r
        assert r["refreshed"] and r["rows"] == 3

    def test_existing_child_rejects(self):
        src = _key("fsrc")
        _seed(src)
        child = _key("fout")
        first = _t()["customer_dataset_filter"](child_key=child, parent_key=src)
        assert "error" not in first, first
        again = _t()["customer_dataset_filter"](child_key=child, parent_key=src)
        assert "error" in again and "已存在" in again["error"]

    def test_bad_operator_rejected(self):
        src = _key("fsrc")
        _seed(src)
        r = _t()["customer_dataset_filter"](
            child_key=_key("fout"),
            parent_key=src,
            filters=[{"column": "region", "op": "regex", "value": ".*"}],
        )
        assert "error" in r and "操作符" in r["error"]


class TestProject:
    def test_project_renames_and_reorders(self):
        src = _key("psrc")
        _seed(src)
        child = _key("pout")
        r = _t()["customer_dataset_project"](
            child_key=child,
            parent_key=src,
            columns=[
                {"source": "region", "as": "区域"},
                {"source": "amount"},
            ],
        )
        assert "error" not in r, r
        assert r["rows"] == 5
        cols = [c["name"] for c in _t()["customer_dataset_overview"](dataset_key=child)["columns"]]
        assert cols == ["区域", "amount"]  # 列序=声明序
        rows = _query(child)["rows"]
        assert rows[0]["区域"] == "华东" and rows[0]["amount"] == 10.5

    def test_project_drops_pk_when_key_not_projected(self):
        src = _key("psrc")
        _seed(src)
        child = _key("pout")
        r = _t()["customer_dataset_project"](
            child_key=child, parent_key=src, columns=[{"source": "region"}]
        )
        assert "error" not in r, r
        assert _t()["customer_dataset_overview"](dataset_key=child)["pk_columns"] == []

    def test_project_follows_renamed_pk(self):
        src = _key("psrc")
        _seed(src)
        child = _key("pout")
        r = _t()["customer_dataset_project"](
            child_key=child,
            parent_key=src,
            columns=[{"source": "id", "as": "编号"}, {"source": "region"}],
        )
        assert "error" not in r, r
        assert _t()["customer_dataset_overview"](dataset_key=child)["pk_columns"] == ["编号"]

    def test_duplicate_target_rejected(self):
        src = _key("psrc")
        _seed(src)
        r = _t()["customer_dataset_project"](
            child_key=_key("pout"),
            parent_key=src,
            columns=[
                {"source": "region", "as": "x"},
                {"source": "note", "as": "x"},
            ],
        )
        assert "error" in r and "重复" in r["error"]

    def test_unknown_source_rejected(self):
        src = _key("psrc")
        _seed(src)
        r = _t()["customer_dataset_project"](
            child_key=_key("pout"), parent_key=src, columns=[{"source": "nope"}]
        )
        assert "error" in r and "不存在" in r["error"]

    def test_refresh_replays(self):
        src = _key("psrc")
        _seed(src)
        child = _key("pout")
        _t()["customer_dataset_project"](
            child_key=child, parent_key=src, columns=[{"source": "region", "as": "r"}]
        )
        r = _t()["customer_dataset_derive_refresh"](child_key=child)
        assert "error" not in r, r
        assert r["refreshed"] and r["kind"] == "project"


# ── 1.2 aggregate_derive ─────────────────────────────────────────────


class TestAggregateDerive:
    def test_group_counts_are_materialized(self):
        src = _key("asrc")
        _seed(src)
        child = _key("aout")
        r = _t()["customer_dataset_aggregate_derive"](
            child_key=child,
            parent_key=src,
            group_by=["region"],
            measures=[{"func": "count"}, {"func": "sum", "column": "amount"}],
        )
        assert "error" not in r, r
        assert r["kind"] == "aggregate"
        rows = {x["region"]: x for x in _query(child)["rows"]}
        assert set(rows) == {"华东", "华北", "华南"}
        assert rows["华东"]["count"] == 2
        assert rows["华东"]["sum__amount"] == pytest.approx(30.5)
        # 聚合输出无主键
        assert _t()["customer_dataset_overview"](dataset_key=child)["pk_columns"] == []

    def test_avg_on_text_column_rejected(self):
        src = _key("asrc")
        _seed(src)
        r = _t()["customer_dataset_aggregate_derive"](
            child_key=_key("aout"),
            parent_key=src,
            group_by=["region"],
            measures=[{"func": "avg", "column": "note"}],
        )
        assert "error" in r and "TEXT" in r["error"]

    def test_unknown_group_column_rejected(self):
        src = _key("asrc")
        _seed(src)
        r = _t()["customer_dataset_aggregate_derive"](
            child_key=_key("aout"),
            parent_key=src,
            group_by=["nope"],
            measures=[{"func": "count"}],
        )
        assert "error" in r and "分组列不存在" in r["error"]

    def test_group_by_and_measure_caps(self):
        src = _key("asrc")
        _seed(src)
        too_many = [{"func": "count"} for _ in range(21)]
        r = _t()["customer_dataset_aggregate_derive"](
            child_key=_key("aout"), parent_key=src, group_by=["region"], measures=too_many
        )
        assert "error" in r and "上限" in r["error"]

    def test_refresh_replays(self):
        src = _key("asrc")
        _seed(src)
        child = _key("aout")
        _t()["customer_dataset_aggregate_derive"](
            child_key=child,
            parent_key=src,
            group_by=["region"],
            measures=[{"func": "count"}],
        )
        _seed(src, rows=_ROWS + [{"id": 9, "region": "华东", "amount": 1.0, "note": "n"}])
        r = _t()["customer_dataset_derive_refresh"](child_key=child)
        assert "error" not in r, r
        rows = {x["region"]: x["count"] for x in _query(child)["rows"]}
        assert rows["华东"] == 3


# ── 1.3 union ────────────────────────────────────────────────────────


class TestUnion:
    def test_union_appends_and_fills_missing_with_null(self):
        left = _key("uleft")
        right = _key("uright")
        _seed(left, columns=_COLS, rows=[_ROWS[0], _ROWS[1]])
        _seed(
            right,
            columns=[{"name": "id", "type": "INTEGER"}, {"name": "note", "type": "TEXT"}],
            rows=[{"id": 10, "note": "from-right"}],
        )
        child = _key("uout")
        r = _t()["customer_dataset_union"](
            child_key=child, left_key=left, right_key=right
        )
        assert "error" not in r, r
        assert r["rows"] == 3
        cols = [c["name"] for c in _t()["customer_dataset_overview"](dataset_key=child)["columns"]]
        assert cols == ["id", "region", "amount", "note"]  # 并集、首现顺序
        rows = _query(child)["rows"]
        appended = [x for x in rows if x["id"] == 10][0]
        assert appended["region"] is None and appended["amount"] is None
        assert appended["note"] == "from-right"

    def test_union_type_conflict_rejects_without_dataset(self):
        left = _key("uleft")
        right = _key("uright")
        _seed(left, columns=[{"name": "id", "type": "INTEGER"}], rows=[{"id": 1}])
        _seed(
            right,
            columns=[{"name": "id", "type": "TEXT"}],
            rows=[{"id": "1"}],
        )
        child = _key("uout")
        r = _t()["customer_dataset_union"](
            child_key=child, left_key=left, right_key=right
        )
        assert "error" in r and "不兼容" in r["error"]
        assert not _exists(child), "类型冲突时不得产生任何数据集"

    def test_integer_and_real_widen_to_real(self):
        left = _key("uleft")
        right = _key("uright")
        _seed(left, columns=[{"name": "id", "type": "INTEGER"}], rows=[{"id": 1}])
        _seed(
            right,
            columns=[{"name": "id", "type": "REAL"}],
            rows=[{"id": 2.5}],
        )
        child = _key("uout")
        r = _t()["customer_dataset_union"](
            child_key=child, left_key=left, right_key=right
        )
        assert "error" not in r, r
        cols = _t()["customer_dataset_overview"](dataset_key=child)["columns"]
        assert cols == [{"name": "id", "type": "REAL"}]

    def test_union_output_has_no_pk(self):
        left = _key("uleft")
        right = _key("uright")
        _seed(left)
        _seed(right)
        child = _key("uout")
        r = _t()["customer_dataset_union"](
            child_key=child, left_key=left, right_key=right
        )
        assert "error" not in r, r
        assert _t()["customer_dataset_overview"](dataset_key=child)["pk_columns"] == []

    def test_unsupported_on_rejected(self):
        left = _key("uleft")
        right = _key("uright")
        _seed(left)
        _seed(right)
        r = _t()["customer_dataset_union"](
            child_key=_key("uout"), left_key=left, right_key=right, on="by_position"
        )
        assert "error" in r and "by_name" in r["error"]

    def test_refresh_replays(self):
        left = _key("uleft")
        right = _key("uright")
        _seed(left, rows=[_ROWS[0]])
        _seed(right, rows=[_ROWS[1]])
        child = _key("uout")
        _t()["customer_dataset_union"](child_key=child, left_key=left, right_key=right)
        _seed(right, rows=[_ROWS[1], _ROWS[2]])
        r = _t()["customer_dataset_derive_refresh"](child_key=child)
        assert "error" not in r, r
        assert r["refreshed"] and r["rows"] == 3


# ── 1.4 SQL 步骤与护栏 ────────────────────────────────────────────────


class TestSql:
    def test_legal_sql_with_placeholder_materializes(self):
        src = _key("ssrc")
        _seed(src)
        child = _key("sout")
        r = _t()["customer_dataset_sql"](
            child_key=child,
            sql="SELECT region, count(*) AS n FROM {orders} GROUP BY region ORDER BY region",
            tables=[{"name": "orders", "dataset_key": src}],
        )
        assert "error" not in r, r
        assert r["kind"] == "sql"
        cols = [c["name"] for c in _t()["customer_dataset_overview"](dataset_key=child)["columns"]]
        assert cols == ["region", "n"]
        rows = {x["region"]: x["n"] for x in _query(child)["rows"]}
        assert rows["华东"] == 2
        # 血缘留档 SQL 文本 + 占位映射
        spec = _lineage(child)["derivation"][0]["spec"]
        assert "count(*)" in spec["sql"]
        assert spec["tables"] == [{"name": "orders", "dataset_key": src}]

    def test_undeclared_table_reference_rejected(self):
        src = _key("ssrc")
        _seed(src)
        child = _key("sout")
        r = _t()["customer_dataset_sql"](
            child_key=child,
            sql="SELECT * FROM {orders} JOIN {secret} ON 1=1",
            tables=[{"name": "orders", "dataset_key": src}],
        )
        assert "error" in r and "未声明" in r["error"]
        assert not _exists(child)

    def test_declared_but_unused_placeholder_rejected(self):
        src = _key("ssrc")
        _seed(src)
        r = _t()["customer_dataset_sql"](
            child_key=_key("sout"),
            sql="SELECT * FROM {orders}",
            tables=[
                {"name": "orders", "dataset_key": src},
                {"name": "typo", "dataset_key": src},
            ],
        )
        assert "error" in r and "未使用" in r["error"]

    @pytest.mark.parametrize(
        "sql,needle",
        [
            ("DELETE FROM {orders}", "DELETE"),
            ("INSERT INTO {orders} VALUES (1)", "INSERT"),
            # ATTACH/PRAGMA 被关键字黑名单先命中（比"起始语句"更准确的口径）
            ("ATTACH DATABASE '/tmp/x.db' AS x", "ATTACH"),
            ("SELECT * FROM {orders}; DROP TABLE cust_x", "多语句"),
            ("PRAGMA table_info('x')", "PRAGMA"),
            ("SELECT load_extension('/tmp/evil')", "被禁函数"),
            ("SELECT readfile('/etc/passwd')", "被禁函数"),
        ],
    )
    def test_dangerous_sql_rejected(self, sql: str, needle: str):
        src = _key("ssrc")
        _seed(src)
        child = _key("sout")
        r = _t()["customer_dataset_sql"](
            child_key=child, sql=sql, tables=[{"name": "orders", "dataset_key": src}]
        )
        assert "error" in r, r
        assert needle in r["error"], r["error"]
        assert not _exists(child), "护栏拒收时不得产生任何数据集"

    def test_authorizer_blocks_undeclared_real_table(self):
        """绕过文本层直接引用真实表名：授权回调结构性拒绝。"""
        src = _key("ssrc")
        _seed(src)
        other = _key("sother")
        _seed(other)
        children = _key("sout")
        r = _t()["customer_dataset_sql"](
            child_key=children,
            sql=f"SELECT * FROM cust_{other}",
            tables=[{"name": "orders", "dataset_key": src}],
        )
        assert "error" in r, r
        assert not _exists(children)

    def test_literal_params_bound_not_concatenated(self):
        src = _key("ssrc")
        _seed(src)
        child = _key("sout")
        r = _t()["customer_dataset_sql"](
            child_key=child,
            sql="SELECT * FROM {orders} WHERE region = :r",
            tables=[{"name": "orders", "dataset_key": src}],
            params={"r": "华东"},
        )
        assert "error" not in r, r
        assert r["rows"] == 2

    def test_sql_step_refresh_replays(self):
        src = _key("ssrc")
        _seed(src)
        child = _key("sout")
        _t()["customer_dataset_sql"](
            child_key=child,
            sql="SELECT count(*) AS n FROM {orders}",
            tables=[{"name": "orders", "dataset_key": src}],
        )
        _seed(src, rows=_ROWS + [{"id": 7, "region": "华东", "amount": 1.0, "note": "x"}])
        r = _t()["customer_dataset_derive_refresh"](child_key=child)
        assert "error" not in r, r
        rows = _query(child)["rows"]  # query 行带引擎行锚 __row_id
        assert [x["n"] for x in rows] == [6]

    def test_preview_creates_nothing(self):
        src = _key("ssrc")
        _seed(src)
        before = _t()["customer_dataset_overview"](dataset_key=src)["rows"]
        r = _t()["customer_dataset_sql_preview"](
            sql="SELECT * FROM {orders}",
            tables=[{"name": "orders", "dataset_key": src}],
        )
        assert "error" not in r, r
        assert r["preview"] is True
        assert r["limit"] == 100
        assert r["row_count"] == before
        assert [c["name"] for c in r["columns"]] == ["id", "region", "amount", "note"]
        # 绝不落数据集：父集行数不变、且没有新数据集出现
        assert _t()["customer_dataset_overview"](dataset_key=src)["rows"] == before

    def test_preview_caps_at_100(self):
        src = _key("ssrc")
        _seed(src, rows=[{"id": i, "region": "r", "amount": 1.0, "note": "n"} for i in range(150)])
        r = _t()["customer_dataset_sql_preview"](
            sql="SELECT * FROM {orders}",
            tables=[{"name": "orders", "dataset_key": src}],
        )
        assert "error" not in r, r
        assert r["row_count"] == 100 and len(r["rows"]) == 100

    def test_preview_rejects_bad_sql(self):
        src = _key("ssrc")
        _seed(src)
        r = _t()["customer_dataset_sql_preview"](
            sql="DELETE FROM {orders}", tables=[{"name": "orders", "dataset_key": src}]
        )
        assert "error" in r and "DELETE" in r["error"]

    def test_comments_do_not_trip_blacklist(self):
        """注释里出现 DROP TABLE 不该误伤（真语句本身是只读 SELECT）。"""
        src = _key("ssrc")
        _seed(src)
        child = _key("sout")
        r = _t()["customer_dataset_sql"](
            child_key=child,
            sql="-- DROP TABLE later\nSELECT count(*) AS n FROM {orders}",
            tables=[{"name": "orders", "dataset_key": src}],
        )
        assert "error" not in r, r
        # 语句是 count(*)：1 行，值是 5
        assert r["rows"] == 1
        assert [x["n"] for x in _query(child)["rows"]] == [5]

    def test_cte_query_allowed(self):
        src = _key("ssrc")
        _seed(src)
        child = _key("sout")
        r = _t()["customer_dataset_sql"](
            child_key=child,
            sql=(
                "WITH grouped AS (SELECT region, count(*) AS n FROM {orders} GROUP BY region)"
                " SELECT * FROM grouped ORDER BY region"
            ),
            tables=[{"name": "orders", "dataset_key": src}],
        )
        assert "error" not in r, r
        assert r["rows"] == 3

    def test_multiple_placeholders(self):
        left = _key("ssrc")
        right = _key("sother")
        _seed(left, rows=[_ROWS[0], _ROWS[1]])
        _seed(right, rows=[_ROWS[2]])
        child = _key("sout")
        r = _t()["customer_dataset_sql"](
            child_key=child,
            sql=(
                "SELECT a.id AS id, b.id AS other_id FROM {orders} a, {extra} b"
            ),
            tables=[
                {"name": "orders", "dataset_key": left},
                {"name": "extra", "dataset_key": right},
            ],
        )
        assert "error" not in r, r
        assert r["rows"] == 2  # 2 × 1


# ── 1.5 能力声明 ─────────────────────────────────────────────────────


def test_capabilities_advertise_new_operators():
    caps = _t()["customer_dataset_overview"](dataset_key=_key("nope"))
    # overview 对不存在的数据集返回 error；能力声明挂在成功响应上
    assert "error" in caps
    src = _key("caps")
    _seed(src)
    caps = _t()["customer_dataset_overview"](dataset_key=src)["capabilities"]
    for key in ("filter", "project", "aggregate_derive", "union", "sql"):
        assert caps.get(key) is True, key


# ── 4.1 真库冒烟：filter→aggregate 链 + SQL 步骤 + 试运行 ─────────────


def test_smoke_filter_then_aggregate_chain() -> None:
    """真 SQLite 上跑 filter→aggregate 两步链：输出行数与手算对账。

    基础表 5 行（华东 2 / 华北 2 / 华南 1）；过滤 id>=3 后剩 3 行
    （华北 2 / 华南 1）；按 region 聚合 → 2 行（华北 2、华南 1）。
    """
    t = _t()
    src = _key("smoke")
    _seed(src)
    filtered = _key("smoke_f")
    r1 = t["customer_dataset_filter"](
        child_key=filtered,
        parent_key=src,
        filters=[{"column": "id", "op": "gte", "value": 3}],
    )
    assert "error" not in r1, r1
    assert r1["rows"] == 3

    agg = _key("smoke_a")
    r2 = t["customer_dataset_aggregate_derive"](
        child_key=agg,
        parent_key=filtered,
        group_by=["region"],
        measures=[{"func": "count"}],
    )
    assert "error" not in r2, r2
    assert r2["rows"] == 2
    rows = {x["region"]: x["count"] for x in _query(agg)["rows"]}
    assert rows == {"华北": 2, "华南": 1}


def test_smoke_sql_step_then_preview() -> None:
    """真 SQLite 上跑 SQL 步骤物化 + 试运行（预览零副作用）。"""
    t = _t()
    src = _key("smoke")
    _seed(src)
    child = _key("smoke_s")
    r = t["customer_dataset_sql"](
        child_key=child,
        sql=(
            "SELECT region, count(*) AS n FROM {orders}"
            " WHERE amount > :floor GROUP BY region ORDER BY region"
        ),
        tables=[{"name": "orders", "dataset_key": src}],
        params={"floor": 6},
    )
    assert "error" not in r, r
    rows = {x["region"]: x["n"] for x in _query(child)["rows"]}
    # amount > 6：华东 10.5+20.0=2 行、华北 30.25=1 行、华南 5.0 被排除
    assert rows == {"华东": 2, "华北": 1}

    # 试运行：同一执行器，零副作用（源集与物化集行数都不变）
    before_src = _query(src)["total"]
    before_child = _query(child)["total"]
    preview = t["customer_dataset_sql_preview"](
        sql="SELECT region, count(*) AS n FROM {orders} GROUP BY region ORDER BY region",
        tables=[{"name": "orders", "dataset_key": src}],
    )
    assert "error" not in preview, preview
    assert preview["preview"] is True
    assert preview["row_count"] == 3
    assert _query(src)["total"] == before_src
    assert _query(child)["total"] == before_child
