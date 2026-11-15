"""Incrementally top up EXISTING A-share scraw tables to today (channel B).

Read-only by default. Two explicit modes:

  * ``--check`` (default): report, per table, max(date), the existing adjustment
    caliber from scraw_configs, and how many NEW rows a fresh akshare fetch
    would add. Touches NOTHING on disk -- the DB is opened with sqlite
    ``mode=ro`` + ``PRAGMA query_only=ON``. No writes, no new tables, no
    caliber changes.
  * ``--apply``: perform the inserts. For each table, re-fetch with the SAME
    akshare route (sp.fetch_bars, identical to seed_astock_price.py), then
    ``INSERT OR IGNORE`` on the ``date`` PRIMARY KEY. Existing rows are NEVER
    updated or deleted, and scraw_configs (the caliber record) is NOT modified.

Design constraints honored:

  * do NOT rebuild tables and do NOT modify historical rows  -> ``INSERT OR
    IGNORE`` (NOT the ``ON CONFLICT DO UPDATE`` used by the original seed's
    sp._upsert_bars); only rows with date > max(date) are inserted;
  * preserve the existing caliber record                     -> scraw_configs is
    read (for the fetch's ``adjust``) but never written;
  * same akshare route as the original seed                  -> sp.fetch_bars;
  * receive the DB path from ``--db`` (default: local authoritative library);
  * no credentials hardcoded; clear per-table failure messages.

Only ``--apply`` opens a writable connection. Index tables (created by
seed_astock_index.py, caliber source ``index_zh_a_hist``) are excluded so a
refresh never sends an index code down the stock routes.

Reproducible run (fd-paper-trading venv, cwd-agnostic):

    python refresh_astock_to_today.py --check             # dry-run report
    python refresh_astock_to_today.py --apply             # actually write
    python refresh_astock_to_today.py --check --symbols 600519,000001
    python refresh_astock_to_today.py --db /path/daas.db --apply

NOTE (qfq caveat): forward-adjusted (qfq) history is anchored at the latest
price; a corporate action between max(date) and today can make already-stored
historical qfq values stale. This script deliberately does NOT rebase history
(the task forbids touching historical rows). If a split/dividend occurred,
re-seed that table with ``seed_astock_price.py --replace`` instead.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

_THIS = Path(__file__).resolve()
_DAAS_MCP = _THIS.parent
_FD_DAAS_MCP = _DAAS_MCP.parent
for _p in (str(_FD_DAAS_MCP), str(_DAAS_MCP)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from dotenv import load_dotenv
    load_dotenv(_FD_DAAS_MCP.parent / ".env")
    load_dotenv(_DAAS_MCP / ".env", override=True)
except ImportError:
    pass

import seed_astock_price as sp  # noqa: E402  (reuse fetch_bars / _table_name / _clear_proxy)

DEFAULT_DB = "/Users/chengsishi/code/DAAS/daas.db"
DEFAULT_ADJUST = "qfq"   # used only when a table has NO caliber record (warn)
INDEX_CODES = {"000300", "000905"}   # belt-and-suspenders: never refresh index codes


def _db_path(db_arg: str) -> str:
    """Normalize --db to an absolute filesystem path (also accepts sqlite:/// URLs)."""
    s = db_arg
    if s.startswith("sqlite:////"):
        return s[len("sqlite:///"):]
    if s.startswith("sqlite:///"):
        return os.path.abspath(s[len("sqlite:///"):])
    return os.path.abspath(os.path.expanduser(s))


def _connect(db_arg: str, readonly: bool) -> sqlite3.Connection:
    path = _db_path(db_arg)
    if not os.path.isfile(path):
        raise SystemExit(
            f"ERROR: DB file not found: {path}\n"
            "       pass an existing sqlite file via --db"
        )
    if readonly:
        con = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
        con.execute("PRAGMA query_only=ON")
    else:
        con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def _caliber_info(con: sqlite3.Connection, code: str) -> tuple[str | None, str | None]:
    """Read (adjust, source) from the astock_<code> scraw_configs row.

    Mirrors sp._caliber_of but over a raw sqlite3 connection (so --check stays
    strictly read-only) and also returns the akshare source label, which tells
    an index table (index_zh_a_hist) apart from a stock table (stock_zh_a_hist).
    """
    row = con.execute(
        "SELECT columns_json FROM scraw_configs WHERE name = ? "
        "ORDER BY id DESC LIMIT 1",
        (f"astock_{code}",),
    ).fetchone()
    if row is None or row[0] is None:
        return None, None
    try:
        cfg = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    except (TypeError, ValueError):
        return None, None
    return cfg.get("adjust"), cfg.get("source")


def _classify(con: sqlite3.Connection, code: str) -> tuple[bool, str | None, str]:
    """-> (is_stock, adjust_to_fetch_with, note)."""
    if code in INDEX_CODES:
        return False, None, "known index code (excluded)"
    adjust, source = _caliber_info(con, code)
    if source == "index_zh_a_hist":
        return False, None, "index table (source=index_zh_a_hist) -- excluded"
    if adjust is None:
        return True, DEFAULT_ADJUST, f"no caliber record; defaulting to {DEFAULT_ADJUST}"
    return True, adjust, f"caliber={adjust}"


def _discover_codes(con: sqlite3.Connection) -> list[str]:
    """All 6-digit-numeric scraw_<code>_daily tables (A-share shape)."""
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name LIKE 'scraw_%_daily' ORDER BY name"
    ).fetchall()
    out: list[str] = []
    for r in rows:
        name = r[0]
        inner = name[len("scraw_"):-len("_daily")]
        if len(inner) == 6 and inner.isdigit():
            out.append(inner)
    return out


