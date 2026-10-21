# -*- coding: utf-8 -*-
"""EP-TW (episodic-pivot gap) daily-bar proxy event study.

Implements docs/ep_backtest_preregistration_2026-10.md section by section
(the section number is cited next to each rule). Pure functions, stdlib only,
no I/O -- scripts/run_ep_backtest.py does the loading, the lock check and the
printing. Nothing here may change a rule in that document; a rule change means
a new pre-registration.

Statistics (dedup_monthly, cluster_bootstrap_ci, paired_diff_ci, summarize,
evaluate_criteria) are NOT re-implemented: the script calls pullback_backtest's
so the gates are computed exactly as in the pullback study. Nothing in
pullback_strategy.py / pullback_backtest.py is modified.

Bars: ascending [{"date","o","h","l","c","v"}] already back-adjusted onto
today's price scale (pullback_backtest.adjust_cache_bars()). Liquidity uses the
RAW close x volume from the factor-cache bars, so scan functions also take
`raw` (same length and dates as the adjusted bars).
"""
import bisect
import random
import re

import pullback_backtest as bt
import pullback_strategy as ps

SEED = bt.SEED                    # section 2.4: control draw (bootstrap seed is bt.SEED too)
COOLDOWN_BARS = bt.COOLDOWN_BARS  # section 2.3 item 7: same per-code sequencing as the pullback study
COMMON_STOCK_RE = re.compile(r"^[1-9]\d{3}$")   # 4-digit codes: excludes ETFs (00xx) and warrants/bonds
TERMINAL_DAYS = 30                # section 2.1: same delisting rule as run_survivorship_study.terminal_codes()
HOLD_BUCKETS = ((0, 0), (1, 5), (6, 10), (11, 20), (21, 30))

# Section 2.2, frozen. The pre-registration lists every key; tests check that.
EP_PARAMS = {
    "min_turnover": 50_000_000, "avg_window": 20,
    "min_gap_pct": 4.0, "min_volume_mult": 3.0,
    "trigger_ticks": 1, "stop_buffer_ticks": 1,
    "min_stop_pct": 3.0, "max_stop_pct": 8.0,
    "r_targets": (3, 5), "scale_fractions": (1 / 3, 1 / 3), "trail_ema": 9,
    "max_hold_bars": 30,
    "limit_tol_ticks": 1,                       # the 10% daily limit itself is an exchange rule, not a parameter
    "catalyst_min_yoy_pct": 30.0,
    "fee_rate": 0.001425, "fee_discount": 0.22, "tax_rate": 0.003,
}
EPS = 1e-9


def is_common_stock(code):
    return bool(COMMON_STOCK_RE.match(str(code)))


# ------------------------------------------------------- price limits ----

def limit_up_price(prev_close):
    """TWSE limit-up: prev close x 1.1, rounded DOWN onto the tick grid."""
    return ps.round_to_tick(prev_close * 1.10, "down")


def limit_down_price(prev_close):
    """TWSE limit-down: prev close x 0.9, rounded UP onto the tick grid."""
    return ps.round_to_tick(prev_close * 0.90, "up")


def _locked_up(bar, prev_close, p):
    """Bar traded only at (within tolerance of) the limit-up price: low is at
    the limit, so open = high = low = limit. The tolerance absorbs the tick-grid
    mismatch of back-adjusted prices (the limit is computed on the adjusted
    scale, the real one on the raw scale)."""
    up = limit_up_price(prev_close)
    return bar["l"] >= up - p["limit_tol_ticks"] * ps.tw_tick_size(up) - EPS


def _locked_down(bar, prev_close, p):
    """Mirror of _locked_up(): bar traded only at the limit-down price (high at the limit)."""
    dn = limit_down_price(prev_close)
    return bar["h"] <= dn + p["limit_tol_ticks"] * ps.tw_tick_size(dn) + EPS


# ------------------------------------------------------------- signals ----

def revenue_label(series, date, min_yoy_pct=EP_PARAMS["catalyst_min_yoy_pct"]):
    """Section 2.5 grouping label (reported, never graded): "yes" / "no" when
    the latest revenue_series row with usable_date <= date has yoy_pct >= / <
    the threshold, "na" when there is no such row. The factor cache keeps only
    {usable_date, yoy_pct} and usable_date is mostly a fixed-lag proxy, so this
    is a label, not an announcement-date catalyst test."""
    if not series:
        return "na"
    dates = [r["usable_date"] for r in series]
    k = bisect.bisect_right(dates, date) - 1
    if k < 0 or series[k].get("yoy_pct") is None:
        return "na"
    return "yes" if series[k]["yoy_pct"] >= min_yoy_pct else "no"


