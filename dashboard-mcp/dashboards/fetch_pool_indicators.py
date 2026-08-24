#!/usr/bin/env python3
"""Register + fetch + indicator for pool A symbols (dynamic leaders)."""
import sqlite3, shutil, sys, math
from pathlib import Path
import yfinance as yf

DB = Path("daas.db").resolve()
SCRIPTS = Path(".claude/skills/fd-daas-based-data-fetch/scripts").resolve()
sys.path.insert(0, str(SCRIPTS))
import run_indicator as ri

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
pool_a = [r[0] for r in cur.execute(
    "SELECT symbol FROM scraw_us_top300_screen WHERE in_pool_a=1 ORDER BY symbol").fetchall()]
print(f"pool A: {len(pool_a)} -> {pool_a}")

# fetch daily OHLCV
fetched = []
for sym in pool_a:
    t = f"scraw_{sym.lower()}_daily"
    try:
        df = yf.Ticker(sym).history(period="1y", auto_adjust=False)
    except Exception as e:
        print(f"{sym}: FETCH FAIL {e}")
        continue
    if df is None or len(df) == 0:
        print(f"{sym}: EMPTY")
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
    print(f"{sym}: {len(rows)} rows")
conn.commit()
conn.close()
print(f"fetched: {len(fetched)}/{len(pool_a)}")

# create + run indicators
ok = fail = 0
for sym in fetched:
    tbl = f"scraw_{sym.lower()}_daily"
    for suf, op, params, vc in SPECS:
        name = f"{sym}_{suf}"
        cur2 = sqlite3.connect(DB); cur2.execute("PRAGMA foreign_keys=ON")
        cur2.execute(
            "INSERT OR IGNORE INTO indicator_rules "
            "(name,datasource,function_name,source_table,date_column,value_column,op,params_json,indicator_name,enabled) "
            "VALUES (?,?,?,?,?,?,?,?,?,1)",
            (name, "yfinance", tbl, tbl, "date", vc, op, params, name))
        cur2.commit(); cur2.close()
        res = ri.run_indicator(name)
        if "error" in res:
            fail += 1; print(f"{name}: ERROR {res['error']}")
        else:
            ok += 1
print(f"indicators: ok={ok} fail={fail}")