def _max_date(con: sqlite3.Connection, code: str) -> tuple[int, str | None, bool]:
    """-> (row_count, max(date), table_exists). Empty table -> (0, None, True)."""
    tbl = sp._table_name(code)                       # reused naming
    exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
    ).fetchone()
    if not exists:
        return 0, None, False
    n, dmax = con.execute(
        f'SELECT count(*), max(date) FROM "{tbl}"'
    ).fetchone()
    return n, dmax, True


def _fetch_new_bars(code: str, adjust: str, dmax: str | None,
                    end_compact: str) -> tuple[list[dict], list[dict]]:
    """Reuse sp.fetch_bars (sina->eastmoney->tencent) starting at max(date)."""
    start_compact = (dmax or "1990-01-01").replace("-", "")
    bars = sp.fetch_bars(code, adjust, start_compact, end_compact)
    # Only rows strictly after max(date) are candidates; INSERT OR IGNORE would
    # skip the boundary row anyway, but filtering makes the count exact and
    # guarantees no historical row is even presented for an update.
    new = [b for b in bars if dmax is None or b["date"] > dmax]
    return bars, new


#: 接缝容差：新旧两份数据在**同一个交易日**上的收盘价允许差多少。
#: 1% 是刻意紧的：同一标的同一天的收盘价只有一种，差 0.5% 就说明两份序列
#: 不是同一个复权基准（口径已经分叉），而不是"抓取抖动"。
SEAM_TOL = 0.01

#: 浮点边界补偿。`101/100` 在二进制里不是精确的 1.01，`abs(1.01-1.0)` 会算出
#: `0.010000000000000009 > 0.01`，于是"恰好 1%"被拒 —— 与常量名承诺的语义
#: 相反，而这种偏差只在个别数值上出现（我第一版就在自证里撞到）。
SEAM_EPS = 1e-9

#: volume 量纲容差（比值区间）。sina 的 volume 是**股**、tencent 是**手**（差
#: 100 倍）、东财也是手 —— 换路由时如果不在这一层拦住，新行会以另一种量纲接在
#: 老历史上，**价格列没事、成交量列静默错 100 倍**，而任何"非正/跳变"式质量门
#: 都看不见它（成交量不在 `_validate_series` 的判据里）。
SEAM_VOL_LOW = 1 / 5
SEAM_VOL_HIGH = 5


