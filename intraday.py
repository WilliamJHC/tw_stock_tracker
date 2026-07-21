# -*- coding: utf-8 -*-
"""Intraday day-trading indicator engine. Pure functions only: arrays/dataclass
lists in, JSON-ready dicts out. No network, no file I/O.

Phase 2 of docs/dev_plan_intraday.md section 3.1. Consumes the Quote/Trade/Depth
dataclasses produced by realtime/ (Phase 1) adapters. Callers are responsible
for filtering out TW pre-open call-auction data (08:30-09:00, Quote.is_pretrade
== True) before feeding continuous-session accumulators such as vwap_bands(),
realtime_cvd() and intraday_vp() -- these functions themselves do not inspect
is_pretrade and simply accumulate whatever they are given.
"""
import math

from analysis import volume_profile

# Parameters centralised here (mirrors analysis.py's SCORE_WEIGHTS pattern).
INTRADAY_WEIGHTS = {
    # placeholder for future composite intraday scoring (Phase 3); no
    # function in this module currently combines multiple indicators into
    # one weighted score, so this dict is intentionally empty for now.
}

INTRADAY_THRESHOLDS = {
    # depth_imbalance(): |imbalance_ratio| above this -> bid_heavy/ask_heavy,
    # otherwise "balanced".
    "imbalance_side": 0.2,
    # depth_imbalance(): a price level's volume must exceed this multiple of
    # the same-side average (excluding itself) to be flagged as a "wall".
    "wall_multiple": 3.0,
    # depth_imbalance(): a level whose volume drops by more than this
    # fraction vs. the previous snapshot is flagged as a pulled order.
    "pulled_order_pct": 0.5,
    # volume_anomaly(): ratio above this -> is_anomaly True.
    "volume_anomaly_ratio": 2.0,
}


# --------------------------------------------------------------- VWAP ----

def vwap_bands(prices, vols, ts=None):
    """Volume-weighted average price plus 1-sigma/2-sigma bands.

    prices/vols: same-length sequences (minute bars or ticks). ts is accepted
    for interface symmetry with other functions but unused here.
    Standard deviation is volume-weighted (weighted variance of price around
    VWAP).
    """
    n = min(len(prices), len(vols))
    if n == 0:
        return {"vwap": None, "upper1": None, "lower1": None,
                "upper2": None, "lower2": None, "sample_count": 0}
    total_vol = sum(vols[:n])
    if total_vol <= 0:
        return {"vwap": None, "upper1": None, "lower1": None,
                "upper2": None, "lower2": None, "sample_count": n}
    vwap = sum(prices[i] * vols[i] for i in range(n)) / total_vol
    var = sum(vols[i] * (prices[i] - vwap) ** 2 for i in range(n)) / total_vol
    sigma = math.sqrt(var)
    return {
        "vwap": vwap,
        "upper1": vwap + sigma, "lower1": vwap - sigma,
        "upper2": vwap + 2 * sigma, "lower2": vwap - 2 * sigma,
        "sample_count": n,
    }


# ------------------------------------------------------- opening range ----

def opening_range(prices, ts, session_open, window_minutes=15):
    """Opening-range breakout: high/low of the first `window_minutes` after
    session_open, and whether the latest price has broken out of it.

    prices/ts: same-length sequences, ts are datetime objects. session_open
    is a datetime (TW: 09:00). Caller is responsible for excluding pre-open
    call-auction data -- this function only looks at ts relative to
    session_open and does not know about auction sessions.
    """
    n = min(len(prices), len(ts))
    if n == 0:
        return {"high": None, "low": None, "breakout": None, "window_minutes": window_minutes}
    window_end = session_open + _minutes_delta(window_minutes)
    window_prices = [prices[i] for i in range(n)
                      if session_open <= ts[i] < window_end]
    if not window_prices:
        return {"high": None, "low": None, "breakout": None, "window_minutes": window_minutes}
    high = max(window_prices)
    low = min(window_prices)
    current = prices[n - 1]
    breakout = None
    if current > high:
        breakout = "up"
    elif current < low:
        breakout = "down"
    return {"high": high, "low": low, "breakout": breakout, "window_minutes": window_minutes}


def _minutes_delta(minutes):
    import datetime as dt
    return dt.timedelta(minutes=minutes)


# ------------------------------------------------------ volume profile ----

def intraday_vp(highs, lows, vols, price):
    """Today's Volume Profile. Thin wrapper over analysis.volume_profile()
    with lookback covering the whole supplied series (no truncation)."""
    return volume_profile(highs, lows, vols, price, lookback=len(vols))


# ------------------------------------------------------------- CVD ----

def realtime_cvd(closes, vols, method="tick_rule", sides=None):
    """Cumulative volume delta.

    method="tick_rule" (TW, ESTIMATED level): applies the same tick-rule
    logic as analysis.of_from_minutes() -- a close higher than the previous
    close marks the bar buyer-initiated (+), lower marks it seller-initiated
    (-), unchanged carries the previous sign forward. This is an estimate,
    not a true bid/ask classification; the returned dict sets
    is_estimated=True so callers (UI/alerts) know to label it "估算".

    method="true_side" (crypto, TRUE level): closes/vols are ignored in
    favour of `sides`, a list of "buy"/"sell" strings (e.g. from Binance
    aggTrade's Trade.side, already resolved from the `m` field). Each side
    directly determines the sign of that trade's volume; no estimation is
    involved, so is_estimated=False.
    """
    if method == "true_side":
        n = min(len(sides or []), len(vols))
        cum = []
        acc = 0.0
        for i in range(n):
            delta = vols[i] if sides[i] == "buy" else -vols[i]
            acc += delta
            cum.append(acc)
        return {"cum": cum, "delta_total": acc if n else 0.0, "is_estimated": False}

    # tick_rule
    n = min(len(closes), len(vols))
    cum = []
    acc = 0.0
    sign = 0
    for i in range(n):
        if i > 0:
            if closes[i] > closes[i - 1]:
                sign = 1
            elif closes[i] < closes[i - 1]:
                sign = -1
        acc += sign * vols[i]
        cum.append(acc)
    return {"cum": cum, "delta_total": acc if n else 0.0, "is_estimated": True}


