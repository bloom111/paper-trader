"""
Non-trading periods: days when the market is under stress and neither bot opens new positions.

A day is a non-trading day when, at the close, SPY
  - fell more than `drop5_pct` % over the last 5 trading days, or
  - has a 20-day volatility (per year) above `vol_pct` % that is higher than it was 20 trading days earlier.
Open positions are still sold by their normal rules; only new buys stop.

Backtest behind the defaults (scanner data, 2019-08 to 2024-12, both bots' settings of November 2026):
pausing on these days (about 10% of days) cut the largest drop roughly in half
(market-type bot -36% -> -19%; RSI bot without fees -51% -> -23%) at the cost of some upside
in sharp rebounds such as spring 2020.

Config (in config.json and regime/config.json):
  "pause": {"on": true, "drop5_pct": 4, "vol_pct": 25}
"""
import math

DEFAULT = {"on": True, "drop5_pct": 4, "vol_pct": 25}
MIN_CLOSES = 42   # 20-day volatility today and 20 trading days earlier


def settings(cfg):
    p = dict(DEFAULT)
    p.update(cfg.get("pause") or {})
    return p


def vol20(closes, end):
    """Annualised volatility of the 20 daily log returns ending at index `end` (inclusive)."""
    if end < 20:
        return None
    r = [math.log(closes[j] / closes[j - 1]) for j in range(end - 19, end + 1)]
    m = sum(r) / 20
    return math.sqrt(sum((x - m) ** 2 for x in r) / 19 * 252)


def measures(closes):
    """5-day change, 20-day volatility now and 20 trading days ago, from a list of daily closes (oldest first)."""
    i = len(closes) - 1
    return {
        "drop5": closes[i] / closes[i - 5] - 1 if i >= 5 else None,
        "vol": vol20(closes, i),
        "vol_before": vol20(closes, i - 20),
    }


def check(closes, cfg):
    """(paused, reason, measures). `cfg` is the bot's whole config. Never pauses on missing data."""
    p = settings(cfg)
    m = measures(closes)
    if not p["on"]:
        return False, "", m
    reasons = []
    if m["drop5"] is not None and m["drop5"] < -p["drop5_pct"] / 100:
        reasons.append(f"SPY fell {-m['drop5'] * 100:.1f}% in 5 days (limit {p['drop5_pct']:g}%)")
    if m["vol"] is not None and m["vol_before"] is not None and m["vol"] > p["vol_pct"] / 100 and m["vol"] > m["vol_before"]:
        reasons.append(f"volatility {m['vol'] * 100:.1f}% is above {p['vol_pct']:g}% and rising (was {m['vol_before'] * 100:.1f}%)")
    return bool(reasons), "; ".join(reasons), m


def describe(cfg):
    p = settings(cfg)
    if not p["on"]:
        return "off"
    return f"no new buys when SPY falls more than {p['drop5_pct']:g}% in 5 days, or its volatility is above {p['vol_pct']:g}% and rising"
