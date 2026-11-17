#!/usr/bin/env python3
"""
RSI dip paper trader for an Alpaca paper account.

Runs once per US trading day, after the close:
  1. Book yesterday's orders that filled at this morning's open.
  2. Download daily prices for the S&P 500 (split/dividend adjusted).
  3. Queue sell orders for positions whose RSI closed above the sell level.
  4. Queue buy orders for the most traded stocks whose RSI closed below the buy level.
  5. Save the virtual account and append everything to the CSV logs in ./state.

The script keeps its own virtual budget (start_capital in config.json), so the
paper account's large default balance does not matter. Amounts are US dollars.

Environment:
  ALPACA_KEY_ID, ALPACA_SECRET_KEY   paper-trading API keys (required)
  DRY_RUN=1                          show what would happen; place no orders, save nothing
Only the Python standard library is used.
"""
import csv
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OPEN_STATES = {"new", "accepted", "pending_new", "partially_filled", "held", "accepted_for_bidding",
               "calculated", "pending_replace", "pending_cancel", "done_for_day", "replaced", "stopped", "suspended"}


class APIError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"HTTP {code}: {msg[:300]}")
        self.code = code


# ---------------------------------------------------------------- Alpaca API
class AlpacaAPI:
    def __init__(self, key, secret, feed="sip", paper=True):
        self.trade = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"
        self.data = "https://data.alpaca.markets"
        self.headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"}
        self.feed = feed
        self.notes = []

    def _req(self, method, url, params=None, body=None, retries=4):
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        headers = dict(self.headers)
        if data:
            headers["Content-Type"] = "application/json"
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, data=data, method=method, headers=headers)
                with urllib.request.urlopen(req, timeout=30) as r:
                    txt = r.read().decode()
                    return json.loads(txt) if txt.strip() else None
            except urllib.error.HTTPError as e:
                msg = e.read().decode(errors="replace")
                if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                    time.sleep(3 * 2 ** attempt)
                    continue
                raise APIError(e.code, msg)
            except urllib.error.URLError as e:
                if attempt < retries - 1:
                    time.sleep(3 * 2 ** attempt)
                    continue
                raise APIError(0, str(e))

    def clock(self):
        return self._req("GET", self.trade + "/v2/clock")

    def calendar(self, start, end):
        return self._req("GET", self.trade + "/v2/calendar", {"start": start, "end": end})

    def get_order(self, order_id):
        return self._req("GET", f"{self.trade}/v2/orders/{order_id}")

    def cancel_order(self, order_id):
        try:
            self._req("DELETE", f"{self.trade}/v2/orders/{order_id}")
        except APIError:
            pass

    def submit_order(self, **order):
        return self._req("POST", self.trade + "/v2/orders", body=order)

    def daily_bars(self, symbols, start, end):
        """{symbol: [{date, o, c, v}, ...]} ascending, adjusted for splits and dividends."""
        out = {}
        for i in range(0, len(symbols), 100):
            chunk = symbols[i:i + 100]
            token = None
            while True:
                p = {"symbols": ",".join(chunk), "timeframe": "1Day", "start": start, "end": end,
                     "adjustment": "all", "feed": self.feed, "limit": 10000}
                if token:
                    p["page_token"] = token
                try:
                    r = self._req("GET", self.data + "/v2/stocks/bars", p)
                except APIError as e:
                    if self.feed == "sip" and e.code in (403, 422):
                        self.feed = "iex"   # free plans may not allow recent SIP data
                        self.notes.append("SIP data not allowed on this plan; used IEX data (volumes are IEX-only).")
                        out.clear()
                        return self.daily_bars(symbols, start, end)
                    raise
                for sym, bars in (r.get("bars") or {}).items():
                    out.setdefault(sym, []).extend(
                        {"date": b["t"][:10], "o": b["o"], "c": b["c"], "v": b["v"]} for b in bars)
                token = r.get("next_page_token")
                if not token:
                    break
        for sym in out:
            out[sym].sort(key=lambda b: b["date"])
        return out


# ---------------------------------------------------------------- indicators
def rsi(closes, n):
    """Wilder RSI, same formula as the backtest pages. Returns a list (None during warm-up)."""
    out = [None] * len(closes)
    g = l = 0.0
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        up, dn = max(d, 0.0), max(-d, 0.0)
        if i <= n:
            g += up
            l += dn
            if i == n:
                g /= n
                l /= n
                out[i] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
        else:
            g = (g * (n - 1) + up) / n
            l = (l * (n - 1) + dn) / n
            out[i] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    return out


def sma_last(values, n):
    return sum(values[-n:]) / n if len(values) >= n else None


