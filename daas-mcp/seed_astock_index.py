"""Seed A-share INDEX daily bars into the local DAAS (channel B).

Adapted from ``seed_astock_price.py``, which this module imports and reuses
verbatim for everything that is NOT index-specific:

  * table DDL / shape            -> sp._ensure_table, sp._table_name
  * idempotent bar upsert        -> sp._upsert_bars
  * caliber guard + record       -> sp._caliber_of, sp._record_caliber
  * OHLC quality gate            -> sp._validate_series
  * datasource registration      -> sp._ensure_datasource  (the ``astock`` family)
  * price rule registration      -> sp.register_rules  (sp.SPECS / create_indicator / run_indicator)
  * readiness self-check         -> sp.readiness_check (data -> rules -> indicator chain)
  * proxy handling               -> sp._clear_proxy

The only things that differ for an index:

  * fetch route: ``ak.index_zh_a_hist`` (eastmoney index adapter) instead of the
    stock_zh_a_* routes -- indices have no adjustment, so there is one route and
    no sina/tencent qfq fallbacks;
  * caliber is fixed to ``"none"`` (indices have no qfq/hfq concept);
  * provenance label is ``index_zh_a_hist`` instead of ``stock_zh_a_hist``
    (applied once via ``sp.FUNCTION_NAME`` override, read by register_rules and
    _record_caliber);
  * rule names still follow the existing convention ``<code>_<suffix>`` and use
    the SAME ``astock`` datasource (same concept family) as the individual-stock
    tables, so the 5 price rules per index are identical in shape to e.g.
    ``600519_close``.

Policy: prices are fetched client-side (channel B); the server does not re-open
a crawl. Index seeding uses the same path.

The DB path comes from ``--db`` (default: the local authoritative library
``/Users/chengsishi/code/DAAS/daas.db``). No credentials are hardcoded.

Usage (run with the fd-paper-trading venv, cwd-agnostic):

    python seed_astock_index.py --check 000300           # readiness self-check only
    python seed_astock_index.py                          # seed 000300,000905
    python seed_astock_index.py --symbols 000300,000905,000016
    python seed_astock_index.py --symbols 000300 --replace   # re-seed cleanly
    python seed_astock_index.py --symbols 000300 --no-rules  # data only
    python seed_astock_index.py --db /path/to/daas.db --symbols 000300
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

_THIS = Path(__file__).resolve()
_DAAS_MCP = _THIS.parent          # .../fd-daas-mcp/daas-mcp
_FD_DAAS_MCP = _DAAS_MCP.parent   # .../fd-daas-mcp
# process_database lives in daas-mcp/; the `models` package it imports lives in
# the parent fd-daas-mcp/. Both must be importable (the original seed script
# relies on the repo venv/PYTHONPATH for this; we make it explicit here so the
# script is reproducible from the fd-paper-trading venv too).
for _p in (str(_FD_DAAS_MCP), str(_DAAS_MCP)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from dotenv import load_dotenv
    load_dotenv(_FD_DAAS_MCP.parent / ".env")       # /Users/.../DAAS/.env
    load_dotenv(_DAAS_MCP / ".env", override=True)
except ImportError:
    pass

import seed_astock_price as sp  # noqa: E402  (reuse; see module docstring)
from process_database import ProcessDatabase  # noqa: E402

DEFAULT_DB = "/Users/chengsishi/code/DAAS/daas.db"
DEFAULT_SYMBOLS = "000300,000905"
INDEX_FUNCTION = "index_zh_a_hist"
CALIBER = "none"                 # indices have no forward/backward adjustment

# Accurate provenance for rule registration + the scraw_configs caliber record.
# sp.register_rules / sp._record_caliber read this module-level constant at call
# time, so a single override keeps index rows labelled with the right akshare
# function without forking their logic.
sp.FUNCTION_NAME = INDEX_FUNCTION


def _db_path(db_arg: str) -> str:
    """Normalize --db to an absolute filesystem path (also accepts sqlite:/// URLs)."""
    s = db_arg
    if s.startswith("sqlite:////"):
        return s[len("sqlite:///"):]
    if s.startswith("sqlite:///"):
        return os.path.abspath(s[len("sqlite:///"):])
    return os.path.abspath(os.path.expanduser(s))


def _open_db(db_arg: str) -> ProcessDatabase:
    path = _db_path(db_arg)
    if not os.path.isfile(path):
        raise SystemExit(
            f"ERROR: DB file not found: {path}\n"
            "       pass an existing sqlite file via --db"
        )
    # absolute path -> sqlite:////... , exactly the shape the original uses.
    return ProcessDatabase(f"sqlite:///{path}")


def fetch_index_bars(code: str, start: str | None, end: str | None,
                     retries: int = 2) -> list[dict]:
    """Fetch daily index OHLCV via akshare ``index_zh_a_hist``.

    Single route (eastmoney index adapter); indices have no qfq/hfq so there is
    no route chain. ``start``/``end`` are YYYYMMDD (akshare form). The fetched
    series is put through the SAME quality gate as the stock seed
    (sp._validate_series: OHLC > 0 and no >±120% daily log return).
    """
    import akshare as ak

    ren = {"日期": "date", "开盘": "open", "最高": "high",
           "最低": "low", "收盘": "close", "成交量": "volume"}
    errors: list[str] = []
    df = None
    for attempt in range(1, retries + 1):
        try:
            df = ak.index_zh_a_hist(
                symbol=code, period="daily",
                start_date=start or "19900101", end_date=end or "20991231",
            )
            if df is not None and len(df) > 0:
                break
            errors.append(f"{INDEX_FUNCTION}: empty result for {code}")
            df = None
            break
        except Exception as e:
            errors.append(f"{INDEX_FUNCTION}: {type(e).__name__}: {e}")
            df = None
            if attempt < retries:
                time.sleep(2 * attempt)
    if df is None or len(df) == 0:
        raise RuntimeError("index fetch failed -> " + "; ".join(errors))

    df = df.rename(columns=ren)
    need = ["date", "open", "high", "low", "close", "volume"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise RuntimeError(
            f"{INDEX_FUNCTION}: missing columns {missing} for {code} "
            f"(got {list(df.columns)})"
        )
    df = df[need]                      # subset to the scraw shape before itertuples
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
        raise RuntimeError(f"{INDEX_FUNCTION}: no parseable rows for {code}")
    rejection = sp._validate_series(out, INDEX_FUNCTION)
    if rejection:
        raise RuntimeError(f"index series rejected for {code}: {rejection}")
    print(f"[fetch] {code}: route={INDEX_FUNCTION} rows={len(out)} "
          f"range={out[0]['date']}..{out[-1]['date']}")
    return out


def seed_one(db: ProcessDatabase, code: str,
             start: str | None, end: str | None, with_rules: bool,
             replace: bool = False) -> dict:
    """Fetch -> calibrate(none) -> upsert -> (rules) for one index. Never raises.

    Mirrors sp.seed_one but swaps the stock routes for fetch_index_bars and
    fixes the caliber to ``none``; every DB-shape / caliber / rule / upsert step
    is reused from sp.
    """
    result = {"code": code, "ok": False, "rows": 0, "created": 0,
              "existed": 0, "errors": []}
    try:
        bars = fetch_index_bars(code, start, end)
    except Exception as e:  # network / invalid code / upstream change
        result["errors"].append(f"fetch failed: {type(e).__name__}: {e}")
        return result
    if not bars:
        result["errors"].append("fetch returned no rows (invalid code or empty window)")
        return result

    sp._ensure_table(db, code)                         # reused DDL
    existing = sp._caliber_of(db, code)                # reused caliber guard
    if existing is not None and existing != CALIBER:
        result["errors"].append(
            f"caliber conflict: {sp._table_name(code)} recorded as '{existing}', "
            f"index caliber is fixed to '{CALIBER}' -- refusing to mix"
        )
        return result

    if replace:
        from sqlalchemy import text as _text
        with db.engine.begin() as conn:
            conn.execute(_text(f'DELETE FROM "{sp._table_name(code)}"'))

    result["rows"] = sp._upsert_bars(db, code, bars)   # reused upsert SQL
    sp._record_caliber(db, code, CALIBER, result["rows"])  # reused caliber record
    if with_rules:
        created, existed, errs = sp.register_rules(db, code)  # reused rules
        result["created"], result["existed"] = created, existed
        result["errors"].extend(errs)
    result["ok"] = not result["errors"]
    return result


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Seed A-share INDEX daily prices into local DAAS (channel B).")
    ap.add_argument("--symbols", default=DEFAULT_SYMBOLS,
                    help=f"comma-separated index codes (default {DEFAULT_SYMBOLS})")
    ap.add_argument("--symbols-file", help="file with one index code per line")
    ap.add_argument("--start-date", help="YYYYMMDD (optional)")
    ap.add_argument("--end-date", help="YYYYMMDD (optional)")
    ap.add_argument("--db", default=DEFAULT_DB,
                    help=f"sqlite DB path (default {DEFAULT_DB})")
    ap.add_argument("--no-rules", action="store_true",
                    help="data only, skip rule registration")
    ap.add_argument("--replace", action="store_true",
                    help="delete the code's existing rows before upsert (re-seed)")
    ap.add_argument("--check", metavar="CODE",
                    help="readiness self-check (data->rules->indicator), no fetching")
    args = ap.parse_args()

    db = _open_db(args.db)

    if args.check:
        return sp.readiness_check(db, args.check)   # reused self-check

    codes: list[str] = []
    if args.symbols:
        codes += [c.strip() for c in args.symbols.split(",") if c.strip()]
    if args.symbols_file:
        p = Path(args.symbols_file)
        if not p.is_file():
            raise SystemExit(f"ERROR: --symbols-file not found: {p}")
        codes += [ln.strip() for ln in p.read_text().splitlines() if ln.strip()]
    seen: set[str] = set()
    uniq: list[str] = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    codes = uniq
    if not codes:
        ap.error("provide --symbols, --symbols-file, or --check CODE")

    print(f"[seed] DB={_db_path(args.db)}  symbols={','.join(codes)}  "
          f"caliber={CALIBER}  route={INDEX_FUNCTION}  replace={args.replace}  "
          f"rules={not args.no_rules}")
    sp._clear_proxy()                 # reused proxy handling
    sp._ensure_datasource(db)        # reused: registers the `astock` family source

    results = []
    for code in codes:
        r = seed_one(db, code, args.start_date, args.end_date,
                     with_rules=not args.no_rules, replace=args.replace)
        results.append(r)
        status = "OK" if r["ok"] else "FAIL"
        print(f"[{status}] {code}: rows={r['rows']} "
              f"rules(created={r['created']}, existed={r['existed']})"
              + (f" errors={r['errors']}" if r["errors"] else ""))

    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    print("\n" + "=" * 64)
    print(f"A 股指数种子完成: 成功 {len(ok)} / 失败 {len(bad)} "
          f"(caliber={CALIBER}, route={INDEX_FUNCTION})")
    for r in bad:
        print(f"  - {r['code']}: {'; '.join(r['errors'])}")
    print("=" * 64)
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
