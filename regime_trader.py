#!/usr/bin/env python3
"""
Market-type paper trader for a second Alpaca paper account.

Same daily routine as paper_trader.py (book this morning's fills, scan the S&P 500
after the close, queue orders for the next open), but the rules come from the
S&P 500 Dip Scanner: each evening the market is classified by SPY's distance from
its 200-day average (trend) and its 20-day volatility, and the strategy assigned to
that market type looks for new buys. Every position is sold by the rule of the
strategy that bought it.

Settings: regime/config.json (written from the scanner page by the scheduled task).
Results:  regime/state/  (state.json, equity.csv, trades.csv, orders.csv, signals.csv)

Environment:
  ALPACA_REGIME_KEY_ID, ALPACA_REGIME_SECRET_KEY   keys of the second paper account
  DRY_RUN=1                                        show what would happen; place nothing, save nothing
"""
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import market_filter
from paper_trader import (OPEN_STATES, APIError, AlpacaAPI, append_log, load_json, money, parse_ts,
                          save_json, sp500_symbols)
import paper_trader

HERE = os.path.dirname(os.path.abspath(__file__))
BOT_DIR = os.path.join(HERE, "regime")
REGIMES = ["upCalm", "upWild", "downCalm", "downWild"]
REGIME_LABEL = {"upCalm": "rising, calm", "upWild": "rising, volatile", "downCalm": "falling, calm", "downWild": "falling, volatile"}
STRAT_LABEL = {"rsi": "RSI dip", "sma": "Moving-average crossover", "macd": "MACD crossover",
               "boll": "Bollinger band bounce", "breakout": "Breakout", "none": "No new buys"}

LOGS = {
    "orders.csv": paper_trader.LOGS["orders.csv"],
    "trades.csv": paper_trader.LOGS["trades.csv"] + ["strategy", "market"],
    "signals.csv": ["session", "symbol", "strategy", "value", "close", "volume", "action"],
    "equity.csv": paper_trader.LOGS["equity.csv"] + ["market", "trend", "volatility", "strategy"],
}


def log_rows(state_dir, name, rows):
    """append_log with this bot's column sets."""
    saved = paper_trader.LOGS.get(name)
    paper_trader.LOGS[name] = LOGS[name]
    try:
        append_log(state_dir, name, rows)
    finally:
        paper_trader.LOGS[name] = saved


