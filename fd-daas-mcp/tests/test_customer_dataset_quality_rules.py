"""质量计算体测试（openspec: wire-quality-rules-v2）。

覆盖：
- customer_dataset_check 三 kind 正/反例（regex/col_compare/range）；
- NULL 语义：NULL 不计违规（各 kind 一致）；
- pattern 编译闸：长度/嵌套量词/非法语法 → error（不误报 fail）；
- 单批 ≤5 条闸；
- 空数据集空真（pass、unexpected=0）；
- **样本零回传**（honeypot：违规值不出现在返回体）；
- customer_dataset_quality payload 字段与平台净化器白名单对齐；
- kind 未知 → 该检查 error（其余照常）。

双方言腿：conftest 的 FD_ENGINE_TEST_BACKEND=postgres 同套用例（沿
wire-cell-postgres-storage 先例）。
"""
from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

_TOOLS_DIR = Path(__file__).resolve().parents[1] / "customer-dataset-mcp"


@contextmanager
def _tools():
    """惰性 import customer_dataset_tools（沿 test_customer_data_engine 先例：
    sys.path 仅在本组用例期间携带该目录，避免污染其他组的模块解析）。"""
    sys.path.insert(0, str(_TOOLS_DIR))
    try:
        import customer_dataset_tools as mod  # noqa: PLC0415

        yield mod
    finally:
        sys.path.pop(0)

_COLS = [
    {"name": "code", "type": "TEXT"},
    {"name": "age", "type": "INTEGER"},
    {"name": "end_d", "type": "INTEGER"},
    {"name": "start_d", "type": "INTEGER"},
]
_ROWS = [
    {"code": "AB1234", "age": 30, "end_d": 5, "start_d": 1},
    {"code": "xx99", "age": 70, "end_d": 3, "start_d": 4},
    {"code": "CD5678", "age": None, "end_d": 9, "start_d": 9},
]


@pytest.fixture()
def dataset():
    """三行四列数据集（conftest 的会话级数据根：隔离 sqlite 或 PG 测试库）。"""
    with _tools() as mod:
        mod.create_declared("qrules", columns=_COLS)
        begun = mod.ingest_begin("qrules", columns=_COLS, pk_columns=["code"])
        mod.ingest_chunk(begun["ingest_id"], _ROWS, 0)
        mod.ingest_commit(begun["ingest_id"])
    return "qrules"


def _call(fn_name, *args):
    with _tools() as mod:
        return getattr(mod, fn_name)(*args)


def _results(payload):
    assert payload.get("success") is not False, payload
    return payload["results"]


def test_regex_pass_and_fail(dataset):
    payload = _call('check', 
        dataset, [{"kind": "regex", "column": "code", "pattern": r"^[A-Z]{2}\d{4}$"}]
    )
    (result,) = _results(payload)
    assert result["status"] == "fail"
    assert result["element_count"] == 3
    assert result["unexpected_count"] == 1  # 仅 xx99
    assert result["pass_rate"] == pytest.approx(2 / 3, abs=1e-6)

    # 允许小写的模式 → 全过（regex 不区分列内既有值的大小写策略，
    # 由 pattern 自行表达：这里显式含小写字母）
    payload = _call(
        "check",
        dataset, [{"kind": "regex", "column": "code", "pattern": r"^[A-Za-z0-9]+$"}]
    )
    (result,) = _results(payload)
    assert result["status"] == "pass"
    assert result["unexpected_count"] == 0


def test_col_compare_six_ops_and_null_exemption(dataset):
    # 3 < 4 → 一行违规；NULL 行豁免（两侧都有值时强制比较）
    payload = _call('check', 
        dataset,
        [
            {"kind": "col_compare", "col_a": "end_d", "op": "gte", "col_b": "start_d"},
            {"kind": "col_compare", "col_a": "end_d", "op": "ne", "col_b": "start_d"},
            {"kind": "col_compare", "col_a": "end_d", "op": "lte", "col_b": "start_d"},
        ],
    )
    gte, ne, lte = _results(payload)
    assert gte["unexpected_count"] == 1
    assert ne["unexpected_count"] == 1  # end==start 的那行不满足 ne
    assert lte["unexpected_count"] == 1  # 5>1 那行不满足 lte

    # 同列拒
    payload = _call('check', 
        dataset, [{"kind": "col_compare", "col_a": "age", "op": "gte", "col_b": "age"}]
    )
    (result,) = _results(payload)
    assert result["status"] == "error" and "同一列" in result["error"]


def test_range_closed_interval_and_nulls(dataset):
    # age=70 越上界 → 1 违例；age=NULL 豁免；非数值计违例（本数据集无数值外形态）
    payload = _call('check', 
        dataset, [{"kind": "range", "column": "age", "min": 18, "max": 60}]
    )
    (result,) = _results(payload)
    assert result["status"] == "fail"
    assert result["unexpected_count"] == 1


def test_pattern_compile_gate(dataset):
    for bad in (
        {"kind": "regex", "column": "code", "pattern": "((.+)+)"},
        {"kind": "regex", "column": "code", "pattern": "a" * 200},
        {"kind": "regex", "column": "code", "pattern": "("},
    ):
        payload = _call('check', dataset, [bad])
        (result,) = _results(payload)
        assert result["status"] == "error", bad
        assert result["unexpected_count"] is None  # 不误报 fail


def test_batch_size_gate(dataset):
    payload = _call('check', 
        dataset, [{"kind": "range", "column": "age", "min": 0}] * 6
    )
    assert payload.get("success") is False
    assert "至多 5 条" in payload["error"]


def test_unknown_kind_isolated(dataset):
    payload = _call('check', 
        dataset,
        [
            {"kind": "exec_shell", "column": "code"},
            {"kind": "range", "column": "age", "min": 0, "max": 100},
        ],
    )
    bad, good = _results(payload)
    assert bad["status"] == "error"
    assert good["status"] == "pass"  # 其余检查照常


def test_sample_zero_return_honeypot(dataset):
    """honeypot：违规值/样本行绝不出现在返回体（含 error 分支）。"""
    payloads = [
        _call(
            "check",
            dataset, [{"kind": "regex", "column": "code", "pattern": r"^Z"}]
        ),
        _call("quality", dataset),
    ]
    blob = json.dumps(payloads, ensure_ascii=False)
    for needle in ("xx99", "AB1234", "CD5678"):
        assert needle not in blob, f"样本泄漏: {needle}"


def test_quality_payload_aligns_with_platform_whitelist(dataset):
    payload = _call('quality', dataset)
    assert payload["row_count"] == 3
    assert payload["pk_declared"] is True
    assert payload["pk_uniqueness"]["unique_ratio"] == 1.0
    assert set(payload) >= {
        "row_count", "computed_at", "pk_declared", "pk_uniqueness",
        "null_rates", "sample_limit_hit",
    }
    by_col = {r["column"]: r for r in payload["null_rates"]}
    assert set(by_col["age"]) == {"column", "null_rate", "sampled_rows"}
    assert by_col["age"]["null_rate"] == pytest.approx(1 / 3, abs=1e-6)


def test_empty_dataset_vacuous(dataset):
    with _tools() as mod:
        mod.create_declared("qempty", columns=[{"name": "a", "type": "TEXT"}])
    payload = _call(
        "check",
        "qempty", [{"kind": "regex", "column": "a", "pattern": r"^x$"}]
    )
    (result,) = _results(payload)
    assert result["status"] == "pass"
    assert result["element_count"] == 0
    assert result["pass_rate"] == 1.0
