# Research: us-leaders-trend

- **Status:** draft
- **Generated:** 2026-11-25T13:25:57.762385+00:00

Livermore×O'Neil 美股强势股趋势监控研究包. 动态池A (S&P500 top300成交额筛出 × 7/20/60/120日涨幅Top5并集) + SPY/QQQ基准. 组件: us_leadership_pool 实体集合(17只动态池A) / us-leaders-indicators 指标集合(324规则,54标的) / us-leaders-trend-monitor 看板 / 12飞书预警代理. 2026-07-23 刷新: 数据至 2026-07-22, poolA 换血6只(新增 ABT/CTAS/HPE/SMCI/TRV/VLO; 剔除 AXON/CBOE/FTNT/META/MRNA/STX). 详细分析(缺口/决策/延后工作)见 construction/leaders-strategy-roadmap.md.

## Entities

| Code | Type |
|---|---|
| DDOG | stock |
| DELL | stock |
| HUM | stock |
| MPC | stock |
| MRVL | stock |
| MU | stock |
| PANW | stock |
| PSX | stock |
| PYPL | stock |
| SNDK | stock |
| TECH | stock |
| SMCI | stock |
| ABT | stock |
| HPE | stock |
| VLO | stock |
| TRV | stock |
| CTAS | stock |

## Indicators

