#!/usr/bin/env python3
"""
Riskier market-type paper trader, on the archived RSI bot's Alpaca paper account.

Same rules and code as regime_trader.py, with its own settings that never sit out:
no non-trading period (the market stress pause is off) and up to 3 new buys a day.
Every market type has a strategy, so it looks for buys every evening.
It runs next to the market-type bot so the two can be compared on the dashboard.
It keeps its own book (cash and positions) in risky/state and never reads the account's
positions, so the RSI bot's leftover positions in that account do not affect it.

Settings: risky/config.json (fixed by hand; not synced from the scanner page)
Results:  risky/state/  (same files as regime/state/)

Environment:
  ALPACA_RISKY_KEY_ID, ALPACA_RISKY_SECRET_KEY   paper account keys (the workflow passes the RSI bot's)
  DRY_RUN=1                                      show what would happen; place nothing, save nothing

Backtest (2021-01..2026-11, point-in-time S&P 500, 0.05% slippage, IBKR fees, $2,500 / $500):
Sharpe 1.13, 22.2% a year, worst drop -19.5% (market-type bot: 1.19, 22.5%, -21.4%).
The March 2020 crash could not be tested, and that is where the pause helps most.
"""
import json
import os

from paper_trader import AlpacaAPI
import regime_trader

BOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "risky")


def main():
    with open(os.path.join(BOT_DIR, "config.json")) as f:
        cfg = json.load(f)
    key, secret = os.environ.get("ALPACA_RISKY_KEY_ID"), os.environ.get("ALPACA_RISKY_SECRET_KEY")
    out = os.environ.get("GITHUB_STEP_SUMMARY")
    if not key or not secret:
        print("Riskier market-type bot skipped: add ALPACA_RISKY_KEY_ID and ALPACA_RISKY_SECRET_KEY as repository secrets to start it.")
        if out:
            with open(out, "a") as f:
                f.write("## Riskier market-type bot\nSkipped: the secrets ALPACA_RISKY_KEY_ID and ALPACA_RISKY_SECRET_KEY are not set yet.\n")
        return
    dry = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
    api = AlpacaAPI(key, secret, feed=cfg.get("data_feed", "sip"))
    res = regime_trader.run(api, cfg, os.path.join(BOT_DIR, "state"), dry_run=dry)
    if out and res:
        with open(out, "a") as f:
            f.write(res["summary"].replace("## Market-type bot", "## Riskier market-type bot", 1) + "\n")


if __name__ == "__main__":
    main()
