"""Seed A-share daily prices into the local DAAS (channel B).

For each code: fetch daily OHLCV via akshare (direct, client-side), upsert
into `scraw_<code>_daily`, register base price indicator rules, and (with
`--check`) verify the data → rule → indicator chain.

The look-and-feel mirrors `seed_us_leaders_indicators.py` (same table shape,
same rule shape), with three A-share specifics:

  * proxy env vars MUST be cleared first (sina/tencent answer on a direct
    connection; eastmoney needs the local proxy), so the script clears them
    itself rather than trusting the caller's shell;
  * the fetch is route-ranked sina → eastmoney → tencent, and every fetched
    series passes a quality gate before it may land: a non-positive OHLC price
    or a >±120% daily jump rejects the route. This exists because tencent's
    qfq is ADDITIVE — it shifts old prices by cumulative dividends and goes
    negative once they exceed the anchor (000333 Close=-12.19 in 2013), which
    silently poisons every downstream return;
  * the adjustment caliber (qfq/hfq/none) is recorded in `scraw_configs` so a
    same-table caliber change can be refused instead of silently mixed.

Usage (from the daas-mcp venv, cwd-agnostic):

    python seed_astock_price.py --symbols 600519,000001
    python seed_astock_price.py --symbols-file codes.txt
    python seed_astock_price.py --symbols 600519 --adjust hfq
    python seed_astock_price.py --symbols 600519 --replace   # re-seed cleanly
    python seed_astock_price.py --check 600519        # readiness self-check only
    python seed_astock_price.py --symbols 600519 --no-rules   # data only
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_THIS = Path(__file__).resolve()
_REPO_ROOT = _THIS.parent.parent.parent  # daas-mcp/ → fd-daas-mcp/ → DAAS root
try:
    from dotenv import load_dotenv
    load_dotenv(_REPO_ROOT / ".env")
    load_dotenv(_THIS.parent / ".env", override=True)
except ImportError:
    pass

sys.path.insert(0, str(_THIS.parent))

from process_database import ProcessDatabase, ProcessError  # noqa: E402

# Base price rules registered per code. (suffix, op, params, value_column)
# `close` is the level series; the moving averages carry the caliber tag so a
# mixed-caliber database is visible in rule names, not silent.
SPECS = [
    ("close",   "level",       {},              "Close"),
    ("ma5",     "sma",         {"window": 5},   "Close"),
    ("ma10",    "sma",         {"window": 10},  "Close"),
    ("ma20",    "sma",         {"window": 20},  "Close"),
    ("rsi14",   "rsi",         {"window": 14},  "Close"),
]

DATASOURCE = "astock"          # registered in `sources` by this script
FUNCTION_NAME = "stock_zh_a_hist"  # the akshare function actually used
ADJUSTS = ("qfq", "hfq", "none")

# A fetched series must pass these before it may land. Measured on this host:
# tencent's qfq is ADDITIVE (old prices are shifted by cumulative dividends and
# go negative once dividends exceed the anchor — 000333 showed Close=-12.19),
# while sina's qfq is MULTIPLICATIVE (positive throughout, last bar equals the
# raw price). A negative close is therefore never a market event, always a
# caliber artifact: refuse it here rather than storing a price world that
# silently poisons every downstream return.
MAX_ABS_DAILY_LOG_RETURN = 1.2   # ±120%: beyond this it is a splice, not a move


def _clear_proxy() -> None:
    """Report the proxy situation instead of guessing it.

    Measured on this host: akshare's eastmoney endpoint is reachable ONLY
    through the local proxy (127.0.0.1:7892) — a direct call is refused
    (`RemoteDisconnected`), while the proxied call returns 200. So we do NOT
    disable proxying; we only clear a broken override if the shell set one,
    and make the effective route visible so failures are diagnosable.
    """
    for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        val = os.environ.pop(var, None)
        if val:
            print(f"[proxy] cleared shell override {var}={val} (system proxy is used)")
    try:
        import requests
        eff = requests.utils.getproxies()
    except Exception:
        eff = {}
    print(f"[proxy] effective route for akshare: {eff or 'direct'}")


def _table_name(code: str) -> str:
    return f"scraw_{code.lower()}_daily"


def _caliber_of(db: ProcessDatabase, code: str) -> str | None:
    """Read the recorded adjustment caliber for a code's price table."""
    import json
    from sqlalchemy import text as _text

    with db.engine.connect() as conn:
        row = conn.execute(
            _text("SELECT columns_json FROM scraw_configs WHERE name = :n ORDER BY id DESC LIMIT 1"),
            {"n": f"astock_{code.lower()}"},
        ).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        cfg = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    except (TypeError, ValueError):
        return None
    return cfg.get("adjust")


