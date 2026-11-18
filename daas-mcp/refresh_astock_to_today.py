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
        if mode == "check":
            print(f"[check 汇总] 共 {len(codes)} 张表，预计将新增 {total_planned} 行；"
                  f"失败 {errors}。未执行任何写库操作。加 --apply 才写入。")
        else:
            print(f"[apply 汇总] 共 {len(codes)} 张表，实际新增 {total_written} 行；"
                  f"失败 {errors}。caliber 记录全部保留。")
        return 1 if errors else 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
