"""Customer data engine tools (wire-customer-data-engine, sections 1–4).

Structured query/aggregate with the shared SQL-builder guards (operator
whitelist, row cap, progress-handler timeout), capability advertisement,
channel-attributed batch writes, declared creation, add-column evolution,
derivation lineage (derive/derive_refresh/join_indicators) and the
parent-deletion guard. Tools are exercised through ``registry.build()`` so
the AST discovery + namespacing path stays under test, mirroring
``test_customer_datasets.py``.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

_DAAS_MCP = Path(__file__).resolve().parents[1] / "daas-mcp"
sys.path.insert(0, str(_DAAS_MCP))
from daas_database import get_database  # noqa: E402
sys.path.remove(str(_DAAS_MCP))

from daas.fd_daas_mcp import registry, selfcheck  # noqa: E402

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


def _seed(key: str, rows: list | None = None, pk: list | None = None, columns: list | None = None) -> None:
    t = _t()
    begun = t["customer_dataset_ingest_begin"](
        dataset_key=key, columns=columns or _COLS, pk_columns=pk or ["id"]
    )
    assert "error" not in begun, begun
    chunked = t["customer_dataset_ingest_chunk"](
        ingest_id=begun["ingest_id"], rows=rows if rows is not None else _ROWS, start_index=0
    )
    assert "error" not in chunked, chunked
    committed = t["customer_dataset_ingest_commit"](ingest_id=begun["ingest_id"])
    assert "error" not in committed, committed


# ── section 1: query / aggregate / capabilities ──────────────────────


class TestQuery:
    def test_every_filter_operator(self):
        key = _key("q")
        _seed(key)
        t = _t()
        q = lambda **kw: t["customer_dataset_query"](dataset_key=key, **kw)  # noqa: E731

        r = q(filters=[{"column": "region", "op": "eq", "value": "华东"}])
        assert [row["id"] for row in r["rows"]] == [1, 2] and r["total"] == 2

        r = q(filters=[{"column": "region", "op": "ne", "value": "华东"}])
        assert r["total"] == 3

        r = q(filters=[{"column": "amount", "op": "gt", "value": 10}])
        assert r["total"] == 3  # 10.5, 20.0, 30.25

        r = q(filters=[{"column": "amount", "op": "gte", "value": 10.5}])
        assert r["total"] == 3

        r = q(filters=[{"column": "amount", "op": "lt", "value": 10.5}])
        assert r["total"] == 1

        r = q(filters=[{"column": "amount", "op": "lte", "value": 5.0}])
        assert r["total"] == 1

        r = q(filters=[{"column": "region", "op": "in", "values": ["华东", "华南"]}])
        assert r["total"] == 3

        r = q(filters=[{"column": "region", "op": "notin", "values": ["华东"]}])
        assert r["total"] == 3

        r = q(filters=[{"column": "note", "op": "like", "value": "a%"}])
        assert r["total"] == 2

        r = q(filters=[{"column": "note", "op": "isnull"}])
        assert r["total"] == 1

        r = q(filters=[{"column": "note", "op": "notnull"}])
        assert r["total"] == 4

    def test_filters_and_compose_and_unknown_rejected(self):
        key = _key("q")
        _seed(key)
        t = _t()
        r = t["customer_dataset_query"](
            dataset_key=key,
            filters=[
                {"column": "region", "op": "eq", "value": "华东"},
                {"column": "amount", "op": "gt", "value": 15},
            ],
        )
        assert r["total"] == 1 and r["rows"][0]["id"] == 2

        bad = t["customer_dataset_query"](
            dataset_key=key, filters=[{"column": "region", "op": "regex", "value": "x"}]
        )
        assert "error" in bad and "非法过滤操作符" in bad["error"]

        missing = t["customer_dataset_query"](
            dataset_key=key, filters=[{"column": "nope", "op": "eq", "value": 1}]
        )
        assert "error" in missing and "过滤列不存在" in missing["error"]

    def test_sort_paging_total_and_truncated(self):
        key = _key("q")
        _seed(key)
        t = _t()
        r = t["customer_dataset_query"](
            dataset_key=key,
            sort=[{"column": "region", "dir": "asc"}, {"column": "id", "dir": "desc"}],
            limit=2,
            offset=1,
        )
        # UTF-8 byte order: 华东 < 华北 < 华南; id desc within region
        assert [row["id"] for row in r["rows"]] == [1, 4]
        assert r["total"] == 5 and r["truncated"] is True
        assert r["limit"] == 2 and r["offset"] == 1

        full = t["customer_dataset_query"](dataset_key=key, sort=[{"column": "id", "dir": "asc"}])
        assert full["truncated"] is False

    def test_rows_carry_row_id_anchors(self):
        key = _key("q")
        _seed(key)
        t = _t()
        r = t["customer_dataset_query"](
            dataset_key=key, filters=[{"column": "id", "op": "eq", "value": 3}]
        )
        assert r["rows"][0]["__row_id"] == "pk:[3]"

    def test_count_only_and_distinct(self):
        key = _key("q")
        _seed(key)
        t = _t()
        r = t["customer_dataset_query"](dataset_key=key, count_only=True)
        assert r == {"count": 5}

        d = t["customer_dataset_query"](dataset_key=key, distinct_column="region")
        assert d["values"] == ["华东", "华北", "华南"] and d["count"] == 3
        assert d["truncated"] is False

        dn = t["customer_dataset_query"](
            dataset_key=key,
            distinct_column="note",
            filters=[{"column": "note", "op": "notnull"}],
        )
        assert dn["count"] == 4 and None not in dn["values"]

    def test_raw_reads_base_snapshot_not_edits(self):
        key = _key("q")
        _seed(key)
        t = _t()
        edited = t["customer_dataset_correction_add"](
            dataset_key=key,
            op="update",
            anchor="pk:[1]",
            values={"amount": 999.0},
            actor="tester",
        )
        assert "error" not in edited, edited
        r = t["customer_dataset_query"](
            dataset_key=key, filters=[{"column": "id", "op": "eq", "value": 1}]
        )
        assert r["rows"][0]["amount"] == 999.0
        b = t["customer_dataset_query"](
            dataset_key=key, filters=[{"column": "id", "op": "eq", "value": 1}], raw=True
        )
        assert b["rows"][0]["amount"] == 10.5

    def test_limit_clamped_to_cap(self):
        key = _key("q")
        _seed(key)
        t = _t()
        r = t["customer_dataset_query"](dataset_key=key, limit=100000)
        assert r["limit"] == 1000  # QUERY_ROW_CAP


class TestQueryTimeout:
    def test_raw_query_timed_aborts_past_deadline(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "customer-dataset-mcp"))
        try:
            import customer_dataset_tools as tools
        finally:
            sys.path.pop(0)
        with pytest.raises(Exception, match="查询超时"):
            tools._raw_query_timed(
                "WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM cnt"
                " WHERE x < 50000000) SELECT COUNT(*) FROM cnt",
                timeout=0.01,
            )


class TestAggregate:
    def test_group_by_with_measures(self):
        key = _key("ag")
        _seed(key)
        t = _t()
        r = t["customer_dataset_aggregate"](
            dataset_key=key,
            group_by=["region"],
            measures=[
                {"func": "count"},
                {"func": "sum", "column": "amount"},
                {"func": "avg", "column": "amount"},
                {"func": "min", "column": "amount"},
                {"func": "max", "column": "amount"},
            ],
        )
        by_region = {g["region"]: g for g in r["groups"]}
        assert r["group_count"] == 3 and r["truncated"] is False
        # 华东: 10.5 + 20.0 = 30.5, avg 15.25
        assert by_region["华东"]["count"] == 2
        assert by_region["华东"]["sum__amount"] == pytest.approx(30.5)
        assert by_region["华东"]["avg__amount"] == pytest.approx(15.25)
        # 华北 has one NULL amount: count(*) = 2, sum over (10.5? no—30.25, NULL)
        assert by_region["华北"]["count"] == 2
        assert by_region["华北"]["sum__amount"] == pytest.approx(30.25)
        assert by_region["华北"]["min__amount"] == pytest.approx(30.25)

    def test_filters_apply_and_groups_honest(self):
        key = _key("ag")
        _seed(key)
        t = _t()
        r = t["customer_dataset_aggregate"](
            dataset_key=key,
            group_by=["region"],
            measures=[{"func": "count"}],
            filters=[{"column": "region", "op": "eq", "value": "华东"}],
        )
        # only existing groups appear — no fabricated zero-groups
        assert [g["region"] for g in r["groups"]] == ["华东"]
        assert r["group_count"] == 1 and r["groups"][0]["count"] == 2

    def test_bare_count_and_rejections(self):
        key = _key("ag")
        _seed(key)
        t = _t()
        bad_func = t["customer_dataset_aggregate"](
            dataset_key=key, group_by=["region"], measures=[{"func": "median", "column": "amount"}]
        )
        assert "error" in bad_func and "非法聚合函数" in bad_func["error"]

        bad_type = t["customer_dataset_aggregate"](
            dataset_key=key, group_by=["region"], measures=[{"func": "sum", "column": "note"}]
        )
        assert "error" in bad_type and "TEXT" in bad_type["error"]

        no_group = t["customer_dataset_aggregate"](
            dataset_key=key, group_by=[], measures=[{"func": "count"}]
        )
        assert "error" in no_group


class TestCapabilities:
    def test_overview_advertises_engine_capabilities(self):
        key = _key("cap")
        _seed(key)
        t = _t()
        r = t["customer_dataset_overview"](dataset_key=key)
        caps = r.get("capabilities")
        assert isinstance(caps, dict)
        for name in (
            "query",
            "aggregate",
            "write_batch",
            "create_declared",
            "add_column",
            "derive",
            "derive_refresh",
            "join_indicators",
        ):
            assert name in caps


# ── section 2: channel attribution / batch write / declared / add-column ──


class TestWriteBatch:
    def test_mixed_batch_applies_with_channel_attribution(self):
        key = _key("wb")
        _seed(key)
        t = _t()
        r = t["customer_dataset_write_batch"](
            dataset_key=key,
            ops=[
                {"op": "insert", "values": {"id": 6, "region": "华东", "amount": 1.5, "note": "d1"}},
                {"op": "update", "anchor": "pk:[1]", "values": {"amount": 11.0}},
                {"op": "delete", "anchor": "pk:[5]"},
            ],
            actor="order-sync",
            channel="data-key",
            actor_id="wdk_abcd1234",
            actor_label="订单同步",
        )
        assert "error" not in r, r
        assert r["applied"] == 3 and len(r["correction_ids"]) == 3

        lst = t["customer_dataset_correction_list"](dataset_key=key)
        by_channel = [c for c in lst["corrections"] if c["channel"] == "data-key"]
        assert len(by_channel) == 3
        assert {c["actor_id"] for c in by_channel} == {"wdk_abcd1234"}

        total = t["customer_dataset_query"](dataset_key=key, count_only=True)
        assert total["count"] == 5  # 5 + 1 insert - 1 delete

    def test_batch_atomicity_on_mid_failure(self):
        key = _key("wb")
        _seed(key)
        t = _t()
        before = t["customer_dataset_query"](dataset_key=key, count_only=True)["count"]
        r = t["customer_dataset_write_batch"](
            dataset_key=key,
            ops=[
                {"op": "update", "anchor": "pk:[1]", "values": {"amount": 99.0}},
                {"op": "update", "anchor": "pk:[999]", "values": {"amount": 1.0}},  # miss
                {"op": "update", "anchor": "pk:[2]", "values": {"amount": 88.0}},
            ],
            channel="agent",
        )
        assert "error" in r and "第 2 条失败" in r["error"] and "行锚未命中" in r["error"]

        after = t["customer_dataset_query"](dataset_key=key, count_only=True)["count"]
        assert after == before  # zero changes
        row1 = t["customer_dataset_query"](
            dataset_key=key, filters=[{"column": "id", "op": "eq", "value": 1}]
        )
        assert row1["rows"][0]["amount"] == 10.5  # first op rolled back too
        lst = t["customer_dataset_correction_list"](dataset_key=key)
        assert lst["corrections"] == []  # zero audit rows leaked

    def test_batch_cap_and_type_rejection(self):
        key = _key("wb")
        _seed(key)
        t = _t()
        ops = [{"op": "update", "anchor": "pk:[1]", "values": {"amount": 1.0}}] * 1001
        r = t["customer_dataset_write_batch"](dataset_key=key, ops=ops)
        assert "error" in r and "1001" in r["error"]

        bad_col = t["customer_dataset_write_batch"](
            dataset_key=key, ops=[{"op": "update", "anchor": "pk:[1]", "values": {"nope": 1}}]
        )
        assert "error" in bad_col and "第 1 条失败" in bad_col["error"]

    def test_lww_same_anchor_two_channels(self):
        key = _key("wb")
        _seed(key)
        t = _t()
        t["customer_dataset_write_batch"](
            dataset_key=key,
            ops=[{"op": "update", "anchor": "pk:[1]", "values": {"amount": 100.0}}],
            channel="agent",
            actor="agent-a",
        )
        t["customer_dataset_correction_add"](
            dataset_key=key, op="update", anchor="pk:[1]", values={"amount": 200.0},
            actor="human", channel="console",
        )
        row = t["customer_dataset_query"](
            dataset_key=key, filters=[{"column": "id", "op": "eq", "value": 1}]
        )
        assert row["rows"][0]["amount"] == 200.0  # last writer wins
        lst = t["customer_dataset_correction_list"](dataset_key=key)
        assert len([c for c in lst["corrections"] if not c.get("reverted_at")]) == 2  # both kept


class TestCreateDeclared:
    def test_declared_creation_write_and_pk_anchor(self):
        key = _key("dc")
        t = _t()
        r = t["customer_dataset_create_declared"](
            dataset_key=key,
            columns=[
                {"name": "order_no", "type": "TEXT"},
                {"name": "qty", "type": "INTEGER"},
            ],
            pk_columns=["order_no"],
            name="订单表",
        )
        assert "error" not in r, r
        ov = t["customer_dataset_overview"](dataset_key=key)
        assert ov["rows"] == 0 and ov["name"] == "订单表"

        w = t["customer_dataset_write_batch"](
            dataset_key=key,
            ops=[{"op": "insert", "values": {"order_no": "A-1", "qty": 3}}],
            channel="data-key",
        )
        assert "error" not in w, w
        upd = t["customer_dataset_write_batch"](
            dataset_key=key,
            ops=[{"op": "update", "anchor": 'pk:["A-1"]', "values": {"qty": 5}}],
        )
        assert "error" not in upd, upd
        row = t["customer_dataset_query"](
            dataset_key=key, filters=[{"column": "order_no", "op": "eq", "value": "A-1"}]
        )
        assert row["rows"][0]["qty"] == 5

    def test_duplicate_key_rejected(self):
        key = _key("dc")
        _seed(key)
        t = _t()
        r = t["customer_dataset_create_declared"](
            dataset_key=key, columns=[{"name": "a", "type": "TEXT"}]
        )
        assert "error" in r and "已存在" in r["error"]


class TestAddColumn:
    def test_add_column_then_query_and_write(self):
        key = _key("ac")
        _seed(key)
        t = _t()
        r = t["customer_dataset_add_column"](
            dataset_key=key, column={"name": "status", "type": "TEXT"}, actor="ops"
        )
        assert "error" not in r, r
        nulls = t["customer_dataset_query"](
            dataset_key=key, filters=[{"column": "status", "op": "isnull"}]
        )
        assert nulls["total"] == 5  # existing rows null
        w = t["customer_dataset_write_batch"](
            dataset_key=key,
            ops=[{"op": "update", "anchor": "pk:[1]", "values": {"status": "paid"}}],
        )
        assert "error" not in w, w
        paid = t["customer_dataset_query"](
            dataset_key=key, filters=[{"column": "status", "op": "eq", "value": "paid"}]
        )
        assert paid["total"] == 1

    def test_duplicate_and_open_ingest_guards(self):
        key = _key("ac")
        _seed(key)
        t = _t()
        dup = t["customer_dataset_add_column"](dataset_key=key, column={"name": "region", "type": "TEXT"})
        assert "error" in dup and "列已存在" in dup["error"]

        begun = t["customer_dataset_ingest_begin"](
            dataset_key=key, columns=_COLS, pk_columns=["id"]
        )
        assert "error" not in begun
        busy = t["customer_dataset_add_column"](dataset_key=key, column={"name": "extra", "type": "TEXT"})
        assert "error" in busy and "ingest" in busy["error"]
        t["customer_dataset_ingest_abort"](ingest_id=begun["ingest_id"])


# ── section 3: lineage / derive / derive_refresh / delete guard ──────


class TestDerive:
    def test_query_derive_lineage_and_editable_child(self):
        src = _key("dv")
        child = _key("dvc")
        _seed(src)
        t = _t()
        r = t["customer_dataset_derive"](
            child_key=child,
            parent_key=src,
            filters=[{"column": "region", "op": "eq", "value": "华东"}],
            sort=[{"column": "id", "dir": "asc"}],
        )
        assert "error" not in r, r
        assert r["rows"] == 2 and r["kind"] == "query"

        lin = t["customer_dataset_lineage"](dataset_key=child)
        assert lin["derivation"][0]["parent_key"] == src
        assert lin["derivation"][0]["spec"]["filters"]
        parent_lin = t["customer_dataset_lineage"](dataset_key=src)
        assert [c["dataset_key"] for c in parent_lin["children"]] == [child]

        # child inherits pk → editable via pk anchor
        w = t["customer_dataset_write_batch"](
            dataset_key=child, ops=[{"op": "update", "anchor": "pk:[1]", "values": {"amount": 42.0}}]
        )
        assert "error" not in w, w

    def test_derive_rejections(self):
        src = _key("dv")
        _seed(src)
        t = _t()
        self_derive = t["customer_dataset_derive"](child_key=src, parent_key=src)
        assert "error" in self_derive and "不能与父集相同" in self_derive["error"]

        child = _key("dvc")
        t["customer_dataset_derive"](child_key=child, parent_key=src)
        again = t["customer_dataset_derive"](child_key=child, parent_key=src)
        assert "error" in again and "derive_refresh" in again["error"]

        missing_parent = t["customer_dataset_derive"](child_key=_key("dvc"), parent_key="no_such")
        assert "error" in missing_parent and "父数据集不存在" in missing_parent["error"]

    def test_refresh_picks_up_parent_edits(self):
        src = _key("dv")
        child = _key("dvc")
        _seed(src)
        t = _t()
        t["customer_dataset_derive"](
            child_key=child, parent_key=src,
            filters=[{"column": "region", "op": "eq", "value": "华东"}],
        )
        t["customer_dataset_write_batch"](
            dataset_key=src,
            ops=[{"op": "update", "anchor": "pk:[1]", "values": {"amount": 777.0}}],
        )
        r = t["customer_dataset_derive_refresh"](child_key=child)
        assert "error" not in r, r
        row = t["customer_dataset_query"](
            dataset_key=child, filters=[{"column": "id", "op": "eq", "value": 1}]
        )
        assert row["rows"][0]["amount"] == 777.0

    def test_parent_delete_blocked_then_freed(self):
        src = _key("dv")
        child = _key("dvc")
        grand = _key("dvg")
        _seed(src)
        t = _t()
        t["customer_dataset_derive"](child_key=child, parent_key=src)
        t["customer_dataset_derive"](child_key=grand, parent_key=child)

        blocked = t["customer_dataset_delete"](dataset_key=src)
        assert "error" in blocked and child in blocked["error"]

        blocked2 = t["customer_dataset_delete"](dataset_key=child)
        assert "error" in blocked2 and grand in blocked2["error"]

        assert "error" not in t["customer_dataset_delete"](dataset_key=grand)
        assert "error" not in t["customer_dataset_delete"](dataset_key=child)
        assert "error" not in t["customer_dataset_delete"](dataset_key=src)

    def test_tool_surface_selfcheck_cell_profile(self):
        result = selfcheck.run_invariants(profile="cell")
        assert result["ok"] is True, result
        assert result["group_counts"]["customer_dataset"] == 22


# ── section 4: join_indicators ───────────────────────────────────────


class _FakeGateway:
    """Deterministic stand-in for the wire-entrance indicator fetcher."""

    def __init__(self, series_by_entity):
        # {(entity_type, entity_id): {concept_id: [(date, value), ...]}}
        self.series_by_entity = series_by_entity
        self.read_range_calls = []

    def resolve_codes(self, codes):
        known = {"GDP_MO": 101, "CPI_YOY": 102}
        missing = [c for c in codes if c not in known]
        if missing:
            raise Exception(f"指标 code 未在目录中解析到: {missing}")
        return {c: known[c] for c in codes}

    def resolve_entity(self, name):
        return {"中国": ("country", 1), "浙江": ("province", 2), "广东": ("province", 3)}.get(name)

    def read_range(self, concept_ids, entity_type, entity_id, start, end):
        self.read_range_calls.append((entity_type, entity_id, start, end))
        out = {}
        for cid in concept_ids:
            pts = self.series_by_entity.get((entity_type, entity_id), {}).get(cid, [])
            out[cid] = [{"date": d, "value": v} for d, v in pts]
        return out


def _inject_gateway(monkeypatch, fake) -> None:
    """Inject the fake gateway into the LIVE group module. registry.build()
    loads the group under a fresh module instance each build, so patching a
    separately-imported `customer_dataset_tools` would miss — the tool
    function's ``__globals__`` IS the live module dict."""
    g = _t()["customer_dataset_join_indicators"].__globals__
    monkeypatch.setitem(g, "_gateway_factory", (lambda: fake) if fake is not None else None)