def _record_caliber(db: ProcessDatabase, code: str, adjust: str, rows: int) -> None:
    """Record (or refresh) the adjustment caliber for a code's price table."""
    import json
    from sqlalchemy import text as _text

    name = f"astock_{code.lower()}"
    cols = json.dumps({
        "adjust": adjust,
        "rows": rows,
        "source": FUNCTION_NAME,
        "note": "A-share daily bars via akshare; caliber recorded so a "
                "same-table caliber change is refused, not silently mixed.",
    }, ensure_ascii=False)
    with db.engine.begin() as conn:
        conn.execute(
            _text("DELETE FROM scraw_configs WHERE name = :n"), {"n": name}
        )
        conn.execute(
            _text(
                "INSERT INTO scraw_configs (url, name, columns_json) "
                "VALUES (:u, :n, :c)"
            ),
            {"u": f"akshare:{FUNCTION_NAME}", "n": name, "c": cols},
        )


def _ensure_table(db: ProcessDatabase, code: str) -> None:
    """Create the daily table in the existing US-table shape when absent."""
    from sqlalchemy import text as _text

    tbl = _table_name(code)
    if db.table_exists(tbl):
        return
    with db.engine.begin() as conn:
        conn.execute(_text(
            f'CREATE TABLE "{tbl}" ('
            "date TEXT PRIMARY KEY, Open REAL, High REAL, Low REAL, "
            "Close REAL, Volume INTEGER)"
        ))


def _upsert_bars(db: ProcessDatabase, code: str, bars: list[dict]) -> int:
    """Idempotent upsert on `date` — re-runs do not grow the table."""
    from sqlalchemy import text as _text

    if not bars:
        return 0
    tbl = _table_name(code)
    records = [
        {
            "d": b["date"], "o": b["open"], "h": b["high"],
            "l": b["low"], "c": b["close"], "v": b["volume"],
        }
        for b in bars
    ]
    with db.engine.begin() as conn:
        conn.execute(
            _text(
                f'INSERT INTO "{tbl}" (date, Open, High, Low, Close, Volume) '
                "VALUES (:d, :o, :h, :l, :c, :v) "
                "ON CONFLICT(date) DO UPDATE SET "
                "Open=excluded.Open, High=excluded.High, Low=excluded.Low, "
                "Close=excluded.Close, Volume=excluded.Volume"
            ),
            records,
        )
    return len(records)


def _fetch_via_sina(code: str, adjust: str, start: str | None, end: str | None):
    """Primary route: akshare's sina adapter — the only source with clean qfq.

    Measured on this host: sina's qfq is multiplicative (000333 Close ranges
    6.44..84.60, last bar == raw price, no negatives), while tencent's qfq is
    additive and goes negative for older windows. Sina also returns
    `outstanding_share`/`turnover`, which the other routes lack.
    """
    import akshare as ak

    prefix = "sh" if code.startswith(("6", "9")) else "sz"
    df = ak.stock_zh_a_daily(
        symbol=f"{prefix}{code}",
        start_date=start or "19900101", end_date=end or "20991231",
        adjust=adjust if adjust != "none" else "",
    )
    if df is None or len(df) == 0:
        return []
    df = df.rename(columns={"amount": "volume"}) if "volume" not in df.columns else df
    return df


def _fetch_via_eastmoney(code: str, adjust: str, start: str | None, end: str | None):
    """Secondary route: akshare's eastmoney adapter (richest columns).

    Measured on this host: eastmoney is reachable ONLY through the local proxy
    (127.0.0.1:7892) and even then drops connections mid-session, so it is not
    the primary.
    """
    import akshare as ak

    df = ak.stock_zh_a_hist(
        symbol=code, period="daily", adjust=adjust,
        start_date=start or "19900101", end_date=end or "20991231",
    )
    if df is None or len(df) == 0:
        return []
    ren = {"日期": "date", "开盘": "open", "最高": "high",
           "最低": "low", "收盘": "close", "成交量": "volume"}
    df = df.rename(columns=ren)
    return df


