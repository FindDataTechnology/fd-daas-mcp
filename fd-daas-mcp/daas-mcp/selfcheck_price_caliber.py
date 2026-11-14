"""A 股价格表的**口径记录**（`scraw_configs`）与表本身是不是还对得上？

## 为什么需要它

研究取价走**通道 B**（本库的 `scraw_<code>_daily` + `scraw_configs`）。要让下游把
一条序列判成"可比/不可比"，返回时必须带上出处（`source_id` / `adjustment` /
`adjustment_anchor` / `as_of_max`）。**出处只能从 `scraw_configs` 取，不能猜**——
而这个记录本身没有任何东西在看着它：

* 新增一张价格表却忘了写 `scraw_configs` ⇒ 那张表的复权口径**无人知晓**，
  下游给它挂 ref 就是替一个没声明过的口径背书；
* `columns_json` 里的 `source`/`adjust` 与数据不符 ⇒ 记录在撒谎，比缺失更坏
  （缺了会发现，撒了谎看起来一切正常）；
* 有人用 `seed_astock_price.py --replace` 重播历史 ⇒ **实际行数会比记录里的
  `rows` 少**，也就是"历史被重写、锚点静默移动"（登记表 D1：换锚点 = 换口径 =
  换 source_id，不是原地改写历史行）。

`refresh_astock_to_today.py` 是**追加式**的（`INSERT OR IGNORE`，脚本头注明确
"existing rows are NEVER updated or deleted, and scraw_configs is NOT modified"），
所以「实际 ≥ 记录」是它成立的不变量；反过来一旦实际 < 记录，就说明有人动了历史。
这条不变量正是本闸 R3。

## 判据

* **R1** 每张 `scraw_<6 位>_daily` 必须有 `scraw_configs` 行（键 `astock_<code>`，
  与种子的命名一致）。
* **R2** 记录的 `source` 必须是已知的 akshare 端点，且 `adjust` 与它一致
  （`stock_zh_a_hist` ⇒ `qfq`；`index_zh_a_hist` ⇒ `none`/`not_applicable`）。
  **不看代码前缀判类别**——`000001` 既是上证综指也是平安银行（见
  `bare-code-collides-across-markets`），按前缀推断会把个股当指数。
* **R3** 记录里的 `rows` ≤ 实际行数。实际更少 ⇒ 历史被重写/删除 ⇒ 命中。
* **事实档**（不计命中）：水位 `max(date)` 各表不统一时逐表打印；`rows` 与实际
  的差额也打印（追加式刷新的常态，今天 `600519` 记 6022、实际 6037）。

**扫到 0 张价格表 ⇒ 判不出（退 2）**：本地开发库就是这样（只有美股/宏观表）。
"什么都没扫"不许读成"一个缺口都没有"。

## 用法

    python selfcheck_price_caliber.py                    # 缺省读仓根 daas.db（只读）
    python selfcheck_price_caliber.py --db /app/fd-daas-mcp/data/daas.db
    python selfcheck_price_caliber.py --self-test        # 不碰磁盘，9 条夹具

只读：`sqlite3` 以 `mode=ro` + `PRAGMA query_only=ON` 打开，不写盘、不改库。

    2026-11-15 实测（本闸的由来）：生产 `daas.db` 14 张价格表、14 行口径记录，
    12 只个股 `stock_zh_a_hist/qfq`、2 个指数 `index_zh_a_hist/none`，水位统一
    `2026-11-06`，R1–R3 全绿（`rows` 记录值普遍小于实际，属追加式刷新的常态）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

#: 价格表形状。只认 6 位代码的日线表；`scraw_aapl_daily` 这类美股表不归本闸管
#: （它们走另一套来源，重复噪音只会把真缺陷淹掉）。
PRICE_TABLE = re.compile(r"^scraw_(\d{6})_daily$")

#: akshare 端点 → 该端点应有的复权口径。**这是判据，不是文档**：出现表外端点
#: 就说明有人换了抓取路径而没有更新登记，此时 ref 的取舍没人能背书。
EXPECTED_ADJUST = {
    "stock_zh_a_hist": {"qfq"},
    "index_zh_a_hist": {"none", "not_applicable"},
}


def judge(facts: list[dict]) -> tuple[list[dict], list[str]]:
    """纯函数：`facts` 是每张表一条 {table, actual, max_date, config}。

    `config` 是解析后的 `columns_json`（None=没有记录行，str=解析失败）。
    返回 (per-table 结论, 事实行)。纯函数是刻意的：三档结论要能在没有库的情况下
    自证，否则"全都判红"与"判据写反"在输出上同形。
    """
    out: list[dict] = []
    notes: list[str] = []
    grown: list[tuple[str, int]] = []
    for f in facts:
        entry = {"table": f["table"], "actual": f["actual"],
                 "max_date": f.get("max_date")}
        cfg = f.get("config")
        if cfg is None:
            entry.update(verdict="no_config",
                         detail="没有 scraw_configs 行 —— 复权口径无人知晓，"
                                "下游无法为它生成 ref（先补记录再谈挂 ref）")
            out.append(entry)
            continue
        if isinstance(cfg, str):
            entry.update(verdict="bad_config",
                         detail=f"columns_json 解析失败（{cfg[:60]}）——记录不可读")
            out.append(entry)
            continue
        source = cfg.get("source")
        adjust = cfg.get("adjust")
        if source not in EXPECTED_ADJUST:
            entry.update(verdict="unknown_source",
                         detail=f"source={source!r} 不在已知端点表里（{sorted(EXPECTED_ADJUST)}）"
                                f" —— 换了抓取路径而没有更新登记")
            out.append(entry)
            continue
        if adjust not in EXPECTED_ADJUST[source]:
            entry.update(verdict="adjust_mismatch",
                         detail=f"source={source} 记为 adjust={adjust!r}，"
                                f"应为 {'/'.join(sorted(EXPECTED_ADJUST[source]))}"
                                f" —— 记录与数据口径不一致，比缺失更坏")
            out.append(entry)
            continue
        recorded = cfg.get("rows")
        if isinstance(recorded, int) and f["actual"] < recorded:
            entry.update(verdict="shrunk",
                         detail=f"实际 {f['actual']} 行 < 记录 rows={recorded}"
                                f" —— 历史被重写或删除（锚点静默移动）；"
                                f"按登记表 D1 应换 source_id，不是原地改")
            out.append(entry)
            continue
        entry.update(verdict="ok",
                     detail=f"{source}/{adjust}，实际 {f['actual']} 行"
                            f"{'' if not isinstance(recorded, int) else f'（记录 {recorded}）'}")
        if isinstance(recorded, int) and f["actual"] > recorded:
            grown.append((f["table"], f["actual"] - recorded))
        out.append(entry)
    # 追加式刷新会让「记录 rows」永远小于实际；逐表打一行会把真正的缺陷淹掉
    # （2026-11-15 首跑就是这样刷了 12 行同样的字），故聚合成一条。
    if grown:
        deltas = sorted({d for _, d in grown})
        notes.append(
            f"~ {len(grown)} 张表实际 > 记录 rows（追加式刷新的常态）："
            f"差额 {'/'.join(str(d) for d in deltas)} 行"
            f"{'（各表同期，说明是同一轮增量推上去的）' if len(deltas) == 1 else ''}；"
            f"记录里的 rows 是「种下时」的数，不是当前数")
    waters = {o["max_date"] for o in out if o.get("max_date")}
    if len(waters) > 1:
        notes.append("~ 水位不统一：" + "，".join(
            f"{o['table']}={o['max_date']}" for o in sorted(out, key=lambda x: x["table"])))
    elif waters:
        notes.append(f"~ 水位统一 {waters.pop()}（各表同期，说明是一轮种子+增量推上去的）")
    return out, notes


def _read_facts(db: str) -> list[dict]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only=ON")
        tables = [r[0] for r in con.execute(
            "select name from sqlite_master where type='table'")]
        cfgs = {}
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
            n = con.execute(f'select count(*) from "{t}"').fetchone()[0]
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{t}")')]
            mx = (con.execute(f'select max(date) from "{t}"').fetchone()[0]
                  if "date" in cols else None)
            raw = cfgs.get(f"astock_{code}")
            cfg = None
            if raw is not None:
                try:
                    cfg = json.loads(raw)
                except Exception as exc:  # noqa: BLE001
                    cfg = f"{type(exc).__name__}: {exc}"
            facts.append({"table": t, "actual": n, "max_date": mx, "config": cfg})
        return facts
    finally:
        con.close()


def _self_test() -> int:
    """9 条：绿（含追加）/无记录/记录坏/source 未知/口径不符/行数缩水/水位不统一/
    空库判不出/前缀不推断类别。"""
    ok = True

    def check(label, cond, extra=""):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"  {'✓' if cond else '✗'} {label}{'' if cond else f'  ← {extra}'}")

    def row(table, actual, cfg, mx="2026-11-06"):
        return {"table": table, "actual": actual, "max_date": mx, "config": cfg}

    good = {"source": "stock_zh_a_hist", "adjust": "qfq", "rows": 10}
    idx = {"source": "index_zh_a_hist", "adjust": "none", "rows": 10}

    v, notes = judge([row("scraw_600519_daily", 10, good), row("scraw_000300_daily", 10, idx)])
    check("个股 qfq + 指数 none ⇒ 全绿", all(x["verdict"] == "ok" for x in v),
          [x["verdict"] for x in v])
    check("水位统一只报一行事实", any("水位统一" in n for n in notes), notes)

    v, notes = judge([row("scraw_600519_daily", 12, good)])
    check("实际 > 记录（追加式）仍 ok 且聚合成一条事实行",
          v[0]["verdict"] == "ok"
          and sum("追加式" in n for n in notes) == 1, notes)

    v, _ = judge([row("scraw_600519_daily", 12, None)])
    check("无口径记录 ⇒ no_config（命中）", v[0]["verdict"] == "no_config")

    v, _ = judge([row("scraw_600519_daily", 12, "JSONDecodeError: boom")])
    check("columns_json 坏了 ⇒ bad_config", v[0]["verdict"] == "bad_config")

    v, _ = judge([row("scraw_600519_daily", 12,
                      {"source": "sina_realtime", "adjust": "qfq", "rows": 1})])
    check("来源换了没登记 ⇒ unknown_source", v[0]["verdict"] == "unknown_source")

    v, _ = judge([row("scraw_600519_daily", 12,
                      {"source": "stock_zh_a_hist", "adjust": "none", "rows": 1})])
    check("个股记成不复权 ⇒ adjust_mismatch", v[0]["verdict"] == "adjust_mismatch")

    v, _ = judge([row("scraw_600519_daily", 8, good)])
    check("实际 < 记录 ⇒ shrunk（历史被重写）", v[0]["verdict"] == "shrunk")

    v, notes = judge([row("scraw_600519_daily", 10, good, mx="2026-11-06"),
                      row("scraw_000333_daily", 10, good, mx="2026-10-31")])
    check("水位不统一 ⇒ 逐表点名", any("水位不统一" in n and "000333" in n for n in notes),
          notes)

    # 前缀不推断：000001 是平安银行（个股），记录说 stock 就必须按 stock 判
    v, _ = judge([row("scraw_000001_daily", 10,
                      {"source": "stock_zh_a_hist", "adjust": "qfq", "rows": 10})])
    check("000001 按记录判个股（不按前缀猜指数）", v[0]["verdict"] == "ok")

    # 空库 ⇒ 判不出（不是"没有缺口"）。用真文件走一遍 `_read_facts` + 退出码逻辑。
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        con = sqlite3.connect(path)
        con.execute("create table unrelated (x)")
        con.commit()
        con.close()
        empty = _read_facts(path)
        check("扫到 0 张价格表 ⇒ 判不出", empty == [], empty)
    finally:
        os.unlink(path)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None,
                    help="daas.db 路径（缺省=仓根 daas.db；生产在 /app/fd-daas-mcp/data/）")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()

    repo_root = Path(__file__).resolve().parent.parent.parent
    db = args.db or os.environ.get("DAAS_DB_PATH") or str(repo_root / "daas.db")
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
              f"「什么都没扫」不等于「没有缺口」", file=sys.stderr)
        return 2

    results, notes = judge(facts)
    hits = [r for r in results if r["verdict"] != "ok"]
    mark = {"ok": "✓", "no_config": "✗", "bad_config": "✗",
            "unknown_source": "✗", "adjust_mismatch": "✗", "shrunk": "✗"}
    print(f"{db}：{len(results)} 张价格表")
    for r in results:
        print(f"{mark[r['verdict']]} {r['table']} [{r['verdict']}]: {r['detail']}")
    for n in notes:
        print(n)
    if hits:
        print("\n改法：① 缺记录 ⇒ 用 seed_astock_price.py 重播该表（会写 scraw_configs）；"
              "② 记录与数据不符 ⇒ 先查是哪边错，别只改 json（改记录比改数据便宜，"
              "所以更容易改错）；③ 行数缩水 ⇒ 历史被重写过，按 D1 换 source_id 并在"
              "登记表里补新锚点。")
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