def _check_seam(con: sqlite3.Connection, code: str, dmax: str | None,
                bars: list[dict]) -> str | None:
    """新旧两份数据在接缝那一天是否一致。返回原因字符串或 None（一致）。

    ## 为什么这条必须存在

    `sp._validate_series` 只会在**本次抓到的窗口内部**判非正与 >±120% 跳变。
    窗口是从 `dmax` 开始的，所以它能看见 11-13 → 11-14 这种跳变，**看不见
    「11-13 这一行，我抓到的值和库里存的值不一样」**——而后者才是真正的
    口径漂移：前复权在分红后会被整体重算（本脚本头注把这种情形列为止损点是
    `--replace` 重播），换一条路由（sina ↔ tencent）也可能在接缝上带进
    细微差异（两个适配器的 qfq 基准不同：sina 乘性、tencent 加性）。

    法律上这是一张价格表，接缝错 0.5% 不会让任何一行看起来"非正"或"跳变"，
    但它让**跨接缝的任何收益率计算**带上一个假跳空——而这条路径正是研究取价
    拿去做回测的输入。所以在写入之前拿**库里存的那一天**当锚，比一比。

    返回 `None` 表示一致；否则返回形如
    `seam_mismatch: 2026-11-13 存 1453 抓 1420（-2.27%，容差 ±1%）` 的原因串，
    调用方据此跳过该表并列进汇总（不写一行）。

    **注意容差量级**：1% 意味着 `1441.2 vs 1453`（-0.81%）这种差异是**放行**的。
    这不是漏判——同一来源不同批次的末位差就在这个量级；真正的口径分叉（分红后
    整体重算）通常是百分位以上。我第一版把 -0.81% 写进 docstring 当"分叉示例"，
    与常量自相矛盾，实测会放行才发现。
    """
    if dmax is None:
        return None  # 空表：整段都是新数据，没有接缝可比
    row = con.execute(
        f'SELECT "Close", "Volume" FROM "{sp._table_name(code)}" WHERE "date" = ?',
        (dmax,),
    ).fetchone()
    if row is None or not row[0]:
        return None  # 水位行读不到收盘价：判不出接缝，交给质量门，不在此拦
    stored, stored_vol = float(row[0]), float(row[1] or 0)
    fetched = next((b for b in bars if b["date"] == dmax), None)
    if fetched is None:
        # 抓回来的窗口里没有接缝那一天：可能是停牌、也可能是路由返回不完整。
        # 这不是"一致"，但也不该在这儿判死——让新增行照走，留一条可见事实。
        return f"seam_unverifiable: 抓取结果里没有 {dmax}（停牌或路由不完整？）"
    got = float(fetched["close"])
    if stored == 0:
        return None
    ratio = got / stored
    if abs(ratio - 1.0) > SEAM_TOL + SEAM_EPS:
        return (f"seam_mismatch: {dmax} 存 {stored:g} 抓 {got:g}"
                f"（{ratio - 1:+.2%}，容差 ±{SEAM_TOL:.0%}）"
                f" —— 复权基准已分叉（分红后需 --replace 重播），拒绝追加")
    # volume 量纲：同一个交易日的成交量同源只该有**一种量纲**。sina=股、
    # tencent/东财=手，差 100 倍；换路由而不拦，价格列看起来完全正常，
    # 成交量列从此混两种单位，而任何"非正/跳变"质量门都看不见成交量。
    got_vol = float(fetched.get("volume") or 0)
    if stored_vol > 0 and got_vol > 0:
        vratio = got_vol / stored_vol
        if not (SEAM_VOL_LOW - SEAM_EPS <= vratio <= SEAM_VOL_HIGH + SEAM_EPS):
            return (f"seam_volume_scale: {dmax} 存 {stored_vol:g} 抓 {got_vol:g}"
                    f"（×{vratio:.3g}，容许 {SEAM_VOL_LOW:.2g}~{SEAM_VOL_HIGH:g}）"
                    f" —— 换了量纲（股 vs 手差 100 倍），拒绝追加")
    return None



