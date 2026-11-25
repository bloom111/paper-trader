#!/usr/bin/env python3
"""
Build the dashboard's data files from both bots' results.

Usage: python tools/dashboard_files.py OUT_DIR
Writes 19 CSV files into OUT_DIR (header-only where a bot has no results yet):
  RSI bot:          equity.csv trades.csv orders.csv signals.csv positions.csv config.csv
  Market-type bot:  mt_equity.csv mt_trades.csv mt_orders.csv mt_signals.csv mt_positions.csv mt_config.csv
  Riskier bot:      rk_equity.csv rk_trades.csv rk_orders.csv rk_signals.csv rk_positions.csv rk_config.csv
  All:              benchmarks.csv  session,spy_close,vt_close for every day either bot has a result
                    (SPY from the bots' logs, VT = FTSE All-World stand-in from the market data archive)
"""
import csv
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import market_filter  # noqa: E402
import paper_trader  # noqa: E402
import regime_trader  # noqa: E402

STRAT_LONG = {
    "rsi": lambda p: f"RSI dip: buy when the {p['n']}-day RSI closes below {p['lo']}, sell above {p['hi']}",
    "sma": lambda p: f"Moving-average crossover: {p['f']}-day over {p['s']}-day average",
    "macd": lambda p: f"MACD crossover: {p['f']}/{p['s']} with a {p['g']}-day signal line",
    "boll": lambda p: f"Bollinger bounce: below the lower band ({p['n']} days, {p['k']}σ), sell above the middle",
    "breakout": lambda p: f"Breakout: new {p['n']}-day high, sell on a new {p['m']}-day low",
}


def fee_text(m):
    parts = [f"${m['fee']:g}"] if m.get("fee") else []
    if m.get("feePct"): parts.append(f"{m['feePct']:g}% of the order")
    if m.get("feePerShare"): parts.append(f"${m['feePerShare']:g} per share")
    t = " + ".join(parts) or "$0"
    if m.get("feeMin"): t += f", at least ${m['feeMin']:g}"
    if m.get("feeMaxPct"): t += f", at most {m['feeMaxPct']:g}% of the order"
    return t


def copy_or_header(src, dst, header):
    if os.path.exists(src):
        with open(src, newline="") as f:
            rows = list(csv.reader(f))
        if rows:
            with open(dst, "w", newline="") as f:
                csv.writer(f).writerows(rows)
            return
    with open(dst, "w", newline="") as f:
        csv.writer(f).writerow(header)


