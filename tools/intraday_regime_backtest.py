#!/usr/bin/env python3
"""
Day-regime-filtered intraday backtest: classify each stock's day at 10:00 New York time, then trade an
opening-range breakout (ORB) on trend days and VWAP reversion on range days; skip unclear days.
Same data, costs and capital rules as tools/intraday_backtest.py (500 EUR per trade, 2,500 EUR capital,
cash account -> max 5 trades a day, IBKR Pro Tiered + pass-through fees, 0.05% slippage per fill,
IBKR market data). Long only, flat at the close. Research only: places no orders.

Day classification (all measures known at 10:00, none look ahead):
- ATR = 14-day average true range of the regular session, up to yesterday.
- gap = (today's open - yesterday's close) / ATR.  orr = (9:30-10:00 high - low) / ATR.
- Market (as in the market-type bot): SPY's last close vs its 200-day average, and SPY's 20-day volatility.
  VIX itself is not in Alpaca's data, so SPY's 20-day volatility stands in for it.
- Skip every trade when SPY's 20-day volatility is above VOL_MAX.
- Trend day: SPY above its 200-day average, the stock trades above its open at 10:00, and
  (gap >= GAP_TREND or orr <= ORR_NARROW). Trade: buy when a 5-minute bar closes above the 9:30-10:00 high,
  stop at the 9:30-10:00 low, sell at the close.
- Range day: |gap| <= GAP_FLAT and orr >= ORR_WIDE. Trade: buy when a bar closes at least DEV x ATR below
  the session VWAP (10:00-14:30), sell when price gets back to VWAP, stop DEV x ATR below the buy, else at the close.
- Anything else: no trade.
The thresholds were set before looking at results; two looser/stricter sets are run as a sensitivity check.
Every buy fills at the next bar's open.

Usage: python tools/intraday_regime_backtest.py [--from 2022-01-01] [--to 2026-11-25] [--out results/intraday]
"""
import argparse
import csv
import json
import math
import os
from collections import defaultdict

import intraday_backtest as ib

SETS = {
    "base":   dict(VOL_MAX=0.30, GAP_TREND=0.25, ORR_NARROW=0.35, GAP_FLAT=0.15, ORR_WIDE=0.50, DEV=0.30),
    "looser": dict(VOL_MAX=0.35, GAP_TREND=0.15, ORR_NARROW=0.45, GAP_FLAT=0.25, ORR_WIDE=0.40, DEV=0.25),
    "strict": dict(VOL_MAX=0.25, GAP_TREND=0.40, ORR_NARROW=0.25, GAP_FLAT=0.10, ORR_WIDE=0.60, DEV=0.40),
}
OR_END = 600            # 10:00
VWAP_LAST_ENTRY = 870   # 14:30


def daily(days):
    """[(date, o, h, l, c)] from 5-minute regular-session bars."""
    return [(d, b[0][1], max(x[2] for x in b), min(x[3] for x in b), b[-1][4]) for d, b in sorted(days.items()) if b]


