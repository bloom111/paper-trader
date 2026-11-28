# Regime-filtered intraday backtest after IBKR fees

2022-01-01 to 2026-11-25, 5-minute SIP bars, 20 liquid US stocks/ETFs. 500 EUR per trade, 2,500 EUR capital, cash account, IBKR Pro Tiered + pass-through fees, 0.05% slippage per fill, IBKR market data. Net = after all costs. % a year on 2,500 EUR, no compounding. Holdout = 2025-01-01 on.

| Strategy | Max trades/day | Trades | Trades/day | ORB / VWAP trades | Win rate (net) | Avg gross/trade | Avg net/trade | Gross $ | Costs $ | Net $ | Net %/yr | Avg gross 2022-24 | Avg gross 2025-26 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ORB 30 min, every day | 1 | 1207 | 0.98 | 1207 / 0 | 41% | +0.033% | -0.197% | 228 | 2,466 | -2,238 | -15.8% | -0.013% | +0.103% |
| ORB 30 min, every day | 3 | 3524 | 2.87 | 3524 / 0 | 39% | -0.008% | -0.238% | -165 | 4,969 | -5,135 | -36.3% | +0.007% | -0.031% |
| ORB 30 min, every day | 5 | 5643 | 4.59 | 5643 / 0 | 39% | +0.032% | -0.197% | 1,063 | 7,781 | -6,718 | -47.5% | +0.040% | +0.021% |
| VWAP reversion, every day | 1 | 1138 | 0.93 | 0 / 1138 | 51% | -0.006% | -0.236% | -40 | 2,373 | -2,414 | -17.1% | -0.000% | -0.015% |
| VWAP reversion, every day | 3 | 3034 | 2.47 | 0 / 3034 | 50% | +0.007% | -0.222% | 127 | 4,348 | -4,221 | -29.8% | +0.014% | -0.003% |
| VWAP reversion, every day | 5 | 4478 | 3.64 | 0 / 4478 | 49% | +0.002% | -0.227% | 54 | 6,238 | -6,184 | -43.7% | +0.010% | -0.009% |
| Regime filter (base) | 1 | 984 | 0.80 | 635 / 349 | 42% | -0.025% | -0.254% | -143 | 2,163 | -2,306 | -16.3% | -0.022% | -0.029% |
| Regime filter (base) | 3 | 2480 | 2.02 | 1662 / 818 | 42% | +0.026% | -0.203% | 368 | 3,751 | -3,383 | -23.9% | +0.027% | +0.023% |
| Regime filter (base) | 5 | 3338 | 2.71 | 2253 / 1085 | 41% | +0.019% | -0.209% | 372 | 4,834 | -4,462 | -31.5% | +0.029% | +0.006% |
| Regime filter (looser) | 1 | 1119 | 0.91 | 523 / 596 | 48% | +0.034% | -0.196% | 218 | 2,348 | -2,131 | -15.1% | +0.051% | +0.007% |
| Regime filter (looser) | 3 | 3168 | 2.58 | 1730 / 1438 | 44% | +0.023% | -0.206% | 426 | 4,600 | -4,174 | -29.5% | +0.035% | +0.006% |
| Regime filter (looser) | 5 | 4862 | 3.95 | 2719 / 2143 | 43% | +0.011% | -0.218% | 324 | 6,764 | -6,440 | -45.5% | +0.023% | -0.006% |
| Regime filter (stricter) | 1 | 671 | 0.55 | 511 / 160 | 40% | +0.027% | -0.200% | 106 | 1,742 | -1,636 | -11.6% | +0.019% | +0.038% |
| Regime filter (stricter) | 3 | 1259 | 1.02 | 972 / 287 | 39% | +0.043% | -0.185% | 311 | 2,517 | -2,206 | -15.6% | +0.038% | +0.048% |
| Regime filter (stricter) | 5 | 1448 | 1.18 | 1128 / 320 | 40% | +0.042% | -0.185% | 356 | 2,726 | -2,371 | -16.7% | +0.024% | +0.066% |

## Gross edge by trade type (max 5 trades/day)

| Strategy | Type | Trades | Avg gross/trade | Gross win rate |
|---|---|---|---|---|
| ORB 30 min, every day | ORB | 5643 | +0.032% | 49% |
| VWAP reversion, every day | VWAP | 4478 | +0.002% | 55% |
| Regime filter (base) | ORB | 2253 | +0.025% | 50% |
| Regime filter (base) | VWAP | 1085 | +0.006% | 54% |
| Regime filter (looser) | ORB | 2719 | +0.010% | 49% |
| Regime filter (looser) | VWAP | 2143 | +0.013% | 54% |
| Regime filter (stricter) | ORB | 1128 | +0.055% | 49% |
| Regime filter (stricter) | VWAP | 320 | -0.002% | 56% |
