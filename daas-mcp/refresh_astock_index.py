#!/usr/bin/env python3
"""Top up A-share **index** daily bars (``scraw_<code>_daily``) — the layer nobody refreshes.

Why this file exists (measured 2026-11-15, ``cheap/fd-prod``):

  * ``refresh_astock_to_today.py`` excludes index codes on purpose — its own
    comment reads ``INDEX_CODES = {"000300", "000905"}  # belt-and-suspenders:
    never refresh index codes``. So the nightly refresh advances 12 stock tables
    and never touches the two index tables.
  * ``seed_astock_index.py`` *can* write them, but it is a one-shot seeder with
    no schedule, so its output froze at 2026-11-06 while every stock table was
    current to 2026-11-13.
  * ``fd_quant``'s ``settle_decision`` needs the benchmark series through the
    exit date (it refuses a window it cannot cover), so a frozen benchmark makes
    settlement impossible — and the failure is silent: the decision simply stays
    ``pending`` while the report reads as if it settled.

Route choice is measured, never assumed. From inside the cluster:

    push2his.eastmoney.com   -> RemoteDisconnected   (akshare's stock/index history route)
    money.finance.sina.com.cn -> HTTP 200, real bars
    web.ifzq.gtimg.cn         -> HTTP 200, real bars

So the eastmoney history route is not attempted here at all: it is the one host
that is refused from this network. Order is sina -> tencent, and a route only
wins if its series passes the quality gate below.

Caliber / provenance: indices have no adjustment, so there is nothing to
reconcile. What matters is that ``scraw_configs.source`` never claims a route
that did not actually run — when this script inserts rows for a table whose
recorded source differs from the route it used, it updates that row and says so.

Usage:

    python3 refresh_astock_index.py --db /app/fd-daas-mcp/data/daas.db            # check (default, read-only)
    python3 refresh_astock_index.py --db … --apply                                # write
    python3 refresh_astock_index.py --db … --symbols 000300,000905 --apply
    python3 refresh_astock_index.py --db … --json                                 # machine-readable report

Exit codes: 0 = nothing to do or (with --apply) written; 1 = a route failed;
2 = cannot judge (no DB / no index table) — never silently "all good".
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone

# Explicit, never inferred: 000001 is 上证综指 as an index and 平安银行 as a stock,
# and both are 6-digit. Guessing the market prefix from digits is exactly how you
# fetch the wrong series with no error anywhere.
INDEX_SYMBOLS: dict[str, str] = {
    "000300": "sh000300",   # 沪深300
    "000905": "sh000905",   # 中证500
    "000001": "sh000001",   # 上证综指
    "000016": "sh000016",   # 上证50
    "399001": "sz399001",   # 深证成指
    "399006": "sz399006",   # 创业板指
}

DEFAULT_DB = "/app/fd-daas-mcp/data/daas.db"
TABLE_TMPL = "scraw_{code}_daily"
MAX_DAY_MOVE = 0.35          # an index that moves >35% in a day is a data defect


# ── fetch routes ────────────────────────────────────────────────────────────

def _fetch_sina(index_symbol: str) -> list[dict]:
    import akshare as ak
    df = ak.stock_zh_index_daily(symbol=index_symbol)
    return _to_bars(df)


def _fetch_tencent(index_symbol: str) -> list[dict]:
    import akshare as ak
    df = ak.stock_zh_index_daily_tx(symbol=index_symbol)
    return _to_bars(df)


ROUTES = (("sina", _fetch_sina), ("tencent", _fetch_tencent))


def _to_bars(df) -> list[dict]:
    """akshare frame -> [{date, open, high, low, close, volume}] (English columns)."""
    if df is None or len(df) == 0:
        return []
    ren = {"日期": "date", "开盘": "open", "最高": "high",
           "最低": "low", "收盘": "close", "成交量": "volume"}
    df = df.rename(columns=ren)
    need = ("date", "open", "high", "low", "close")
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise ValueError(f"route returned columns {list(df.columns)}, missing {missing}")
    out: list[dict] = []
    for r in df.itertuples(index=False):
        d = getattr(r, "date")
        if hasattr(d, "strftime"):
            d = d.strftime("%Y-%m-%d")
        else:
            d = str(d)[:10]
        try:
            volume = getattr(r, "volume", None)
            out.append({
                "date": d,
                "open": float(getattr(r, "open")),
                "high": float(getattr(r, "high")),
                "low": float(getattr(r, "low")),
                "close": float(getattr(r, "close")),
                # tencent labels it `amount`; volume is not needed for settlement,
                # but the table column is NOT NULL-ish so keep a number in it.
                "volume": int(float(volume)) if volume is not None else 0,
            })
        except (TypeError, ValueError):
            continue
    return out


def validate_series(bars: list[dict], route: str, since: str | None = None,
                    prev_close: float | None = None) -> str | None:
    """Return a rejection reason, or None if the series is usable.

    A broken caliber must never become the stored price world, so each route has
    to earn its rows before any of them is written.

    The day-move gate applies **only to the rows this script would insert**, and
    it is checked in two places:

      * between consecutive *new* rows (both after ``since``);
      * across the boundary — the first new row against ``prev_close``, the close
        already stored at ``max(date)``.

    Both halves matter. Judging deep history rejects real events (measured
    2026-11-15: 上证综指 moved +105% on 1992-05-21, pre-price-limit era, and a
    whole-history gate refused the series — a false rejection that would have
    left the index stale forever). Comparing a new row against *whatever precedes
    it in the fetched series* is the mirror-image error: when the table is stale
    by years the two rows are not neighbours, and the gate fires on a 34-year gap
    (self-test arm caught this at +195802%).
    """
    if not bars:
        return f"{route}: empty"
    dates = [b["date"] for b in bars]
    if dates != sorted(dates):
        return f"{route}: dates not ascending"
    if len(set(dates)) != len(dates):
        return f"{route}: duplicate dates"
    for b in bars:
        for k in ("open", "high", "low", "close"):
            if not (b[k] > 0):
                return f"{route}: non-positive {k} on {b['date']}"
        if b["low"] > min(b["open"], b["close"]) or b["high"] < max(b["open"], b["close"]):
            return f"{route}: high/low inconsistent on {b['date']}"

    def _jump(p: float, b: dict) -> str | None:
        if p and p > 0:
            move = abs(b["close"] / p - 1)
            if move > MAX_DAY_MOVE:
                return f"{route}: {b['date']} jumps {move:.0%} (>{MAX_DAY_MOVE:.0%})"
        return None

    prev = prev_close
    for b in bars:
        is_new = since is None or b["date"] > since
        if is_new:
            bad = _jump(prev, b) if prev is not None else None
            if bad:
                return bad
        prev = b["close"] if is_new else prev
    return None


# ── db ──────────────────────────────────────────────────────────────────────

def _table(code: str) -> str:
    return TABLE_TMPL.format(code=code)


def _exists(con: sqlite3.Connection, code: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_table(code),)
    ).fetchone() is not None


def _max_date(con: sqlite3.Connection, code: str) -> str | None:
    return con.execute(f'SELECT max(date) FROM "{_table(code)}"').fetchone()[0]


def _close_at(con: sqlite3.Connection, code: str, date: str | None) -> float | None:
    """Close already stored at the table's latest row (the boundary anchor)."""
    if date is None:
        return None
    row = con.execute(f'SELECT Close FROM "{_table(code)}" WHERE date = ?', (date,)).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def _recorded_source(con: sqlite3.Connection, code: str) -> tuple[str | None, str | None]:
    """-> (adjust, source) from the scraw_configs caliber row."""
    row = con.execute(
        "SELECT columns_json FROM scraw_configs WHERE name = ? ORDER BY id DESC LIMIT 1",
        (f"astock_{code}",),
    ).fetchone()
    if row is None or row[0] is None:
        return None, None
    try:
        cfg = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    except (TypeError, ValueError):
        return None, None
    return cfg.get("adjust"), cfg.get("source")


