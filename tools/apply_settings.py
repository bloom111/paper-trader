#!/usr/bin/env python3
"""
Check settings saved on a settings page and write them into a bot's config file.

Usage: python tools/apply_settings.py rsi|regime SAVED.json
  SAVED.json is the saved settings document's `config` object:
    rsi:    from Trader Settings  (keys like config.json: strategy.rsi_length, position_size, ...)
    regime: from the scanner page (keys like regime/config.json: mode, map, mult, rsi, size, ...)
Prints one JSON line: {"status": "applied" | "unchanged" | "refused", "reason": "...", "file": "..."}.
Only the editable keys are taken from SAVED.json; every other key of the config file is kept.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STRATS = ["rsi", "sma", "macd", "boll", "breakout"]
REGIMES = ["upCalm", "upWild", "downCalm", "downWild"]


def num(v, lo, hi, integer=False):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi and (not integer or float(v).is_integer())


def check_rsi(c):
    s = c.get("strategy", {})
    tests = [
        (num(s.get("rsi_length"), 2, 30, True), "RSI length must be a whole number from 2 to 30."),
        (num(s.get("buy_below"), 1, 49), "Buy level must be from 1 to 49."),
        (num(s.get("sell_above"), 2, 99) and num(s.get("buy_below"), 1, 49) and s["sell_above"] > s["buy_below"], "Sell level must be above the buy level and at most 99."),
        (num(c.get("position_size"), 5, 1000), "Per position must be from 5 to 1000."),
        (num(c.get("max_new_per_day"), 1, 20, True), "Most new buys per day must be a whole number from 1 to 20."),
        (num(c.get("fee_per_order"), 0, 20), "Simulated fee must be from 0 to 20."),
        (c.get("rank_by") in ("shares", "value"), "Preference must be shares or value."),
        (isinstance(c.get("trend_filter_200d"), bool), "The 200-day filter must be on or off."),
        (num(c.get("stop_loss_pct"), 0, 50), "Stop-loss must be from 0 to 50%."),
        (num(c.get("max_days_held"), 0, 60, True), "Most days held must be a whole number from 0 to 60."),
    ]
    return [msg for ok, msg in tests if not ok]


def check_regime(c):
    out = []
    t = lambda ok, msg: out.append(msg) if not ok else None
    t(c.get("mode") in ("single", "regime"), "Strategy mode must be single or regime.")
    t(c.get("strat") in STRATS, "Unknown strategy.")
    for r in REGIMES:
        t(c.get("map", {}).get(r) in STRATS + ["none"], f"Unknown strategy for market type {r}.")
        t(num(c.get("mult", {}).get(r), 0, 3), f"Size multiplier for {r} must be from 0 to 3.")
    g = lambda k, f: c.get(k, {}).get(f)
    t(num(g("rsi", "n"), 2, 30, True) and num(g("rsi", "lo"), 1, 49) and num(g("rsi", "hi"), 2, 99) and g("rsi", "hi") > g("rsi", "lo"),
      "RSI: length 2–30, buy level 1–49, sell level above the buy level and at most 99.")
    t(num(g("sma", "f"), 2, 100, True) and num(g("sma", "s"), 3, 250, True) and g("sma", "f") < g("sma", "s"),
      "Moving averages: fast 2–100 days, slow 3–250 days and longer than fast.")
    t(num(g("macd", "f"), 2, 50, True) and num(g("macd", "s"), 3, 100, True) and g("macd", "f") < g("macd", "s") and num(g("macd", "g"), 2, 50, True),
      "MACD: fast 2–50, slow 3–100 and longer than fast, signal 2–50.")
    t(num(g("boll", "n"), 5, 100, True) and num(g("boll", "k"), 0.5, 4), "Bollinger: period 5–100 days, width 0.5–4.")
    t(num(g("breakout", "n"), 5, 250, True) and num(g("breakout", "m"), 5, 250, True), "Breakout: both periods 5–250 days.")
    t(num(c.get("trendTh"), -20, 20) and num(c.get("volTh"), 5, 80), "Market type: rising threshold −20 to 20%, volatile threshold 5–80%.")
    t(isinstance(c.get("volSize"), bool) and num(c.get("volTarget"), 5, 80) and num(c.get("multMin"), 0.1, 1) and num(c.get("multMax"), 1, 5),
      "Volatility sizing: normal size at 5–80%, smallest 0.1–1×, largest 1–5×.")
    t(isinstance(c.get("trend"), bool) and num(c.get("stop"), 0, 50) and num(c.get("maxHold"), 0, 60, True), "Stop-loss 0–50%, most days held 0–60.")
    t(num(c.get("size"), 5, 1000) and num(c.get("maxNew"), 1, 20, True) and num(c.get("fee"), 0, 20) and c.get("rank") in ("shares", "value"),
      "Per position 5–1000, new buys per day 1–20, fee 0–20.")
    return out


RSI_KEYS = [("strategy", "rsi_length"), ("strategy", "buy_below"), ("strategy", "sell_above"), ("position_size",), ("max_new_per_day",),
            ("fee_per_order",), ("rank_by",), ("trend_filter_200d",), ("stop_loss_pct",), ("max_days_held",)]
REGIME_KEYS = ["mode", "strat", "map", "mult", "rsi", "sma", "macd", "boll", "breakout", "trendTh", "volTh", "volSize", "volTarget",
               "multMin", "multMax", "trend", "stop", "maxHold", "size", "maxNew", "fee", "rank"]


def clean_numbers(v):
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, dict):
        return {k: clean_numbers(x) for k, x in v.items()}
    return v


def main(bot, saved_path):
    saved = json.load(open(saved_path))
    path = os.path.join(ROOT, "config.json" if bot == "rsi" else os.path.join("regime", "config.json"))
    cur = json.load(open(path))
    problems = check_rsi(saved) if bot == "rsi" else check_regime(saved)
    if problems:
        print(json.dumps({"status": "refused", "reason": problems[0], "file": path}))
        return
    new = json.loads(json.dumps(cur))
    if bot == "rsi":
        for k in RSI_KEYS:
            src, dst = saved, new
            for part in k[:-1]:
                src, dst = src[part], dst.setdefault(part, {})
            dst[k[-1]] = clean_numbers(src[k[-1]])
    else:
        for k in REGIME_KEYS:
            new[k] = clean_numbers(json.loads(json.dumps(saved[k])))
    if new == cur:
        print(json.dumps({"status": "unchanged", "reason": "", "file": path}))
        return
    with open(path, "w") as f:
        json.dump(new, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(json.dumps({"status": "applied", "reason": "", "file": path}))


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("rsi", "regime"):
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
