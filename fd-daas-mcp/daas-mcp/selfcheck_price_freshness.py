"""A 股价格表**还在不在更新**？（change1 task 4.4 的供给面判据）

## 为什么这条必须单独有闸

`selfcheck_price_caliber.py` 判的是**口径**（记录在不在、记录与数据符不符、历史有没
有被重写）——它全绿与「数据是今天的」完全是两件事。2026-11-15 实测就是这种形态：

* 14 张价格表口径记录 14/14 齐全、R1–R3 全绿；
* 而**全集群没有任何 CronJob 跑 `refresh_astock_to_today.py`**（grep 五套集群 +
  逐仓清单：零命中）⇒ 水位从 **2026-11-06 冻到 2026-11-15**，整整 5 个交易日；
* 同一天 `get_series_latest` 返回的价格看着完全正常，只是**是 9 天前的**。

口径闸看不见这件事，因为「没刷新」不改变任何一条记录。`series_ref` 会带
`as_of_max`，但那要下游自己去比今天——**闸不能指望下游**。

## 两条判据，一条不需要日历，一条需要

* **R1 组内漂移（不需要日历，最灵）**：同一口径组（`stock_zh_a_hist` /
  `index_zh_a_hist`）里的表，水位必须**完全一致**。A 股同一交易日对所有 A 股标的一起
  收盘，所以任何一张表落后同组 max 就是缺陷——**与今天几号、是不是长假都无关**。
  这一条抓的正是「族级新鲜度藏住死掉的实体类型」的反面：只看族 max 会看不见单表
  死掉，只看单表绝对值又会被日历搅乱，所以两件分开判。
* **R2 组对今天（需要日历，抓整体停摆）**：组水位落后今天多少个**工作日**，
  超过 `--max-lag-bdays`（缺省 3）即命中。R1 抓不到「整组一起停」，R2 抓不到
  「单表掉队」，两条都不多余。

**R2 的日历边界（说清楚，别让它变成常年红闸）**：没有交易日历，只按周一到周五算。
春节/国庆那种一周以上的休市会让它**每天都红**，此时用 `--max-lag-bdays` 显式放宽
并把日期记进当日记录——**放宽是一次有日期的决定，不是把阈值改成 30 就完事**
（改完就再也抓不到停摆了）。反过来，把缺省调大同样有害：3 个工作日意味着最长
一次停摆 3 天后就响，而长假一年只有两三次。

## 三态

扫到 0 张价格表、读不了库、某张表取不到水位 ⇒ **判不出（退 2）**，不许把
「什么都没查」读成「都在更新」。单表取不到水位是 `no_watermark`（命中），
因为它恰恰是「这张表不归任何人管」的形态。

## 用法

    python selfcheck_price_freshness.py                       # 缺省读仓根 daas.db（只读）
    python selfcheck_price_freshness.py --db /app/fd-daas-mcp/data/daas.db
    python selfcheck_price_freshness.py --today 2026-11-06    # 虚拟今天（红证，不改数据）
    python selfcheck_price_freshness.py --self-test

只读：`sqlite3` 以 `mode=ro` + `PRAGMA query_only=ON` 打开。

    2026-11-15 实跑（补完 12 张股票表之后）：个股组 2026-11-13（落后 0 个工作日）绿；
    指数组 2026-11-06（落后 5 个工作日）红 —— 因为唯一路由 `index_zh_a_hist`
    是 eastmoney、本机与集群都连不上，**没有偷偷换路由**（换路由而不改
    `scraw_configs` 就是替没登记过的出处背书）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

#: 价格表形状。只认 6 位代码的日线表（与口径闸同一口径，理由同源）。
PRICE_TABLE = re.compile(r"^scraw_(\d{6})_daily$")

#: 口径端点 → 组名。组是**判据的单位**：同日收盘把所有 A 股标的一起推进，
#: 所以同组内水位必须一致；跨组不一致是合法的（两个端点、两个来源）。
GROUP_BY_SOURCE = {
    "stock_zh_a_hist": "stock",
    "index_zh_a_hist": "index",
}

DEFAULT_MAX_LAG_BDAYS = 3

CN_TZ = dt.timezone(dt.timedelta(hours=8))


def business_days_after(start: str, today: str) -> int | None:
    """`start` 之后到 `today`（含）之间的工作日数。解析失败返回 None（判不出）。"""
    try:
        d0 = dt.date.fromisoformat(start[:10])
        d1 = dt.date.fromisoformat(today[:10])
    except (TypeError, ValueError):
        return None
    if d1 < d0:
        return 0  # 水位在未来：时钟不一致，按 0 落后处理，由调用方另计事实行
    n, cur = 0, d0 + dt.timedelta(days=1)
    while cur <= d1:
        if cur.weekday() < 5:
            n += 1
        cur += dt.timedelta(days=1)
    return n


def judge(facts: list[dict], today: str,
          max_lag_bdays: int = DEFAULT_MAX_LAG_BDAYS) -> tuple[list[dict], list[str]]:
    """纯函数。`facts` 每条 {table, max_date, group}；group 来自口径记录。

    返回 (结论列表, 事实行)。结论 `verdict` ∈ ok / lag_sibling / stale_group /
    no_watermark / no_group。纯函数是刻意的：三档要能在没有库的情况下自证，
    否则「全都判红」与「判据写反」在输出上同形。
    """
    out: list[dict] = []
    notes: list[str] = []

    # 分组。没有口径记录的表进不了任何组——它连「该跟谁比」都不知道。
    groups: dict[str, list[dict]] = {}
    for f in facts:
        g = f.get("group")
        if g is None:
            out.append({"table": f["table"], "verdict": "no_group",
                        "detail": "没有口径记录 ⇒ 不知道它属于哪个端点组，"
                                  "无法判断它该跟谁对齐（先补记录，见口径闸）"})
            continue
        if not f.get("max_date"):
            out.append({"table": f["table"], "verdict": "no_watermark",
                        "detail": "取不到 max(date) —— 这张表不归任何人管"
                                  "（空表也是这个形态）"})
            continue
        groups.setdefault(g, []).append(f)

    for g, members in sorted(groups.items()):
        waters = {m["max_date"] for m in members}
        gmax = max(waters)
        # R1：同组内水位必须完全一致（日历无关）
        for m in members:
            if m["max_date"] != gmax:
                lag = business_days_after(m["max_date"], gmax)
                out.append({
                    "table": m["table"], "verdict": "lag_sibling",
                    "detail": f"组 {g} 内水位 {m['max_date']} 落后同组 max {gmax}"
                              f"（{lag} 个工作日）—— 同一天收盘对所有成员一起生效，"
                              f"落后即该表没被刷新"})
            else:
                out.append({"table": m["table"], "verdict": "ok",
                            "detail": f"组 {g} 水位 {gmax}，与同组一致"})
        # R2：整组对今天（需要日历）
        lag = business_days_after(gmax, today)
        if lag is None:
            out.append({"table": f"<group {g}>", "verdict": "no_watermark",
                        "detail": f"水位 {gmax!r} 或今天 {today!r} 解析失败 —— 判不出"})
            continue
        if gmax > today[:10]:
            notes.append(f"~ 组 {g} 水位 {gmax} 晚于今天 {today}（时钟不一致），"
                         f"按落后 0 处理但事实行要留痕")
        elif lag > max_lag_bdays:
            out.append({
                "table": f"<group {g}>", "verdict": "stale_group",
                "detail": f"组水位 {gmax} 落后今天 {today} 共 {lag} 个工作日 "
                          f"> 阈值 {max_lag_bdays} —— 整组停摆（查刷新调度在不在）"})
        else:
            notes.append(f"~ 组 {g} 水位 {gmax}，落后今天 {today} 共 {lag} 个工作日"
                         f"（阈值 {max_lag_bdays}）")

    return out, notes


def _read_facts(db: str) -> list[dict]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only=ON")
        tables = [r[0] for r in con.execute(
            "select name from sqlite_master where type='table'")]
        cfgs: dict[str, str] = {}
        try:
            for name, cols in con.execute("select name, columns_json from scraw_configs"):
                cfgs[name] = cols
        except sqlite3.Error:
            cfgs = {}
        facts = []
        for t in sorted(tables):
            m = PRICE_TABLE.match(t)
            if not m:
                continue
            code = m.group(1)
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{t}")')]
            mx = (con.execute(f'select max(date) from "{t}"').fetchone()[0]
                  if "date" in cols else None)
            group = None
            raw = cfgs.get(f"astock_{code}")
            if raw:
                try:
                    group = GROUP_BY_SOURCE.get(json.loads(raw).get("source"))
                except Exception:  # noqa: BLE001
                    group = None
            facts.append({"table": t, "max_date": str(mx) if mx else None,
                          "group": group})
        return facts
    finally:
        con.close()


def _count_other_daily(db: str) -> int:
    """`scraw_*_daily` 里不归本闸管的张数。排除项看不见最坑人，故可数。"""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return sum(1 for (t,) in con.execute(
            "select name from sqlite_master where type='table' "
            "and name like 'scraw_%_daily'") if not PRICE_TABLE.match(t))
    finally:
        con.close()


def _self_test() -> int:
    """10 条自证：工作日算对（含跨周末/同日/未来）、组内漂移、整组停摆、
    绿、no_group、no_watermark、阈值边界（==阈值不红、阈值+1 红）、空库判不出。"""
    ok = True

    def check(label, cond, extra=""):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"  {'✓' if cond else '✗'} {label}{'' if cond else f'  ← {extra}'}")

    def row(table, mx, group="stock"):
        return {"table": table, "max_date": mx, "group": group}

    check("工作日：周五→周六 = 0", business_days_after("2026-11-13", "2026-11-14") == 0)
    check("工作日：周五→周一 = 1", business_days_after("2026-11-13", "2026-11-16") == 1)
    check("工作日：周五→下周五 = 5", business_days_after("2026-11-06", "2026-11-13") == 5)
    check("工作日：同日 = 0", business_days_after("2026-11-13", "2026-11-13") == 0)
    check("工作日：未来日期 = 0（不当负）",
          business_days_after("2026-11-20", "2026-11-15") == 0)
    check("工作日：解析失败 = None", business_days_after("not-a-date", "2026-11-15") is None)

    # R1：组内一张掉队
    out, _ = judge([row("scraw_600519_daily", "2026-11-13"),
                    row("scraw_000333_daily", "2026-11-06")],
                   today="2026-11-14", max_lag_bdays=3)
    lag = [o for o in out if o["verdict"] == "lag_sibling"]
    check("R1 组内掉队 ⇒ lag_sibling 且点名落后那张",
          len(lag) == 1 and lag[0]["table"] == "scraw_000333_daily", lag)

    # R2：整组停摆（水位一致但都旧）
    out, _ = judge([row("scraw_600519_daily", "2026-11-06"),
                    row("scraw_000333_daily", "2026-11-06")],
                   today="2026-11-15", max_lag_bdays=3)
    stale = [o for o in out if o["verdict"] == "stale_group"]
    check("R2 整组停摆 ⇒ stale_group（R1 此时不该响）",
          len(stale) == 1 and not [o for o in out if o["verdict"] == "lag_sibling"], out)

    # 绿
    out, notes = judge([row("scraw_600519_daily", "2026-11-13"),
                        row("scraw_000300_daily", "2026-11-13", "index")],
                       today="2026-11-14", max_lag_bdays=3)
    check("全新鲜 ⇒ 全部 ok", all(o["verdict"] == "ok" for o in out), out)
    check("绿时仍打落后事实行（趋势线）", any("落后今天" in n for n in notes), notes)

    # 阈值边界：恰好等于阈值不红，阈值+1 红
    out, _ = judge([row("scraw_600519_daily", "2026-11-06")],
                   today="2026-11-11", max_lag_bdays=3)  # 11-09/10/11 = 3
    check("落后 == 阈值 ⇒ 不红", not [o for o in out if o["verdict"] == "stale_group"], out)
    out, _ = judge([row("scraw_600519_daily", "2026-11-06")],
                   today="2026-11-12", max_lag_bdays=3)  # 4 个工作日
    check("落后 == 阈值+1 ⇒ 红", [o for o in out if o["verdict"] == "stale_group"], out)

    # 没有口径记录 ⇒ 不知道该跟谁比
    out, _ = judge([{"table": "scraw_600519_daily", "max_date": "2026-11-13",
                     "group": None}], today="2026-11-14")
    check("无口径记录 ⇒ no_group（命中，不是跳过）",
          out[0]["verdict"] == "no_group", out)

    # 取不到水位 ⇒ 命中
    out, _ = judge([{"table": "scraw_600519_daily", "max_date": None,
                     "group": "stock"}], today="2026-11-14")
    check("取不到水位 ⇒ no_watermark（命中）",
          out[0]["verdict"] == "no_watermark", out)

    # 空库 ⇒ 判不出（用真文件走 _read_facts）
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        con = sqlite3.connect(path)
        con.execute("create table unrelated (x)")
        con.commit()
        con.close()
        check("扫到 0 张价格表 ⇒ 判不出", _read_facts(path) == [], _read_facts(path))
    finally:
        os.unlink(path)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None,
                    help="daas.db 路径（缺省=仓根 daas.db；生产在 /app/fd-daas-mcp/data/）")
    ap.add_argument("--today", default=None,
                    help="虚拟今天（YYYY-MM-DD，用于红证；缺省=北京时间今天）")
    ap.add_argument("--max-lag-bdays", type=int, default=DEFAULT_MAX_LAG_BDAYS,
                    help=f"整组对今天允许落后的工作日数（缺省 {DEFAULT_MAX_LAG_BDAYS}；"
                         f"长假期间必须显式放宽并记录日期）")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()

    repo_root = Path(__file__).resolve().parent.parent.parent
    db = args.db or os.environ.get("DAAS_DB_PATH") or str(repo_root / "daas.db")
    today = args.today or dt.datetime.now(CN_TZ).date().isoformat()
    if not os.path.isfile(db):
        print(f"判不出（EXIT=2）：找不到库 {db}", file=sys.stderr)
        return 2
    try:
        facts = _read_facts(db)
    except sqlite3.Error as exc:
        print(f"判不出（EXIT=2）：读不了 {db}：{exc}", file=sys.stderr)
        return 2
    if not facts:
        print(f"判不出（EXIT=2）：{db} 里没有 scraw_<6位>_daily 表 —— "
              f"「什么都没扫」不等于「都在更新」", file=sys.stderr)
        return 2

    results, notes = judge(facts, today, args.max_lag_bdays)
    hits = [r for r in results if r["verdict"] != "ok"]
    mark = {"ok": "✓", "lag_sibling": "✗", "stale_group": "✗",
            "no_watermark": "✗", "no_group": "✗"}
    print(f"{db}（今天={today}，阈值={args.max_lag_bdays} 工作日）："
          f"{len(results)} 条结论")
    for r in results:
        print(f"{mark[r['verdict']]} {r['table']} [{r['verdict']}]: {r['detail']}")
    for n in notes:
        print(n)
    other = _count_other_daily(db)
    if other:
        print(f"~ 另有 {other} 张非 6 位代码的 scraw_*_daily 未纳入本闸（美股/宏观）")
    if hits:
        print("\n改法：① 单表掉队 ⇒ 只刷那一张（refresh_astock_to_today.py --apply "
              "--symbols <code>），别整批重播；② 整组停摆 ⇒ 查刷新作业在不在"
              "（不是改阈值）；③ 该组唯一路由不可达 ⇒ 换路由必须同笔改 "
              "scraw_configs 的 source，否则 ref 会替没登记过的出处背书；"
              "④ 万一当前是长假，用 --max-lag-bdays 放宽并记下日期。")
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
