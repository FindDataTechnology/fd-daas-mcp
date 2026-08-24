#!/usr/bin/env python3
"""US top-300-by-turnover screener -> pool A/B for the leaders strategy.

Universe: S&P 500 constituents (GitHub raw datasets/s-and-p-500-companies).
For each symbol: yfinance 1y daily -> 20d avg turnover (Close*Volume) +
7/20/60/120d cumulative returns. Rank by turnover -> top 300. Pool A = Top5
by each return period (union); Pool B = Top15 (union). Persist to
scraw_us_top300_screen (with in_pool_a / in_pool_b flags) for the dynamic
entity-collection rule + dashboard.

    uv run --with yfinance --with pandas --with requests --with lxml python \
        /tmp/us_top300_screener.py
"""
import io, json, sqlite3, shutil, math, time
from pathlib import Path
import requests
import pandas as pd
import yfinance as yf

DB = Path("daas.db").resolve()
shutil.copy2(DB, DB.with_suffix(".db.bak"))

SP500_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv"
CHUNK = 50  # tickers per yfinance download call (threads=True via proxy for speed)


def fetch_universe():
    r = requests.get(SP500_URL, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    # normalize yfinance symbol format: BRK.B -> BRK-B
    df["symbol"] = df["Symbol"].str.replace(".", "-", regex=False)
    return df[["symbol", "Security", "GICS Sector"]].to_dict("records")


def fnum(v):
    try:
        x = float(v)
        return None if math.isnan(x) else x
    except (TypeError, ValueError):
        return None


def main():
    univ = fetch_universe()
    print(f"universe: {len(univ)} S&P 500 constituents")

    syms = [u["symbol"] for u in univ]
    sec = {u["symbol"]: u["Security"] for u in univ}

    rows = {}  # symbol -> metrics
    for i in range(0, len(syms), CHUNK):
        chunk = syms[i:i + CHUNK]
        try:
            d = yf.download(chunk, period="1y", interval="1d", group_by="ticker",
                            auto_adjust=False, progress=False, threads=True)
        except Exception as e:
            print(f"  chunk {i}:{i+len(chunk)} download fail: {str(e)[:80]}")
            continue
        for s in chunk:
            try:
                if isinstance(d.columns, pd.MultiIndex):
                    sub = d[s] if s in d.columns.get_level_values(0) else None
                else:  # single-ticker chunk
                    sub = d if chunk[0] == s else None
                if sub is None or sub.empty:
                    continue
                close = sub["Close"].dropna()
                vol = sub["Volume"].dropna()
                if len(close) < 121:
                    continue
                c = close.tolist()
                v = vol.tolist()
                # align: use last 121+ for returns, last 20 for turnover
                last_close = c[-1]
                avg_to = float((close.iloc[-20:] * vol.iloc[-20:]).mean())
                def ret(n):
                    if len(c) < n + 1:
                        return None
                    a, b = c[-1], c[-1 - n]
                    if b in (None, 0):
                        return None
                    return (a / b - 1.0) * 100.0
                rows[s] = {
                    "symbol": s, "security": sec.get(s, s),
                    "avg_turnover_20d": avg_to,
                    "ret_7d": ret(7), "ret_20d": ret(20),
                    "ret_60d": ret(60), "ret_120d": ret(120),
                }
            except Exception:
                continue
        print(f"  chunk {i}:{i+len(chunk)} -> total {len(rows)} ok", flush=True)
        time.sleep(0.3)

    print(f"downloaded metrics for {len(rows)} symbols")

    # rank by turnover -> top 300
    ranked = sorted(rows.values(), key=lambda r: r["avg_turnover_20d"] or 0, reverse=True)
    top300 = ranked[:300]
    for i, r in enumerate(top300, 1):
        r["turnover_rank"] = i

    # pool A = Top5 by each return period (union); pool B = Top15
    pool_a, pool_b = set(), set()
    for n in ("ret_7d", "ret_20d", "ret_60d", "ret_120d"):
        valid = [r for r in top300 if r[n] is not None]
        valid.sort(key=lambda r: r[n], reverse=True)
        for r in valid[:5]:
            pool_a.add(r["symbol"])
        for r in valid[:15]:
            pool_b.add(r["symbol"])
    for r in top300:
        r["in_pool_a"] = 1 if r["symbol"] in pool_a else 0
        r["in_pool_b"] = 1 if r["symbol"] in pool_b else 0

    # persist
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute('DROP TABLE IF EXISTS scraw_us_top300_screen')
    conn.execute(
        'CREATE TABLE scraw_us_top300_screen (symbol TEXT PRIMARY KEY, security TEXT, '
        'avg_turnover_20d REAL, ret_7d REAL, ret_20d REAL, ret_60d REAL, ret_120d REAL, '
        'turnover_rank INTEGER, in_pool_a INTEGER, in_pool_b INTEGER, as_of TEXT)'
    )
    today = top300[0]["symbol"] and __import__("datetime").date.today().isoformat()
    conn.executemany(
        'INSERT OR REPLACE INTO scraw_us_top300_screen '
        '(symbol,security,avg_turnover_20d,ret_7d,ret_20d,ret_60d,ret_120d,turnover_rank,in_pool_a,in_pool_b,as_of) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        [(r["symbol"], r["security"], r["avg_turnover_20d"], r["ret_7d"], r["ret_20d"],
          r["ret_60d"], r["ret_120d"], r["turnover_rank"], r["in_pool_a"], r["in_pool_b"], today)
         for r in top300],
    )
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM scraw_us_top300_screen").fetchone()[0]
    conn.close()
    print(f"wrote scraw_us_top300_screen: {n} rows (as_of {today})")
    print(f"pool A ({len(pool_a)}): {sorted(pool_a)}")
    print(f"pool B ({len(pool_b)}): {sorted(pool_b)}")


if __name__ == "__main__":
    main()