def classify_index_table(con: sqlite3.Connection, code: str) -> tuple[bool, str]:
    """Decide from the table's **own caliber record** whether it holds an index.

    This gate outranks any code→symbol table, and it exists because of a measured
    near-miss (2026-11-15): ``scraw_000001_daily`` looks like 上证综指 by code and
    is registered in INDEX_SYMBOLS as ``sh000001``, but the table actually holds
    平安银行 (qfq, close 0.12–19.71, ``source: stock_zh_a_hist``). Refreshing it as
    an index would have written ~4000-point index levels into a ~12-yuan stock
    table — and every gate downstream would still have passed.

    So: a table is refreshed as an index only when its caliber says index. When
    the caliber says stock, refuse loudly instead of trusting the code.
    """
    adjust, source = _recorded_source(con, code)
    if source and source.startswith("index_zh_a"):
        return True, f"caliber source={source} (adjust={adjust})"
    if source is not None:
        return False, f"caliber says stock (source={source}, adjust={adjust}) — index bars never belong here"
    if adjust in (None, "none"):
        return True, "no source recorded and adjust=none — treated as index"
    return False, f"no caliber record and adjust={adjust} — refusing to guess"


def _upsert(con: sqlite3.Connection, code: str, bars: list[dict]) -> int:
    before = con.total_changes
    con.executemany(
        f'INSERT OR IGNORE INTO "{_table(code)}" (date, Open, High, Low, Close, Volume) '
        "VALUES (:date, :open, :high, :low, :close, :volume)",
        bars,
    )
    return con.total_changes - before


