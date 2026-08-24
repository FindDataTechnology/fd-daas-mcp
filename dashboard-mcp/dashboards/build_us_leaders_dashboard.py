#!/usr/bin/env python3
"""Build the 强势股趋势监控 Phase 2 dashboard (dynamic pool) as self-contained HTML.

Phase 2 (dynamic): leaders = `us_leadership_pool` collection membership, which is
pool A = Top5-by-return (7/20/60/120d, union) from the top-300-by-turnover screen
(scraw_us_top300_screen). Re-run after refreshing the screen + syncing the
collection. Reads canonical repo-root daas.db; loads observations by `indicator`
(unique). Vendored Chart.js. Stdlib only.

    python3 fd-daas-mcp/dashboard-mcp/dashboards/build_us_leaders_dashboard.py
"""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

THIS = Path(__file__).resolve()
REPO = THIS.parent.parent.parent.parent
DB = REPO / "daas.db"
OUT = THIS.parent / "us-leaders-trend-monitor.html"

INDEXES = ["SPY", "QQQ"]
METRICS = ["ma5", "ma10", "ma20", "rsi14", "volstd20", "high20"]
RS_PERIODS = [7, 20, 60, 120]


def fnum(x):
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if not math.isnan(v) else None


def load_symbol(conn, sym):
    cur = conn.cursor()
    rows = cur.execute(
        f'SELECT date, Close, High FROM scraw_{sym.lower()}_daily ORDER BY date ASC'
    ).fetchall()
    dates = [r[0] for r in rows]
    close = [fnum(r[1]) for r in rows]
    high = [fnum(r[2]) for r in rows]
    out = {"dates": dates, "close": close, "high": high}
    for m in METRICS:
        mrows = cur.execute(
            "SELECT date, value FROM observations WHERE source='yfinance' AND indicator=? "
            "ORDER BY date ASC",
            (f"{sym}_{m}",),
        ).fetchall()
        mdict = {r[0]: fnum(r[1]) for r in mrows}
        out[m] = [mdict.get(d) for d in dates]
    return out


def last(arr):
    for v in reversed(arr):
        if v is not None:
            return v
    return None


def ret_n(close, n):
    if len(close) < n + 1:
        return None
    a, b = close[-1], close[-1 - n]
    if a is None or b is None or b == 0:
        return None
    return (a / b - 1.0) * 100.0


def new_high20(close):
    if len(close) < 21:
        return False
    today = close[-1]
    lookback = close[-21:-1]
    if today is None or any(v is None for v in lookback):
        return False
    return today >= max(lookback)