_JOIN_COLS = [
    {"name": "id", "type": "INTEGER"},
    {"name": "month", "type": "TEXT"},
    {"name": "region", "type": "TEXT"},
]


def _seed_join_source(key: str) -> None:
    t = _t()
    begun = t["customer_dataset_ingest_begin"](dataset_key=key, columns=_JOIN_COLS, pk_columns=["id"])
    rows = [
        {"id": 1, "month": "2026-03-15", "region": "浙江"},
        {"id": 2, "month": "2026-03-20", "region": "广东"},
        {"id": 3, "month": "2026-04-01", "region": "浙江"},
        {"id": 4, "month": "2026-05-09", "region": "未知地区"},
    ]
    t["customer_dataset_ingest_chunk"](ingest_id=begun["ingest_id"], rows=rows, start_index=0)
    t["customer_dataset_ingest_commit"](ingest_id=begun["ingest_id"])


class TestJoinIndicators:
    def test_monthly_containment_region_and_null_miss(self, monkeypatch):
        fake = _FakeGateway(
            {
                ("province", 2): {101: [("2026-03-01", 3.5), ("2026-04-01", 3.8)]},
                ("province", 3): {101: [("2026-03-01", 2.1)]},
            }
        )
        _inject_gateway(monkeypatch, fake)

        src = _key("j")
        child = _key("jc")
        _seed_join_source(src)
        t = _t()
        r = t["customer_dataset_join_indicators"](
            child_key=child,
            parent_key=src,
            indicators=[{"code": "GDP_MO", "as": "gdp"}],
            date_column="month",
            region_column="region",
        )
        assert "error" not in r, r
        assert r["rows"] == 4 and r["unresolved_regions"] == ["未知地区"]
        assert fake.read_range_calls  # series actually fetched

        rows = t["customer_dataset_query"](dataset_key=child, sort=[{"column": "id", "dir": "asc"}])["rows"]
        assert rows[0]["gdp"] == 3.5  # 03-15 falls in 2026-03 (monthly point)
        assert rows[1]["gdp"] == 2.1  # 广东 03-20
        assert rows[2]["gdp"] == 3.8  # exact 04-01
        assert rows[3]["gdp"] is None  # unresolved region → null, row kept

        lin = t["customer_dataset_lineage"](dataset_key=child)
        assert lin["derivation"][0]["kind"] == "join"

    def test_no_neighbor_interpolation(self, monkeypatch):
        fake = _FakeGateway({("country", 1): {101: [("2026-03-01", 9.9)]}})
        _inject_gateway(monkeypatch, fake)

        src = _key("j")
        child = _key("jc")
        _seed_join_source(src)
        t = _t()
        r = t["customer_dataset_join_indicators"](
            child_key=child,
            parent_key=src,
            indicators=[{"code": "GDP_MO", "as": "gdp"}],
            date_column="month",
        )
        assert "error" not in r, r
        rows = t["customer_dataset_query"](dataset_key=child, sort=[{"column": "id", "dir": "asc"}])["rows"]
        assert rows[0]["gdp"] == 9.9  # March containment via default entity 中国
        assert rows[2]["gdp"] is None  # April has no point → null, no neighbor

    def test_code_cap_and_missing_config(self, monkeypatch):
        _inject_gateway(monkeypatch, None)
        monkeypatch.delenv("FD_WIRE_ENTRANCE_URL", raising=False)
        monkeypatch.delenv("FD_CELL_WGK_KEY", raising=False)

        src = _key("j")
        _seed_join_source(src)
        t = _t()
        too_many = t["customer_dataset_join_indicators"](
            child_key=_key("jc"),
            parent_key=src,
            indicators=[{"code": f"C{i}", "as": f"c{i}"} for i in range(11)],
            date_column="month",
        )
        assert "error" in too_many and "10" in too_many["error"]

        no_channel = t["customer_dataset_join_indicators"](
            child_key=_key("jc"),
            parent_key=src,
            indicators=[{"code": "GDP_MO"}],
            date_column="month",
        )
        assert "error" in no_channel and "指标拉取通道未配置" in no_channel["error"]

    def test_e2e_declare_write_join_refresh(self, monkeypatch):
        fake = _FakeGateway({("country", 1): {101: [("2026-03-01", 1.0)]}})
        _inject_gateway(monkeypatch, fake)

        t = _t()
        src = _key("j")
        child = _key("jc")
        t["customer_dataset_create_declared"](
            dataset_key=src, columns=_JOIN_COLS, pk_columns=["id"]
        )
        t["customer_dataset_write_batch"](
            dataset_key=src,
            ops=[{"op": "insert", "values": {"id": 1, "month": "2026-03-15"}}],
            channel="data-key",
            actor_id="wdk_test",
        )
        r = t["customer_dataset_join_indicators"](
            child_key=child,
            parent_key=src,
            indicators=[{"code": "GDP_MO", "as": "gdp"}],
            date_column="month",
        )
        assert "error" not in r, r
        assert t["customer_dataset_query"](dataset_key=child)["rows"][0]["gdp"] == 1.0

        # upstream series moved → refresh re-fetches and updates the child
        fake.series_by_entity[("country", 1)] = {101: [("2026-03-01", 2.0)]}
        rr = t["customer_dataset_derive_refresh"](child_key=child)
        assert "error" not in rr and rr.get("refreshed") is True
        assert t["customer_dataset_query"](dataset_key=child)["rows"][0]["gdp"] == 2.0