def _fetch_via_tencent(code: str, adjust: str, start: str | None, end: str | None):
    """Last-resort route: akshare's tencent adapter.

    Its qfq is ADDITIVE and produces negative closes on old windows (000333:
    Close=-12.19 in 2013) — usable for `none`/`hfq` only. It stays in the chain
    because it answers on a direct connection when both other routes are down;
    the positive-price guard in `fetch_bars` rejects its bad qfq output.
    """
    import akshare as ak

    prefix = "sh" if code.startswith(("6", "9")) else "sz"
    df = ak.stock_zh_a_hist_tx(
        symbol=f"{prefix}{code}",
        start_date=start or "19900101", end_date=end or "20991231",
        adjust=adjust if adjust != "none" else "",
    )
    if df is None or len(df) == 0:
        return []
    df = df.rename(columns={"amount": "volume"})
    return df


def _validate_series(bars: list[dict], route: str) -> str | None:
    """Return a rejection reason, or None when the series is usable.

    Two checks, both measured against real failures on this host:
      * every OHLC price must be > 0 — a negative close is an adjustment
        artifact (additive qfq), never a market event;
      * no single daily log return may exceed ±120% — a larger jump means two
        price worlds were spliced together (different calibers, or a
        split/dividend applied on one side only).
    """
    import math

    bad = [b for b in bars if not (b["close"] > 0 and b["open"] > 0
                                   and b["high"] > 0 and b["low"] > 0)]
    if bad:
        return (f"{route}: {len(bad)}/{len(bars)} bars have a non-positive "
                f"OHLC price (first at {bad[0]['date']}, close={bad[0]['close']}) "
                f"— additive-qfq artifact, not a market event")
    worst = (0.0, None)
    for prev, cur in zip(bars, bars[1:]):
        try:
            r = math.log(cur["close"] / prev["close"])
        except (ValueError, ZeroDivisionError):
            return f"{route}: un-computable log return at {cur['date']}"
        if abs(r) > abs(worst[0]):
            worst = (r, cur["date"])
    if abs(worst[0]) > MAX_ABS_DAILY_LOG_RETURN:
        return (f"{route}: daily log return {worst[0]:+.2f} at {worst[1]} "
                f"exceeds ±{MAX_ABS_DAILY_LOG_RETURN:.1f} — spliced calibers")
    return None


def fetch_bars(code: str, adjust: str, start: str | None, end: str | None,
               retries: int = 2) -> list[dict]:
    """Fetch daily bars, trying sina → eastmoney → tencent, with a quality gate.

    Each route is validated before it can win: a route that returns rows which
    fail `_validate_series` is discarded and the next route is tried, so a
    broken caliber never becomes the stored price world. Retries cover the
    measured transport flakiness of the proxy-backed routes.
    """
    import time as _time

    routes = (("sina", _fetch_via_sina), ("eastmoney", _fetch_via_eastmoney),
              ("tencent", _fetch_via_tencent))
    errors: list[str] = []
    for route_name, fn in routes:
        df = None
        for attempt in range(1, retries + 1):
            try:
                df = fn(code, adjust, start, end)
                if df is not None and len(df) > 0:
                    break
                errors.append(f"{route_name}: empty result")
                df = None
                break
            except Exception as e:
                errors.append(f"{route_name}: {type(e).__name__}")
                df = None
                if attempt < retries:
                    _time.sleep(2 * attempt)
        if df is None or len(df) == 0:
            continue
        # akshare returns Chinese column names on some routes; map to scraw shape.
        ren = {"日期": "date", "开盘": "open", "最高": "high",
               "最低": "low", "收盘": "close", "成交量": "volume"}
        df = df.rename(columns=ren)
        out: list[dict] = []
        for r in df.itertuples(index=False):
            try:
                out.append({
                    "date": str(getattr(r, "date"))[:10],
                    "open": float(getattr(r, "open")),
                    "high": float(getattr(r, "high")),
                    "low": float(getattr(r, "low")),
                    "close": float(getattr(r, "close")),
                    "volume": int(float(getattr(r, "volume"))),
                })
            except (TypeError, ValueError):
                continue
        if not out:
            errors.append(f"{route_name}: no parseable rows")
            continue
        rejection = _validate_series(out, route_name)
        if rejection:
            errors.append(rejection)
            continue
        print(f"[fetch] {code}: route={route_name} rows={len(out)} "
              f"range={out[0]['date']}..{out[-1]['date']}")
        return out
    raise RuntimeError("all sources failed → " + "; ".join(errors))


