#!/usr/bin/env python3
"""Refresh fresh daily OHLCV for ALL tracked US leaders symbols + any new poolA members.

Covers the gap that fetch_pool_indicators.py leaves: that script only refreshes the
17 poolA members. This script refreshes the full ~48 tracked set (curated mega-caps +
SPY/QQQ + poolA) so every indicator in the us-leaders-indicators collection has fresh
source data after a refresh run. Idempotent upsert; does not compute indicators
(research_refresh does that).

    uv run --with yfinance --with pandas --with numpy python \
        fd-daas-mcp/dashboard-mcp/dashboards/refresh_us_leaders_data.py
"""
import sqlite3, math, sys
from pathlib import Path
import yfinance as yf

DB = Path("daas.db").resolve()
SCRIPTS = Path(".claude/skills/fd-daas-based-data-fetch/scripts").resolve()
sys.path.insert(0, str(SCRIPTS))

SPECS = [("ma5", "sma", '{"window":5}', "Close"), ("ma10", "sma", '{"window":10}', "Close"),
         ("ma20", "sma", '{"window":20}', "Close"), ("rsi14", "rsi", '{"window":14}', "Close"),
         ("volstd20", "rolling_std", '{"window":20}', "Close"), ("high20", "rolling_max", '{"window":20}', "High")]


def fnum(v):
    try:
        x = float(v)
        return 0.0 if math.isnan(x) else x
    except (TypeError, ValueError):
        return 0.0


conn = sqlite3.connect(DB)
conn.execute("PRAGMA foreign_keys=ON")
cur = conn.cursor()

# tracked = existing scraw_<sym>_daily tables (the curated set + previously registered poolA)
tracked = [r[0].upper() for r in cur.execute(
    "SELECT substr(name,7,length(name)-12) FROM sqlite_master "
    "WHERE type='table' AND name LIKE 'scraw_%_daily'").fetchall()]
# poolA from the (just-refreshed) screen
pool_a = [r[0].upper() for r in cur.execute(
    "SELECT symbol FROM scraw_us_top300_screen WHERE in_pool_a=1").fetchall()]

syms = sorted(set(tracked) | set(pool_a))
new_pool = sorted(set(pool_a) - set(tracked))
print(f"tracked={len(tracked)} poolA={len(pool_a)} -> refreshing {len(syms)} symbols; new poolA vs tracked: {new_pool}")

# fetch + upsert daily OHLCV
fetched, failed = [], []
for sym in syms:
    t = f"scraw_{sym.lower()}_daily"
    try:
        df = yf.Ticker(sym).history(period="1y", auto_adjust=False)
    except Exception as e:
        print(f"{sym}: FETCH FAIL {str(e)[:80]}")
        failed.append(sym)
        continue
    if df is None or len(df) == 0:
        print(f"{sym}: EMPTY")
        failed.append(sym)
        continue
    df = df.reset_index().rename(columns={"Stock Splits": "Stock_Splits"})
    df["date"] = df["Date"].dt.strftime("%Y-%m-%d")
    cur.execute(
        f'CREATE TABLE IF NOT EXISTS "{t}" ("date" TEXT, "Open" REAL, "High" REAL, "Low" REAL, '
        '"Close" REAL, "Volume" INTEGER, "Dividends" REAL, "Stock_Splits" REAL)')
    cur.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS "idx_{t}_keys" ON "{t}" ("date")')
    rows = [(r["date"], fnum(r["Open"]), fnum(r["High"]), fnum(r["Low"]), fnum(r["Close"]),
             int(fnum(r["Volume"])), fnum(r.get("Dividends", 0)), fnum(r.get("Stock_Splits", 0)))
            for _, r in df.iterrows()]
    cur.executemany(
        f'INSERT OR REPLACE INTO "{t}" '
        '("date","Open","High","Low","Close","Volume","Dividends","Stock_Splits") VALUES (?,?,?,?,?,?,?,?)',
        rows)
    fetched.append(sym)
    last = df["date"].iloc[-1] if len(df) else "?"
    print(f"{sym}: {len(rows)} rows (last {last})")
conn.commit()

# ensure indicator_rules exist for poolA members (idempotent) so research_refresh can compute them
for sym in pool_a:
    tbl = f"scraw_{sym.lower()}_daily"
    for suf, op, params, vc in SPECS:
        name = f"{sym}_{suf}"
        cur.execute(
            "INSERT OR IGNORE INTO indicator_rules "
            "(name,datasource,function_name,source_table,date_column,value_column,op,params_json,indicator_name,enabled) "
            "VALUES (?,?,?,?,?,?,?,?,?,1)",
            (name, "yfinance", tbl, tbl, "date", vc, op, params, name))
conn.commit()
conn.close()
print(f"\nDONE: fetched {len(fetched)}/{len(syms)} (failed {len(failed)}: {failed})")
print(f"latest dates: " + ", ".join(
    f"{s}=" + (sqlite3.connect(DB).execute(
        f'SELECT date FROM "scraw_{s.lower()}_daily" ORDER BY date DESC LIMIT 1').fetchone() or ['?'])[0]
    for s in syms[:3]) + " ...")
