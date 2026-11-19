#!/usr/bin/env python3
"""
Build the Market Data page's files and the scanner's backtest file from the archive in ./data.

Usage: python tools/archive_files.py OUT_DIR [--years N]
Writes into OUT_DIR:
  summary.json          coverage per month, SPY closes and non-trading days, splits (for the Market Data page)
  sp500_archive.json    split-adjusted prices in the scanner's format (dates, tickers, names, open, close, vol, index),
                        open/close in US cents, index = SPY close, bench2 = VT close (FTSE All-World stand-in);
                        last N years only (default 5)
  bars/YYYY-MM.csv      copies of the monthly files (unadjusted, as archived)
"""
import csv
import glob
import json
import os
import shutil
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import market_filter  # noqa: E402

DATA = os.path.join(ROOT, "data")
MARKET = "SPY"
WORLD = "VT"   # stand-in for the FTSE All-World
NOT_STOCKS = {MARKET, WORLD}


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def main(out, years=5):
    os.makedirs(os.path.join(out, "bars"), exist_ok=True)
    files = sorted(glob.glob(os.path.join(DATA, "bars", "*.csv")))
    splits = {}
    for r in read_csv(os.path.join(DATA, "splits.csv")):
        splits.setdefault(r["symbol"], []).append((r["ex_date"], float(r["factor"])))

    def factor(sym, d):
        f = 1.0
        for ex, x in splits.get(sym, []):
            if d < ex:
                f *= x
        return f

    months, px = [], {}
    for path in files:
        rows = read_csv(path)
        shutil.copyfile(path, os.path.join(out, "bars", os.path.basename(path)))
        months.append({"month": os.path.basename(path)[:7], "sessions": len({r["date"] for r in rows}),
                       "symbols": len({r["symbol"] for r in rows}), "rows": len(rows), "bytes": os.path.getsize(path)})
        for r in rows:
            f = factor(r["symbol"], r["date"])
            px.setdefault(r["symbol"], {})[r["date"]] = (float(r["open"]) * f, float(r["close"]) * f, float(r["volume"]) / f)

    spy = px.get(MARKET, {})
    all_dates = sorted(spy)
    spy_c = [spy[d][1] for d in all_dates]
    pause = []
    for i in range(len(all_dates)):
        if i + 1 >= market_filter.MIN_CLOSES:
            p, _, _ = market_filter.check(spy_c[:i + 1], {})
            pause.append(1 if p else 0)
        else:
            pause.append(None)
    summary = {
        "updatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "first": all_dates[0] if all_dates else None, "last": all_dates[-1] if all_dates else None,
        "sessions": len(all_dates), "symbols": len([s for s in px if s not in NOT_STOCKS]),
        "rows": sum(m["rows"] for m in months), "bytes": sum(m["bytes"] for m in months),
        "months": months,
        "spy": [[d, round(c, 2), p] for d, c, p in zip(all_dates, spy_c, pause)],
        "vt": [[d, round(px[WORLD][d][1], 2)] for d in all_dates if d in px.get(WORLD, {})],
        "splits": [{"symbol": s, "ex_date": ex, "factor": x} for s, v in sorted(splits.items()) for ex, x in v],
        "pauseRule": market_filter.describe({}),
    }
    with open(os.path.join(out, "summary.json"), "w") as f:
        json.dump(summary, f, separators=(",", ":"))

    # scanner file: last `years` years of sessions, stocks with at least one price in that window
    if all_dates:
        cut = str(int(all_dates[-1][:4]) - years) + all_dates[-1][4:]
        dates = [d for d in all_dates if d > cut]
        tickers = sorted(s for s in px if s not in NOT_STOCKS and any(d in px[s] for d in dates))
        vt = px.get(WORLD, {})
        cents = lambda v: None if v is None else int(round(v * 100))
        col = lambda s, k: [cents(px[s][d][k]) if d in px[s] else None for d in dates]
        scan = {"dates": dates, "tickers": tickers, "names": [""] * len(tickers),
                "open": [col(s, 0) for s in tickers], "close": [col(s, 1) for s in tickers],
                "vol": [[int(px[s][d][2]) if d in px[s] else None for d in dates] for s in tickers],
                "index": [round(spy[d][1], 2) for d in dates], "source": "Alpaca archive (bloom111/paper-trader data/)",
                "indexName": "SPY", "bench2": [round(vt[d][1], 2) if d in vt else None for d in dates],
                "bench2Name": "FTSE All-World (VT)"}
        with open(os.path.join(out, "sp500_archive.json"), "w") as f:
            json.dump(scan, f, separators=(",", ":"))
    print(json.dumps({"sessions": len(all_dates), "first": summary["first"], "last": summary["last"], "months": len(months)}))


if __name__ == "__main__":
    args = sys.argv[1:]
    yrs = 5
    if "--years" in args:
        i = args.index("--years")
        yrs = int(args[i + 1])
        del args[i:i + 2]
    main(args[0] if args else "archive_files", yrs)
