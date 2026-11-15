"""`alerts-mcp/series_ref.py` 的判据测试，外加读路径的接线。

这道模块管的是 change1 task 4.4：研究取价（通道 B）返回序列时要带出处。三件事必须钉住：

1. **指纹与上游逐位相同。** DAAS 不能 import `fd_quant`（另一个镜像），只能复刻
   `compute_price_series_fingerprint`；跨仓复刻的危险是静默分叉，所以这里用
   `fd-paper-trading` 的 `_vendor/fd_quant` 现算出来的两个 sha256 当向量。
2. **不确定就不出 ref。** 缺记录 / 记录坏 / 来源没登记 / 口径词不认识 / 没有水位 /
   前复权没有锚点，六种形态各给一个原因码——三态留痕，不留空对象。
3. **`none` 不带锚点。** 登记表里 `index-seed` 那档明确写 `adjustment_anchor: null`；
   给不复权序列挂一个锚点，既是噪音也与登记矛盾。

一条 `get_series_latest` 的接线用例（价格表带 ref、`observations` 形状不变）。
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

_ALERTS_MCP = Path(__file__).resolve().parents[1] / "alerts-mcp"
sys.path.insert(0, str(_ALERTS_MCP))

import series_ref  # noqa: E402
from alert_database import get_db  # noqa: E402
import alert_tools  # noqa: E402

# 这两个 sha256 由 `fd-paper-trading` 的 `_vendor/fd_quant` 算出（上游契约同一实现）：
#   compute_price_series_fingerprint(source_id='daas_akshare_stock_qfq', symbol='600519',
#       adjustment='qfq', as_of_max='2026-11-06', adjustment_anchor='2026-10-17',
#       db_source='daas.db', window_start='2026-10-17', window_end='2026-11-06',
#       row_count=6037)
#   …(source_id='daas_akshare_index_none', symbol='000300', adjustment='none',
#       as_of_max='2026-11-06', adjustment_anchor=None, db_source='daas.db',
#       window_start=None, window_end='2026-11-06', row_count=5304)
STOCK_VECTOR = "81ecfc293267e669d377105af6528d6dae0a4b5fb5ebf4a643af7f578c54f2c9"
INDEX_VECTOR = "cd7247944688eae7b9f7ec842eb472917ed2fb7dd6a2f1c2399ef9d050e77f08"

STOCK_CFG = {"source": "stock_zh_a_hist", "adjust": "qfq", "rows": 6022,
             "created_at": "2026-10-17 15:57:59"}
INDEX_CFG = {"source": "index_zh_a_hist", "adjust": "none", "rows": 5304,
             "created_at": "2026-11-06 15:51:56"}


def _stock():
    return series_ref.build_ref(
        table="scraw_600519_daily", code="600519", config=STOCK_CFG,
        min_date="2026-10-17", max_date="2026-11-06", row_count=6037,
        db_source="daas.db")


def _index():
    return series_ref.build_ref(
        table="scraw_000300_daily", code="000300", config=INDEX_CFG,
        min_date=None, max_date="2026-11-06", row_count=5304,
        db_source="daas.db")


# --------------------------------------------------------------- 指纹一致性

def test_stock_fingerprint_matches_the_upstream_vector():
    ref, why = _stock()
    assert why is None
    assert ref["fingerprint"] == STOCK_VECTOR


def test_index_fingerprint_matches_the_upstream_vector():
    ref, why = _index()
    assert why is None
    assert ref["fingerprint"] == INDEX_VECTOR


def test_fingerprint_moves_when_the_watermark_moves():
    """水位前移 ⇒ 指纹必变。这是「两次结果不一致可归因为输入变了」的机器判据。"""
    a, _ = _stock()
    b, _ = series_ref.build_ref(
        table="scraw_600519_daily", code="600519", config=STOCK_CFG,
        min_date="2026-10-17", max_date="2026-11-09", row_count=6040,
        db_source="daas.db")
    assert a["fingerprint"] != b["fingerprint"]


def test_fingerprint_moves_when_the_anchor_moves():
    """`--replace` 重播会改 created_at（锚点移动）⇒ 指纹必变，即使 source_id 没变。

    这条是把登记表 D1（换锚点＝换口径）变成可机器判定的东西。
    """
    a, _ = _stock()
    moved, _ = series_ref.build_ref(
        table="scraw_600519_daily", code="600519",
        config={**STOCK_CFG, "created_at": "2026-11-20 09:00:00"},
        min_date="2026-10-17", max_date="2026-11-06", row_count=6037,
        db_source="daas.db")
    assert a["fingerprint"] != moved["fingerprint"]
    assert a["source_id"] == moved["source_id"]


# --------------------------------------------------------------- ref 的形状

def test_stock_ref_carries_the_four_contract_fields():
    ref, _ = _stock()
    for key in ("source_id", "symbol", "adjustment", "as_of_max", "fingerprint"):
        assert ref.get(key)
    assert ref["source_id"] == "daas_akshare_stock_qfq"
    assert ref["adjustment"] == "qfq"
    assert ref["adjustment_anchor"] == "2026-10-17"


def test_index_ref_has_no_anchor():
    """不复权挂锚点是噪音，且与 `index-seed` 的 `adjustment_anchor: null` 矛盾。"""
    ref, _ = _index()
    assert ref["adjustment"] == "none"
    assert "adjustment_anchor" not in ref
    assert ref["source_id"] == "daas_akshare_index_none"


def test_ref_describes_the_whole_table_not_the_returned_page():
    """`limit` 是分页参数，不是数据身份：窗口/行数取表内 min/max/count。"""
    ref, _ = _stock()
    assert ref["window_start"] == "2026-10-17"
    assert ref["window_end"] == "2026-11-06"
    assert ref["row_count"] == 6037


# --------------------------------------------------------------- 六种 withhold

@pytest.mark.parametrize("config,reason", [
    (None, "no_config"),
    ("JSONDecodeError: boom", "bad_config"),
    ({"source": "sina_realtime", "adjust": "qfq", "created_at": "2026-10-17"}, "unknown_source"),
    ({"source": "stock_zh_a_hist", "adjust": "unknown", "created_at": "2026-10-17"}, "caliber_unknown"),
    ({"source": "stock_zh_a_hist", "adjust": "none", "created_at": "2026-10-17"}, "caliber_unknown"),
])
def test_withheld_reasons(config, reason):
    ref, why = series_ref.build_ref(
        table="scraw_600519_daily", code="600519", config=config,
        min_date="2026-10-17", max_date="2026-11-06", row_count=6037)
    assert ref is None
    assert why == reason


def test_qfq_without_anchor_is_withheld():
    """前复权没有锚点，读者无法判断两批是否可比——宁可不出。"""
    ref, why = series_ref.build_ref(
        table="scraw_600519_daily", code="600519",
        config={"source": "stock_zh_a_hist", "adjust": "qfq", "rows": 1},
        min_date="2026-10-17", max_date="2026-11-06", row_count=6037)
    assert ref is None and why == "no_adjustment_anchor"


def test_missing_watermark_is_withheld():
    ref, why = series_ref.build_ref(
        table="scraw_600519_daily", code="600519", config=STOCK_CFG,
        min_date=None, max_date=None, row_count=0)
    assert ref is None and why == "no_as_of_max"


# --------------------------------------------------------------- 形状识别

@pytest.mark.parametrize("name,code", [
    ("scraw_600519_daily", "600519"),
    ("scraw_000001_daily", "000001"),
    ("scraw_aapl_daily", None),
    ("observations", None),
    ("scraw_60051_daily", None),
    ("", None),
])
def test_price_table_shape(name, code):
    """只认 6 位代码：美股/宏观表走另一套来源，替它们出 ref 就是替没声明的口径背书。"""
    assert series_ref.is_price_table(name) == code


# --------------------------------------------------------------- 读路径接线

def _seed_price_table() -> None:
    db = get_db()
    with db.engine.connect() as conn:
        conn.execute(text("DROP TABLE IF EXISTS scraw_600519_daily"))
        conn.execute(text('CREATE TABLE scraw_600519_daily '
                          '(date TEXT PRIMARY KEY, "Open" REAL, Close REAL)'))
        conn.execute(text('INSERT INTO scraw_600519_daily VALUES '
                          "('2026-11-05', 1.0, 1.0), ('2026-11-06', 2.0, 2.0)"))
        conn.execute(text("DROP TABLE IF EXISTS scraw_configs"))
        conn.execute(text("CREATE TABLE scraw_configs "
                          "(id INTEGER PRIMARY KEY, url TEXT, name TEXT, "
                          "columns_json TEXT, created_at TEXT, updated_at TEXT)"))
        conn.execute(
            text("INSERT INTO scraw_configs (url, name, columns_json, created_at, updated_at) "
                 "VALUES ('akshare:stock_zh_a_hist', 'astock_600519', :c, "
                 "'2026-10-17 15:57:59', '2026-10-17 15:57:59')"),
            {"c": '{"adjust": "qfq", "rows": 2, "source": "stock_zh_a_hist"}'})
        conn.commit()


def _drop_price_table() -> None:
    with get_db().engine.connect() as conn:
        conn.execute(text("DROP TABLE IF EXISTS scraw_600519_daily"))
        conn.execute(text("DROP TABLE IF EXISTS scraw_configs"))
        conn.commit()


def test_get_series_latest_attaches_a_ref_for_a_price_table():
    _seed_price_table()
    try:
        out = alert_tools.get_series_latest("scraw_600519_daily", "date", "Close", limit=2)
        assert out["rows"] and out["rows"][0]["date"] == "2026-11-06"
        ref = out["series_ref"]
        assert ref["source_id"] == "daas_akshare_stock_qfq"
        assert ref["as_of_max"] == "2026-11-06"
        assert ref["window_start"] == "2026-11-05"
        assert ref["row_count"] == 2
        assert ref["adjustment_anchor"] == "2026-10-17"
        assert ref["fingerprint"]
        assert "series_ref_withheld" not in out
    finally:
        _drop_price_table()


def test_get_series_latest_withholds_with_a_reason_when_caliber_is_missing():
    """记录没了不能报错、也不能假装有出处——给原因码，让调用方三态留痕。"""
    _seed_price_table()
    try:
        with get_db().engine.connect() as conn:
            conn.execute(text("DELETE FROM scraw_configs"))
            conn.commit()
        out = alert_tools.get_series_latest("scraw_600519_daily", "date", "Close", limit=2)
        assert out["rows"]
        assert "series_ref" not in out
        assert out["series_ref_withheld"] == "no_config"
    finally:
        _drop_price_table()


def test_get_series_latest_leaves_non_price_tables_alone():
    """`observations` 没有口径声明，返回体形状不变（不替它背书）。"""
    out = alert_tools.get_series_latest("observations", "date", "value", limit=1)
    assert "series_ref" not in out
    assert "series_ref_withheld" not in out