class TestWriteBatchRowCap:
    def test_inserts_beyond_cap_reject_whole_batch(self, monkeypatch):
        src = _key("cap")
        _seed(src)
        t = _t()
        # registry.build 每次构建都是新模块实例——patch 活模块的工具 globals
        g = t["customer_dataset_write_batch"].__globals__
        monkeypatch.setitem(g, "_max_rows", lambda: 5)
        # 现有 5 行（seed），再 insert 1 条即越限
        r = t["customer_dataset_write_batch"](
            dataset_key=src,
            ops=[{"op": "insert", "values": {"id": 99, "region": "x", "amount": 1.0}}],
        )
        assert "error" in r and "超过上限" in r["error"]
        total = t["customer_dataset_query"](dataset_key=src, count_only=True)
        assert total["count"] == 5  # 零变更


class TestEntityMirrorFallback:
    def test_canonical_entity_when_mirror_unavailable(self, monkeypatch):
        class _DownGateway:
            def resolve_codes(self, codes):
                return {"GDP_MO": 101}

            def resolve_entity(self, name):
                # 与 _EntranceGateway.resolve_entity 同逻辑的直测：
                # 这里直接断言模块级规范名表在镜像挂时的可用性
                return tools_module()._CANONICAL_ENTITIES.get(name)

        def tools_module():
            g = _t()["customer_dataset_join_indicators"].__globals__
            import types
            return types.SimpleNamespace(_CANONICAL_ENTITIES=g["_CANONICAL_ENTITIES"])

        g = _t()["customer_dataset_join_indicators"].__globals__
        assert g["_CANONICAL_ENTITIES"]["中国"] == ("country", 1)
        fake = _FakeGateway({("country", 1): {101: [("2026-03-01", 3.3)]}})
        _inject_gateway(monkeypatch, fake)
        src = _key("fb")
        child = _key("fbc")
        _seed_join_source(src)
        t = _t()
        r = t["customer_dataset_join_indicators"](
            child_key=child,
            parent_key=src,
            indicators=[{"code": "GDP_MO", "as": "gdp"}],
            date_column="month",
        )
        assert "error" not in r, r
        rows = t["customer_dataset_query"](dataset_key=child, sort=[{"column": "id", "dir": "asc"}])["rows"]
        assert rows[0]["gdp"] == 3.3  # 默认实体经规范名表解析成功