def main():
    conn = sqlite3.connect(str(DB))
    try:
        leaders = [r[0] for r in conn.execute(
            "SELECT ci.code FROM entity_collection_items ci "
            "JOIN entity_collections c ON c.id=ci.collection_id "
            "WHERE c.name='us_leadership_pool' ORDER BY ci.code").fetchall()]
        symbols = INDEXES + leaders
        series = {sym: load_symbol(conn, sym) for sym in symbols}
        turnover_rank = {r[0]: r[1] for r in conn.execute(
            "SELECT symbol, turnover_rank FROM scraw_us_top300_screen").fetchall()}
    finally:
        conn.close()

    latest = []
    for sym in leaders:
        s = series[sym]
        close = last(s["close"])
        high20 = last(s["high20"])
        dist = None
        if close is not None and high20 not in (None, 0):
            dist = (close - high20) / high20 * 100.0
        latest.append({
            "symbol": sym,
            "close": close,
            "ma5": last(s["ma5"]), "ma10": last(s["ma10"]), "ma20": last(s["ma20"]),
            "rsi14": last(s["rsi14"]), "volstd20": last(s["volstd20"]), "high20": high20,
            "dist_from_high": dist,
            "ret_7d": ret_n(s["close"], 7), "ret_20d": ret_n(s["close"], 20),
            "ret_60d": ret_n(s["close"], 60), "ret_120d": ret_n(s["close"], 120),
            "new_high20": new_high20(s["close"]),
            "turnover_rank": turnover_rank.get(sym),
        })

    # 60d return rank within the pool (1 = strongest)
    ranked60 = sorted([r for r in latest if r["ret_60d"] is not None],
                      key=lambda r: r["ret_60d"], reverse=True)
    rs_rank_60d = {r["symbol"]: i for i, r in enumerate(ranked60, 1)}
    top5_60d = set(r["symbol"] for r in ranked60[:5])
    for r in latest:
        r["rs_rank_60d"] = rs_rank_60d.get(r["symbol"])
        r["top5_60d"] = r["symbol"] in top5_60d

    payload = {
        "indexes": INDEXES,
        "leaders": leaders,
        "symbols": symbols,
        "lastDate": max((s["dates"][-1] for s in series.values() if s["dates"]), default=""),
        "series": series,
        "latest": latest,
        "top5_60d": sorted(top5_60d),
    }

    html = HTML_TEMPLATE.replace("__PAYLOAD__", json.dumps(payload, separators=(",", ":")))
    OUT.write_text(html, encoding="utf-8")
    print(f"Wrote {OUT}  ({OUT.stat().st_size:,} bytes)")
    print(f"  leaders: {len(leaders)}  last date: {payload['lastDate']}")
    print(f"  leaders: {leaders}")


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>强势股趋势监控 - Phase 2 (动态池A)</title>
<script src="vendor/chart.umd.min.js"></script>
<script>
if (typeof Chart === 'undefined') {
  document.querySelectorAll('.chart-box').forEach(b => {
    b.innerHTML = '<div class="cdn-fail">⚠ Chart.js 未加载 - 请确认 vendor/chart.umd.min.js 存在</div>';
  });
}
</script>
<style>
  :root { --bg:#0f1419; --panel:#1a2028; --ink:#e4e7eb; --dim:#8b95a5; --accent:#3b82f6; --green:#22c55e; --red:#ef4444; --amber:#f59e0b; --top:#a855f7; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--ink); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; font-size:14px; }
  header { padding:1rem 1.5rem; border-bottom:1px solid #2a3340; }
  header h1 { margin:0; font-size:1.25rem; font-weight:600; }
  header .sub { color:var(--dim); font-size:0.8rem; margin-top:0.25rem; }
  .wrap { max-width:1500px; margin:0 auto; padding:1rem 1.5rem 4rem; }
  .panel { background:var(--panel); border:1px solid #2a3340; border-radius:8px; padding:1rem 1.25rem; margin-bottom:1.25rem; }
  .panel h2 { margin:0 0 0.75rem; font-size:1rem; font-weight:600; color:var(--ink); }
  .hint { color:var(--dim); font-size:0.78rem; margin-bottom:0.75rem; }
  table { width:100%; border-collapse:collapse; }
  th, td { padding:0.4rem 0.5rem; text-align:right; white-space:nowrap; }
  th { color:var(--dim); font-weight:500; font-size:0.7rem; text-transform:uppercase; letter-spacing:0.03em; border-bottom:1px solid #2a3340; cursor:pointer; user-select:none; }
  th:hover { color:var(--ink); }
  th.sym, td.sym { text-align:left; }
  th.sort-asc::after { content:' ▲'; color:var(--accent); }
  th.sort-desc::after { content:' ▼'; color:var(--accent); }
  tbody tr { cursor:pointer; border-bottom:1px solid #1f2630; }
  tbody tr:hover { background:#222b36; }
  tbody tr.sel { background:#1e3a5f; }
  tbody tr.top5 { background:rgba(168,85,247,0.10); }
  td .tag { font-size:0.62rem; padding:1px 5px; border-radius:3px; margin-left:4px; vertical-align:middle; }
  .tag.breakout { background:rgba(34,197,94,0.18); color:var(--green); }
  .tag.oversold { background:rgba(239,68,68,0.18); color:var(--red); }
  .tag.top5 { background:rgba(168,85,247,0.20); color:var(--top); }
  .num-neg { color:var(--red); } .num-pos { color:var(--green); }
  .grids { display:grid; grid-template-columns:1fr 1fr; gap:1.25rem; }
  @media (max-width:1000px){ .grids{ grid-template-columns:1fr; } }
  .chart-box { position:relative; height:340px; }
  select { background:#0f1419; color:var(--ink); border:1px solid #2a3340; border-radius:4px; padding:0.3rem 0.5rem; font-size:0.85rem; }
  .ctrl { display:flex; align-items:center; gap:0.6rem; margin-bottom:0.5rem; }
  .ctrl label { color:var(--dim); font-size:0.8rem; }
  .cdn-fail { color:var(--amber); padding:1rem; }
  .scroll-x { overflow-x:auto; }
</style>
</head>
<body>
<header>
  <h1>强势股趋势监控 - Phase 2 <span style="color:var(--dim);font-weight:400;font-size:0.85rem;">(动态池A · top300成交额筛出)</span></h1>
  <div class="sub">龙头池 = 池A (S&P500 top300成交额 × 7/20/60/120日涨幅Top5并集, 动态) + SPY/QQQ 基准 · 数据截至 <span id="lastDate">-</span> · 日K快照(重跑 build_us_leaders_dashboard.py 刷新)</div>
</header>
<div class="wrap">

  <div class="panel">
    <h2>池A 龙头榜 - 多周期相对强度 (点击表头排序 · 点击行看详情 · 紫底=60日Top5)</h2>
    <div class="hint">绿标=距20日高点-2%内(突破位) · 红标=RSI14&lt;30(超卖回踩买点) · 紫=60日涨幅Top5 · 成交额排名=top300内名次 · 涨幅%为7/20/60/120日累计</div>
    <div class="scroll-x">
    <table id="board">
      <thead><tr>
        <th class="sym" data-k="symbol">Symbol</th>
        <th data-k="turnover_rank">成交额排名</th>
        <th data-k="close">Close</th>
        <th data-k="ma5">MA5</th><th data-k="ma10">MA10</th><th data-k="ma20">MA20</th>
        <th data-k="rsi14">RSI14</th><th data-k="volstd20">volstd20</th><th data-k="high20">high20</th>
        <th data-k="dist_from_high">距高点%</th>
        <th data-k="ret_7d">7d%</th><th data-k="ret_20d">20d%</th><th data-k="ret_60d">60d%</th><th data-k="ret_120d">120d%</th>
        <th data-k="new_high20">新高</th><th data-k="rs_rank_60d">60d排名</th>
      </tr></thead>
      <tbody id="boardBody"></tbody>
    </table>
    </div>
  </div>

  <div class="grids">
    <div class="panel">
      <h2>相对强度排名 - 60日涨幅 (Top5 紫色)</h2>
      <div class="chart-box" style="height:420px;"><canvas id="rsChart"></canvas></div>
    </div>
    <div class="panel">
      <h2>大盘温度计 - SPY(左轴+MA+20日高) / QQQ(右轴)</h2>
      <div class="chart-box"><canvas id="regimeChart"></canvas></div>
    </div>
  </div>

  <div class="grids">
    <div class="panel">
      <div class="ctrl"><label>龙头详情:</label><select id="leaderSel"></select></div>
      <h2>详情 - Close + MA20 + high20(左轴) / volstd20(右轴)</h2>
      <div class="chart-box"><canvas id="detailChart"></canvas></div>
    </div>
    <div class="panel">
      <h2>RSI14 全龙头 (30/70带, 选中高亮)</h2>
      <div class="chart-box" style="height:340px;"><canvas id="rsiChart"></canvas></div>
    </div>
  </div>

  <div class="panel">
    <div class="hint">Phase 2 动态池: 票池由 top300 成交额筛出 + RS Top5并集 自动派生 (scraw_us_top300_screen + us_leadership_pool rule_script). 预警代理: 飞书 RSI突破70 / SPY·QQQ日跌&gt;1%. 暂不含: 2h K线 / HV分位 / 复合突破·回踩信号 / 次新股通道 (见 construction/leaders-strategy-roadmap.md). 注: 票池同步由 daas_sync_entity_collection (RuleEngine, script 规则) 驱动; 旧的 manual_sync_pool.py 已移除. 本页为构建时快照.</div>
  </div>
</div>

<script>
const D = __PAYLOAD__;
function fmt(v, d=2){ return (v===null||v===undefined||isNaN(v)) ? "-" : Number(v).toFixed(d); }
function pct(v){ return (v===null||v===undefined||isNaN(v)) ? "-" : (v>=0?"+":"")+v.toFixed(2)+"%"; }
function cls(v){ return (v===null||v===undefined||isNaN(v)) ? "" : (v<0?'num-neg':'num-pos'); }
document.getElementById('lastDate').textContent = D.lastDate;

let sortKey = 'rs_rank_60d', sortDir = 1;
const numKeys = new Set(['turnover_rank','close','ma5','ma10','ma20','rsi14','volstd20','high20','dist_from_high','ret_7d','ret_20d','ret_60d','ret_120d','rs_rank_60d']);
function renderBoard(){
  const tbody = document.getElementById('boardBody'); tbody.innerHTML = '';
  const rows = D.latest.slice().sort((a,b)=>{
    const k = sortKey;
    if (k === 'symbol') return sortDir * a.symbol.localeCompare(b.symbol);
    if (k === 'new_high20') return sortDir * ((a.new_high20?1:0) - (b.new_high20?1:0));
    const av = a[k], bv = b[k];
    if (av === null || av === undefined) return 1;
    if (bv === null || bv === undefined) return -1;
    return sortDir * (av - bv);
  });
  rows.forEach(r => {
    const distBreakout = r.dist_from_high !== null && r.dist_from_high >= -2 && r.dist_from_high <= 0;
    const oversold = r.rsi14 !== null && r.rsi14 < 30;
    const distCls = r.dist_from_high !== null && r.dist_from_high < 0 ? 'num-neg' : (r.dist_from_high>0?'num-pos':'');
    const tr = document.createElement('tr');
    if (r.top5_60d) tr.classList.add('top5');
    tr.dataset.sym = r.symbol;
    tr.innerHTML = `<td class="sym">${r.symbol}${r.top5_60d?'<span class="tag top5">60dTop5</span>':''}${distBreakout?'<span class="tag breakout">突破位</span>':''}${oversold?'<span class="tag oversold">超卖</span>':''}</td>`
      + `<td>${r.turnover_rank ?? '-'}</td>`
      + `<td>${fmt(r.close)}</td><td>${fmt(r.ma5)}</td><td>${fmt(r.ma10)}</td><td>${fmt(r.ma20)}</td>`
      + `<td>${fmt(r.rsi14,1)}</td><td>${fmt(r.volstd20,3)}</td><td>${fmt(r.high20)}</td>`
      + `<td class="${distCls}">${pct(r.dist_from_high)}</td>`
      + `<td class="${cls(r.ret_7d)}">${pct(r.ret_7d)}</td><td class="${cls(r.ret_20d)}">${pct(r.ret_20d)}</td>`
      + `<td class="${cls(r.ret_60d)}">${pct(r.ret_60d)}</td><td class="${cls(r.ret_120d)}">${pct(r.ret_120d)}</td>`
      + `<td>${r.new_high20?'<span style="color:var(--green)">✓</span>':'-'}</td>`
      + `<td>${r.rs_rank_60d ?? '-'}</td>`;
    tr.addEventListener('click', () => selectLeader(r.symbol));
    tbody.appendChild(tr);
  });
  document.querySelectorAll('#board thead th').forEach(th => {
    th.classList.remove('sort-asc','sort-desc');
    if (th.dataset.k === sortKey) th.classList.add(sortDir === 1 ? 'sort-asc' : 'sort-desc');
  });
}
document.querySelectorAll('#board thead th').forEach(th => {
  th.addEventListener('click', () => {
    const k = th.dataset.k;
    if (sortKey === k) sortDir = -sortDir; else { sortKey = k; sortDir = 1; }
    renderBoard();
  });
});
renderBoard();

const sel = document.getElementById('leaderSel');
D.leaders.forEach(s => { const o=document.createElement('option'); o.value=s; o.textContent=s; sel.appendChild(o); });
sel.addEventListener('change', () => selectLeader(sel.value));
function selectLeader(sym){
  sel.value = sym;
  document.querySelectorAll('#boardBody tr').forEach(tr => tr.classList.toggle('sel', tr.dataset.sym===sym));
  renderDetail(sym);
  if (rsiChart) { rsiChart.data.datasets.forEach(ds => ds.borderColor = ds._sym===sym ? ds._hi : ds._lo); rsiChart.update('none'); }
}
function align(sym, key, refDates){
  const s = D.series[sym];
  if (s.dates.length === refDates.length && s.dates.every((d,i)=>d===refDates[i])) return s[key];
  const m = {}; s.dates.forEach((d,i)=>{ if(s[key][i]!==null) m[d]=s[key][i]; });
  return refDates.map(d => m.hasOwnProperty(d) ? m[d] : null);
}
function mkDs(label, data, color, yid){ return { label, data, borderColor:color, backgroundColor:color, yAxisID:yid, pointRadius:0, borderWidth:1.4, tension:0.25, spanGaps:true }; }
const X_TICKS = { color:'#8b95a5', maxTicksLimit:8, autoSkip:true };
const X_GRID = { color:'#1f2630' };
const Y_LEFT = { position:'left', grid:{color:'#1f2630'}, ticks:{color:'#8b95a5'} };
function bandLine(val, color, label, refDates){ return { label, data: refDates.map(()=>val), borderColor:color, backgroundColor:color, borderWidth:1, borderDash:[4,4], pointRadius:0, tension:0 }; }

const top5set = new Set(D.top5_60d);
const rsRows = D.latest.filter(r => r.ret_60d !== null).sort((a,b)=>a.ret_60d-b.ret_60d);
new Chart(document.getElementById('rsChart'), {
  type:'bar',
  data:{ labels: rsRows.map(r=>r.symbol),
    datasets:[{ label:'60d %', data: rsRows.map(r=>r.ret_60d),
      backgroundColor: rsRows.map(r=> top5set.has(r.symbol) ? '#a855f7' : '#3b82f6'), borderWidth:0 }] },
  options:{ indexAxis:'y', responsive:true, maintainAspectRatio:false,
    scales:{ x:{ grid:X_GRID, ticks:{color:'#8b95a5', callback:v=>v+'%'} }, y:{ grid:{display:false}, ticks:{color:'#e4e7eb', font:{size:11}} } },
    plugins:{ legend:{display:false}, tooltip:{callbacks:{label:c=>c.raw.toFixed(2)+'%'}} } }
});

const refDates = D.series.SPY.dates;
new Chart(document.getElementById('regimeChart'), {
  type:'line',
  data:{ labels: refDates, datasets:[
    mkDs('SPY Close', align('SPY','close',refDates), '#e4e7eb','y'),
    mkDs('SPY MA5',   align('SPY','ma5',refDates),   '#f59e0b','y'),
    mkDs('SPY MA10',  align('SPY','ma10',refDates),  '#3b82f6','y'),
    mkDs('SPY MA20',  align('SPY','ma20',refDates),  '#a855f7','y'),
    mkDs('SPY 20d High', align('SPY','high20',refDates), '#22c55e','y'),
    mkDs('QQQ Close', align('QQQ','close',refDates), '#06b6d4','y1'),
  ]},
  options:{ responsive:true, maintainAspectRatio:false, interaction:{ mode:'index', intersect:false },
    scales:{ x:{ type:'category', grid:X_GRID, ticks:X_TICKS }, y:Y_LEFT, y1:{ position:'right', grid:{display:false}, ticks:{color:'#06b6d4'} } },
    plugins:{ legend:{ labels:{color:'#8b95a5', boxWidth:12, font:{size:11}} } } }
});

let detailChart = null;
function renderDetail(sym){
  const s = D.series[sym];
  if (detailChart) detailChart.destroy();
  detailChart = new Chart(document.getElementById('detailChart'), {
    type:'line',
    data:{ labels: s.dates, datasets:[
      mkDs('Close', s.close, '#e4e7eb','y'), mkDs('MA20', s.ma20, '#a855f7','y'),
      mkDs('20d High', s.high20, '#22c55e','y'), mkDs('volstd20 (HV)', s.volstd20, '#f59e0b','y1'),
    ]},
    options:{ responsive:true, maintainAspectRatio:false, interaction:{mode:'index',intersect:false},
      scales:{ x:{ type:'category', grid:X_GRID, ticks:X_TICKS }, y:Y_LEFT, y1:{ position:'right', grid:{display:false}, ticks:{color:'#f59e0b'} } },
      plugins:{ legend:{ labels:{color:'#8b95a5', boxWidth:12, font:{size:11}} } } }
  });
}

const rsiColors = ['#3b82f6','#22c55e','#f59e0b','#ef4444','#a855f7','#06b6d4','#ec4899','#84cc16','#14b8a6','#f97316',
                   '#60a5fa','#4ade80','#fbbf24','#f87171','#c084fc','#22d3ee','#f472b6','#a3e635','#2dd4bf','#fb923c',
                   '#818cf8','#86efac','#fcd34d','#fca5a5','#d8b4fe','#67e8f9','#f9a8d4','#bef264','#5eead4','#fdba74'];
const rsiDs = [
  bandLine(70,'#ef4444','70',refDates), bandLine(30,'#22c55e','30',refDates),
  ...D.leaders.map((sym,i)=>{
    const c = rsiColors[i%rsiColors.length];
    return { label:sym, _sym:sym, _hi:c, _lo:'rgba(139,149,165,0.25)', data: align(sym,'rsi14',refDates),
      borderColor:c, backgroundColor:c, borderWidth:1.1, pointRadius:0, tension:0.25, spanGaps:true };
  }),
];
const rsiChart = new Chart(document.getElementById('rsiChart'), {
  type:'line', data:{ labels: refDates, datasets: rsiDs },
  options:{ responsive:true, maintainAspectRatio:false, interaction:{mode:'index',intersect:false},
    scales:{ x:{ type:'category', grid:X_GRID, ticks:X_TICKS }, y:{ min:0, max:100, grid:X_GRID, ticks:{color:'#8b95a5'} } },
    plugins:{ legend:{ labels:{color:'#8b95a5', boxWidth:10, font:{size:10}} } } }
});
selectLeader(D.leaders[0]);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