# ---------------------------------------------------------------- indicators (same formulas as the scanner)
def sma_a(a, n):
    out, s = [None] * len(a), 0.0
    for i, v in enumerate(a):
        s += v
        if i >= n:
            s -= a[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema_a(a, n):
    out, k, e, cnt, tot = [None] * len(a), 2 / (n + 1), None, 0, 0.0
    for i, v in enumerate(a):
        if v is None:
            continue
        if e is None:
            tot += v
            cnt += 1
            if cnt == n:
                e = tot / n
                out[i] = e
        else:
            e = v * k + e * (1 - k)
            out[i] = e
    return out


rsi_a = paper_trader.rsi


def cross_up(a, b, i):
    return i > 0 and None not in (a[i], b[i], a[i - 1], b[i - 1]) and a[i] > b[i] and a[i - 1] <= b[i - 1]


def flags(key, c, p):
    """(entry, exit, value) for the LAST day of closes c, using strategy `key` with params p."""
    i = len(c) - 1
    if key == "rsi":
        r = rsi_a(c, int(p["n"]))[i]
        return (r is not None and r < p["lo"], r is not None and r > p["hi"], r)
    if key == "sma":
        f, s = sma_a(c, int(p["f"])), sma_a(c, int(p["s"]))
        return (cross_up(f, s, i), f[i] is not None and s[i] is not None and f[i] < s[i], None)
    if key == "macd":
        f, s = ema_a(c, int(p["f"])), ema_a(c, int(p["s"]))
        m = [a - b if a is not None and b is not None else None for a, b in zip(f, s)]
        g = ema_a(m, int(p["g"]))
        return (cross_up(m, g, i), m[i] is not None and g[i] is not None and m[i] < g[i], None)
    if key == "boll":
        n = int(p["n"])
        if len(c) < n:
            return (False, False, None)
        w = c[-n:]
        mid = sum(w) / n
        sd = math.sqrt(sum((x - mid) ** 2 for x in w) / n)
        return (c[i] < mid - p["k"] * sd, c[i] > mid, None)
    if key == "breakout":
        n, m = int(p["n"]), int(p["m"])
        hi = max(c[max(0, i - n):i]) if i > 0 else -math.inf
        lo = min(c[max(0, i - m):i]) if i > 0 else math.inf
        return (i >= n and c[i] > hi, i >= m and c[i] < lo, None)
    raise ValueError(f"unknown strategy {key}")


def market_measures(closes):
    """Trend: distance from the 200-day average. Volatility: last 20 daily log returns, per year."""
    if len(closes) < 201:
        return None, None
    trend = closes[-1] / (sum(closes[-200:]) / 200) - 1
    r = [math.log(closes[j] / closes[j - 1]) for j in range(len(closes) - 20, len(closes))]
    m = sum(r) / 20
    vol = math.sqrt(sum((x - m) ** 2 for x in r) / 19 * 252)
    return trend, vol


def regime_of(trend, vol, cfg):
    if trend is None or vol is None:
        return None
    return ("up" if trend >= cfg["trendTh"] / 100 else "down") + ("Wild" if vol > cfg["volTh"] / 100 else "Calm")


def size_mult(vol, cfg):
    if not cfg.get("volSize") or not vol:
        return 1.0
    return min(cfg["multMax"], max(cfg["multMin"], (cfg["volTarget"] / 100) / vol))


def order_fee(cfg, qty, px):
    """Broker commission for one order of qty shares at px.

    fee + feePct % of the order value + feePerShare per share, at least feeMin and,
    when feeMaxPct > 0, at most feeMaxPct % of the order value. Missing keys count as 0,
    so a config with only "fee" keeps the old flat fee per order.
    IBKR Pro Fixed (US stocks): feePerShare 0.0035, feeMin 0.35, feeMaxPct 1.
    """
    value = qty * px
    if value <= 0:
        return 0.0
    f = float(cfg.get("fee", 0)) + value * float(cfg.get("feePct", 0)) / 100 + qty * float(cfg.get("feePerShare", 0))
    f = max(f, float(cfg.get("feeMin", 0)))
    if float(cfg.get("feeMaxPct", 0)) > 0:
        f = min(f, value * float(cfg["feeMaxPct"]) / 100)
    return f


# ---------------------------------------------------------------- main logic
def run(api, cfg, state_dir, dry_run=False, symbols=None, log=print):
    os.makedirs(state_dir, exist_ok=True)
    state_path = os.path.join(state_dir, "state.json")
    st = load_json(state_path, None) or {"cash": float(cfg["capital"]), "positions": {}, "pending": {},
                                         "last_session": None, "started": None, "fees_paid": 0.0}
    notes, summary = [], []

    now = parse_ts(api.clock()["timestamp"])
    cal = api.calendar((now.date() - timedelta(days=500)).isoformat(), now.date().isoformat())
    sessions = [d["date"] for d in cal
                if now >= datetime.fromisoformat(f'{d["date"]}T{d["close"]}').replace(tzinfo=now.tzinfo) + timedelta(minutes=20)]
    if not sessions:
        log("No finished trading session found yet. Nothing to do.")
        return None
    session = sessions[-1]
    if st["last_session"] == session:
        log(f"Session {session} was already processed. Nothing to do.")
        return None
    st["started"] = st["started"] or session
    sidx = {d: i for i, d in enumerate(sessions)}

    def days_between(a, b):
        return sidx[b] - sidx[a] if a in sidx and b in sidx else None

    # ---- 1. book orders from the previous run
    order_rows, trade_rows = [], []
    for oid, p in list(st["pending"].items()):
        o = api.get_order(oid)
        status = o.get("status")
        if status in OPEN_STATES:
            api.cancel_order(oid)
            o = api.get_order(oid)
            notes.append(f"{p['symbol']} {p['side']} order was still open after the session and was cancelled.")
        q = float(o.get("filled_qty") or 0)
        px = float(o.get("filled_avg_price") or 0)
        sym = p["symbol"]
        if q > 0 and p["side"] == "buy":
            fee = order_fee(cfg, q, px)
            cost = q * px + fee
            st["cash"] -= cost
            st["fees_paid"] += fee
            st["positions"][sym] = {"qty": q, "cost": cost, "entry_price": px, "entry_session": session,
                                    "k": p.get("k", "rsi"), "reg": p.get("reg")}
        elif q > 0 and p["side"] == "sell":
            pos = st["positions"][sym]
            share = min(1.0, q / pos["qty"])
            cost_part = pos["cost"] * share
            fee = order_fee(cfg, q, px)
            proceeds = q * px - fee
            st["cash"] += proceeds
            st["fees_paid"] += fee
            trade_rows.append({"symbol": sym, "entry_session": pos["entry_session"], "exit_session": session,
                               "days_held": days_between(pos["entry_session"], session), "entry_price": round(pos["entry_price"], 4),
                               "exit_price": round(px, 4), "qty": round(q, 6), "cost": round(cost_part, 2),
                               "proceeds": round(proceeds, 2), "return_pct": round((proceeds / cost_part - 1) * 100, 2),
                               "pnl": round(proceeds - cost_part, 2), "strategy": pos.get("k", ""), "market": pos.get("reg") or ""})
            pos["qty"] -= q
            pos["cost"] -= cost_part
            if pos["qty"] <= 1e-6:
                del st["positions"][sym]
            else:
                pos["exit_pending"] = True
        elif p["side"] == "sell":
            st["positions"][sym]["exit_pending"] = True
            notes.append(f"Sell order for {sym} did not fill ({status}); it will be placed again.")
        else:
            notes.append(f"Buy order for {sym} did not fill ({status}).")
        order_rows.append({"session": p["session"], "order_id": oid, "symbol": sym, "side": p["side"],
                           "notional": p.get("notional", ""), "qty": p.get("qty", ""), "status": o.get("status"),
                           "filled_qty": q, "filled_avg_price": px, "filled_at": o.get("filled_at") or "", "note": "booked " + session})
        del st["pending"][oid]

    # ---- 2. prices and the market type
    if symbols is None:
        symbols, unote = sp500_symbols(state_dir)
        if unote:
            notes.append(unote)
    mkt = cfg.get("market_symbol", "SPY")
    need = sorted(set(symbols) | set(st["positions"]) | {mkt})
    start = (datetime.fromisoformat(session) - timedelta(days=420)).date().isoformat()
    end = (datetime.now(timezone.utc) - timedelta(minutes=16)).strftime("%Y-%m-%dT%H:%M:%SZ")
    bars = api.daily_bars(need, start, end)
    notes += getattr(api, "notes", [])

    series = {}
    for sym in need:
        b = [x for x in bars.get(sym, []) if x["date"] <= session]
        if b and b[-1]["date"] == session:
            series[sym] = b
    fresh = sum(1 for s in symbols if s in series)
    if fresh < 0.8 * len(symbols):
        raise RuntimeError(f"Only {fresh} of {len(symbols)} stocks have a price for {session}. Stopping without trading.")
    if mkt not in series:
        raise RuntimeError(f"No {mkt} price for {session}, so the market type can't be measured. Stopping without trading.")

    trend, vol = market_measures([x["c"] for x in series[mkt]])
    paused, pause_reason, _ = market_filter.check([x["c"] for x in series[mkt]], cfg)
    reg = regime_of(trend, vol, cfg)
    if cfg.get("mode") == "regime":
        strat_now = (cfg["map"].get(reg) or "none") if reg else "none"
        mult_now = float(cfg.get("mult", {}).get(reg, 1)) if reg else 1.0
    else:
        strat_now, mult_now = cfg["strat"], 1.0
    size = float(cfg["size"]) * size_mult(vol, cfg) * mult_now

    cache = {}

    def sig(sym, key):
        if (sym, key) not in cache:
            c = [x["c"] for x in series[sym]]
            cache[(sym, key)] = flags(key, c, cfg[key])
        return cache[(sym, key)]

    # ---- 3. exits (each position by the rule of the strategy that bought it)
    new_orders, signal_rows = [], []
    est_proceeds = 0.0
    for sym, pos in st["positions"].items():
        if sym not in series:
            notes.append(f"No price for held stock {sym} today; kept it.")
            continue
        close = series[sym][-1]["c"]
        k = pos.get("k", "rsi")
        ent, ext, val = sig(sym, k)
        held = days_between(pos["entry_session"], session) or 0
        reason = None
        if pos.get("exit_pending"):
            reason = "retry"
        elif ext:
            reason = k
        elif cfg.get("stop", 0) > 0 and close <= pos["entry_price"] * (1 - cfg["stop"] / 100):
            reason = "stop"
        elif cfg.get("maxHold", 0) > 0 and held >= cfg["maxHold"]:
            reason = "max days"
        if reason:
            new_orders.append({"symbol": sym, "side": "sell", "qty": round(pos["qty"], 9), "why": reason, "k": k})
            est_proceeds += pos["qty"] * close - order_fee(cfg, pos["qty"], close)
            signal_rows.append({"session": session, "symbol": sym, "strategy": k, "value": round(val, 2) if val is not None else "",
                                "close": close, "volume": series[sym][-1]["v"], "action": f"sell ({reason})"})

    # ---- 4. entries with the strategy for today's market type
    cands = []
    if strat_now != "none":
        for sym in symbols:
            if sym in st["positions"] or sym not in series or sym == mkt:
                continue
            ent, ext, val = sig(sym, strat_now)
            if not ent:
                continue
            if cfg.get("trend"):
                c = [x["c"] for x in series[sym]]
                if len(c) < 200 or c[-1] <= sum(c[-200:]) / 200:
                    continue
            last = series[sym][-1]
            cands.append((sym, last["v"] * last["c"] if cfg.get("rank") == "value" else last["v"], val, last))
        cands.sort(key=lambda x: x[1], reverse=True)
    available = st["cash"] + est_proceeds
    for rank, (sym, _, val, last) in enumerate(cands):
        fee = order_fee(cfg, size / last["c"], last["c"]) if last["c"] else 0.0
        if rank >= int(cfg["maxNew"]):
            action = "not ranked high enough"
        elif paused:
            action = "skipped, non-trading period"
        elif available >= size + fee:
            new_orders.append({"symbol": sym, "side": "buy", "notional": round(size, 2), "why": strat_now, "k": strat_now, "reg": reg})
            available -= size + fee
            action = "buy"
        else:
            action = "skipped, not enough cash"
        signal_rows.append({"session": session, "symbol": sym, "strategy": strat_now, "value": round(val, 2) if val is not None else "",
                            "close": last["c"], "volume": last["v"], "action": action})

    # ---- 5. place orders
    for o in new_orders:
        body = {"symbol": o["symbol"], "side": o["side"], "type": "market", "time_in_force": "day",
                "client_order_id": f"mkt-{session}-{o['side']}-{o['symbol']}-{int(time.time())}"}
        body.update({"notional": str(o["notional"])} if o["side"] == "buy" else {"qty": str(o["qty"])})
        if dry_run:
            o["status"] = "dry run"
            continue
        try:
            r = api.submit_order(**body)
            o["id"], o["status"] = r["id"], r.get("status", "submitted")
            entry = {"symbol": o["symbol"], "side": o["side"], "session": session, "k": o["k"]}
            entry.update({"notional": o["notional"], "reg": o.get("reg")} if o["side"] == "buy" else {"qty": o["qty"]})
            st["pending"][r["id"]] = entry
            if o["side"] == "sell":
                st["positions"][o["symbol"]].pop("exit_pending", None)
        except APIError as e:
            o["status"] = f"rejected: {e}"
            notes.append(f"{o['side'].title()} order for {o['symbol']} was rejected: {e}")
            if o["side"] == "sell":
                st["positions"][o["symbol"]]["exit_pending"] = True
        order_rows.append({"session": session, "order_id": o.get("id", ""), "symbol": o["symbol"], "side": o["side"],
                           "notional": o.get("notional", ""), "qty": o.get("qty", ""), "status": o["status"], "note": "submitted"})

    # ---- 6. account value at today's close
    pos_value = sum(p["qty"] * series[s][-1]["c"] for s, p in st["positions"].items() if s in series)
    equity = st["cash"] + pos_value
    eq_row = {"session": session, "cash": round(st["cash"], 2), "positions_value": round(pos_value, 2), "equity": round(equity, 2),
              "open_positions": len(st["positions"]), "spy_close": series[mkt][-1]["c"], "fees_paid_total": round(st["fees_paid"], 2),
              "market": reg or "", "trend": round(trend, 5) if trend is not None else "",
              "volatility": round(vol, 5) if vol is not None else "", "strategy": strat_now}
    st["last_session"] = session

    # ---- 7. summary
    start_cap = float(cfg["capital"])
    summary.append(f"## Market-type bot, session {session}{' (dry run, nothing placed or saved)' if dry_run else ''}")
    summary.append(f"Market: **{REGIME_LABEL.get(reg, 'unknown')}** (SPY {trend * 100:+.1f}% vs its 200-day average, volatility {vol * 100:.1f}%). "
                   f"New buys use **{STRAT_LABEL[strat_now]}**" + (f", {money(size)} each." if strat_now != "none" else "."))
    summary.append(f"Account value **{money(equity)}** ({(equity / start_cap - 1) * 100:+.1f}% since {st['started']}), "
                   f"cash {money(st['cash'])}, {len(st['positions'])} open position(s).")
    if paused:
        summary.append(f"\n**Non-trading period: no new buys.** {pause_reason[0].upper() + pause_reason[1:]}.")
    if trade_rows:
        summary.append("\n**Closed today**\n\n| Stock | Strategy | Days | Return | P&L |\n|---|---|---|---|---|")
        summary += [f"| {t['symbol']} | {STRAT_LABEL.get(t['strategy'], t['strategy'])} | {t['days_held']} | {t['return_pct']:+.2f}% | {money(t['pnl'], True)} |" for t in trade_rows]
    if new_orders:
        summary.append("\n**Orders for the next open**\n\n| Stock | Side | Amount | Why | Status |\n|---|---|---|---|---|")
        def amount(o):
            return money(o["notional"]) if o["side"] == "buy" else "%.4f sh" % o["qty"]
        summary += [f"| {o['symbol']} | {o['side']} | {amount(o)} | {STRAT_LABEL.get(o['why'], o['why'])} | {o['status']} |" for o in new_orders]
    else:
        summary.append("\nNo orders for the next open.")
    if st["positions"]:
        summary.append("\n**Holding**\n\n| Stock | Strategy | Since | Value | Result |\n|---|---|---|---|---|")
        for s, p in sorted(st["positions"].items()):
            val = p["qty"] * (series[s][-1]["c"] if s in series else p["entry_price"])
            summary.append(f"| {s} | {STRAT_LABEL.get(p.get('k'), p.get('k'))} | {p['entry_session']} | {money(val)} | {(val / p['cost'] - 1) * 100:+.2f}% |")
    if notes:
        summary.append("\n**Notes**\n" + "\n".join(f"- {n}" for n in notes))
    text = "\n".join(summary)
    log(text)

    if not dry_run:
        save_json(state_path, st)
        log_rows(state_dir, "orders.csv", order_rows)
        log_rows(state_dir, "trades.csv", trade_rows)
        log_rows(state_dir, "signals.csv", signal_rows)
        log_rows(state_dir, "equity.csv", [eq_row])
    return {"session": session, "equity": equity, "orders": new_orders, "trades": trade_rows, "summary": text, "market": reg,
            "paused": paused, "pause_reason": pause_reason}


def main():
    with open(os.path.join(BOT_DIR, "config.json")) as f:
        cfg = json.load(f)
    key, secret = os.environ.get("ALPACA_REGIME_KEY_ID"), os.environ.get("ALPACA_REGIME_SECRET_KEY")
    if not key or not secret:
        print("Market-type bot skipped: add ALPACA_REGIME_KEY_ID and ALPACA_REGIME_SECRET_KEY as repository secrets to start it.")
        out = os.environ.get("GITHUB_STEP_SUMMARY")
        if out:
            with open(out, "a") as f:
                f.write("## Market-type bot\nSkipped: the secrets ALPACA_REGIME_KEY_ID and ALPACA_REGIME_SECRET_KEY are not set yet.\n")
        return
    dry = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
    api = AlpacaAPI(key, secret, feed=cfg.get("data_feed", "sip"))
    res = run(api, cfg, os.path.join(BOT_DIR, "state"), dry_run=dry)
    out = os.environ.get("GITHUB_STEP_SUMMARY")
    if out and res:
        with open(out, "a") as f:
            f.write(res["summary"] + "\n")


if __name__ == "__main__":
    main()
