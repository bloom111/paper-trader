#!/usr/bin/env python3
"""
Market data archive: daily prices of every S&P 500 stock, SPY and VT, kept in ./data for backtesting.

Runs after the bots each weekday. Adds every finished session of the last two weeks that the archive
doesn't have yet, so a missed evening is filled in by the next run.

  data/bars/YYYY-MM.csv     date,symbol,open,high,low,close,volume,vwap,trades
                            Prices exactly as traded that day (not adjusted), in US dollars.
  data/splits.csv           symbol,ex_date,factor
                            Multiply prices before ex_date by factor (and divide volumes by it)
                            to make them comparable with prices from ex_date on. Found by comparing
                            Alpaca's raw and split-adjusted prices. Dividends are not adjusted.
  data/sp500_members.csv    symbol,first_seen,last_seen   (S&P 500 list as seen by the bots)

Usage:
  python market_data.py                      add missing recent sessions
  python market_data.py --from 2019-01-01    backfill from that date (current S&P 500 members only)
  BACKFILL_FROM=2019-01-01 python market_data.py   the same, as the workflow passes it

Environment: ALPACA_KEY_ID, ALPACA_SECRET_KEY (the RSI bot's keys; market data is free with any account).
Only the Python standard library is used.
"""
import csv
import glob
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

from paper_trader import AlpacaAPI, APIError, parse_ts, sp500_symbols

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
BAR_COLS = ["date", "symbol", "open", "high", "low", "close", "volume", "vwap", "trades"]
MARKET = "SPY"
# Benchmarks archived next to the stocks. VT (Vanguard Total World Stock ETF) stands in for the
# FTSE All-World: it tracks the FTSE Global All Cap index and trades in the US, so Alpaca has it.
BENCHMARKS = ["SPY", "VT"]


# ---------------------------------------------------------------- download
def fetch(api, symbols, start, end, adjustment):
    """{symbol: {date: bar}} for daily bars from Alpaca's data API."""
    out = {}
    for i in range(0, len(symbols), 100):
        chunk = symbols[i:i + 100]
        token = None
        while True:
            p = {"symbols": ",".join(chunk), "timeframe": "1Day", "start": start, "end": end,
                 "adjustment": adjustment, "feed": api.feed, "limit": 10000}
            if token:
                p["page_token"] = token
            r = api._req("GET", api.data + "/v2/stocks/bars", p)
            for sym, bars in (r.get("bars") or {}).items():
                d = out.setdefault(sym, {})
                for b in bars:
                    d[b["t"][:10]] = b
            token = r.get("next_page_token")
            if not token:
                break
    return out


def find_splits(raw, adj):
    """[(symbol, ex_date, factor)] where the split-adjusted/raw price ratio jumps between two sessions."""
    found = []
    for sym, days in raw.items():
        a = adj.get(sym, {})
        prev = None
        for d in sorted(days):
            if d not in a or not days[d]["c"]:
                continue
            ratio = a[d]["c"] / days[d]["c"]
            if prev is not None and abs(ratio / prev[1] - 1) > 0.02:
                found.append((sym, d, round(prev[1] / ratio, 6)))
            prev = (d, ratio)
    return found


# ---------------------------------------------------------------- files
def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, cols, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def archived_sessions():
    """Sessions already in the archive (from the last two monthly files)."""
    have = set()
    for path in sorted(glob.glob(os.path.join(DATA, "bars", "*.csv")))[-2:]:
        have |= {r["date"] for r in read_csv(path)}
    return have


def save_bars(raw, sessions):
    """Merge bars of the given sessions into the monthly files. Returns the number of rows written."""
    by_month = {}
    for sym, days in raw.items():
        for d, b in days.items():
            if d in sessions:
                by_month.setdefault(d[:7], []).append({
                    "date": d, "symbol": sym, "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"],
                    "volume": int(b["v"]), "vwap": b.get("vw", ""), "trades": b.get("n", "")})
    n = 0
    for month, rows in by_month.items():
        path = os.path.join(DATA, "bars", f"{month}.csv")
        keep = {(r["date"], r["symbol"]): r for r in read_csv(path)}
        for r in rows:
            keep[(r["date"], r["symbol"])] = r
        write_csv(path, BAR_COLS, sorted(keep.values(), key=lambda r: (r["date"], r["symbol"])))
        n += len(rows)
    return n


