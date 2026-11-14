"""`selfcheck_price_caliber.py` 的判据测试。

这道闸管的是「通道 B 的**口径记录**（`scraw_configs`）与表本身还对不对得上」：
研究取价要给出处（`source_id` / `adjustment` / `as_of_max` / `fingerprint`），
而出处只能从这份记录取。记录缺失、记录与数据不符、或历史被重写（行数缩水），
下游拿到的 ref 就会替一个没人声明过的口径背书——三种形态都不会自己报错。

测试只碰 `judge` 与我们自己造的临时库：不连生产 `daas.db`、不发网络请求。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # -> fd-daas-mcp/
SCRIPT = os.path.join(REPO, "daas-mcp", "selfcheck_price_caliber.py")


def _load():
    """按仓内既有约定用 spec 加载（`daas-mcp/` 不是包）。"""
    name = "daas_price_caliber_selfcheck"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


gate = _load()

STOCK = {"source": "stock_zh_a_hist", "adjust": "qfq", "rows": 10}
INDEX = {"source": "index_zh_a_hist", "adjust": "none", "rows": 10}


def row(table, actual, cfg, mx="2026-11-06"):
    return {"table": table, "actual": actual, "max_date": mx, "config": cfg}


def verdicts(facts):
    return {r["table"]: r["verdict"] for r in gate.judge(facts)[0]}


def test_stock_and_index_calibers_are_ok():
    got = verdicts([row("scraw_600519_daily", 10, STOCK),
                    row("scraw_000905_daily", 10, INDEX)])
    assert got == {"scraw_600519_daily": "ok", "scraw_000905_daily": "ok"}


def test_append_only_growth_stays_ok():
    """追加式刷新（refresh_astock_to_today.py 的 INSERT OR IGNORE）会让实际 > 记录。"""
    assert verdicts([row("scraw_600519_daily", 15, STOCK)])["scraw_600519_daily"] == "ok"


def test_append_growth_is_one_aggregated_fact_line_not_one_per_table():
    """逐表刷同样一行会把真正的缺陷淹掉（首跑就刷了 12 行同样的字）。"""
    facts = [row(f"scraw_6005{i}_daily", 15, STOCK) for i in range(6)]
    _res, notes = gate.judge(facts)
    assert sum("追加式" in n for n in notes) == 1


def test_missing_record_is_a_hit():
    _res, _notes = gate.judge([row("scraw_600519_daily", 10, None)])
    assert _res[0]["verdict"] == "no_config"
    assert "无人知晓" in _res[0]["detail"]


def test_unparseable_record_is_a_hit():
    assert verdicts([row("scraw_600519_daily", 10, "JSONDecodeError: x")])[
        "scraw_600519_daily"] == "bad_config"


def test_unknown_source_is_a_hit():
    """换了抓取路径却没有更新登记 —— 下游没人能替这个口径背书。"""
    got = verdicts([row("scraw_600519_daily", 10,
                        {"source": "sina_realtime", "adjust": "qfq", "rows": 1})])
    assert got["scraw_600519_daily"] == "unknown_source"


def test_adjust_mismatch_is_a_hit():
    """个股记成不复权：记录与数据不一致比缺失更坏（缺失会被发现，撒谎不会）。"""
    got = verdicts([row("scraw_600519_daily", 10,
                        {"source": "stock_zh_a_hist", "adjust": "none", "rows": 1})])
    assert got["scraw_600519_daily"] == "adjust_mismatch"


def test_shrunk_rows_means_history_was_rewritten():
    """实际 < 记录 ⇒ 有人重播了历史 ⇒ 锚点静默移动（登记表 D1 说该换 source_id）。"""
    _res, _notes = gate.judge([row("scraw_600519_daily", 8, STOCK)])
    assert _res[0]["verdict"] == "shrunk"
    assert "D1" in _res[0]["detail"]


def test_index_accepts_not_applicable():
    got = verdicts([row("scraw_000300_daily", 10,
                        {"source": "index_zh_a_hist", "adjust": "not_applicable", "rows": 10})])
    assert got["scraw_000300_daily"] == "ok"


def test_class_comes_from_the_record_not_the_code_prefix():
    """`000001` 既是上证综指也是平安银行——按前缀猜类别会把个股当指数。"""
    got = verdicts([row("scraw_000001_daily", 10,
                        {"source": "stock_zh_a_hist", "adjust": "qfq", "rows": 10})])
    assert got["scraw_000001_daily"] == "ok"


def test_non_uniform_watermark_is_named_per_table():
    _res, notes = gate.judge([row("scraw_600519_daily", 10, STOCK, mx="2026-11-06"),
                              row("scraw_000333_daily", 10, STOCK, mx="2026-10-31")])
    line = [n for n in notes if "水位不统一" in n]
    assert line and "000333" in line[0] and "2026-10-31" in line[0]


def test_read_facts_reads_a_real_db_and_returns_nothing_when_no_price_tables():
    """扫到 0 张价格表时必须返回空 —— main 靠它退 2，不许读成「没有缺口」。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        con = sqlite3.connect(path)
        con.execute("create table scraw_aapl_daily (date text, close real)")
        con.commit()
        con.close()
        assert gate._read_facts(path) == []
    finally:
        os.unlink(path)


def test_read_facts_maps_config_by_astock_code_key():
    """配置键是 `astock_<code>`（种子与刷新的命名），不是表名。键错会全报 no_config。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        con = sqlite3.connect(path)
        con.execute("create table scraw_configs (name text, columns_json text)")
        con.execute("create table scraw_600519_daily (date text, close real)")
        con.executemany("insert into scraw_600519_daily values (?,?)",
                        [("2026-11-05", 1.0), ("2026-11-06", 2.0)])
        con.execute("insert into scraw_configs values (?,?)",
                    ("astock_600519", json.dumps(STOCK)))
        con.commit()
        con.close()
        facts = gate._read_facts(path)
        assert facts == [{"table": "scraw_600519_daily", "actual": 2,
                          "max_date": "2026-11-06",
                          "config": {"source": "stock_zh_a_hist", "adjust": "qfq",
                                     "rows": 10}}]
    finally:
        os.unlink(path)
