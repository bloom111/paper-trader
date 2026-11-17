#!/usr/bin/env python3
"""
Build the dashboard's data files from both bots' results.

Usage: python tools/dashboard_files.py OUT_DIR
Writes 12 CSV files into OUT_DIR (header-only where a bot has no results yet):
  RSI bot:          equity.csv trades.csv orders.csv signals.csv positions.csv config.csv
  Market-type bot:  mt_equity.csv mt_trades.csv mt_orders.csv mt_signals.csv mt_positions.csv mt_config.csv
"""
import csv
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import paper_trader  # noqa: E402
import regime_trader  # noqa: E402

STRAT_LONG = {
    "rsi": lambda p: f"RSI dip: buy when the {p['n']}-day RSI closes below {p['lo']}, sell above {p['hi']}",
    "sma": lambda p: f"Moving-average crossover: {p['f']}-day over {p['s']}-day average",
    "macd": lambda p: f"MACD crossover: {p['f']}/{p['s']} with a {p['g']}-day signal line",
    "boll": lambda p: f"Bollinger bounce: below the lower band ({p['n']} days, {p['k']}σ), sell above the middle",
    "breakout": lambda p: f"Breakout: new {p['n']}-day high, sell on a new {p['m']}-day low",
}


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
        ["max_days_held", c["max_days_held"]], ["start_capital", c["start_capital"]]])

    # Market-type bot
    md = os.path.join(ROOT, "regime", "state")
    for name, cols in regime_trader.LOGS.items():
        copy_or_header(os.path.join(md, name), os.path.join(out, "mt_" + name), cols)
    write_rows(os.path.join(out, "mt_positions.csv"),
               ["symbol", "entry_session", "qty", "entry_price", "cost", "exit_pending", "strategy", "market"],
               positions(os.path.join(md, "state.json"), extra=True))
    m = json.load(open(os.path.join(ROOT, "regime", "config.json")))
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
    rows += [["Per position", f"${m['size']:g}"], ["Most new buys per day", m["maxNew"]], ["Simulated fee per order", f"${m['fee']:g}"],
             ["With more signals than buys, prefer", "most money traded" if m["rank"] == "value" else "most shares traded"],
             ["Only stocks above their 200-day average", "yes" if m["trend"] else "no"],
             ["Stop-loss", f"{m['stop']:g}%" if m["stop"] else "off"], ["Most days held", m["maxHold"] or "no limit"]]
    if m.get("volSize"):
        rows.append(["Size by volatility", f"normal at {m['volTarget']:g}%, between ×{m['multMin']:g} and ×{m['multMax']:g}"])
    rows.append(["Start budget", f"${m['capital']:g}"])
    write_rows(os.path.join(out, "mt_config.csv"), ["setting", "value"], rows)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "dashboard_files")