def atr_table(dl, n=14):
    """{date: (ATR up to the day before, previous close)}"""
    out, trs = {}, []
    for i in range(1, len(dl)):
        if len(trs) >= n:
            out[dl[i][0]] = (sum(trs[-n:]) / n, dl[i - 1][4])
        _, o, h, l, c = dl[i]
        pc = dl[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return out


def market_table(path):
    """{date: (spy above 200-day average yesterday, 20-day volatility yesterday)} from data/benchmarks.csv."""
    rows = [(r["session"], float(r["spy_close"])) for r in csv.DictReader(open(path)) if r.get("spy_close")]
    out = {}
    for i in range(1, len(rows)):
        c = [x[1] for x in rows[:i]]
        if len(c) < 201:
            continue
        r = [math.log(c[k] / c[k - 1]) for k in range(len(c) - 20, len(c))]
        m = sum(r) / 20
        vol = math.sqrt(sum((x - m) ** 2 for x in r) / 19 * 252)
        out[rows[i][0]] = (c[-1] > sum(c[-200:]) / 200, vol)
    return out


def exit_scan(bars, stop, target_fn):
    """Walk bars from the fill bar on; stop first when a bar touches both. target_fn(bar, k) -> target or None."""
    for k, x in enumerate(bars):
        if x[3] <= stop:
            return min(stop, x[1]), "stop"
        t = target_fn(x, k)
        if t is not None and x[2] >= t:
            return max(t, x[1]), "target"
    return bars[-1][4], "close"


def day_trade(bars, atr, prev_close, mkt, p, mode):
    """One stock-day -> dict(kind, entry_m, strength, entry, exit, why) or None.
    mode: 'regime' (classified), 'orb' (ORB every day), 'vwap' (VWAP reversion every day)."""
    rng = [b for b in bars if b[0] < OR_END]
    rest = [b for b in bars if b[0] >= OR_END]
    if len(rng) < 6 or len(rest) < 3 or atr <= 0:
        return None
    hi, lo = max(b[2] for b in rng), min(b[3] for b in rng)
    gap = (bars[0][1] - prev_close) / atr
    orr = (hi - lo) / atr
    up, vol = mkt
    kind = mode
    if mode == "regime":
        if vol > p["VOL_MAX"]:
            return None
        if up and rng[-1][4] > bars[0][1] and (gap >= p["GAP_TREND"] or orr <= p["ORR_NARROW"]):
            kind = "orb"
        elif abs(gap) <= p["GAP_FLAT"] and orr >= p["ORR_WIDE"]:
            kind = "vwap"
        else:
            return None

    if kind == "orb":
        for i, b in enumerate(rest[:-1]):
            if b[4] > hi:
                nb = rest[i + 1]
                if nb[0] > ib.LAST_ENTRY or nb[1] <= lo:
                    return None
                ex, why = exit_scan(rest[i + 1:], lo, lambda x, k: None)
                return {"kind": "orb", "entry_m": nb[0], "strength": b[4] / hi - 1, "entry": nb[1], "exit": ex, "why": why}
        return None

    # VWAP reversion
    pv = sum((b[2] + b[3] + b[4]) / 3 * b[5] for b in rng)
    vv = sum(b[5] for b in rng)
    vwaps = []
    for b in rest:
        pv += (b[2] + b[3] + b[4]) / 3 * b[5]
        vv += b[5]
        vwaps.append(pv / vv if vv else b[4])
    for i, b in enumerate(rest[:-1]):
        if b[0] > VWAP_LAST_ENTRY:
            return None
        dev = (vwaps[i] - b[4]) / atr
        if dev >= p["DEV"]:
            nb = rest[i + 1]
            entry = nb[1]
            stop = entry - p["DEV"] * atr
            off = i + 1
            ex, why = exit_scan(rest[off:], stop, lambda x, k: vwaps[off + k - 1] if k > 0 else vwaps[i])
            return {"kind": "vwap", "entry_m": nb[0], "strength": dev, "entry": entry, "exit": ex, "why": why}
    return None


def run(data, atrs, mkt, p, mode, cap):
    dates = sorted({d for s in data.values() for d in s if d in mkt})
    picked, kinds = [], defaultdict(int)
    for d in dates:
        cands = []
        for sym, days in data.items():
            if d in days and d in atrs[sym]:
                atr, pc = atrs[sym][d]
                t = day_trade(days[d], atr, pc, mkt[d], p, mode)
                if t:
                    t.update(sym=sym, date=d)
                    cands.append(t)
        cands.sort(key=lambda t: (t["entry_m"], -t["strength"]))
        picked += cands[:min(cap, ib.SLOTS)]
    out = []
    for t in picked:
        kinds[t["kind"]] += 1
        buy_px, sell_px = t["entry"] * (1 + ib.SLIP), t["exit"] * (1 - ib.SLIP)
        qty = ib.SIZE / buy_px
        fb, cb = ib.order_fees(qty, buy_px, False)
        fs, cs = ib.order_fees(qty, sell_px, True)
        gross = qty * (t["exit"] - t["entry"])
        slip = qty * (buy_px - t["entry"]) + qty * (t["exit"] - sell_px)
        out.append({**t, "gross": gross, "slip": slip, "fees": fb + fs, "comm": cb + cs, "net": gross - slip - fb - fs})
    return out, dates, dict(kinds)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default="2022-01-01")
    ap.add_argument("--to", dest="end", default="2026-11-25")
    ap.add_argument("--out", default="results/intraday")
    ap.add_argument("--cache", default="data/intraday_cache")
    ap.add_argument("--benchmarks", default="data/benchmarks.csv")
    a = ap.parse_args()
    data = ib.fetch(ib.SYMBOLS, a.start, a.end, a.cache)
    atrs = {s: atr_table(daily(d)) for s, d in data.items()}
    mkt = market_table(a.benchmarks)
    os.makedirs(a.out, exist_ok=True)

    runs = [("ORB 30 min, every day", "orb", "base"), ("VWAP reversion, every day", "vwap", "base"),
            ("Regime filter (base)", "regime", "base"), ("Regime filter (looser)", "regime", "looser"),
            ("Regime filter (stricter)", "regime", "strict")]
    lines = ["# Regime-filtered intraday backtest after IBKR fees", "",
             f"{a.start} to {a.end}, 5-minute SIP bars, {len(ib.SYMBOLS)} liquid US stocks/ETFs. 500 EUR per trade, "
             "2,500 EUR capital, cash account, IBKR Pro Tiered + pass-through fees, 0.05% slippage per fill, IBKR market "
             "data. Net = after all costs. % a year on 2,500 EUR, no compounding. Holdout = 2025-01-01 on.", "",
             "| Strategy | Max trades/day | Trades | Trades/day | ORB / VWAP trades | Win rate (net) | Avg gross/trade | "
             "Avg net/trade | Gross $ | Costs $ | Net $ | Net %/yr | Avg gross 2022-24 | Avg gross 2025-26 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    js = []
    for name, mode, pset in runs:
        for cap in ib.CAPS:
            tr, dates, kinds = run(data, atrs, mkt, SETS[pset], mode, cap)
            s = ib.summarise(tr, dates)
            ins = [t for t in tr if t["date"] < "2025-01-01"]
            hold = [t for t in tr if t["date"] >= "2025-01-01"]
            g_in = sum(t["gross"] for t in ins) / max(len(ins), 1) / ib.SIZE * 100
            g_ho = sum(t["gross"] for t in hold) / max(len(hold), 1) / ib.SIZE * 100
            costs = s["slip"] + s["fees"] + s["data"]
            lines.append(f"| {name} | {cap} | {s['trades']} | {s['per_day']:.2f} | {kinds.get('orb', 0)} / {kinds.get('vwap', 0)} | "
                         f"{s['win'] * 100:.0f}% | {s['avg_gross_pct']:+.3f}% | {s['avg_net_pct']:+.3f}% | {s['gross']:,.0f} | "
                         f"{costs:,.0f} | {s['net']:,.0f} | {s['net_yr_pct']:+.1f}% | {g_in:+.3f}% | {g_ho:+.3f}% |")
            print(lines[-1], flush=True)
            by_kind = {}
            for k in ("orb", "vwap"):
                kt = [t for t in tr if t["kind"] == k]
                by_kind[k] = {"trades": len(kt), "avg_gross_pct": sum(t["gross"] for t in kt) / max(len(kt), 1) / ib.SIZE * 100,
                              "win_gross": sum(t["gross"] > 0 for t in kt) / max(len(kt), 1)}
            js.append({"strategy": name, "params": SETS[pset], "cap": cap, "kinds": kinds, "by_kind": by_kind,
                       "gross_2022_24_pct": g_in, "gross_2025_26_pct": g_ho, **s})
    lines += ["", "## Gross edge by trade type (max 5 trades/day)", "", "| Strategy | Type | Trades | Avg gross/trade | Gross win rate |",
              "|---|---|---|---|---|"]
    for j in js:
        if j["cap"] == 5:
            for k, v in j["by_kind"].items():
                if v["trades"]:
                    lines.append(f"| {j['strategy']} | {k.upper()} | {v['trades']} | {v['avg_gross_pct']:+.3f}% | {v['win_gross'] * 100:.0f}% |")
    with open(os.path.join(a.out, "regime_results.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(a.out, "regime_results.json"), "w") as f:
        json.dump(js, f, indent=1)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