def _prefix_sums(raw_bars, bars):
    pv, pt = [0.0], [0.0]
    for rb, b in zip(raw_bars, bars):
        pv.append(pv[-1] + (b.get("v") or 0))
        pt.append(pt[-1] + (rb.get("c") or 0) * (rb.get("v") or 0))
    return pv, pt


def scan_code(raw_bars, bars, params=None, revenue=None, start_date=None, end_date=None):
    """Section 2.3 item 1 for one code: every bar k that is a signal day t.
    The 20-bar averages (volume, raw turnover) cover the 20 bars BEFORE t, so
    the event bar's own volume spike cannot qualify the stock for liquidity or
    inflate the volume baseline. Returns [{"date","gap_pct","vol_ratio","revenue_label"}]."""
    p = {**EP_PARAMS, **(params or {})}
    if len(raw_bars) != len(bars):
        raise ValueError("raw and adjusted bars must have the same length")
    w = p["avg_window"]
    pv, pt = _prefix_sums(raw_bars, bars)
    out = []
    for k in range(w, len(bars)):
        b, prev = bars[k], bars[k - 1]
        if start_date and b["date"] < start_date:
            continue
        if end_date and b["date"] > end_date:
            continue
        sum_v, sum_t = pv[k] - pv[k - w], pt[k] - pt[k - w]
        if sum_t <= p["min_turnover"] * w or sum_v <= 0:
            continue
        if (b.get("v") or 0) * w < p["min_volume_mult"] * sum_v - EPS:
            continue
        gap = b["o"] / prev["c"] - 1
        if gap < p["min_gap_pct"] / 100 - EPS:
            continue
        if b["c"] < b["o"]:
            continue
        out.append({"date": b["date"], "gap_pct": gap * 100, "vol_ratio": (b.get("v") or 0) * w / sum_v,
                    "revenue_label": revenue_label(revenue, b["date"], p["catalyst_min_yoy_pct"])})
    return out


def scan_signals(raw, universe, params=None, revenue=None, start_date=None, end_date=None):
    """Section 2.3 item 1 over every code. raw: {code: factor-cache bars};
    universe: {code: adjusted bars} (same length/dates as raw[code]);
    revenue: optional {code: revenue_series}. Only common-stock codes are
    scanned (section 2.1). Returns [{"date","code",...}] sorted by (date, code)."""
    signals = []
    for code in sorted(universe):
        if not is_common_stock(code):
            continue
        for s in scan_code(raw[code], universe[code], params, (revenue or {}).get(code), start_date, end_date):
            signals.append({"code": code, **s})
    return sorted(signals, key=lambda s: (s["date"], s["code"]))


def liquid_pool(raw, days, params=None):
    """Section 2.4: {day: [codes]} -- common stocks that traded on `day` and
    pass the same liquidity rule as the signals (20-bar raw turnover before
    that day > min_turnover). The control group draws from this pool."""
    p = {**EP_PARAMS, **(params or {})}
    w = p["avg_window"]
    days = sorted(set(days))
    out = {d: [] for d in days}
    for code in sorted(raw):
        if not is_common_stock(code):
            continue
        rb = raw[code]
        dates = [b["date"] for b in rb]
        pt = [0.0]
        for b in rb:
            pt.append(pt[-1] + (b.get("c") or 0) * (b.get("v") or 0))
        for d in days:
            k = bisect.bisect_left(dates, d)
            if k >= w and k < len(dates) and dates[k] == d and pt[k] - pt[k - w] > p["min_turnover"] * w:
                out[d].append(code)
    return out


def terminal_codes(universe, days=TERMINAL_DAYS):
    """Codes whose last bar is more than `days` calendar days before the
    universe's last bar date (same rule as run_survivorship_study.terminal_codes):
    treated as delisted, so a trade still open at their last bar is closed at
    that bar's close instead of being dropped as censored."""
    import datetime as dt
    ends = {c: b[-1]["date"] for c, b in universe.items() if b}
    if not ends:
        return set()
    cutoff = (dt.date.fromisoformat(max(ends.values())) - dt.timedelta(days=days)).isoformat()
    return {c for c, d in ends.items() if d < cutoff}


# ------------------------------------------------------------ one trade ----

def _no_fill(reason):
    return {"no_fill": reason}