# ---------------------------------------------------------------- universe
def sp500_symbols(state_dir):
    cache = os.path.join(state_dir, "universe.json")
    try:
        req = urllib.request.Request("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                                     headers={"User-Agent": "Mozilla/5.0 (paper-trader)"})
        html = urllib.request.urlopen(req, timeout=30).read().decode()
        table = re.search(r'<table[^>]*id="constituents".*?</table>', html, re.S).group(0)
        rows = re.findall(r"<tr>\s*<td[^>]*>(.*?)</td>", table, re.S)
        syms = sorted({re.sub(r"<[^>]+>", "", r).strip() for r in rows} - {""})
        if len(syms) < 450:
            raise ValueError(f"only {len(syms)} symbols parsed")
        with open(cache, "w") as f:
            json.dump(syms, f)
        return syms, None
    except Exception as e:  # fall back to the last good list
        if os.path.exists(cache):
            with open(cache) as f:
                return json.load(f), f"Could not refresh the S&P 500 list ({e}); used the saved list."
        raise RuntimeError(f"Could not load the S&P 500 list and no saved copy exists: {e}")


# ---------------------------------------------------------------- state + logs
def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


LOGS = {
    "orders.csv": ["session", "order_id", "symbol", "side", "notional", "qty", "status", "filled_qty", "filled_avg_price", "filled_at", "note"],
    "trades.csv": ["symbol", "entry_session", "exit_session", "days_held", "entry_price", "exit_price", "qty", "cost", "proceeds", "return_pct", "pnl"],
    "signals.csv": ["session", "symbol", "rsi", "close", "volume", "action"],
    "equity.csv": ["session", "cash", "positions_value", "equity", "open_positions", "spy_close", "fees_paid_total"],
}


def append_log(state_dir, name, rows):
    if not rows:
        return
    path = os.path.join(state_dir, name)
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOGS[name], extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def money(x, sign=False):
    return f"{'+' if sign and x >= 0 else '-' if x < 0 else ''}${abs(x):,.2f}"


def parse_ts(ts):
    """Alpaca timestamps carry nanoseconds; Python wants at most microseconds."""
    ts = re.sub(r"(\.\d{6})\d+", r"\1", ts.replace("Z", "+00:00"))
    return datetime.fromisoformat(ts)


# ---------------------------------------------------------------- main logic
def run(api, cfg, state_dir, dry_run=False, symbols=None, log=print):
    os.makedirs(state_dir, exist_ok=True)
    s_cfg = cfg["strategy"]
    fee = float(cfg.get("fee_per_order", 0))
    state_path = os.path.join(state_dir, "state.json")
    st = load_json(state_path, None) or {
        "cash": float(cfg["start_capital"]), "positions": {}, "pending": {}, "last_session": None,
        "started": None, "fees_paid": 0.0}
    notes, summary = [], []

    # ---- which session are we processing?
    now = parse_ts(api.clock()["timestamp"])           # carries the New York offset
    cal = api.calendar((now.date() - timedelta(days=500)).isoformat(), now.date().isoformat())
    sessions = []
    for d in cal:
        close = datetime.fromisoformat(f'{d["date"]}T{d["close"]}').replace(tzinfo=now.tzinfo)
        if now >= close + timedelta(minutes=20):
            sessions.append(d["date"])
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
            api.cancel_order(oid)           # should have filled at this morning's open
            o = api.get_order(oid)
            notes.append(f"{p['symbol']} {p['side']} order was still open after the session and was cancelled.")
        q = float(o.get("filled_qty") or 0)
        px = float(o.get("filled_avg_price") or 0)
        filled_on = (o.get("filled_at") or "")[:10] or session
        sym = p["symbol"]
        if q > 0 and p["side"] == "buy":
            cost = q * px + fee
            st["cash"] -= cost
            st["fees_paid"] += fee
            pos = st["positions"].get(sym)
            if pos:   # should not happen, but merge if it does
                pos["qty"] += q
                pos["cost"] += cost
            else:
                st["positions"][sym] = {"qty": q, "cost": cost, "entry_price": px, "entry_session": session}
        elif q > 0 and p["side"] == "sell":
            pos = st["positions"][sym]
            share = min(1.0, q / pos["qty"])
            cost_part = pos["cost"] * share
            proceeds = q * px - fee
            st["cash"] += proceeds
            st["fees_paid"] += fee
            trade_rows.append({"symbol": sym, "entry_session": pos["entry_session"], "exit_session": session,
                               "days_held": days_between(pos["entry_session"], session), "entry_price": round(pos["entry_price"], 4),
                               "exit_price": round(px, 4), "qty": round(q, 6), "cost": round(cost_part, 2),
                               "proceeds": round(proceeds, 2), "return_pct": round((proceeds / cost_part - 1) * 100, 2),
                               "pnl": round(proceeds - cost_part, 2)})
            pos["qty"] -= q
            pos["cost"] -= cost_part
            if pos["qty"] <= 1e-6:
                del st["positions"][sym]
            else:
                pos["exit_pending"] = True
        elif p["side"] == "sell":
            st["positions"][sym]["exit_pending"] = True   # try again tonight
            notes.append(f"Sell order for {sym} did not fill ({status}); it will be placed again.")
        else:
            notes.append(f"Buy order for {sym} did not fill ({status}).")
        order_rows.append({"session": p["session"], "order_id": oid, "symbol": sym, "side": p["side"],
                           "notional": p.get("notional", ""), "qty": p.get("qty", ""), "status": o.get("status"),
                           "filled_qty": q, "filled_avg_price": px, "filled_at": o.get("filled_at") or "", "note": "booked " + session})
        del st["pending"][oid]

    # ---- 2. prices
    universe_note = None
    if symbols is None:
        if cfg.get("universe", "sp500") == "sp500":
            symbols, universe_note = sp500_symbols(state_dir)
        else:
            symbols = list(cfg["universe"])
    if universe_note:
        notes.append(universe_note)
    need = sorted(set(symbols) | set(st["positions"]) | {"SPY"})
    lookback_days = 330 if cfg.get("trend_filter_200d") else 70
    start = (datetime.fromisoformat(session) - timedelta(days=lookback_days)).date().isoformat()
    end = (datetime.now(timezone.utc) - timedelta(minutes=16)).strftime("%Y-%m-%dT%H:%M:%SZ")
    bars = api.daily_bars(need, start, end)
    notes += getattr(api, "notes", [])

    info = {}
    for sym in need:
        b = [x for x in bars.get(sym, []) if x["date"] <= session]
        if not b or b[-1]["date"] != session:
            continue
        closes = [x["c"] for x in b]
        r = rsi(closes, int(s_cfg["rsi_length"]))
        info[sym] = {"close": closes[-1], "rsi": r[-1], "vol": b[-1]["v"], "sma200": sma_last(closes, 200)}
    fresh = sum(1 for s in symbols if s in info)
    if fresh < 0.8 * len(symbols):
        raise RuntimeError(f"Only {fresh} of {len(symbols)} stocks have a price for {session}. Stopping without trading.")

    # ---- 3. exits
    new_orders, signal_rows = [], []
    est_proceeds = 0.0
    for sym, pos in st["positions"].items():
        i = info.get(sym)
        if i is None:
            notes.append(f"No price for held stock {sym} today; kept it.")
            continue
        held = days_between(pos["entry_session"], session) or 0
        reason = None
        if pos.get("exit_pending"):
            reason = "retry"
        elif i["rsi"] is not None and i["rsi"] > s_cfg["sell_above"]:
            reason = "rsi"
        elif cfg.get("stop_loss_pct", 0) > 0 and i["close"] <= pos["entry_price"] * (1 - cfg["stop_loss_pct"] / 100):
            reason = "stop"
        elif cfg.get("max_days_held", 0) > 0 and held >= cfg["max_days_held"]:
            reason = "max_days"
        if reason:
            new_orders.append({"symbol": sym, "side": "sell", "qty": round(pos["qty"], 9), "why": reason})
            est_proceeds += pos["qty"] * i["close"] - fee
            signal_rows.append({"session": session, "symbol": sym, "rsi": round(i["rsi"] or 0, 2), "close": i["close"],
                                "volume": i["vol"], "action": f"sell ({reason})"})

    # ---- 4. entries
    def entry_ok(sym):
        i = info.get(sym)
        if i is None or i["rsi"] is None or i["rsi"] >= s_cfg["buy_below"]:
            return False
        if cfg.get("trend_filter_200d") and (i["sma200"] is None or i["close"] <= i["sma200"]):
            return False
        return True
    cands = [s for s in symbols if s not in st["positions"] and entry_ok(s)]
    key = (lambda s: info[s]["vol"] * info[s]["close"]) if cfg.get("rank_by") == "value" else (lambda s: info[s]["vol"])
    cands.sort(key=key, reverse=True)
    size = float(cfg["position_size"])
    available = st["cash"] + est_proceeds
    for rank, sym in enumerate(cands):
        i = info[sym]
        if rank >= int(cfg["max_new_per_day"]):
            action = "not ranked high enough"
        elif available >= size + fee:
            new_orders.append({"symbol": sym, "side": "buy", "notional": round(size, 2), "why": "rsi"})
            available -= size + fee
            action = "buy"
        else:
            action = "skipped, not enough cash"
        signal_rows.append({"session": session, "symbol": sym, "rsi": round(i["rsi"], 2), "close": i["close"],
                            "volume": i["vol"], "action": action})

    # ---- 5. place orders
    for o in new_orders:
        body = {"symbol": o["symbol"], "side": o["side"], "type": "market", "time_in_force": "day",
                "client_order_id": f"rsi-{session}-{o['side']}-{o['symbol']}-{int(time.time())}"}
        body.update({"notional": str(o["notional"])} if o["side"] == "buy" else {"qty": str(o["qty"])})
        if dry_run:
            o["status"] = "dry run"
            continue
        try:
            r = api.submit_order(**body)
            o["id"], o["status"] = r["id"], r.get("status", "submitted")
            st["pending"][r["id"]] = {"symbol": o["symbol"], "side": o["side"], "session": session,
                                      **({"notional": o["notional"]} if o["side"] == "buy" else {"qty": o["qty"]})}
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
    pos_value = sum(p["qty"] * info[s]["close"] for s, p in st["positions"].items() if s in info)
    equity = st["cash"] + pos_value
    eq_row = {"session": session, "cash": round(st["cash"], 2), "positions_value": round(pos_value, 2), "equity": round(equity, 2),
              "open_positions": len(st["positions"]), "spy_close": info.get("SPY", {}).get("close", ""),
              "fees_paid_total": round(st["fees_paid"], 2)}
    st["last_session"] = session

    # ---- 7. summary
    start_cap = float(cfg["start_capital"])
    summary.append(f"## Paper trade, session {session}{' (dry run, nothing placed or saved)' if dry_run else ''}")
    summary.append(f"Account value **${equity:,.2f}** ({(equity / start_cap - 1) * 100:+.1f}% since {st['started']}), "
                   f"cash ${st['cash']:,.2f}, {len(st['positions'])} open position(s).")
    if trade_rows:
        summary.append("\n**Closed today**\n\n| Stock | Days | Return | P&L |\n|---|---|---|---|")
        summary += [f"| {t['symbol']} | {t['days_held']} | {t['return_pct']:+.2f}% | {money(t['pnl'], True)} |" for t in trade_rows]
    if new_orders:
        summary.append("\n**Orders for the next open**\n\n| Stock | Side | Amount | Why | Status |\n|---|---|---|---|---|")
        summary += [f"| {o['symbol']} | {o['side']} | {money(o['notional']) if o['side'] == 'buy' else f"{o['qty']:.4f} sh"} | {o['why']} | {o['status']} |" for o in new_orders]
    else:
        summary.append("\nNo orders for the next open.")
    if st["positions"]:
        summary.append("\n**Holding**\n\n| Stock | Since | RSI | Value | Result |\n|---|---|---|---|---|")
        for s, p in sorted(st["positions"].items()):
            i = info.get(s, {})
            val = p["qty"] * i.get("close", p["entry_price"])
            rs = i.get("rsi")
            summary.append(f"| {s} | {p['entry_session']} | {rs:.1f} | ${val:,.2f} | {(val / p['cost'] - 1) * 100:+.2f}% |" if rs is not None
                           else f"| {s} | {p['entry_session']} | – | ${val:,.2f} | {(val / p['cost'] - 1) * 100:+.2f}% |")
    if notes:
        summary.append("\n**Notes**\n" + "\n".join(f"- {n}" for n in notes))
    text = "\n".join(summary)
    log(text)

    if not dry_run:
        save_json(state_path, st)
        append_log(state_dir, "orders.csv", order_rows)
        append_log(state_dir, "trades.csv", trade_rows)
        append_log(state_dir, "signals.csv", signal_rows)
        append_log(state_dir, "equity.csv", [eq_row])
    return {"session": session, "equity": equity, "orders": new_orders, "trades": trade_rows, "summary": text}


def main():
    with open(os.path.join(HERE, "config.json")) as f:
        cfg = json.load(f)
    key, secret = os.environ.get("ALPACA_KEY_ID"), os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        sys.exit("Set ALPACA_KEY_ID and ALPACA_SECRET_KEY (GitHub: Settings → Secrets and variables → Actions).")
    dry = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
    api = AlpacaAPI(key, secret, feed=cfg.get("data_feed", "sip"))
    res = run(api, cfg, os.path.join(HERE, "state"), dry_run=dry)
    out = os.environ.get("GITHUB_STEP_SUMMARY")
    if out and res:
        with open(out, "a") as f:
            f.write(res["summary"] + "\n")


if __name__ == "__main__":
    main()
