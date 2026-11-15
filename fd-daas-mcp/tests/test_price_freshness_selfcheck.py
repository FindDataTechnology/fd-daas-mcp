"""`selfcheck_price_freshness.py` 的判据测试。

这道闸管的是「通道 B 的价格表**还在不在更新**」——与口径闸正交：
口径闸全绿与「数据是今天的」完全是两件事（2026-11-15 实测：14/14 口径齐全，
而水位冻了 5 个交易日，因为全集群没有任何刷新调度）。

三条判据要钉住：
1. **R1 组内漂移（日历无关）**：同一天收盘对所有 A 股标的一起生效，所以同组内
   水位必须完全一致；任何一张落后同组 max 就是缺陷，与今天几号无关。
2. **R2 组对今天（需要日历）**：组水位落后今天超过阈值个工作日记命中。R1 抓不到
   「整组一起停」，R2 抓不到「单表掉队」，两条都不多余。
3. **判不出不许退 0**：0 张价格表、库读不到、单表取不到水位，分别退 2 / 命中。

测试只碰 `judge` / `business_days_after` / `_read_facts` 与本地临时库：
不连生产 `daas.db`、不发网络请求。
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # -> fd-daas-mcp/
SCRIPT = os.path.join(REPO, "daas-mcp", "selfcheck_price_freshness.py")


def _load():
    name = "daas_price_freshness_selfcheck"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


gate = _load()


def row(table, mx, group="stock"):
    return {"table": table, "max_date": mx, "group": group}


def verdicts(out):
    return {o["table"]: o["verdict"] for o in out}


# ------------------------------------------------------------------ 工作日算法

def test_business_days_across_a_weekend():
    # 2026-11-13 是周五，11-14/15 是周末
    assert gate.business_days_after("2026-11-13", "2026-11-14") == 0
    assert gate.business_days_after("2026-11-13", "2026-11-15") == 0
    assert gate.business_days_after("2026-11-13", "2026-11-16") == 1


def test_business_days_full_week():
    # 11-06 周五 → 11-13 周五：11-09..11-13 共 5 个工作日
    assert gate.business_days_after("2026-11-06", "2026-11-13") == 5


def test_business_days_same_day_and_future():
    assert gate.business_days_after("2026-11-13", "2026-11-13") == 0
    # 未来水位（时钟不一致）不许算成负数
    assert gate.business_days_after("2026-11-20", "2026-11-15") == 0


def test_business_days_unparseable_is_none():
    assert gate.business_days_after("not-a-date", "2026-11-15") is None
    assert gate.business_days_after("2026-11-06", "") is None


# ------------------------------------------------------------------ R1 组内漂移

def test_sibling_lag_is_a_hit_and_names_the_laggard():
    out, _ = gate.judge(
        [row("scraw_600519_daily", "2026-11-13"),
         row("scraw_000333_daily", "2026-11-06")],
        today="2026-11-14")
    v = verdicts(out)
    assert v["scraw_600519_daily"] == "ok"
    assert v["scraw_000333_daily"] == "lag_sibling"


def test_sibling_criterion_needs_no_calendar():
    """水位一致就绿，哪怕那个水位是一年前的——R2 才管日历，R1 不管。

    这条是刻意的：把日历揉进组内比较，长假期间会得到「全组红」，而那时其实
    没有任何一张表掉队。
    """
    out, _ = gate.judge(
        [row("scraw_600519_daily", "2025-01-02"),
         row("scraw_000333_daily", "2025-01-02")],
        today="2025-01-03")  # 相对水位是「今天」「昨天」，都新鲜
    assert all(o["verdict"] == "ok" for o in out), out


def test_different_groups_are_judged_separately():
    """指数组落后个股组不算组内漂移——两个端点、两个来源，跨组不一致合法。"""
    out, _ = gate.judge(
        [row("scraw_600519_daily", "2026-11-13"),
         row("scraw_000300_daily", "2026-11-13", "index")],
        today="2026-11-14")
    assert all(o["verdict"] == "ok" for o in out), out


# ------------------------------------------------------------------ R2 整组停摆

def test_stale_group_when_whole_group_stops():
    out, _ = gate.judge(
        [row("scraw_600519_daily", "2026-11-06"),
         row("scraw_000333_daily", "2026-11-06")],
        today="2026-11-15", max_lag_bdays=3)
    v = verdicts(out)
    assert v["<group stock>"] == "stale_group"
    # R1 此时不该响（组内一致）
    assert "lag_sibling" not in v.values()


def test_threshold_boundary_is_exclusive():
    """落后 == 阈值不红，阈值 +1 红（边界只放一边，写反会让闸早一天响）。"""
    at = gate.judge([row("scraw_600519_daily", "2026-11-06")],
                    today="2026-11-11", max_lag_bdays=3)[0]  # 3 个工作日
    over = gate.judge([row("scraw_600519_daily", "2026-11-06")],
                      today="2026-11-12", max_lag_bdays=3)[0]  # 4 个工作日
    assert not [o for o in at if o["verdict"] == "stale_group"], at
    assert [o for o in over if o["verdict"] == "stale_group"], over


def test_holiday_tolerance_is_an_explicit_knob():
    """长假放宽必须是显式参数，而不是把缺省调大（调大就再也抓不到停摆）。"""
    args = dict(today="2026-11-15")
    facts = [row("scraw_600519_daily", "2026-11-06")]  # 落后 5 个工作日
    assert [o for o in gate.judge(facts, **args)[0]
            if o["verdict"] == "stale_group"]
    assert not [o for o in gate.judge(facts, max_lag_bdays=6, **args)[0]
                if o["verdict"] == "stale_group"]


def test_fresh_run_still_prints_the_lag_trend_line():
    """全绿时也要打出落后的工作日数——那是趋势线，涨上去之前没人会注意到。"""
    out, notes = gate.judge([row("scraw_600519_daily", "2026-11-13")],
                            today="2026-11-14")
    assert all(o["verdict"] == "ok" for o in out)
    assert any("落后今天" in n for n in notes), notes


# ------------------------------------------------------------------ 三态

def test_no_config_means_no_group_and_is_a_hit():
    """没有口径记录 ⇒ 不知道该跟谁比。跳过它等于把它变成盲区。"""
    out, _ = gate.judge([{"table": "scraw_600519_daily",
                          "max_date": "2026-11-13", "group": None}],
                        today="2026-11-14")
    assert verdicts(out)["scraw_600519_daily"] == "no_group"


def test_missing_watermark_is_a_hit():
    """空表 / 取不到 max(date)：这正是「这张表不归任何人管」的形态。"""
    out, _ = gate.judge([{"table": "scraw_600519_daily",
                          "max_date": None, "group": "stock"}],
                        today="2026-11-14")
    assert verdicts(out)["scraw_600519_daily"] == "no_watermark"


def test_empty_db_reads_as_no_facts_so_main_exits_two():
    """0 张价格表 ⇒ `_read_facts` 返回空 ⇒ main 退 2（不是「没有缺口」）。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        con = sqlite3.connect(path)
        con.execute("create table unrelated (x)")
        con.commit()
        con.close()
        assert gate._read_facts(path) == []
    finally:
        os.unlink(path)


def test_group_is_derived_from_the_caliber_record_not_the_code_prefix():
    """`000001` 既是上证综指也是平安银行——组必须来自记录，不能按前缀猜。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        con = sqlite3.connect(path)
        con.execute('create table scraw_000001_daily (date text, Close real)')
        con.execute("insert into scraw_000001_daily values ('2026-11-13', 1.0)")
        con.execute("create table scraw_configs (name text, columns_json text)")
        con.execute("insert into scraw_configs values "
                    "('astock_000001', '{\"source\": \"stock_zh_a_hist\", "
                    "\"adjust\": \"qfq\"}')")
        con.commit()
        con.close()
        facts = gate._read_facts(path)
        assert facts == [{"table": "scraw_000001_daily",
                          "max_date": "2026-11-13", "group": "stock"}], facts
    finally:
        os.unlink(path)