def simulate_trade(bars, i, mode, params=None, terminal=False, ema9=None):
    """One trade from signal bar i (section 2.3 items 2-6).

    mode "ep":      fill at the trigger (signal-day high + 1 tick) on bar i+1,
                    or at the open when it is already above the trigger;
    mode "control": fill at bar i+1's open (section 2.4).
    Returns a trade dict; {"no_fill": reason} when there is no entry;
    {"censored": True} when the data ends before the exit and `terminal` is
    False. With terminal=True (delisted code) the open remainder is closed at
    the last bar's close, exit_reason "terminal".
    ema9: optional precomputed ps.ema_series(closes, trail_ema) for these bars.

    Conventions where the plan is silent (all conservative or pullback-aligned):
      * the entry bar is checked for the stop first; a stop-out there uses the
        stop price (the open is not a fill price on the entry bar);
      * on a later bar an open already through the stop exits at the OPEN;
      * if the exit bar is locked limit-down the exit slips to the OPEN of the
        first bar that is not locked;
      * EMA9 trailing exit applies only after the first scale-out (3R), as in
        the pullback study; the stop is never moved after a scale-out.
    """
    p = {**EP_PARAMS, **(params or {})}
    j = i + 1
    if i < 1 or j >= len(bars):
        return _no_fill("no_next_bar")
    sig, nxt = bars[i], bars[j]

    # --- entry (section 2.3 item 2, limit rules item 5)
    if _locked_up(nxt, sig["c"], p):
        return _no_fill("locked_limit_up")
    if mode == "ep":
        trigger = ps.round_to_tick(sig["h"] + p["trigger_ticks"] * ps.tw_tick_size(sig["h"]), "up")
        if trigger > limit_up_price(sig["c"]):
            return _no_fill("trigger_above_limit")
        if nxt["o"] >= trigger:
            fill, entry_kind = nxt["o"], "open"
        elif nxt["h"] >= trigger:
            fill, entry_kind = trigger, "trigger"
        else:
            return _no_fill("no_trigger")
    else:
        fill, entry_kind = nxt["o"], "open"

    # --- stop (section 2.3 item 3)
    raw_stop = ps.round_to_tick(sig["l"] - p["stop_buffer_ticks"] * ps.tw_tick_size(sig["l"]), "down")
    if raw_stop >= fill:
        return _no_fill("stop_above_fill")
    raw_stop_pct = (fill - raw_stop) / fill * 100
    stop, widened = raw_stop, False
    if raw_stop_pct < p["min_stop_pct"] - EPS:
        stop, widened = ps.round_to_tick(fill * (1 - p["min_stop_pct"] / 100), "down"), True
    if (fill - stop) / fill * 100 > p["max_stop_pct"] + EPS:
        return _no_fill("stop_too_wide")
    r = fill - stop
    t3 = ps.round_to_tick(fill + p["r_targets"][0] * r, "nearest")
    t5 = ps.round_to_tick(fill + p["r_targets"][1] * r, "nearest")

    if ema9 is None:
        ema9 = ps.ema_series([b["c"] for b in bars], p["trail_ema"])
    f3, f5 = p["scale_fractions"]

    # --- exit loop (section 2.3 items 4-6)
    exits = []                                   # [(fraction, price)]
    sold = 0                                     # scale-outs done: 0, 1 (3R), 2 (5R)
    pending = None                               # None | "stop" | "ema9" | "time": exit at the next tradable open
    deferred = False
    last = len(bars) - 1
    d = j
    while True:
        if d > last:
            if not terminal:
                return {"censored": True}
            exits.append((1.0 - sum(f for f, _ in exits), bars[last]["c"]))
            reason, d = "terminal", last
            break
        b = bars[d]
        entry_bar = d == j
        remaining = 1.0 - sum(f for f, _ in exits)
        if pending:
            if _locked_down(b, bars[d - 1]["c"], p):     # cannot sell a locked limit-down bar
                deferred = True
                d += 1
                continue
            exits.append((remaining, b["o"]))
            reason = pending
            break
        if d - j >= p["max_hold_bars"]:
            pending = "time"
            continue
        if not entry_bar and b["o"] <= stop:             # gap through the stop
            pending = "stop"
            continue
        if b["l"] <= stop:                               # stop first, whatever else the bar did
            exits.append((remaining, stop))
            reason = "stop"
            break
        if sold == 0:
            if b["o"] >= t3:
                exits.append((f3, b["o"]))
                sold = 1
            elif b["h"] >= t3:
                exits.append((f3, t3))
                sold = 1
        if sold == 1 and b["h"] >= t5:
            exits.append((f5, max(t5, b["o"])))
            sold = 2
        if sold >= 1 and ema9[d] is not None and b["c"] < ema9[d]:
            pending = "ema9"
        d += 1

    gross = sum(f * (px - fill) / r for f, px in exits)
    net = gross - bt._cost_rate(p) * fill / r
    return {
        "signal_date": sig["date"], "entry_date": nxt["date"], "exit_date": bars[min(d, last)]["date"],
        "entry_idx": j, "exit_idx": d, "fill": fill, "entry_kind": entry_kind, "stop": stop,
        "stop_widened": widened, "raw_stop_pct": raw_stop_pct, "stop_pct": r / fill * 100,
        "gross_r": gross, "net_r": net, "hold_bars": d - j, "exit_reason": reason,
        "scaled_out": sold, "deferred": deferred,
    }


