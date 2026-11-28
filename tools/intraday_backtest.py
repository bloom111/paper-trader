#!/usr/bin/env python3
"""
Fee-aware backtest of an intraday opening-range breakout (ORB) on 5-minute bars from Alpaca (SIP).
Research only: places no orders and never touches the bots' state.

Setup, like the interday bots but intraday:
- 500 EUR per trade (USD 580 at EURUSD 1.16), capital 2,500 EUR (USD 2,900), long only, all positions closed
  at the close. Fractional shares (IBKR allows them for US stocks).
- Cash account: a sale settles T+1, so each 500 EUR slot can buy once a day -> at most 5 trades a day,
  and fewer if the daily cap is lower.
- Costs per order: IBKR Pro Tiered commission (0.0035 $/share, min 0.35, max 1% of value) plus pass-through
  fees (exchange 0.0030 $/share for taking liquidity, clearing 0.0002 $/share, FINRA/NYSE pass-through on the
  commission, SEC 0.00206% and FINRA TAF 0.000166 $/share on sales, CAT 0.000003 $/share), plus 0.05% slippage
  per fill. Market data: IBKR US bundle USD 10 a month unless the month's commissions reach USD 30, plus
  USD 4.50 for streaming NYSE/AMEX/NASDAQ.

Strategy: opening range = high/low of the first OR minutes from 9:30 New York time. The first 5-minute bar that
closes above the range high is a buy signal; the buy fills at the next bar's open (no new buys after 15:00).
Stop at the range low; optional profit target at entry + R x (entry - stop); otherwise sell at the close.
Signals at the same bar time are ranked by breakout strength.

Usage: python tools/intraday_backtest.py [--from 2022-01-01] [--to 2026-11-25] [--out results/intraday]
Env:   ALPACA_KEY_ID, ALPACA_SECRET_KEY (free plan is enough: SIP history older than 15 minutes)
"""
import argparse
import csv
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
SYMBOLS = ["SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD", "AVGO",
           "NFLX", "JPM", "BAC", "XOM", "LLY", "COST", "PLTR", "MU"]
EURUSD = 1.16
SIZE = 500 * EURUSD
CAPITAL = 2500 * EURUSD
SLOTS = int(CAPITAL // SIZE + 1e-9)       # 5
SLIP = 0.0005
LAST_ENTRY = 15 * 60                      # minutes after midnight NY
VARIANTS = [(or_min, r) for or_min in (5, 15, 30) for r in (None, 2.0)]
CAPS = (1, 3, 5)


# ---------------------------------------------------------------- data
def fetch(symbols, start, end, cache_dir):
    key, secret = os.environ.get("ALPACA_KEY_ID"), os.environ.get("ALPACA_SECRET_KEY")
    os.makedirs(cache_dir, exist_ok=True)
    out = {}
    for sym in symbols:
        path = os.path.join(cache_dir, f"{sym}_{start}_{end}.csv.gz")
        if not os.path.exists(path):
            if not key:
                sys.exit("ALPACA_KEY_ID / ALPACA_SECRET_KEY missing and no cached data")
            rows, token = [], None
            while True:
                p = {"symbols": sym, "timeframe": "5Min", "start": start, "end": end, "adjustment": "all",
                     "feed": "sip", "limit": 10000}
                if token:
                    p["page_token"] = token
                r = get("https://data.alpaca.markets/v2/stocks/bars?" + urllib.parse.urlencode(p), key, secret)
                rows += [(b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]) for b in (r.get("bars") or {}).get(sym, [])]
                token = r.get("next_page_token")
                if not token:
                    break
            with gzip.open(path, "wt", newline="") as f:
                csv.writer(f).writerows(rows)
            print(f"{sym}: {len(rows)} bars", flush=True)
        out[sym] = load(path)
    return out


