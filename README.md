# RSI dip paper trader

Trades the RSI dip strategy from the S&P 500 Dip Scanner in an Alpaca **paper** account, automatically, once every US trading day. No real money is involved.

**Every weekday at 21:30 UTC (22:30 Berlin in winter, 23:30 in summer)** GitHub runs `paper_trader.py`, which:

1. Books the orders from the previous evening that filled at this morning's US open.
2. Downloads daily prices for all S&P 500 stocks from Alpaca (adjusted for splits and dividends).
3. Sells positions whose 2-day RSI closed above 50.
4. Buys $50 each of up to 5 stocks whose 2-day RSI closed below 10, preferring the ones with the most shares traded, while the virtual $500 budget allows.
5. Saves the virtual account and all logs in the `state` folder.

Orders are market orders placed after the close. Alpaca queues them and executes them at the next open, just like the backtest assumes.

The script keeps its **own $500 budget**, separate from the $100,000 the paper account starts with, so the results match what you'd get with 500. Amounts are in US dollars.

## Setup

You need an Alpaca account and a GitHub account. Both are free, and everything works from a phone browser.

### 1. Alpaca paper keys
1. Sign up at **alpaca.markets** with your email.
2. Make sure the dashboard is on your **Paper** account (top left).
3. On the dashboard's right side, find **API Keys** and tap **Generate New Keys**.
4. Copy the **Key** and the **Secret**. The secret is shown only once.

### 2. GitHub repository
1. Create a **private** repository, e.g. `paper-trader`.
2. Add these files with the same names and folders:
   - `paper_trader.py`
   - `config.json`
   - `README.md`
   - `.github/workflows/paper-trade.yml`

   On a phone: **Add file → Create new file**. Typing `.github/workflows/paper-trade.yml` as the name creates the folders. Paste each file's contents.

### 3. Store the keys as secrets
In the repository: **Settings → Secrets and variables → Actions → New repository secret**. Create two:

| Name | Value |
|---|---|
| `ALPACA_KEY_ID` | the Key from Alpaca |
| `ALPACA_SECRET_KEY` | the Secret from Alpaca |

Never put the keys into a file in the repository.

### 4. Test run
**Actions → Paper trade → Run workflow**. Leave **Dry run** ticked and start it. After a minute, open the run. The summary shows what it would buy and sell for the last finished trading day, without placing anything.

If that works, nothing else is needed. From the next weekday evening on, it runs by itself. You can also start a real run by hand with **Dry run** unticked.

## Following along

- **Each run's summary** (Actions → the run) shows the account value, trades closed that day, orders for the next open, and current positions.
- **`state/equity.csv`**: account value every day, with the SPY price for comparison.
- **`state/trades.csv`**: every closed trade with days held and return.
- **`state/orders.csv`**: every order and how it filled.
- **`state/signals.csv`**: every stock that triggered a signal and what happened to it, including skips for lack of cash.
- **`state/state.json`**: the virtual account (cash, positions, open orders). Don't edit it while orders are open.

## Changing the strategy

Edit `config.json`. Changes apply from the next run.

| Setting | Meaning |
|---|---|
| `strategy.rsi_length` | RSI period in days |
| `strategy.buy_below` / `sell_above` | RSI levels for buying and selling |
| `start_capital` | virtual budget; only used on the very first run |
| `position_size` | dollars per new position |
| `max_new_per_day` | most new buys per evening |
| `fee_per_order` | simulated fee per order (Alpaca charges none); e.g. `1` to mimic a 1€ broker |
| `rank_by` | `"shares"` or `"value"` (shares × price) to choose among many signals |
| `trend_filter_200d` | `true`: only buy stocks above their own 200-day average |
| `stop_loss_pct` | sell early if a position falls this many % below its buy price (`0` = off) |
| `max_days_held` | sell after this many trading days at the latest (`0` = off) |
| `pause.on` | `true`: no new buys during a non-trading period (see below) |
| `pause.drop5_pct` | non-trading when SPY fell more than this many % over the last 5 trading days |
| `pause.vol_pct` | non-trading when SPY's 20-day volatility is above this many % and higher than 20 trading days earlier |

To **start over**, delete the files in the `state` folder. The next run begins again with `start_capital`.

## Non-trading periods

Both bots (this one and the market-type bot in `regime/`) stop opening new positions while the market is under stress:
SPY fell more than 4% in 5 trading days, or its volatility is above 25% and still rising. Positions they already hold
are still sold by their normal rules. The run summary says when a day is a non-trading day, and `signals.csv` marks
the buys it skipped as `skipped, non-trading period`.

In the scanner's backtest (2019–2024) this paused about one day in ten and roughly halved the largest drop of both bots,
at the cost of some upside in fast rebounds such as spring 2020. The logic is in `market_filter.py`; the settings are
`pause` in `config.json` and `regime/config.json`.

## Market data archive

After the bots, each run saves that day's prices of every S&P 500 stock and SPY in `data/` (about 8 MB a year):

- `data/bars/YYYY-MM.csv`: open, high, low, close, volume, VWAP and number of trades per stock and day, as traded (not adjusted).
- `data/splits.csv`: stock splits, to adjust older prices. Dividends are not adjusted.
- `data/sp500_members.csv`: when each stock was first and last seen in the S&P 500 list.

Sessions a run missed are filled in by the next one. To download history once, start **Actions → Paper trade → Run workflow**
with **Dry run** unticked and a date such as `2020-01-01` in **Market data: also download all prices from this date on**.
The history covers today's S&P 500 members only, so stocks that left the index are missing from it.

`python tools/archive_files.py OUT_DIR` turns the archive into the files used by the Market Data page and the scanner.

## Good to know

- Paper fills are kinder than real ones. Alpaca's paper account doesn't simulate market impact or dividends.
- The evening decision uses the closing price to estimate cash from tonight's sells. On days when cash is tight, this can differ slightly from the backtest, which knows the real opening price. On a replay of 2020–2021 data, about 97% of trades matched the backtest exactly.
- If a sell doesn't fill, for example because trading in the stock was halted, it's placed again the next evening.
- If fewer than 80% of stocks have a price for the day, the run stops without trading and the summary says why.
- Running twice for the same day is harmless. The second run sees the day was already processed and does nothing.

# Market-type bot

`regime_trader.py` runs a second strategy in a **separate Alpaca paper account**, right after the RSI bot in the same evening workflow. Each evening it classifies the market by SPY's distance from its 200-day average (rising or falling) and SPY's 20-day volatility (calm or volatile), then looks for new buys with the strategy assigned to that market type:

| Market type | Strategy (default) |
|---|---|
| Rising, calm | Breakout (new 50-day high, sell on a new 20-day low) |
| Rising, volatile | Bollinger band bounce |
| Falling, calm | RSI dip |
| Falling, volatile | RSI dip |

Each position is sold by the rule of the strategy that bought it. The rules and formulas are the same as on the S&P 500 Dip Scanner page; on a replay of 2020–2022 data the bot made exactly the same trades as the scanner's backtest.

- **Settings:** `regime/config.json`. Change them in the scanner page with **Use for paper trading**; the scheduled task writes them here before the next evening run.
- **Results:** `regime/state/` with the same files as the RSI bot, plus the strategy and market type of each trade, and the market measures in `equity.csv`.
- **Keys:** create a second paper account in Alpaca, generate its API keys, and add them as repository secrets `ALPACA_REGIME_KEY_ID` and `ALPACA_REGIME_SECRET_KEY`. Until they exist, the bot skips itself and the RSI bot runs as before.