def _ensure_datasource(db: ProcessDatabase) -> None:
    """Register the `astock` source row (soft ref target for its rules)."""
    from sqlalchemy import text as _text

    with db.engine.begin() as conn:
        row = conn.execute(
            _text("SELECT id FROM sources WHERE name = :n"), {"n": DATASOURCE}
        ).fetchone()
        if row is None:
            conn.execute(
                _text(
                    "INSERT INTO sources (name, label, description, enabled) "
                    "VALUES (:n, :l, :d, 1)"
                ),
                {"n": DATASOURCE, "l": "A股（akshare 客户侧直取）",
                 "d": "A-share daily bars fetched client-side via akshare "
                      "(stock_zh_a_hist); prices stay in the local instance."},
            )


def register_rules(db: ProcessDatabase, code: str) -> tuple[int, int, list[str]]:
    """Create + run the base price rules for a code. Returns (created, existed, errors)."""
    created = existed = 0
    errors: list[str] = []
    table = _table_name(code)
    for suffix, op, params, value_col in SPECS:
        name = f"{code}_{suffix}"
        try:
            db.create_indicator(
                name=name, datasource=DATASOURCE, source_table=table,
                date_column="date", value_column=value_col,
                op=op, params=params or None,
                function_name=FUNCTION_NAME, indicator_name=name,
            )
            created += 1
        except ProcessError as e:
            if "already exists" in str(e):
                existed += 1
            else:
                errors.append(f"{name}: {e}")
                continue
        res = db.run_indicator(name)
        if isinstance(res, dict) and res.get("error"):
            errors.append(f"{name}: {res['error']}")
    return created, existed, errors


def seed_one(db: ProcessDatabase, code: str, adjust: str,
             start: str | None, end: str | None, with_rules: bool,
             replace: bool = False) -> dict:
    """Fetch → calibrate → upsert → (rules) for one code. Never raises."""
    result = {"code": code, "ok": False, "rows": 0, "created": 0,
              "existed": 0, "errors": []}
    try:
        bars = fetch_bars(code, adjust, start, end)
    except Exception as e:  # network / invalid code / upstream change
        result["errors"].append(f"fetch failed: {type(e).__name__}: {e}")
        return result
    if not bars:
        result["errors"].append("fetch returned no rows (invalid code, delisted, or empty window)")
        return result

    _ensure_table(db, code)
    # Caliber guard: a table already recorded under a different caliber must not
    # be overwritten (same table, two calibers = two price worlds).
    existing = _caliber_of(db, code)
    if existing is not None and existing != adjust:
        result["errors"].append(
            f"caliber conflict: table {_table_name(code)} recorded as "
            f"'{existing}', requested '{adjust}' — refusing to mix; use a "
            f"different table/caliber"
        )
        return result

    if replace:
        # A re-seed under the SAME caliber still needs the old rows gone when
        # the previous fetch came from a broken route (e.g. additive-qfq
        # negatives): upsert alone would leave dates the new route does not
        # cover. --replace makes "this fetch is the whole truth" explicit.
        from sqlalchemy import text as _text
        with db.engine.begin() as conn:
            conn.execute(_text(f'DELETE FROM "{_table_name(code)}"'))

    result["rows"] = _upsert_bars(db, code, bars)
    _record_caliber(db, code, adjust, result["rows"])
    if with_rules:
        created, existed, errs = register_rules(db, code)
        result["created"], result["existed"] = created, existed
        result["errors"].extend(errs)
    result["ok"] = not result["errors"]
    return result