def write_rows(dst, header, rows):
    with open(dst, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def positions(state_path, extra=False):
    st = json.load(open(state_path)) if os.path.exists(state_path) else {"positions": {}}
    rows = []
    for sym, p in sorted(st.get("positions", {}).items()):
        row = [sym, p["entry_session"], round(p["qty"], 6), round(p["entry_price"], 4), round(p["cost"], 2),
               "true" if p.get("exit_pending") else "false"]
        if extra:
            row += [p.get("k", ""), p.get("reg") or ""]
        rows.append(row)
    return rows


def benchmarks(out):
    """SPY and VT closes on every session any bot has logged."""
    days = {}
    for path in (os.path.join(ROOT, d, "equity.csv") for d in ("state", os.path.join("regime", "state"), os.path.join("risky", "state"))):
        if os.path.exists(path):
            with open(path, newline="") as f:
                for r in csv.DictReader(f):
                    if r.get("session"):
                        d = days.setdefault(r["session"], {"spy": "", "vt": ""})
                        d["spy"] = d["spy"] or r.get("spy_close", "")
    for m in {s[:7] for s in days}:
        path = os.path.join(ROOT, "data", "bars", f"{m}.csv")
        if os.path.exists(path):
            with open(path, newline="") as f:
                for r in csv.DictReader(f):
                    if r["symbol"] == "VT" and r["date"] in days:
                        days[r["date"]]["vt"] = r["close"]
    write_rows(os.path.join(out, "benchmarks.csv"), ["session", "spy_close", "vt_close"],
               [[s, days[s]["spy"], days[s]["vt"]] for s in sorted(days)])


def market_type_files(out, bot, prefix):
    """Logs, positions and settings of a market-type bot (folder `bot`), as PREFIX*.csv."""
    md = os.path.join(ROOT, bot, "state")
    for name, cols in regime_trader.LOGS.items():
        copy_or_header(os.path.join(md, name), os.path.join(out, prefix + name), cols)
    write_rows(os.path.join(out, prefix + "positions.csv"),
               ["symbol", "entry_session", "qty", "entry_price", "cost", "exit_pending", "strategy", "market"],
               positions(os.path.join(md, "state.json"), extra=True))
    m = json.load(open(os.path.join(ROOT, bot, "config.json")))
    rows = []
    if m["mode"] == "regime":
        used = []
        for r in regime_trader.REGIMES:
            k = m["map"][r]
            mult = m.get("mult", {}).get(r, 1)
            label = regime_trader.REGIME_LABEL[r].capitalize()
            rows.append([label, regime_trader.STRAT_LABEL[k] + (f", size ×{mult}" if k != "none" and mult != 1 else "")])
            if k != "none" and k not in used:
                used.append(k)
        rows.append(["Rising when SPY is above its 200-day average by", f"{m['trendTh']}%"])
        rows.append(["Volatile when 20-day volatility is above", f"{m['volTh']}%"])
    else:
        used = [m["strat"]]
        rows.append(["Strategy in every market", regime_trader.STRAT_LABEL[m["strat"]]])
    for k in used:
        rows.append([regime_trader.STRAT_LABEL[k] + " rule", STRAT_LONG[k](m[k])])
    rows += [["Per position", f"${m['size']:g}"], ["Most new buys per day", m["maxNew"]], ["Simulated fee per order", fee_text(m)],
             ["With more signals than buys, prefer", "most money traded" if m["rank"] == "value" else "most shares traded"],
             ["Only stocks above their 200-day average", "yes" if m["trend"] else "no"],
             ["Stop-loss", f"{m['stop']:g}%" if m["stop"] else "off"], ["Most days held", m["maxHold"] or "no limit"]]
    if m.get("volSize"):
        rows.append(["Size by volatility", f"normal at {m['volTarget']:g}%, between ×{m['multMin']:g} and ×{m['multMax']:g}"])
    rows.append(["Non-trading period", market_filter.describe(m)])
    rows.append(["Start budget", f"${m['capital']:g}"])
    write_rows(os.path.join(out, prefix + "config.csv"), ["setting", "value"], rows)


def main(out):
    os.makedirs(out, exist_ok=True)
    # RSI bot
    sd = os.path.join(ROOT, "state")
    for name, cols in paper_trader.LOGS.items():
        copy_or_header(os.path.join(sd, name), os.path.join(out, name), cols)
    write_rows(os.path.join(out, "positions.csv"), ["symbol", "entry_session", "qty", "entry_price", "cost", "exit_pending"],
               positions(os.path.join(sd, "state.json")))
    c = json.load(open(os.path.join(ROOT, "config.json")))
    write_rows(os.path.join(out, "config.csv"), ["setting", "value"], [
        ["rsi_length", c["strategy"]["rsi_length"]], ["buy_below", c["strategy"]["buy_below"]], ["sell_above", c["strategy"]["sell_above"]],
        ["position_size", c["position_size"]], ["max_new_per_day", c["max_new_per_day"]], ["fee_per_order", c["fee_per_order"]],
        ["rank_by", c["rank_by"]], ["trend_filter_200d", str(c["trend_filter_200d"]).lower()], ["stop_loss_pct", c["stop_loss_pct"]],
        ["max_days_held", c["max_days_held"]], ["start_capital", c["start_capital"]],
        ["non_trading_period", market_filter.describe(c)]])

    market_type_files(out, "regime", "mt_")
    market_type_files(out, "risky", "rk_")
    benchmarks(out)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "dashboard_files")