def get(url, key, secret):
    req = urllib.request.Request(url, headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(3 * 2 ** attempt)
                continue
            raise
        except urllib.error.URLError:
            time.sleep(3 * 2 ** attempt)
    raise RuntimeError("Alpaca request kept failing")


def load(path):
    """{date: [(minute_of_day, o, h, l, c), ...]} regular session only."""
    days = defaultdict(list)
    with gzip.open(path, "rt") as f:
        for t, o, h, l, c, v in csv.reader(f):
            dt = datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(NY)
            m = dt.hour * 60 + dt.minute
            if 570 <= m < 960:
                days[dt.date().isoformat()].append((m, float(o), float(h), float(l), float(c)))
    for d in days:
        days[d].sort()
    return days


# ---------------------------------------------------------------- costs
def order_fees(qty, px, sell):
    value = qty * px
    comm = min(max(0.0035 * qty, 0.35), 0.01 * value)
    f = comm + 0.0030 * qty + 0.0002 * qty + comm * (0.000565 + 0.000175) + 0.000003 * qty
    if sell:
        f += 0.0000206 * value + min(0.000166 * qty, 8.30)
    return f, comm


# ---------------------------------------------------------------- strategy
def signals(bars, or_min, r_target):
    """Trades of one symbol on one day: dict(entry_m, strength, gross_ret, entry, exit, why) or None."""
    or_end = 570 + or_min
    rng = [b for b in bars if b[0] < or_end]
    rest = [b for b in bars if b[0] >= or_end]
    if len(rng) < or_min // 5 or len(rest) < 2:
        return None
    hi, lo = max(b[2] for b in rng), min(b[3] for b in rng)
    for i, b in enumerate(rest[:-1]):
        if b[4] > hi:
            nb = rest[i + 1]
            if nb[0] > LAST_ENTRY:
                return None
            entry = nb[1]
            stop = lo
            if entry <= stop:
                return None
            target = entry + r_target * (entry - stop) if r_target else None
            exit_px, why = None, "close"
            for x in rest[i + 1:]:
                # stop first (conservative when a bar touches both)
                if x[3] <= stop:
                    exit_px, why = min(stop, x[1]), "stop"
                    break
                if target and x[2] >= target:
                    exit_px, why = max(target, x[1]), "target"
                    break
            if exit_px is None:
                exit_px = rest[-1][4]
            return {"entry_m": nb[0], "strength": b[4] / hi - 1, "entry": entry, "exit": exit_px, "why": why}
    return None


def run(data, or_min, r_target, cap):
    dates = sorted({d for s in data.values() for d in s})
    trades = []
    for d in dates:
        cands = []
        for sym, days in data.items():
            if d in days:
                s = signals(days[d], or_min, r_target)
                if s:
                    s.update(sym=sym, date=d)
                    cands.append(s)
        cands.sort(key=lambda s: (s["entry_m"], -s["strength"]))
        trades += cands[:min(cap, SLOTS)]
    out = []
    for t in trades:
        buy_px = t["entry"] * (1 + SLIP)
        sell_px = t["exit"] * (1 - SLIP)
        qty = SIZE / buy_px
        fb, cb = order_fees(qty, buy_px, False)
        fs, cs = order_fees(qty, sell_px, True)
        gross = qty * (t["exit"] - t["entry"])
        slip = qty * (buy_px - t["entry"]) + qty * (t["exit"] - sell_px)
        out.append({**t, "gross": gross, "slip": slip, "fees": fb + fs, "comm": cb + cs,
                    "net": gross - slip - fb - fs})
    return out, dates


def summarise(trades, dates):
    months = sorted({d[:7] for d in dates})
    comm_m = defaultdict(float)
    for t in trades:
        comm_m[t["date"][:7]] += t["comm"]
    data_cost = sum(4.50 + (10.0 if comm_m[m] < 30 else 0.0) for m in months)
    years = len(dates) / 252
    g = sum(t["gross"] for t in trades)
    sl = sum(t["slip"] for t in trades)
    fe = sum(t["fees"] for t in trades)
    n = len(trades)
    net = g - sl - fe - data_cost
    by_year = defaultdict(lambda: [0.0, 0.0, 0])
    for t in trades:
        y = by_year[t["date"][:4]]
        y[0] += t["gross"]
        y[1] += t["net"]
        y[2] += 1
    return {"trades": n, "per_day": n / max(len(dates), 1), "win": sum(t["net"] > 0 for t in trades) / max(n, 1),
            "avg_gross_pct": g / max(n, 1) / SIZE * 100, "avg_net_pct": (g - sl - fe) / max(n, 1) / SIZE * 100,
            "gross": g, "slip": sl, "fees": fe, "data": data_cost, "net": net,
            "gross_yr_pct": g / years / CAPITAL * 100, "net_yr_pct": net / years / CAPITAL * 100,
            "by_year": {k: v for k, v in sorted(by_year.items())}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default="2022-01-01")
    ap.add_argument("--to", dest="end", default="2026-11-25")
    ap.add_argument("--out", default="results/intraday")
    ap.add_argument("--cache", default="data/intraday_cache")
    a = ap.parse_args()
    data = fetch(SYMBOLS, a.start, a.end, a.cache)
    os.makedirs(a.out, exist_ok=True)

    spy = data["SPY"]
    sd = sorted(spy)
    spy_ret = spy[sd[-1]][-1][4] / spy[sd[0]][0][1] - 1
    yrs = len(sd) / 252

    lines = ["# Intraday ORB backtest after IBKR fees", "",
             f"{a.start} to {a.end}, {len(sd)} trading days, 5-minute SIP bars, {len(SYMBOLS)} liquid US stocks/ETFs: "
             f"{', '.join(SYMBOLS)}.",
             f"500 EUR per trade (${SIZE:.0f}), capital 2,500 EUR (${CAPITAL:.0f}), long only, flat at the close, "
             f"cash account (max {SLOTS} trades a day). Costs: IBKR Pro Tiered + pass-through fees, 0.05% slippage per "
             f"fill, IBKR market data. SPY buy-and-hold over the same period: {((1 + spy_ret) ** (1 / yrs) - 1) * 100:.1f}% a year.",
             "", "Gross = before any costs. Net = after slippage, fees and market data. % a year is on the 2,500 EUR capital, "
             "without compounding.", "",
             "| Opening range | Target | Max trades/day | Trades | Trades/day | Win rate (net) | Avg gross/trade | "
             "Avg net/trade | Gross $ | Slippage $ | Fees $ | Data $ | Net $ | Gross %/yr | Net %/yr |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    allrows = []
    for or_min, r in VARIANTS:
        for cap in CAPS:
            tr, dates = run(data, or_min, r, cap)
            s = summarise(tr, dates)
            allrows.append(((or_min, r, cap), s))
            lines.append(f"| {or_min} min | {f'{r:g}R' if r else 'close'} | {cap} | {s['trades']} | {s['per_day']:.2f} | "
                         f"{s['win'] * 100:.0f}% | {s['avg_gross_pct']:+.3f}% | {s['avg_net_pct']:+.3f}% | "
                         f"{s['gross']:,.0f} | {s['slip']:,.0f} | {s['fees']:,.0f} | {s['data']:,.0f} | {s['net']:,.0f} | "
                         f"{s['gross_yr_pct']:+.1f}% | {s['net_yr_pct']:+.1f}% |")
            print(lines[-1], flush=True)
    lines += ["", "## Net $ by year (gross in brackets)", "",
              "| Variant | " + " | ".join(sorted({y for _, s in allrows for y in s["by_year"]})) + " |",
              "|---|" + "---|" * len({y for _, s in allrows for y in s["by_year"]})]
    years = sorted({y for _, s in allrows for y in s["by_year"]})
    for (or_min, r, cap), s in allrows:
        lines.append(f"| {or_min} min, {f'{r:g}R' if r else 'close'}, cap {cap} | " + " | ".join(
            f"{s['by_year'].get(y, [0, 0, 0])[1]:,.0f} ({s['by_year'].get(y, [0, 0, 0])[0]:,.0f})" for y in years) + " |")
    with open(os.path.join(a.out, "orb_results.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(a.out, "orb_results.json"), "w") as f:
        json.dump([{"or_min": k[0], "target_r": k[1], "cap": k[2], **s} for k, s in allrows], f, indent=1)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
