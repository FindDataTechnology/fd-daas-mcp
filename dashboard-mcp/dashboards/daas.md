# Dashboards

| Title | Intro | URL | Source | Refresh |
|---|---|---|---|---|
| 强势股趋势监控 — Phase 1 MVP | Livermore×O'Neil 强势股趋势监控看板。展示美股 leadership 候选标的的日行情（OHLCV）与趋势指标（MA5/10/20、RSI14、20日波动率、20日新高）每日快照，用于识别处于趋势确立 / 加速阶段的强势股。Phase 1 MVP。 | [us-leaders-trend-monitor.html](us-leaders-trend-monitor.html) | scraw_aapl_daily, scraw_amd_daily, scraw_amzn_daily, scraw_avgo_daily, scraw_googl_daily, scraw_meta_daily, scraw_msft_daily, scraw_nflx_daily, scraw_nvda_daily, scraw_qqq_daily, scraw_spy_daily, scraw_tsla_daily, observations | daily 04:30 fetch / 04:45 indicators (Asia/Shanghai); rebuild HTML via build_us_leaders_dashboard.py |
| AAPL 日行情快照 | Apple Inc. (AAPL) 日收盘价看板。展示 scraw_aapl_daily 表中的每日收盘价，支持按时间区间筛选，用于快速查看 AAPL 近一年走势。 | [aapl-daily-snapshot.html](aapl-daily-snapshot.html) | scraw_aapl_daily | static snapshot (rebuild via fd-daas-dashboard-creator) |