| Indicator | Op | Latest Date | Latest Value | Rule |
|---|---|---|---|---|
| ABT_ma5 | sma {'window': 5} | 2026-07-22 | 100.28200073242188 | ABT_ma5 |
| ABT_ma10 | sma {'window': 10} | 2026-07-22 | 96.00800018310547 | ABT_ma10 |
| ABT_ma20 | sma {'window': 20} | 2026-07-22 | 94.78049964904785 | ABT_ma20 |
| ABT_rsi14 | rsi {'window': 14} | 2026-07-22 | 63.88813359751294 | ABT_rsi14 |
| ABT_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 3.833232904835669 | ABT_volstd20 |
| ABT_high20 | rolling_max {'window': 20} | 2026-07-22 | 102.45999908447266 | ABT_high20 |
| CTAS_ma5 | sma {'window': 5} | 2026-07-22 | 202.8540008544922 | CTAS_ma5 |
| CTAS_ma10 | sma {'window': 10} | 2026-07-22 | 193.2050003051758 | CTAS_ma10 |
| CTAS_ma20 | sma {'window': 20} | 2026-07-22 | 183.9484992980957 | CTAS_ma20 |
| CTAS_rsi14 | rsi {'window': 14} | 2026-07-22 | 68.94380069589958 | CTAS_rsi14 |
| CTAS_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 12.650369388112301 | CTAS_volstd20 |
| CTAS_high20 | rolling_max {'window': 20} | 2026-07-22 | 209.72000122070312 | CTAS_high20 |
| HPE_ma5 | sma {'window': 5} | 2026-07-22 | 46.072000885009764 | HPE_ma5 |
| HPE_ma10 | sma {'window': 10} | 2026-07-22 | 47.2200008392334 | HPE_ma10 |
| HPE_ma20 | sma {'window': 20} | 2026-07-22 | 45.868500518798825 | HPE_ma20 |
| HPE_rsi14 | rsi {'window': 14} | 2026-07-22 | 55.879122683302796 | HPE_rsi14 |
| HPE_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 2.2910842711088426 | HPE_volstd20 |
| HPE_high20 | rolling_max {'window': 20} | 2026-07-22 | 51.08000183105469 | HPE_high20 |
| SMCI_ma5 | sma {'window': 5} | 2026-07-22 | 25.75 | SMCI_ma5 |
| SMCI_ma10 | sma {'window': 10} | 2026-07-22 | 26.749999809265137 | SMCI_ma10 |
| SMCI_ma20 | sma {'window': 20} | 2026-07-22 | 27.810999870300293 | SMCI_ma20 |
| SMCI_rsi14 | rsi {'window': 14} | 2026-07-22 | 53.91440178710445 | SMCI_rsi14 |
| SMCI_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 2.3311210162059273 | SMCI_volstd20 |
| SMCI_high20 | rolling_max {'window': 20} | 2026-07-22 | 33.97999954223633 | SMCI_high20 |
| TRV_ma5 | sma {'window': 5} | 2026-07-22 | 363.402001953125 | TRV_ma5 |
| TRV_ma10 | sma {'window': 10} | 2026-07-22 | 350.09400329589846 | TRV_ma10 |
| TRV_ma20 | sma {'window': 20} | 2026-07-22 | 341.3555023193359 | TRV_ma20 |
| TRV_rsi14 | rsi {'window': 14} | 2026-07-22 | 76.24775785239746 | TRV_rsi14 |
| TRV_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 16.030109524409326 | TRV_volstd20 |
| TRV_high20 | rolling_max {'window': 20} | 2026-07-22 | 374.0 | TRV_high20 |
| VLO_ma5 | sma {'window': 5} | 2026-07-22 | 309.7880004882812 | VLO_ma5 |
| VLO_ma10 | sma {'window': 10} | 2026-07-22 | 300.0760009765625 | VLO_ma10 |
| VLO_ma20 | sma {'window': 20} | 2026-07-22 | 282.0500015258789 | VLO_ma20 |
| VLO_rsi14 | rsi {'window': 14} | 2026-07-22 | 70.46705070814038 | VLO_rsi14 |
| VLO_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 21.685701714955428 | VLO_volstd20 |
| VLO_high20 | rolling_max {'window': 20} | 2026-07-22 | 320.239990234375 | VLO_high20 |
| AAPL_high20 | rolling_max {'window': 20} | 2026-07-22 | 334.989990234375 | AAPL_high20 |
| AAPL_ma10 | sma {'window': 10} | 2026-07-22 | 323.84299926757814 | AAPL_ma10 |
| AAPL_ma20 | sma {'window': 20} | 2026-07-22 | 310.06299896240233 | AAPL_ma20 |
| AAPL_ma5 | sma {'window': 5} | 2026-07-22 | 329.4440002441406 | AAPL_ma5 |
| AAPL_rsi14 | rsi {'window': 14} | 2026-07-22 | 62.5390153697843 | AAPL_rsi14 |
| AAPL_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 17.88127686479506 | AAPL_volstd20 |
| ABBV_high20 | rolling_max {'window': 20} | 2026-07-22 | 261.6400146484375 | ABBV_high20 |
| ABBV_ma10 | sma {'window': 10} | 2026-07-22 | 250.65400238037108 | ABBV_ma10 |
| ABBV_ma20 | sma {'window': 20} | 2026-07-22 | 250.90750122070312 | ABBV_ma20 |
| ABBV_ma5 | sma {'window': 5} | 2026-07-22 | 254.33200378417968 | ABBV_ma5 |
| ABBV_rsi14 | rsi {'window': 14} | 2026-07-22 | 60.89237582121328 | ABBV_rsi14 |
| ABBV_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 5.783082509069482 | ABBV_volstd20 |
| ADBE_high20 | rolling_max {'window': 20} | 2026-07-22 | 240.14999389648438 | ADBE_high20 |
| ADBE_ma10 | sma {'window': 10} | 2026-07-22 | 227.50599975585936 | ADBE_ma10 |
| ADBE_ma20 | sma {'window': 20} | 2026-07-22 | 218.52350006103515 | ADBE_ma20 |
| ADBE_ma5 | sma {'window': 5} | 2026-07-22 | 230.56400146484376 | ADBE_ma5 |
| ADBE_rsi14 | rsi {'window': 14} | 2026-07-22 | 46.177245376125484 | ADBE_rsi14 |
| ADBE_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 12.492179576023313 | ADBE_volstd20 |
| AMD_high20 | rolling_max {'window': 20} | 2026-07-22 | 584.72998046875 | AMD_high20 |
| AMD_ma10 | sma {'window': 10} | 2026-07-22 | 531.3300048828125 | AMD_ma10 |
| AMD_ma20 | sma {'window': 20} | 2026-07-22 | 532.5929992675781 | AMD_ma20 |
| AMD_ma5 | sma {'window': 5} | 2026-07-22 | 519.406005859375 | AMD_ma5 |
| AMD_rsi14 | rsi {'window': 14} | 2026-07-22 | 56.089512790920395 | AMD_rsi14 |
| AMD_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 21.3528374436685 | AMD_volstd20 |
| AMZN_high20 | rolling_max {'window': 20} | 2026-07-22 | 258.0799865722656 | AMZN_high20 |
| AMZN_ma10 | sma {'window': 10} | 2026-07-22 | 248.16500091552734 | AMZN_ma10 |
| AMZN_ma20 | sma {'window': 20} | 2026-07-22 | 243.61149978637695 | AMZN_ma20 |
| AMZN_ma5 | sma {'window': 5} | 2026-07-22 | 247.902001953125 | AMZN_ma5 |
| AMZN_rsi14 | rsi {'window': 14} | 2026-07-22 | 48.402927661481144 | AMZN_rsi14 |
| AMZN_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 6.549167624033465 | AMZN_volstd20 |
| AVGO_high20 | rolling_max {'window': 20} | 2026-07-22 | 407.5199890136719 | AVGO_high20 |
| AVGO_ma10 | sma {'window': 10} | 2026-07-22 | 387.52699584960936 | AVGO_ma10 |
| AVGO_ma20 | sma {'window': 20} | 2026-07-22 | 380.7314987182617 | AVGO_ma20 |
| AVGO_ma5 | sma {'window': 5} | 2026-07-22 | 381.35 | AVGO_ma5 |
| AVGO_rsi14 | rsi {'window': 14} | 2026-07-22 | 53.573785492643445 | AVGO_rsi14 |
| AVGO_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 11.620438538528797 | AVGO_volstd20 |
| AXON_high20 | rolling_max {'window': 20} | 2026-07-22 | 665.0700073242188 | AXON_high20 |
| AXON_ma10 | sma {'window': 10} | 2026-07-22 | 536.5380004882812 | AXON_ma10 |
| AXON_ma20 | sma {'window': 20} | 2026-07-22 | 542.8244995117187 | AXON_ma20 |
| AXON_ma5 | sma {'window': 5} | 2026-07-22 | 516.4339965820312 | AXON_ma5 |
| AXON_rsi14 | rsi {'window': 14} | 2026-07-22 | 44.3524931242485 | AXON_rsi14 |
| AXON_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 54.370022666020226 | AXON_volstd20 |
| BAC_high20 | rolling_max {'window': 20} | 2026-07-22 | 62.119998931884766 | BAC_high20 |
| BAC_ma10 | sma {'window': 10} | 2026-07-22 | 60.66499977111816 | BAC_ma10 |
| BAC_ma20 | sma {'window': 20} | 2026-07-22 | 59.522999954223636 | BAC_ma20 |
| BAC_ma5 | sma {'window': 5} | 2026-07-22 | 61.204000091552736 | BAC_ma5 |
| BAC_rsi14 | rsi {'window': 14} | 2026-07-22 | 68.03286733997352 | BAC_rsi14 |
| BAC_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 1.4719451090253541 | BAC_volstd20 |
| CBOE_high20 | rolling_max {'window': 20} | 2026-07-22 | 291.0299987792969 | CBOE_high20 |
| CBOE_ma10 | sma {'window': 10} | 2026-07-22 | 275.0850006103516 | CBOE_ma10 |
| CBOE_ma20 | sma {'window': 20} | 2026-07-22 | 261.3610015869141 | CBOE_ma20 |
| CBOE_ma5 | sma {'window': 5} | 2026-07-22 | 277.1419982910156 | CBOE_ma5 |
| CBOE_rsi14 | rsi {'window': 14} | 2026-07-22 | 55.8858296696315 | CBOE_rsi14 |
| CBOE_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 15.724789534388675 | CBOE_volstd20 |
| COST_high20 | rolling_max {'window': 20} | 2026-07-22 | 969.8599853515625 | COST_high20 |
| COST_ma10 | sma {'window': 10} | 2026-07-22 | 927.2709899902344 | COST_ma10 |
| COST_ma20 | sma {'window': 20} | 2026-07-22 | 936.8974914550781 | COST_ma20 |
| COST_ma5 | sma {'window': 5} | 2026-07-22 | 935.7539916992188 | COST_ma5 |
| COST_rsi14 | rsi {'window': 14} | 2026-07-22 | 42.975148744181126 | COST_rsi14 |
| COST_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 14.27830514080064 | COST_volstd20 |
| CRM_high20 | rolling_max {'window': 20} | 2026-07-22 | 175.42999267578125 | CRM_high20 |
| CRM_ma10 | sma {'window': 10} | 2026-07-22 | 168.18999938964845 | CRM_ma10 |
| CRM_ma20 | sma {'window': 20} | 2026-07-22 | 164.44499893188475 | CRM_ma20 |
| CRM_ma5 | sma {'window': 5} | 2026-07-22 | 170.05999755859375 | CRM_ma5 |
| CRM_rsi14 | rsi {'window': 14} | 2026-07-22 | 45.43531891274962 | CRM_rsi14 |
| CRM_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 6.535982472638569 | CRM_volstd20 |
| CSCO_high20 | rolling_max {'window': 20} | 2026-07-22 | 122.88999938964844 | CSCO_high20 |
| CSCO_ma10 | sma {'window': 10} | 2026-07-22 | 114.44199905395507 | CSCO_ma10 |
| CSCO_ma20 | sma {'window': 20} | 2026-07-22 | 115.06699981689454 | CSCO_ma20 |
| CSCO_ma5 | sma {'window': 5} | 2026-07-22 | 111.33800048828125 | CSCO_ma5 |
| CSCO_rsi14 | rsi {'window': 14} | 2026-07-22 | 45.41565184734191 | CSCO_rsi14 |
| CSCO_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 3.484952385858495 | CSCO_volstd20 |
| CVX_high20 | rolling_max {'window': 20} | 2026-07-22 | 193.6999969482422 | CVX_high20 |
| CVX_ma10 | sma {'window': 10} | 2026-07-22 | 184.1010009765625 | CVX_ma10 |
| CVX_ma20 | sma {'window': 20} | 2026-07-22 | 177.14800033569335 | CVX_ma20 |
| CVX_ma5 | sma {'window': 5} | 2026-07-22 | 189.0000030517578 | CVX_ma5 |
| CVX_rsi14 | rsi {'window': 14} | 2026-07-22 | 69.60420074040663 | CVX_rsi14 |
| CVX_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 8.629707126621053 | CVX_volstd20 |
| DDOG_high20 | rolling_max {'window': 20} | 2026-07-22 | 276.70001220703125 | DDOG_high20 |
| DDOG_ma10 | sma {'window': 10} | 2026-07-22 | 260.6740020751953 | DDOG_ma10 |
| DDOG_ma20 | sma {'window': 20} | 2026-07-22 | 254.85699996948242 | DDOG_ma20 |
| DDOG_ma5 | sma {'window': 5} | 2026-07-22 | 256.95400390625 | DDOG_ma5 |
| DDOG_rsi14 | rsi {'window': 14} | 2026-07-22 | 48.281785099145196 | DDOG_rsi14 |
| DDOG_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 13.48261070237297 | DDOG_volstd20 |
| DELL_high20 | rolling_max {'window': 20} | 2026-07-22 | 463.4800109863281 | DELL_high20 |
| DELL_ma10 | sma {'window': 10} | 2026-07-22 | 419.80699768066404 | DELL_ma10 |
| DELL_ma20 | sma {'window': 20} | 2026-07-22 | 418.38799743652345 | DELL_ma20 |
| DELL_ma5 | sma {'window': 5} | 2026-07-22 | 403.10999755859376 | DELL_ma5 |
| DELL_rsi14 | rsi {'window': 14} | 2026-07-22 | 57.61315033269568 | DELL_rsi14 |
| DELL_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 20.458218120450315 | DELL_volstd20 |
| FTNT_high20 | rolling_max {'window': 20} | 2026-07-22 | 170.35000610351562 | FTNT_high20 |
| FTNT_ma10 | sma {'window': 10} | 2026-07-22 | 160.90999908447264 | FTNT_ma10 |
| FTNT_ma20 | sma {'window': 20} | 2026-07-22 | 157.88299942016602 | FTNT_ma20 |
| FTNT_ma5 | sma {'window': 5} | 2026-07-22 | 159.18000183105468 | FTNT_ma5 |
| FTNT_rsi14 | rsi {'window': 14} | 2026-07-22 | 51.60362777493834 | FTNT_rsi14 |
| FTNT_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 5.216876651368326 | FTNT_volstd20 |
| GOOGL_high20 | rolling_max {'window': 20} | 2026-07-22 | 375.2699890136719 | GOOGL_high20 |
| GOOGL_ma10 | sma {'window': 10} | 2026-07-22 | 354.1470001220703 | GOOGL_ma10 |
| GOOGL_ma20 | sma {'window': 20} | 2026-07-22 | 354.77050018310547 | GOOGL_ma20 |
| GOOGL_ma5 | sma {'window': 5} | 2026-07-22 | 348.4919921875 | GOOGL_ma5 |
| GOOGL_rsi14 | rsi {'window': 14} | 2026-07-22 | 40.552020269362465 | GOOGL_rsi14 |
| GOOGL_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 8.973950839757128 | GOOGL_volstd20 |
| HD_high20 | rolling_max {'window': 20} | 2026-07-22 | 358.8500061035156 | HD_high20 |
| HD_ma10 | sma {'window': 10} | 2026-07-22 | 338.1299987792969 | HD_ma10 |
| HD_ma20 | sma {'window': 20} | 2026-07-22 | 343.1159957885742 | HD_ma20 |
| HD_ma5 | sma {'window': 5} | 2026-07-22 | 336.59600219726565 | HD_ma5 |
| HD_rsi14 | rsi {'window': 14} | 2026-07-22 | 45.747809695752245 | HD_rsi14 |
| HD_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 7.517388274122651 | HD_volstd20 |
| HUM_high20 | rolling_max {'window': 20} | 2026-07-22 | 428.8800048828125 | HUM_high20 |
| HUM_ma10 | sma {'window': 10} | 2026-07-22 | 399.7150024414062 | HUM_ma10 |
| HUM_ma20 | sma {'window': 20} | 2026-07-22 | 394.73450164794923 | HUM_ma20 |
| HUM_ma5 | sma {'window': 5} | 2026-07-22 | 397.3240051269531 | HUM_ma5 |
| HUM_rsi14 | rsi {'window': 14} | 2026-07-22 | 58.47824843463062 | HUM_rsi14 |
| HUM_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 11.462945759077488 | HUM_volstd20 |
| INTU_high20 | rolling_max {'window': 20} | 2026-07-22 | 302.07000732421875 | INTU_high20 |
| INTU_ma10 | sma {'window': 10} | 2026-07-22 | 285.4320037841797 | INTU_ma10 |
| INTU_ma20 | sma {'window': 20} | 2026-07-22 | 276.72950286865233 | INTU_ma20 |
| INTU_ma5 | sma {'window': 5} | 2026-07-22 | 290.81800537109376 | INTU_ma5 |
| INTU_rsi14 | rsi {'window': 14} | 2026-07-22 | 48.54864908010009 | INTU_rsi14 |
| INTU_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 11.597315032759878 | INTU_volstd20 |
| JPM_high20 | rolling_max {'window': 20} | 2026-07-22 | 351.239990234375 | JPM_high20 |
| JPM_ma10 | sma {'window': 10} | 2026-07-22 | 341.2830017089844 | JPM_ma10 |
| JPM_ma20 | sma {'window': 20} | 2026-07-22 | 337.1635009765625 | JPM_ma20 |
| JPM_ma5 | sma {'window': 5} | 2026-07-22 | 343.31199951171874 | JPM_ma5 |
| JPM_rsi14 | rsi {'window': 14} | 2026-07-22 | 64.83090027721506 | JPM_rsi14 |
| JPM_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 6.002247051334973 | JPM_volstd20 |
| KO_high20 | rolling_max {'window': 20} | 2026-07-22 | 85.68000030517578 | KO_high20 |
| KO_ma10 | sma {'window': 10} | 2026-07-22 | 82.86699905395508 | KO_ma10 |
| KO_ma20 | sma {'window': 20} | 2026-07-22 | 82.60399932861328 | KO_ma20 |
| KO_ma5 | sma {'window': 5} | 2026-07-22 | 82.55399932861329 | KO_ma5 |
| KO_rsi14 | rsi {'window': 14} | 2026-07-22 | 50.42066568157524 | KO_rsi14 |
| KO_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 1.2248287317465691 | KO_volstd20 |
| LLY_high20 | rolling_max {'window': 20} | 2026-07-22 | 1249.449951171875 | LLY_high20 |
| LLY_ma10 | sma {'window': 10} | 2026-07-22 | 1173.0170043945313 | LLY_ma10 |
| LLY_ma20 | sma {'window': 20} | 2026-07-22 | 1183.485009765625 | LLY_ma20 |
| LLY_ma5 | sma {'window': 5} | 2026-07-22 | 1166.72001953125 | LLY_ma5 |
| LLY_rsi14 | rsi {'window': 14} | 2026-07-22 | 50.96361993310915 | LLY_rsi14 |
| LLY_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 32.781475255838274 | LLY_volstd20 |
| MA_high20 | rolling_max {'window': 20} | 2026-07-22 | 551.719970703125 | MA_high20 |
| MA_ma10 | sma {'window': 10} | 2026-07-22 | 537.372998046875 | MA_ma10 |
| MA_ma20 | sma {'window': 20} | 2026-07-22 | 526.2864974975586 | MA_ma20 |
| MA_ma5 | sma {'window': 5} | 2026-07-22 | 542.5719848632813 | MA_ma5 |
| MA_rsi14 | rsi {'window': 14} | 2026-07-22 | 55.25487813263884 | MA_rsi14 |
| MA_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 17.495200236348882 | MA_volstd20 |
| META_high20 | rolling_max {'window': 20} | 2026-07-22 | 686.0800170898438 | META_high20 |
| META_ma10 | sma {'window': 10} | 2026-07-22 | 652.714990234375 | META_ma10 |
| META_ma20 | sma {'window': 20} | 2026-07-22 | 615.9314910888672 | META_ma20 |
| META_ma5 | sma {'window': 5} | 2026-07-22 | 645.4759887695312 | META_ma5 |
| META_rsi14 | rsi {'window': 14} | 2026-07-22 | 51.52420632374405 | META_rsi14 |
| META_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 43.70419535212589 | META_volstd20 |
| MPC_high20 | rolling_max {'window': 20} | 2026-07-22 | 326.9200134277344 | MPC_high20 |
| MPC_ma10 | sma {'window': 10} | 2026-07-22 | 303.58399963378906 | MPC_ma10 |
| MPC_ma20 | sma {'window': 20} | 2026-07-22 | 282.6039978027344 | MPC_ma20 |
| MPC_ma5 | sma {'window': 5} | 2026-07-22 | 313.8680053710938 | MPC_ma5 |
| MPC_rsi14 | rsi {'window': 14} | 2026-07-22 | 75.34115955989607 | MPC_rsi14 |
| MPC_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 24.249172325795495 | MPC_volstd20 |
| MRNA_high20 | rolling_max {'window': 20} | 2026-07-22 | 85.5999984741211 | MRNA_high20 |
| MRNA_ma10 | sma {'window': 10} | 2026-07-22 | 64.975 | MRNA_ma10 |
| MRNA_ma20 | sma {'window': 20} | 2026-07-22 | 68.22749977111816 | MRNA_ma20 |
| MRNA_ma5 | sma {'window': 5} | 2026-07-22 | 60.43800048828125 | MRNA_ma5 |
| MRNA_rsi14 | rsi {'window': 14} | 2026-07-22 | 41.514939438206305 | MRNA_rsi14 |
| MRNA_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 7.363502466874462 | MRNA_volstd20 |
| MRVL_high20 | rolling_max {'window': 20} | 2026-07-22 | 300.0 | MRVL_high20 |
| MRVL_ma10 | sma {'window': 10} | 2026-07-22 | 211.61800079345704 | MRVL_ma10 |
| MRVL_ma20 | sma {'window': 20} | 2026-07-22 | 237.27850112915038 | MRVL_ma20 |
| MRVL_ma5 | sma {'window': 5} | 2026-07-22 | 198.17400207519532 | MRVL_ma5 |
| MRVL_rsi14 | rsi {'window': 14} | 2026-07-22 | 43.50779217714974 | MRVL_rsi14 |
| MRVL_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 33.146801238194826 | MRVL_volstd20 |
| MSFT_high20 | rolling_max {'window': 20} | 2026-07-22 | 405.989990234375 | MSFT_high20 |
| MSFT_ma10 | sma {'window': 10} | 2026-07-22 | 392.63099975585936 | MSFT_ma10 |
| MSFT_ma20 | sma {'window': 20} | 2026-07-22 | 384.64249725341796 | MSFT_ma20 |
| MSFT_ma5 | sma {'window': 5} | 2026-07-22 | 397.06000366210935 | MSFT_ma5 |
| MSFT_rsi14 | rsi {'window': 14} | 2026-07-22 | 49.17096347233581 | MSFT_rsi14 |
| MSFT_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 12.565335694697593 | MSFT_volstd20 |
| MU_high20 | rolling_max {'window': 20} | 2026-07-22 | 1255.0 | MU_high20 |
| MU_ma10 | sma {'window': 10} | 2026-07-22 | 929.3250061035156 | MU_ma10 |
| MU_ma20 | sma {'window': 20} | 2026-07-22 | 993.3495086669922 | MU_ma20 |
| MU_ma5 | sma {'window': 5} | 2026-07-22 | 899.5820068359375 | MU_ma5 |
| MU_rsi14 | rsi {'window': 14} | 2026-07-22 | 50.176721228695556 | MU_rsi14 |
| MU_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 101.73885894195185 | MU_volstd20 |
| NFLX_high20 | rolling_max {'window': 20} | 2026-07-22 | 78.44000244140625 | NFLX_high20 |
| NFLX_ma10 | sma {'window': 10} | 2026-07-22 | 71.79799957275391 | NFLX_ma10 |
| NFLX_ma20 | sma {'window': 20} | 2026-07-22 | 72.96699943542481 | NFLX_ma20 |
| NFLX_ma5 | sma {'window': 5} | 2026-07-22 | 69.61999816894532 | NFLX_ma5 |
| NFLX_rsi14 | rsi {'window': 14} | 2026-07-22 | 32.28765853905004 | NFLX_rsi14 |
| NFLX_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 2.831427642216348 | NFLX_volstd20 |
| NOW_high20 | rolling_max {'window': 20} | 2026-07-22 | 113.79000091552734 | NOW_high20 |
| NOW_ma10 | sma {'window': 10} | 2026-07-22 | 104.68599929809571 | NOW_ma10 |
| NOW_ma20 | sma {'window': 20} | 2026-07-22 | 103.31649971008301 | NOW_ma20 |
| NOW_ma5 | sma {'window': 5} | 2026-07-22 | 101.89399871826171 | NOW_ma5 |
| NOW_rsi14 | rsi {'window': 14} | 2026-07-22 | 39.492209133548066 | NOW_rsi14 |
| NOW_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 5.746789439190261 | NOW_volstd20 |
| NVDA_high20 | rolling_max {'window': 20} | 2026-07-22 | 214.38999938964844 | NVDA_high20 |
| NVDA_ma10 | sma {'window': 10} | 2026-07-22 | 207.44099884033204 | NVDA_ma10 |
| NVDA_ma20 | sma {'window': 20} | 2026-07-22 | 202.28749923706056 | NVDA_ma20 |
| NVDA_ma5 | sma {'window': 5} | 2026-07-22 | 206.5679962158203 | NVDA_ma5 |
| NVDA_rsi14 | rsi {'window': 14} | 2026-07-22 | 56.18270435100418 | NVDA_rsi14 |
| NVDA_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 6.4162396268515725 | NVDA_volstd20 |
| ORCL_high20 | rolling_max {'window': 20} | 2026-07-22 | 165.75 | ORCL_high20 |
| ORCL_ma10 | sma {'window': 10} | 2026-07-22 | 130.1720001220703 | ORCL_ma10 |
| ORCL_ma20 | sma {'window': 20} | 2026-07-22 | 138.15850067138672 | ORCL_ma20 |
| ORCL_ma5 | sma {'window': 5} | 2026-07-22 | 124.97799987792969 | ORCL_ma5 |
| ORCL_rsi14 | rsi {'window': 14} | 2026-07-22 | 32.41529568872332 | ORCL_rsi14 |
| ORCL_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 10.347717233217107 | ORCL_volstd20 |
| PANW_high20 | rolling_max {'window': 20} | 2026-07-22 | 368.79998779296875 | PANW_high20 |
| PANW_ma10 | sma {'window': 10} | 2026-07-22 | 344.0189971923828 | PANW_ma10 |
| PANW_ma20 | sma {'window': 20} | 2026-07-22 | 335.5509994506836 | PANW_ma20 |
| PANW_ma5 | sma {'window': 5} | 2026-07-22 | 347.7519958496094 | PANW_ma5 |
| PANW_rsi14 | rsi {'window': 14} | 2026-07-22 | 55.54372347385134 | PANW_rsi14 |
| PANW_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 20.970284475537117 | PANW_volstd20 |
| PEP_high20 | rolling_max {'window': 20} | 2026-07-22 | 149.0399932861328 | PEP_high20 |
| PEP_ma10 | sma {'window': 10} | 2026-07-22 | 136.7239990234375 | PEP_ma10 |
| PEP_ma20 | sma {'window': 20} | 2026-07-22 | 139.03299865722656 | PEP_ma20 |
| PEP_ma5 | sma {'window': 5} | 2026-07-22 | 136.53199768066406 | PEP_ma5 |
| PEP_rsi14 | rsi {'window': 14} | 2026-07-22 | 41.362103335143075 | PEP_rsi14 |
| PEP_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 3.2552356967790406 | PEP_volstd20 |
| PSX_high20 | rolling_max {'window': 20} | 2026-07-22 | 216.0800018310547 | PSX_high20 |
| PSX_ma10 | sma {'window': 10} | 2026-07-22 | 201.4740020751953 | PSX_ma10 |
| PSX_ma20 | sma {'window': 20} | 2026-07-22 | 188.2395004272461 | PSX_ma20 |
| PSX_ma5 | sma {'window': 5} | 2026-07-22 | 208.1320037841797 | PSX_ma5 |
| PSX_rsi14 | rsi {'window': 14} | 2026-07-22 | 74.3929896461498 | PSX_rsi14 |
| PSX_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 15.273304115742736 | PSX_volstd20 |
| PYPL_high20 | rolling_max {'window': 20} | 2026-07-22 | 57.66999816894531 | PYPL_high20 |
| PYPL_ma10 | sma {'window': 10} | 2026-07-22 | 52.364999771118164 | PYPL_ma10 |
| PYPL_ma20 | sma {'window': 20} | 2026-07-22 | 48.25850009918213 | PYPL_ma20 |
| PYPL_ma5 | sma {'window': 5} | 2026-07-22 | 56.29399948120117 | PYPL_ma5 |
| PYPL_rsi14 | rsi {'window': 14} | 2026-07-22 | 74.23584767770518 | PYPL_rsi14 |
| PYPL_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 5.482376535101115 | PYPL_volstd20 |
| QQQ_high20 | rolling_max {'window': 20} | 2026-07-22 | 737.6199951171875 | QQQ_high20 |
| QQQ_ma10 | sma {'window': 10} | 2026-07-22 | 710.9609985351562 | QQQ_ma10 |
| QQQ_ma20 | sma {'window': 20} | 2026-07-22 | 714.2535003662109 | QQQ_ma20 |
| QQQ_ma5 | sma {'window': 5} | 2026-07-22 | 702.3299926757812 | QQQ_ma5 |
| QQQ_rsi14 | rsi {'window': 14} | 2026-07-22 | 46.64735618576546 | QQQ_rsi14 |
| QQQ_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 10.292182568582051 | QQQ_volstd20 |
| SNDK_high20 | rolling_max {'window': 20} | 2026-07-22 | 2347.9990234375 | SNDK_high20 |
| SNDK_ma10 | sma {'window': 10} | 2026-07-22 | 1616.6499877929687 | SNDK_ma10 |
| SNDK_ma20 | sma {'window': 20} | 2026-07-22 | 1784.865985107422 | SNDK_ma20 |
| SNDK_ma5 | sma {'window': 5} | 2026-07-22 | 1469.1039794921876 | SNDK_ma5 |
| SNDK_rsi14 | rsi {'window': 14} | 2026-07-22 | 46.631869539379416 | SNDK_rsi14 |
| SNDK_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 274.82812329486603 | SNDK_volstd20 |
| SPY_high20 | rolling_max {'window': 20} | 2026-07-22 | 755.5800170898438 | SPY_high20 |
| SPY_ma10 | sma {'window': 10} | 2026-07-22 | 749.4260009765625 | SPY_ma10 |
| SPY_ma20 | sma {'window': 20} | 2026-07-22 | 745.6745056152344 | SPY_ma20 |
| SPY_ma5 | sma {'window': 5} | 2026-07-22 | 746.3579956054688 | SPY_ma5 |
| SPY_rsi14 | rsi {'window': 14} | 2026-07-22 | 51.72106243477067 | SPY_rsi14 |
| SPY_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 7.002590317919183 | SPY_volstd20 |
| STX_high20 | rolling_max {'window': 20} | 2026-07-22 | 1111.0 | STX_high20 |
| STX_ma10 | sma {'window': 10} | 2026-07-22 | 850.322998046875 | STX_ma10 |
| STX_ma20 | sma {'window': 20} | 2026-07-22 | 882.327001953125 | STX_ma20 |
| STX_ma5 | sma {'window': 5} | 2026-07-22 | 827.1059936523437 | STX_ma5 |
| STX_rsi14 | rsi {'window': 14} | 2026-07-22 | 53.008142246792275 | STX_rsi14 |
| STX_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 70.7249743678346 | STX_volstd20 |
| TECH_high20 | rolling_max {'window': 20} | 2026-07-22 | 72.12000274658203 | TECH_high20 |
| TECH_ma10 | sma {'window': 10} | 2026-07-22 | 71.54600067138672 | TECH_ma10 |
| TECH_ma20 | sma {'window': 20} | 2026-07-22 | 70.59550037384034 | TECH_ma20 |
| TECH_ma5 | sma {'window': 5} | 2026-07-22 | 71.78799896240234 | TECH_ma5 |
| TECH_rsi14 | rsi {'window': 14} | 2026-07-22 | 74.67411662309914 | TECH_rsi14 |
| TECH_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 2.7892752993396126 | TECH_volstd20 |
| TSLA_high20 | rolling_max {'window': 20} | 2026-07-22 | 432.8599853515625 | TSLA_high20 |
| TSLA_ma10 | sma {'window': 10} | 2026-07-22 | 389.41199951171876 | TSLA_ma10 |
| TSLA_ma20 | sma {'window': 20} | 2026-07-22 | 394.6199981689453 | TSLA_ma20 |
| TSLA_ma5 | sma {'window': 5} | 2026-07-22 | 378.8820007324219 | TSLA_ma5 |
| TSLA_rsi14 | rsi {'window': 14} | 2026-07-22 | 41.69776792390493 | TSLA_rsi14 |
| TSLA_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 16.860067398538824 | TSLA_volstd20 |
| UNH_high20 | rolling_max {'window': 20} | 2026-07-22 | 461.6199951171875 | UNH_high20 |
| UNH_ma10 | sma {'window': 10} | 2026-07-22 | 426.7779968261719 | UNH_ma10 |
| UNH_ma20 | sma {'window': 20} | 2026-07-22 | 423.8064987182617 | UNH_ma20 |
| UNH_ma5 | sma {'window': 5} | 2026-07-22 | 427.7359985351562 | UNH_ma5 |
| UNH_rsi14 | rsi {'window': 14} | 2026-07-22 | 58.60103718502571 | UNH_rsi14 |
| UNH_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 6.893423891205885 | UNH_volstd20 |
| V_high20 | rolling_max {'window': 20} | 2026-07-22 | 365.1400146484375 | V_high20 |
| V_ma10 | sma {'window': 10} | 2026-07-22 | 355.95900573730466 | V_ma10 |
| V_ma20 | sma {'window': 20} | 2026-07-22 | 350.6750030517578 | V_ma20 |
| V_ma5 | sma {'window': 5} | 2026-07-22 | 358.70200805664064 | V_ma5 |
| V_rsi14 | rsi {'window': 14} | 2026-07-22 | 56.16425793664213 | V_rsi14 |
| V_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 9.711176306572334 | V_volstd20 |
| WMT_high20 | rolling_max {'window': 20} | 2026-07-22 | 120.4000015258789 | WMT_high20 |
| WMT_ma10 | sma {'window': 10} | 2026-07-22 | 112.82299880981445 | WMT_ma10 |
| WMT_ma20 | sma {'window': 20} | 2026-07-22 | 113.12549934387206 | WMT_ma20 |
| WMT_ma5 | sma {'window': 5} | 2026-07-22 | 112.22199859619141 | WMT_ma5 |
| WMT_rsi14 | rsi {'window': 14} | 2026-07-22 | 36.33285571228335 | WMT_rsi14 |
| WMT_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 2.4206749376153764 | WMT_volstd20 |
| XOM_high20 | rolling_max {'window': 20} | 2026-07-22 | 154.8000030517578 | XOM_high20 |
| XOM_ma10 | sma {'window': 10} | 2026-07-22 | 145.8279998779297 | XOM_ma10 |
| XOM_ma20 | sma {'window': 20} | 2026-07-22 | 141.73399963378907 | XOM_ma20 |
| XOM_ma5 | sma {'window': 5} | 2026-07-22 | 149.56600036621094 | XOM_ma5 |
| XOM_rsi14 | rsi {'window': 14} | 2026-07-22 | 69.38882030854192 | XOM_rsi14 |
| XOM_volstd20 | rolling_std {'window': 20} | 2026-07-22 | 5.677873437971276 | XOM_volstd20 |