def readiness_check(db: ProcessDatabase, code: str) -> int:
    """Three-step channel-B readiness check: table → rules → a runnable indicator.

    Prints each step with its evidence (table name, rule names, row count) so a
    human can see *why* the check passed or failed. Non-zero exit on failure.
    """
    table = _table_name(code)
    print(f"\n── 通道 B 就绪自检：{code} " + "─" * 40)
    steps_ok = True

    # 1. data landed
    if db.table_exists(table):
        from sqlalchemy import text as _text
        with db.engine.connect() as conn:
            n, dmin, dmax = conn.execute(
                _text(f'SELECT COUNT(*), MIN(date), MAX(date) FROM "{table}"')
            ).fetchone()
        print(f"[1/3] 数据落库: OK — {table} 行数={n} 范围={dmin}..{dmax}")
        if not n:
            steps_ok = False
            print("      ↑ 表存在但无行")
    else:
        steps_ok = False
        print(f"[1/3] 数据落库: FAIL — 表 {table} 不存在（先跑种子）")

    # 2. rules registered
    rules = [f"{code}_{s[0]}" for s in SPECS]
    have: list[str] = []
    for r in rules:
        if db.get_indicator(r) is not None:
            have.append(r)
    if have:
        print(f"[2/3] 规则注册: OK — {len(have)}/{len(rules)} 条: {', '.join(have)}")
    else:
        steps_ok = False
        print(f"[2/3] 规则注册: FAIL — 未见任何规则（先跑种子，或加 --no-rules 后补注册）")

    # 3. an indicator actually computes
    probe = f"{code}_ma5"
    if db.get_indicator(probe) is not None:
        res = db.run_indicator(probe)
        n_written = res.get("rows_written") if isinstance(res, dict) else None
        if n_written:
            print(f"[3/3] 指标可算: OK — {probe} 写入 {n_written} 行观测")
        else:
            steps_ok = False
            print(f"[3/3] 指标可算: FAIL — {probe} 返回 {res}")
    else:
        steps_ok = False
        print(f"[3/3] 指标可算: FAIL — 探针规则 {probe} 不存在")

    print("─" * 64)
    if steps_ok:
        print("通道 B 就绪：柏讯估值套件的前置检查可满足。")
        return 0
    print("通道 B 未就绪（见上方缺失环节）。")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed A-share daily prices into local DAAS.")
    ap.add_argument("--symbols", help="comma-separated A-share codes, e.g. 600519,000001")
    ap.add_argument("--symbols-file", help="file with one code per line")
    ap.add_argument("--adjust", default="qfq", choices=ADJUSTS,
                    help="adjustment caliber (default qfq)")
    ap.add_argument("--start-date", help="YYYYMMDD (optional)")
    ap.add_argument("--end-date", help="YYYYMMDD (optional)")
    ap.add_argument("--no-rules", action="store_true", help="data only, skip rule registration")
    ap.add_argument("--replace", action="store_true",
                    help="delete the code's existing rows before upsert (use when re-seeding "
                         "a table whose earlier fetch came from a rejected route)")
    ap.add_argument("--check", metavar="CODE", help="readiness self-check for one code, no fetching")
    args = ap.parse_args()

    db = ProcessDatabase()

    if args.check:
        return readiness_check(db, args.check)

    codes: list[str] = []
    if args.symbols:
        codes += [c.strip() for c in args.symbols.split(",") if c.strip()]
    if args.symbols_file:
        codes += [ln.strip() for ln in Path(args.symbols_file).read_text().splitlines() if ln.strip()]
    if not codes:
        ap.error("provide --symbols, --symbols-file, or --check CODE")

    _clear_proxy()
    _ensure_datasource(db)

    results = []
    for code in codes:
        r = seed_one(db, code, args.adjust, args.start_date, args.end_date,
                     with_rules=not args.no_rules, replace=args.replace)
        results.append(r)
        status = "OK" if r["ok"] else "FAIL"
        print(f"[{status}] {code}: rows={r['rows']} rules(created={r['created']}, "
              f"existed={r['existed']})" + (f" errors={r['errors']}" if r["errors"] else ""))

    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    print("\n" + "═" * 64)
    print(f"A 股价格种子完成: 成功 {len(ok)} / 失败 {len(bad)}（adjust={args.adjust}）")
    for r in bad:
        print(f"  - {r['code']}: {'; '.join(r['errors'])}")
    print("═" * 64)
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