def _record_route(con: sqlite3.Connection, code: str, route: str) -> bool:
    """Record which transport actually delivered the newest rows.

    ``source`` is the **caliber** (index family) and must not be rewritten by a
    transport choice: other readers switch on it, and rewriting it would claim the
    historical eastmoney-sourced rows came from somewhere else. So the route goes
    into its own keys and the caliber is left alone.
    """
    row = con.execute(
        "SELECT id, columns_json FROM scraw_configs WHERE name = ? ORDER BY id DESC LIMIT 1",
        (f"astock_{code}",),
    ).fetchone()
    if row is None:
        return False
    try:
        cfg = json.loads(row[1]) if isinstance(row[1], str) else dict(row[1] or {})
    except (TypeError, ValueError):
        return False
    cfg["last_refresh_route"] = f"index_zh_a_daily:{route}"
    cfg["last_refresh_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    con.execute("UPDATE scraw_configs SET columns_json = ? WHERE id = ?",
                (json.dumps(cfg, ensure_ascii=False), row[0]))
    return True


# ── self-test (offline: no network, no production DB) ───────────────────────

def _self_test() -> int:
    """Assert the gates that exist because of measured failures.

    Every arm names the concrete thing that broke or nearly broke; an arm that
    only checks True/False would stay green with the gate deleted.
    """
    import tempfile
    fails = 0
    arms = 0

    def arm(label: str, ok: bool, detail: str = ""):
        nonlocal fails, arms
        arms += 1
        print(f"  {'✓' if ok else '✗'} {label}{(' — ' + detail) if (detail and not ok) else ''}")
        if not ok:
            fails += 1

    tmp = tempfile.mkdtemp(prefix="idxselftest-")
    db = f"{tmp}/t.db"
    con = sqlite3.connect(db)
    con.execute('CREATE TABLE scraw_000300_daily (date TEXT PRIMARY KEY, Open REAL, High REAL, '
                'Low REAL, Close REAL, Volume INTEGER)')
    con.execute('CREATE TABLE scraw_000001_daily (date TEXT PRIMARY KEY, Open REAL, High REAL, '
                'Low REAL, Close REAL, Volume INTEGER)')
    con.execute('CREATE TABLE scraw_configs (id INTEGER PRIMARY KEY, name TEXT, columns_json TEXT)')
    con.execute("INSERT INTO scraw_configs (name, columns_json) VALUES ('astock_000300', ?)",
                (json.dumps({"adjust": "none", "source": "index_zh_a_hist"}),))
    con.execute("INSERT INTO scraw_configs (name, columns_json) VALUES ('astock_000001', ?)",
                (json.dumps({"adjust": "qfq", "source": "stock_zh_a_hist"}),))
    con.commit()

    # ① caliber gate — the near-miss that motivated it
    ok, why = classify_index_table(con, "000300")
    arm("index caliber → refresh allowed", ok is True, why)
    ok1, why1 = classify_index_table(con, "000001")
    arm("stock caliber (000001 = 平安银行) → refused, reason names source",
        ok1 is False and "stock_zh_a_hist" in why1, why1)

    # ② jump gate: deep history ignored, new rows and the boundary enforced
    deep = [{"date": "1992-05-20", "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1},
            {"date": "1992-05-21", "open": 1.0, "high": 2.1, "low": 1.0, "close": 2.05, "volume": 1},
            {"date": "2026-11-13", "open": 4000.0, "high": 4020.0, "low": 3990.0, "close": 4016.0, "volume": 1}]
    arm("deep-history +105% jump is not a rejection when it predates `since`",
        validate_series(deep, "sina", since="2026-11-06") is None,
        str(validate_series(deep, "sina", since="2026-11-06")))
    arm("boundary jump is judged against the STORED close, not the previous fetched row",
        validate_series(deep, "sina", since="2026-11-06", prev_close=4000.0) is None
        and validate_series(deep, "sina", since="2026-11-06", prev_close=10.0) is not None,
        "anchor 4000 ok / anchor 10 must fire")
    bad_new = [{"date": "2026-11-06", "open": 4000.0, "high": 4020.0, "low": 3990.0, "close": 4000.0, "volume": 1},
               {"date": "2026-11-09", "open": 4000.0, "high": 6000.0, "low": 3990.0, "close": 5900.0, "volume": 1}]
    rej = validate_series(bad_new, "sina", since="2026-11-06", prev_close=4000.0)
    arm("a +47% jump in the NEW rows is rejected and names the date",
        rej is not None and "2026-11-09" in rej, str(rej))
    arm("high/low inconsistency rejected",
        validate_series([{"date": "2026-11-13", "open": 10.0, "high": 9.0, "low": 11.0, "close": 10.0,
                          "volume": 1}], "x", since=None) is not None)
    arm("duplicate dates rejected",
        validate_series([{"date": "2026-11-13", "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0, "volume": 1}] * 2,
                        "x", since=None) is not None)

    # ③ upsert is idempotent; ④ route recorded without touching the caliber
    bars = [{"date": "2026-11-09", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 7}]
    first = _upsert(con, "000300", bars)
    second = _upsert(con, "000300", bars)
    con.commit()
    arm("upsert inserts once then ignores (idempotent)", first == 1 and second == 0, f"{first}/{second}")
    _record_route(con, "000300", "sina")
    con.commit()
    cfg = json.loads(con.execute(
        "SELECT columns_json FROM scraw_configs WHERE name='astock_000300'").fetchone()[0])
    arm("route recorded as its own key", cfg.get("last_refresh_route") == "index_zh_a_daily:sina", str(cfg))
    arm("caliber `source` is NOT rewritten by a transport choice",
        cfg.get("source") == "index_zh_a_hist", str(cfg))

    # ⑤ check mode cannot write — the guarantee is the open mode, not discipline
    ro = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        ro.execute('INSERT INTO scraw_000300_daily (date) VALUES (?)', ("2026-11-20",))
        ro.commit()
        blocked = False
    except sqlite3.OperationalError:
        blocked = True
    arm("read-only connection refuses an insert (check mode is safe by construction)", blocked)

    con.close()
    print(f"\n{'-'*60}\nself-test: {'PASS' if fails == 0 else f'FAIL ({fails})'} — {arms} arms, offline")
    return 0 if fails == 0 else 1


# ── main ────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Top up index daily bars that nothing else refreshes.")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--symbols", help="comma-separated index codes (default: every index table present)")
    ap.add_argument("--apply", action="store_true", help="write new rows (default: check only)")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ap.add_argument("--self-test", action="store_true",
                    help="offline gate assertions (no network, no production DB)")
    args = ap.parse_args()

    # Explicit mode gate: only the real run reaches the DB/network paths below.
    if args.self_test:
        return _self_test()

    try:
        con = sqlite3.connect(f"file:{args.db}?mode={'rw' if args.apply else 'ro'}", uri=True)
    except sqlite3.Error as e:
        print(f"cannot open {args.db}: {e}", file=sys.stderr)
        return 2

    if args.symbols:
        codes = [c.strip() for c in args.symbols.split(",") if c.strip()]
        bad = [c for c in codes if not (len(c) == 6 and c.isdigit())]
        if bad:
            print(f"--symbols must be 6-digit codes, got {bad}", file=sys.stderr)
            return 2
    else:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'scraw_%_daily'"
        ).fetchall()
        codes = []
        for (name,) in rows:
            inner = name[len("scraw_"):-len("_daily")]
            if len(inner) == 6 and inner.isdigit() and inner in INDEX_SYMBOLS:
                codes.append(inner)
        codes.sort()

    if not codes:
        print("cannot judge: no index table found (nothing to refresh is not the same as success)",
              file=sys.stderr)
        return 2

    report, failed = [], 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for code in codes:
        sym = INDEX_SYMBOLS.get(code)
        if sym is None:
            report.append({"code": code, "skipped": "no explicit index symbol (refusing to guess)"})
            continue
        if not _exists(con, code):
            report.append({"code": code, "skipped": f"table {_table(code)} does not exist"})
            continue
        is_index, why = classify_index_table(con, code)
        if not is_index:
            report.append({"code": code, "skipped": why})
            continue
        dmax = _max_date(con, code)
        anchor = _close_at(con, code, dmax)
        bars, route, errors = [], None, []
        for route_name, fn in ROUTES:
            try:
                cand = fn(sym)
                rejection = validate_series(cand, route_name, since=dmax, prev_close=anchor)
                if rejection:
                    errors.append(rejection)
                    continue
                bars, route = cand, route_name
                break
            except Exception as e:                      # transport or akshare change
                errors.append(f"{route_name}: {type(e).__name__}: {str(e)[:80]}")
        if route is None:
            failed += 1
            report.append({"code": code, "symbol": sym, "max_date": dmax, "errors": errors})
            continue

        new = [b for b in bars if dmax is None or b["date"] > dmax]
        new = [b for b in new if b["date"] <= today]
        entry = {"code": code, "symbol": sym, "route": route, "max_date": dmax,
                 "fetched_last": bars[-1]["date"], "new_rows": len(new),
                 "new_range": [new[0]["date"], new[-1]["date"]] if new else None}
        if args.apply and new:
            entry["inserted"] = _upsert(con, code, new)
            entry["route_recorded"] = _record_route(con, code, route)
            con.commit()
        report.append(entry)

    if args.json:
        print(json.dumps({"apply": args.apply, "rows": report}, ensure_ascii=False, indent=2))
    else:
        print(f"{'APPLY' if args.apply else 'CHECK'} — {len(codes)} index table(s)")
        for r in report:
            if "errors" in r:
                print(f"  ✗ {r['code']}: all routes failed — {'; '.join(r['errors'])}")
            elif "skipped" in r:
                print(f"  · {r['code']}: {r['skipped']}")
            else:
                tail = "" if not r["new_rows"] else f" → +{r['new_rows']} ({r['new_range'][0]}..{r['new_range'][1]})"
                ins = f" inserted={r['inserted']}" if "inserted" in r else ""
                print(f"  {'✓' if not r['new_rows'] else '+'} {r['code']} ({r['symbol']}, {r['route']}): "
                      f"max={r['max_date']} fetched_last={r['fetched_last']}{tail}{ins}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