# ------------------------------------------------- run over all signals ----

def run_trades(universe, signals, mode, params=None, control_days=None, rng_seed=SEED, terminal=()):
    """Section 2.3 items 2-7 over a signal list. Per code: no re-entry before
    exit, and the next entry bar must be > exit bar + COOLDOWN_BARS. Returns
    (trades, stats). mode "control": one random code per day in `control_days`
    ({date: [codes]}) instead of `signals`. `terminal`: codes whose open
    trades are closed at their last close instead of dropped."""
    terminal = set(terminal)
    index = {c: {b["date"]: k for k, b in enumerate(bars)} for c, bars in universe.items()}
    trail = (params or {}).get("trail_ema", EP_PARAMS["trail_ema"])
    emas = {}
    if mode == "control":
        rng = random.Random(rng_seed)
        picks = []
        for day in sorted(control_days):
            pool = sorted(control_days[day])
            if pool:
                picks.append({"date": day, "code": rng.choice(pool)})
        sigs = picks
    else:
        sigs = list(signals)
    sigs = sorted(sigs, key=lambda s: (s["date"], s["code"]))
    blocked = {}                        # code -> last bar index that may NOT start a new entry
    trades = []
    stats = {"signals": len(sigs), "blocked": 0, "censored": 0, "terminal": 0, "no_fill": {}}
    for s in sigs:
        code = s["code"]
        bars = universe[code]
        i = index[code].get(s["date"])
        if i is None:
            continue
        if i + 1 <= blocked.get(code, -1):
            stats["blocked"] += 1
            continue
        if code not in emas:
            emas[code] = ps.ema_series([b["c"] for b in bars], trail)
        t = simulate_trade(bars, i, mode, params, terminal=code in terminal, ema9=emas[code])
        if t.get("no_fill"):
            stats["no_fill"][t["no_fill"]] = stats["no_fill"].get(t["no_fill"], 0) + 1
            continue
        if t.get("censored"):
            stats["censored"] += 1
            continue
        stats["terminal"] += t["exit_reason"] == "terminal"
        t["code"] = code
        t["revenue_label"] = s.get("revenue_label")
        blocked[code] = t["exit_idx"] + COOLDOWN_BARS
        trades.append(t)
    return trades, stats


# ------------------------------------------------------ report helpers ----

def hold_distribution(trades):
    """Reported only (section 4): trade counts per holding-bars bucket and per exit reason."""
    buckets = {f"{lo}" if lo == hi else f"{lo}-{hi}": 0 for lo, hi in HOLD_BUCKETS}
    reasons = {}
    for t in trades:
        for lo, hi in HOLD_BUCKETS:
            if lo <= t["hold_bars"] <= hi:
                buckets[f"{lo}" if lo == hi else f"{lo}-{hi}"] += 1
                break
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
    return {"hold_bars": buckets, "exit_reason": reasons}


def catalyst_groups(trades, n=bt.BOOTSTRAP_N):
    """Reported only (section 4): de-duplicated trades split by revenue label
    ("yes" / "no" / "na"). Groups under MIN_GROUP_FOR_CI report n and mean only."""
    out = {}
    for label in ("yes", "no", "na"):
        ts = [t for t in trades if t.get("revenue_label") == label]
        if len(ts) < bt.MIN_GROUP_FOR_CI:
            out[label] = {"n": len(ts), "mean_net_r": bt._mean([t["net_r"] for t in ts]), "note": "樣本不足"}
            continue
        s = bt.summarize(ts)
        s["ci"] = bt.cluster_bootstrap_ci(ts, n)
        out[label] = s
    return out
