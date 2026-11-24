#!/usr/bin/env python3
"""
Survivorship-free S&P 500 history for backtests.

The market data archive in data/bars only has stocks that are in the S&P 500 today, so a backtest on
it never meets the companies that dropped out (bankruptcies such as SVB, takeovers, demotions). This
script adds them:

  data/sp500_history.csv     symbol,start,end
                             Each period a ticker was in the S&P 500 (end empty = still in it on the
                             list's last day). From github.com/fja05680/sp500 (MIT licence), which is
                             kept up to date from S&P's announcements.
  data/removed/bars.csv      date,symbol,open,close,volume
                             Daily prices of every ticker in that list that is not in today's index,
                             split-adjusted (not dividend-adjusted), from Alpaca. Each ticker is asked for
                             as of its last day in the index, so a later rename doesn't hide its history.
  data/removed/summary.csv   symbol,member_from,member_to,bars,first,last,note

Usage: python tools/fetch_removed.py [--from 2019-01-01]
Environment: ALPACA_KEY_ID, ALPACA_SECRET_KEY. Only the Python standard library is used.
"""
import csv
import io
import json
import os
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from paper_trader import AlpacaAPI, APIError  # noqa: E402

DATA = os.path.join(ROOT, "data")
SOURCE = ("https://raw.githubusercontent.com/fja05680/sp500/master/"
          "S%26P%20500%20Historical%20Components%20%26%20Changes%20(Updated).csv")


def membership(since):
    """[(symbol, start, end_or_None)] from the daily snapshots, for periods touching `since` or later."""
    text = urllib.request.urlopen(SOURCE, timeout=120).read().decode("utf-8")
    rows = sorted((r["date"], set(t for t in r["tickers"].split(",") if t)) for r in csv.DictReader(io.StringIO(text)))
    last_day = rows[-1][0]
    periods, open_ = [], {}
    prev = set()
    for d, members in rows:
        for s in members - prev:
            open_[s] = d
        for s in prev - members:
            periods.append((s, open_.pop(s), d))
        prev = members
    periods += [(s, start, None) for s, start in open_.items()]
    return [p for p in periods if p[2] is None or p[2] >= since], last_day


def current_members():
    with open(os.path.join(DATA, "sp500_members.csv")) as f:
        return {r["symbol"] for r in csv.DictReader(f)}


def fetch_one(api, symbol, start, end, asof):
    bars, token = [], None
    while True:
        p = {"symbols": symbol, "timeframe": "1Day", "start": start, "end": end, "adjustment": "split",
             "feed": api.feed, "limit": 10000, "asof": asof}
        if token:
            p["page_token"] = token
        r = api._req("GET", api.data + "/v2/stocks/bars", p)
        for b in (r.get("bars") or {}).get(symbol, []):
            bars.append((b["t"][:10], b["o"], b["c"], b["v"]))
        token = r.get("next_page_token")
        if not token:
            return bars


def main():
    since = sys.argv[sys.argv.index("--from") + 1] if "--from" in sys.argv else "2019-01-01"
    date.fromisoformat(since)
    key, secret = os.environ.get("ALPACA_KEY_ID"), os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        sys.exit("Set ALPACA_KEY_ID and ALPACA_SECRET_KEY.")
    with open(os.path.join(ROOT, "config.json")) as f:
        feed = json.load(f).get("data_feed", "sip")
    api = AlpacaAPI(key, secret, feed=feed)

    periods, last_day = membership(since)
    with open(os.path.join(DATA, "sp500_history.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "start", "end"])
        w.writerows(sorted((s, a, b or "") for s, a, b in periods))

    now = set(current_members())
    removed = {}
    for s, a, b in periods:
        if s not in now:
            r = removed.setdefault(s, [a, b or last_day])
            r[0], r[1] = min(r[0], a), max(r[1], b or last_day)
    end = (datetime.now(timezone.utc) - timedelta(minutes=16)).strftime("%Y-%m-%dT%H:%M:%SZ")
    os.makedirs(os.path.join(DATA, "removed"), exist_ok=True)
    rows, summary = [], []
    for s in sorted(removed):
        a, b = removed[s]
        note = ""
        try:
            bars = fetch_one(api, s, since, end, b)
        except APIError as e:
            if api.feed == "sip" and e.code in (403, 422):
                api.feed = "iex"
                bars = fetch_one(api, s, since, end, b)
                note = "IEX volumes"
            else:
                bars, note = [], f"error {e.code}"
        rows += [(d, s, o, c, v) for d, o, c, v in bars]
        summary.append((s, a, b, len(bars), bars[0][0] if bars else "", bars[-1][0] if bars else "", note))
    rows.sort()
    with open(os.path.join(DATA, "removed", "bars.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "symbol", "open", "close", "volume"])
        w.writerows(rows)
    with open(os.path.join(DATA, "removed", "summary.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "member_from", "member_to", "bars", "first", "last", "note"])
        w.writerows(summary)
    got = sum(1 for x in summary if x[3])
    msg = (f"S&P 500 history to {last_day}: {len(periods)} membership periods since {since}. "
           f"Removed tickers: {len(summary)}, prices found for {got}, {len(rows)} price rows.")
    missing = [x[0] for x in summary if not x[3]]
    if missing:
        msg += " No prices for: " + ", ".join(missing) + "."
    print(msg)
    out = os.environ.get("GITHUB_STEP_SUMMARY")
    if out:
        with open(out, "a") as f:
            f.write("## Removed S&P 500 stocks\n" + msg + "\n")


if __name__ == "__main__":
    main()