def save_splits(found):
    path = os.path.join(DATA, "splits.csv")
    keep = {(r["symbol"], r["ex_date"]): r for r in read_csv(path)}
    for sym, d, f in found:
        keep[(sym, d)] = {"symbol": sym, "ex_date": d, "factor": f}
    write_csv(path, ["symbol", "ex_date", "factor"], sorted(keep.values(), key=lambda r: (r["ex_date"], r["symbol"])))


def save_members(symbols, session):
    path = os.path.join(DATA, "sp500_members.csv")
    rows = {r["symbol"]: r for r in read_csv(path)}
    for s in symbols:
        r = rows.setdefault(s, {"symbol": s, "first_seen": session, "last_seen": session})
        r["first_seen"] = min(r["first_seen"], session)
        r["last_seen"] = max(r["last_seen"], session)
    write_csv(path, ["symbol", "first_seen", "last_seen"], sorted(rows.values(), key=lambda r: r["symbol"]))


# ---------------------------------------------------------------- main
def run(api, start=None, log=print):
    now = parse_ts(api.clock()["timestamp"])
    first = start or (now.date() - timedelta(days=14)).isoformat()
    cal = api.calendar(first, now.date().isoformat())
    sessions = [d["date"] for d in cal
                if now >= datetime.fromisoformat(f'{d["date"]}T{d["close"]}').replace(tzinfo=now.tzinfo) + timedelta(minutes=20)]
    if not sessions:
        log("No finished session in range.")
        return None
    missing = set(sessions) if start else set(sessions) - archived_sessions()
    if not missing:
        log(f"Archive already has every session up to {sessions[-1]}.")
        return {"added": 0, "last": sessions[-1]}

    symbols, note = sp500_symbols(os.path.join(HERE, "regime", "state"))
    if note:
        log(note)
    need = sorted(set(symbols) | set(BENCHMARKS))
    # one extra session before the first missing one, so a split on that day is still detected
    lo = min(missing)
    earlier = [s for s in sessions if s < lo]
    fetch_from = earlier[-1] if earlier else (date.fromisoformat(lo) - timedelta(days=7)).isoformat()
    end = (datetime.now(timezone.utc) - timedelta(minutes=16)).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw = fetch(api, need, fetch_from, end, "raw")
    adj = fetch(api, need, fetch_from, end, "split")

    have = {d for days in raw.values() for d in days}
    added = sorted(missing & have)
    complete = [d for d in added if sum(1 for s in symbols if d in raw.get(s, {})) >= 0.8 * len(symbols)]
    if len(complete) < len(added):
        log("Skipped incomplete sessions: " + ", ".join(sorted(set(added) - set(complete))))
    if not complete:
        log("No complete sessions to add.")
        return {"added": 0, "last": sessions[-1]}
    rows = save_bars(raw, set(complete))
    splits = find_splits(raw, adj)
    save_splits(splits)
    save_members(symbols, complete[-1])
    msg = (f"Market data: added {len(complete)} session(s) ({complete[0]} to {complete[-1]}), {rows} price rows"
           + (f", {len(splits)} split(s): " + ", ".join(f"{s} {d} ×{f:g}" for s, d, f in splits) if splits else "") + ".")
    if api.feed != "sip":
        msg += " Volumes are IEX-only because SIP data is not available on this plan."
    log(msg)
    return {"added": len(complete), "first": complete[0], "last": complete[-1], "rows": rows, "splits": splits, "summary": msg}


def main():
    key, secret = os.environ.get("ALPACA_KEY_ID"), os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        sys.exit("Set ALPACA_KEY_ID and ALPACA_SECRET_KEY.")
    start = os.environ.get("BACKFILL_FROM", "").strip() or None
    if "--from" in sys.argv:
        start = sys.argv[sys.argv.index("--from") + 1]
    if start:
        date.fromisoformat(start)   # refuses anything that isn't YYYY-MM-DD
    with open(os.path.join(HERE, "regime", "config.json")) as f:
        feed = json.load(f).get("data_feed", "sip")
    api = AlpacaAPI(key, secret, feed=feed)
    try:
        res = run(api, start)
    except APIError as e:
        if api.feed == "sip" and e.code in (403, 422):
            api.feed = "iex"
            res = run(api, start)
        else:
            raise
    out = os.environ.get("GITHUB_STEP_SUMMARY")
    if out and res and res.get("summary"):
        with open(out, "a") as f:
            f.write("## Market data archive\n" + res["summary"] + "\n")


if __name__ == "__main__":
    main()