# -------------------------------------------------------- depth/book ----

def depth_imbalance(bid_prices, bid_vols, ask_prices, ask_vols,
                     prev_bid_vols=None, prev_ask_vols=None):
    """Order-book depth imbalance, wall detection, and optional
    pulled-order detection (only run when previous snapshots are supplied).
    """
    bid_total = sum(bid_vols)
    ask_total = sum(ask_vols)
    denom = bid_total + ask_total
    imbalance_ratio = (bid_total - ask_total) / denom if denom else 0.0

    th = INTRADAY_THRESHOLDS["imbalance_side"]
    if imbalance_ratio > th:
        imbalance_side = "bid_heavy"
    elif imbalance_ratio < -th:
        imbalance_side = "ask_heavy"
    else:
        imbalance_side = "balanced"

    walls = []
    walls += _find_walls("bid", bid_prices, bid_vols)
    walls += _find_walls("ask", ask_prices, ask_vols)

    result = {
        "bid_total": bid_total, "ask_total": ask_total,
        "imbalance_ratio": imbalance_ratio, "imbalance_side": imbalance_side,
        "walls": walls,
    }

    if prev_bid_vols is not None or prev_ask_vols is not None:
        pulled = []
        pulled += _find_pulled(bid_prices, bid_vols, prev_bid_vols or [])
        pulled += _find_pulled(ask_prices, ask_vols, prev_ask_vols or [])
        result["pulled_orders"] = pulled

    return result


def _find_walls(side, prices, vols):
    n = len(vols)
    walls = []
    mult = INTRADAY_THRESHOLDS["wall_multiple"]
    for i in range(n):
        others = [vols[j] for j in range(n) if j != i]
        if not others:
            continue
        avg_others = sum(others) / len(others)
        if avg_others > 0 and vols[i] > mult * avg_others:
            walls.append({"side": side, "price": prices[i], "vol": vols[i]})
    return walls


def _find_pulled(prices, vols, prev_vols):
    n = min(len(vols), len(prev_vols))
    pct = INTRADAY_THRESHOLDS["pulled_order_pct"]
    pulled = []
    for i in range(n):
        prev = prev_vols[i]
        if prev > 0 and (prev - vols[i]) / prev > pct:
            pulled.append({"price": prices[i], "prev_vol": prev, "vol": vols[i]})
    return pulled


# ------------------------------------------------------ relative strength ----

def relative_strength(target_prices, benchmark_prices):
    """Relative strength of target vs. benchmark over the supplied window
    (return computed from first to last element of each series)."""
    if len(target_prices) < 2 or len(benchmark_prices) < 2:
        return {"target_return_pct": None, "benchmark_return_pct": None,
                "rs_diff": None, "rs_ratio": None}
    t0, t1 = target_prices[0], target_prices[-1]
    b0, b1 = benchmark_prices[0], benchmark_prices[-1]
    target_return = (t1 - t0) / t0
    benchmark_return = (b1 - b0) / b0
    rs_ratio = (1 + target_return) / (1 + benchmark_return) if (1 + benchmark_return) != 0 else None
    return {
        "target_return_pct": target_return * 100,
        "benchmark_return_pct": benchmark_return * 100,
        "rs_diff": (target_return - benchmark_return) * 100,
        "rs_ratio": rs_ratio,
    }


# ------------------------------------------------------- volume anomaly ----

def volume_anomaly(current_vol_by_slot, historical_vols_by_slot):
    """Compares today's cumulative volume (by time slot, e.g. 5-min buckets)
    against the same-time-slot baseline from `historical_vols_by_slot`
    (list of same-shaped series, one per historical trading day) -- NOT a
    whole-day average, because TW intraday volume is U-shaped.
    """
    current_values = (list(current_vol_by_slot.values())
                       if isinstance(current_vol_by_slot, dict) else list(current_vol_by_slot))
    current_total = sum(current_values)

    if not historical_vols_by_slot:
        return {"current_total": current_total, "baseline_avg": None,
                "ratio": None, "is_anomaly": False}

    day_totals = []
    for day in historical_vols_by_slot:
        values = list(day.values()) if isinstance(day, dict) else list(day)
        day_totals.append(sum(values))
    baseline_avg = sum(day_totals) / len(day_totals) if day_totals else None

    if not baseline_avg:
        return {"current_total": current_total, "baseline_avg": baseline_avg,
                "ratio": None, "is_anomaly": False}

    ratio = current_total / baseline_avg
    is_anomaly = ratio > INTRADAY_THRESHOLDS["volume_anomaly_ratio"]
    return {"current_total": current_total, "baseline_avg": baseline_avg,
            "ratio": ratio, "is_anomaly": is_anomaly}