def _apply_insert(con: sqlite3.Connection, code: str, new_bars: list[dict]) -> int:
    """INSERT OR IGNORE on the `date` PK. Returns rows actually inserted."""
    tbl = sp._table_name(code)
    before = con.total_changes
    con.executemany(
        f'INSERT OR IGNORE INTO "{tbl}" (date, Open, High, Low, Close, Volume) '
        "VALUES (:d, :o, :h, :l, :c, :v)",
        [
            {"d": b["date"], "o": b["open"], "h": b["high"],
             "l": b["low"], "c": b["close"], "v": b["volume"]}
            for b in new_bars
        ],
    )
    return con.total_changes - before


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Incrementally top up existing A-share scraw tables to today.")
    ap.add_argument("--db", default=DEFAULT_DB,
                    help=f"sqlite DB path (default {DEFAULT_DB})")
    ap.add_argument("--symbols",
                    help="comma-separated 6-digit A-share codes; "
                         "default = auto-discover all existing A-share tables")
    ap.add_argument("--check", action="store_true",
                    help="dry-run report only (default; strictly read-only)")
    ap.add_argument("--apply", action="store_true",
                    help="actually INSERT OR IGNORE new rows (the only mode that writes)")
    ap.add_argument("--end-date",
                    help="YYYYMMDD override for 'today' (reproducibility/testing)")
    args = ap.parse_args()

    if args.check and args.apply:
        ap.error("--check and --apply are mutually exclusive")
    mode = "apply" if args.apply else "check"

    end_compact = args.end_date or date.today().strftime("%Y%m%d")
    if len(end_compact) != 8 or not end_compact.isdigit():
        ap.error(f"--end-date must be YYYYMMDD, got {end_compact!r}")

    codes: list[str] = []
    if args.symbols:
        codes = [c.strip() for c in args.symbols.split(",") if c.strip()]
        bad = [c for c in codes if not (len(c) == 6 and c.isdigit())]
        if bad:
            ap.error(f"--symbols codes must be 6 digits: {bad}")

    readonly = (mode == "check")
    con = _connect(args.db, readonly=readonly)
    errors = 0
    total_planned = 0
    total_written = 0
    seamed = 0  # 被接缝守卫拦下的表数（口径/量纲对不上，一行没写）
    try:
        if not codes:
            codes = _discover_codes(con)
        if not codes:
            print("没有发现任何 6 位数字的 A 股 scraw 表；无需补数。")
            return 0

        print(f"[refresh] DB={_db_path(args.db)}  mode={mode}  "
              f"end_date={end_compact}  tables={len(codes)}")
        if mode == "check":
            print("[refresh] --check 只读：DB 以 mode=ro + query_only=ON 打开，"
                  "不写任何行、不碰 scraw_configs。")
        else:
            print("[refresh] --apply 将写入：仅对 date>max(date) 的新行做 "
                  "INSERT OR IGNORE；历史行与 scraw_configs(caliber) 均不改。")
        sp._clear_proxy()                                # reused proxy handling

        for code in codes:
            is_stock, adjust, note = _classify(con, code)
            if not is_stock:
                print(f"[skip] {code}: {note}")
                continue
            n, dmax, exists = _max_date(con, code)
            if not exists:
                errors += 1
                print(f"[ERROR] {code}: 表 {sp._table_name(code)} 不存在，跳过")
                continue
            try:
                bars, new = _fetch_new_bars(code, adjust or DEFAULT_ADJUST,
                                            dmax, end_compact)
            except Exception as e:  # network / invalid code / upstream change
                errors += 1
                print(f"[ERROR] {code}: 抓取失败 {type(e).__name__}: {e}")
                continue
            # 接缝守卫：**追加之前**先拿库里那一天的收盘价/成交量当锚比一遍。
            # 路由是择优的（sina→东财→tencent），今天谁赢不一定等于种子那天谁
            # 赢；两个适配器的 qfq 基准与 volume 量纲都不同，而质量门只看本次
            # 窗口内部，看不出"接缝那一行对不上"。接不上就不写这一张，逐条点名。
            seam = _check_seam(con, code, dmax, bars)
            if seam is not None:
                seamed += 1
                print(f"[seam] {code}: {seam}")
                continue
            span = f"{bars[0]['date']}..{bars[-1]['date']}" if bars else "empty"
            if mode == "check":
                total_planned += len(new)
                print(f"[check] {code}: table={sp._table_name(code)} rows={n} "
                      f"max(date)={dmax} {note}(保留)  fetch={span}  "
                      f"将新增={len(new)} 行  [no writes]")
            else:
                inserted = _apply_insert(con, code, new)
                con.commit()
                total_written += inserted
                new_max = new[-1]["date"] if new else dmax
                print(f"[apply] {code}: 新增 {inserted} 行 "
                      f"(max {dmax} -> {new_max}); {note} 保留; 历史行未改")

        print("-" * 64)
        seam_note = (f"接缝守卫拦下 {seamed} 张（口径/量纲与库内不符，一行未写）"
                     if seamed else "接缝守卫: 全部通过")
        if mode == "check":
            print(f"[check 汇总] 共 {len(codes)} 张表，预计将新增 {total_planned} 行；"
                  f"失败 {errors}；{seam_note}。未执行任何写库操作。加 --apply 才写入。")
        else:
            print(f"[apply 汇总] 共 {len(codes)} 张表，实际新增 {total_written} 行；"
                  f"失败 {errors}；{seam_note}。caliber 记录全部保留。")
        # 接缝命中算**非零退出**：让 CronJob 变红，而不是"跑完了、日志里有一行"。
        # 它代表的是「这批数据不能按当前口径追加」，必须有人处置（重播或换路由）。
        return 1 if (errors or seamed) else 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