## Dashboard

**强势股趋势监控 - Phase 2 (动态池A)**

Livermore×O'Neil 强势股趋势监控 Phase 2 动态池。龙头池=池A:S&P500 top300成交额筛出 × 7/20/60/120日涨幅Top5并集(当前17只),由 scraw_us_top300_screen + us_leadership_pool rule_script 动态派生。龙头榜含成交额排名/MA/RSI/volstd/距高点/多周期RS涨幅/20日新高/60d排名,可排序;RS排名条形图;大盘温度计;逐只详情;全龙头RSI14。基准 SPY/QQQ。日K快照,重跑 build_us_leaders_dashboard.py 刷新。

Open: file:///Users/chengsishi/code/DAAS/fd-daas-mcp/dashboard-mcp/dashboards/us-leaders-trend-monitor.html

## Pipeline / Cron

_No pipeline collection attached._

## Auxiliary References

**Source tables:** scraw_aapl_daily, scraw_abbv_daily, scraw_adbe_daily, scraw_amd_daily, scraw_amzn_daily, scraw_avgo_daily, scraw_axon_daily, scraw_bac_daily, scraw_block_trades, scraw_cboe_daily, scraw_configs, scraw_cost_daily, scraw_crm_daily, scraw_csco_daily, scraw_cvx_daily, scraw_ddog_daily, scraw_dell_daily, scraw_ftnt_daily, scraw_googl_daily, scraw_hd_daily, scraw_hum_daily, scraw_intu_daily, scraw_jpm_daily, scraw_ko_daily, scraw_lly_daily, scraw_ma_daily, scraw_massive_inflation, scraw_massive_inflation_expectations, scraw_massive_labor_market, scraw_massive_treasury_yields, scraw_meta_daily, scraw_moutai_income_annual, scraw_mpc_daily, scraw_mrna_daily, scraw_mrvl_daily, scraw_msft_daily, scraw_mu_daily, scraw_nflx_daily, scraw_now_daily, scraw_nvda_daily, scraw_orcl_daily, scraw_panw_daily, scraw_pep_daily, scraw_psx_daily, scraw_pypl_daily, scraw_qqq_daily, scraw_sndk_daily, scraw_spo, scraw_spy_daily, scraw_sse_summary, scraw_stx_daily, scraw_szse_summary, scraw_tech_daily, scraw_tsla_daily, scraw_unh_daily, scraw_us_top300_screen, scraw_v_daily, scraw_wmt_daily, scraw_xom_daily
