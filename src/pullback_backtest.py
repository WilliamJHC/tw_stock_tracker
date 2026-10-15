# -*- coding: utf-8 -*-
"""Daily-bar proxy backtest for the strong-stock pullback method.

Implements docs/pullback_backtest_preregistration_2026-10.md section by
section (the section number is cited next to each rule). Pure functions,
stdlib only, no I/O -- scripts/run_pullback_backtest.py does the loading,
the lock check and the printing. Nothing here may change a rule in that
document; a rule change means a new pre-registration.

Bars: ascending [{"date","o","h","l","c","v"}] already back-adjusted onto
today's price scale (see adjust_cache_bars()).
"""
import bisect
import random

import analysis as an
import pullback_strategy as ps

SEED = 20261014               # section 2.4 / 4: control draw and bootstrap
BOOTSTRAP_N = 10_000
HISTORY_WINDOW = 500          # trailing bars handed to evaluate_setup()/prefilter: the longest look-back is
                              # 126 (returns) / 120 (levels); EMA50 and Wilder ATR seeds are below 1e-6 weight
                              # by then, so this is numerically the same as the full history but O(1) per day
TOP_CANDIDATES = 120          # section 2.3 step 1
MAX_HOLD_BARS = 60            # section 2.3 step 4
COOLDOWN_BARS = 5             # section 2.3 step 5
SPLIT_DATE = "2020-01-01"     # section 4 item 5
MIN_TRADES = 300
MIN_HALF_TRADES = 100
MIN_MEAN_NET_R = 0.10
MIN_GROUP_FOR_CI = 30         # section 4.1
STOP_BINS = ((None, 1.5), (1.5, 2.5), (2.5, 3.0), (3.0, None))   # section 4.1; right-closed, last open

# Section 2.2, verbatim. main() aborts when ps.PULLBACK_PARAMS differs.
LOCKED_PARAMS = {
    "return_windows": {"1m": 21, "3m": 63, "6m": 126}, "min_return_pct": 30.0, "top_rank_pct": 0.30,
    "min_turnover": 50_000_000, "turnover_window": 20,
    "base_lookback": 60, "min_base_bars": 5, "max_base_depth_pct": 30.0, "pivot_span": 2,
    "contraction_recent_bars": 5, "contraction_ratio_max": 0.85,
    "ema_fast": 9, "ema_mid": 21, "ema_slow": 50, "ema_slope_bars": 5, "ema_flat_tol_pct": -0.5,
    "level_lookback": 120, "max_level_distance_pct": 12.0, "cluster_pct": 1.5, "near_support_pct": 3.0,
    "touch_tolerance_pct": 0.3, "break_tolerance_pct": 1.0, "retest_lookback": 20,
    "stop_buffer_ticks": 1, "stop_atr_mult": 0.5, "max_stop_pct": 3.0,
    "r_targets": (3, 5), "trail_ema": 9,
    "fee_rate": 0.001425, "fee_discount": 1.0, "tax_rate": 0.003,
}


def params_diff(current=None, locked=None):
    """[(key, locked, current)] for every section 2.2 value that differs."""
    current = ps.PULLBACK_PARAMS if current is None else current
    locked = LOCKED_PARAMS if locked is None else locked
    return [(k, v, current.get(k)) for k, v in locked.items() if current.get(k) != v]


# ------------------------------------------------------------ data prep ----

def adjust_cache_bars(raw_bars):
    """Factor-cache bars (c, c_adj, o, h, l, v) -> adjusted OHLCV bars, or
    None when ANY bar lacks o/h/l/c_adj (section 2.1: the whole code is
    excluded, no filling, no interpolation). Factor = c_adj / c per bar
    (latest segment == 1.0), applied to o/h/l/c; volume stays raw."""
    out = []
    for b in raw_bars:
        c, ca = b.get("c"), b.get("c_adj")
        if not c or ca is None or b.get("o") is None or b.get("h") is None or b.get("l") is None:
            return None
        f = ca / c
        out.append({"date": b["date"], "o": b["o"] * f, "h": b["h"] * f, "l": b["l"] * f,
                    "c": ca, "v": b.get("v") or 0})
    return out


class _View:
    """Read-only prefix of a list that quacks like the list itself (len, int
    and slice indexing). strength_screen() only looks at bars[-1], bars[idx-n]
    and a trailing slice, so a view avoids copying 3,500 bars per code per day."""

    __slots__ = ("base", "end")

    def __init__(self, base, end):
        self.base, self.end = base, end

    def __len__(self):
        return self.end

    def __getitem__(self, i):
        if isinstance(i, slice):
            r = range(self.end)[i]
            return self.base[r.start:r.stop:r.step] if r.step > 0 else [self.base[k] for k in r]
        if i < 0:
            i += self.end
        if not 0 <= i < self.end:
            raise IndexError(i)
        return self.base[i]

    def __iter__(self):
        return (self.base[k] for k in range(self.end))


# -------------------------------------------------------------- signals ----

def scan_signals(raw, universe, params=None, start_date=None, end_date=None, progress=None):
    """Section 2.3 step 1 over every trading day t, using only data <= t.

    raw: {code: factor-cache bars} -- what the live scan feeds strength_screen()
      and close_only_prefilter() (raw close x volume for liquidity, c_adj for returns);
    universe: {code: adjusted OHLCV bars} (adjust_cache_bars() output, same
      length and dates as raw[code]) -- what evaluate_setup() gets.
    Returns (signals, strong_by_day):
      signals = [{"date", "code", "retest_first", "strength"}] -- candidates
        whose evaluate_setup() passed and zone_status() is near_support;
      strong_by_day = {date: [codes passing strength_screen]} for the control group.
    """
    p = {**ps.PULLBACK_PARAMS, **(params or {})}
    dates = sorted({b["date"] for bars in universe.values() for b in bars})
    if start_date:
        dates = [d for d in dates if d >= start_date]
    if end_date:
        dates = [d for d in dates if d <= end_date]
    code_dates = {c: [b["date"] for b in bars] for c, bars in universe.items()}
    raw = {c: raw[c] for c in universe}
    signals, strong_by_day = [], {}
    for n_day, t in enumerate(dates):
        views, ends = {}, {}
        for code in universe:
            ds_ = code_dates[code]
            k = bisect.bisect_left(ds_, t)
            if k < len(ds_) and ds_[k] == t:        # must trade on t itself, no stale bars
                views[code] = {"bars": _View(raw[code], k + 1)}
                ends[code] = k + 1
        screen = ps.strength_screen(views, p)
        if not screen:
            continue
        strong_by_day[t] = sorted(screen)
        ranked = sorted(screen, key=lambda c: (-screen[c]["strength"], c))
        kept = []
        for code in ranked:
            e = ends[code]
            if ps.close_only_prefilter(raw[code][max(0, e - HISTORY_WINDOW):e], p):
                kept.append(code)
            if len(kept) >= TOP_CANDIDATES:
                break
        for code in kept:
            e = ends[code]
            window = universe[code][max(0, e - HISTORY_WINDOW):e]
            setup = ps.evaluate_setup(window, p)
            if setup.get("passed") and setup.get("status") == "near_support":
                signals.append({"date": t, "code": code, "strength": screen[code]["strength"],
                                "retest_first": bool((setup.get("retest") or {}).get("first_retest"))})
        if progress and n_day % 100 == 0:
            progress(n_day, len(dates), t, len(signals))
    return signals, strong_by_day


# ------------------------------------------------------------ one trade ----

def _atr_at(bars, i):
    w = bars[max(0, i + 1 - HISTORY_WINDOW):i + 1]
    return an.atr([b["h"] for b in w], [b["l"] for b in w], [b["c"] for b in w])


def _cost_rate(p):
    fee = p["fee_rate"] * p["fee_discount"]
    return 2 * fee + p["tax_rate"]          # 0.00585 with the locked params


def simulate_trade(bars, i, mode, params=None):
    """One trade from signal bar i (section 2.3 steps 2-4, 6, 7).

    mode "pullback": fill at the trigger (signal-day high + 1 tick) on bar i+1;
    mode "control":  fill at bar i+1's open (section 2.4).
    Returns a trade dict, or None when there is no fill; {"censored": True}
    when the data ends before the exit (such trades are dropped and counted).

    Conventions where the document is silent (all conservative):
      * the entry bar itself is checked for stop/target, stop first;
      * on the entry bar a stop-out always uses the stop price (the open is
        not a fill price there);
      * a gap-down only improves on the stop price for the loss: exits at the
        OPEN when the open is already through the stop on a later bar.
    """
    p = {**ps.PULLBACK_PARAMS, **(params or {})}
    j = i + 1
    if j >= len(bars) or i < 15:
        return None
    sig, nxt = bars[i], bars[j]
    if mode == "pullback":
        trigger = ps.round_to_tick(sig["h"] + ps.tw_tick_size(sig["h"]), "up")
        if nxt["o"] >= trigger:
            fill = nxt["o"]
        elif nxt["h"] >= trigger:
            fill = trigger
        else:
            return None
    else:
        fill = nxt["o"]
    atr = _atr_at(bars, i)
    zone_stop = ps.round_to_tick(sig["l"] - ps.tw_tick_size(sig["l"]), "down")
    atr_stop = ps.atr_floor_stop(fill, atr, p)
    stop, basis = zone_stop, "zone"
    if atr_stop is not None and atr_stop < zone_stop:
        stop, basis = atr_stop, "atr"
    if stop >= fill:
        return None
    r = fill - stop
    t3 = ps.round_to_tick(fill + p["r_targets"][0] * r, "nearest")
    t5 = ps.round_to_tick(fill + p["r_targets"][1] * r, "nearest")

    closes = [b["c"] for b in bars]
    ema9 = ps.ema_series(closes, p["trail_ema"])

    exits = []                                   # [(fraction, price)]
    half = False
    pending_ema = False
    last = len(bars) - 1
    d = j
    while True:
        if d > last:
            return {"censored": True}
        b = bars[d]
        held = d - j
        entry_bar = d == j
        remaining = 0.5 if half else 1.0
        if pending_ema:                          # section 2.3 step 4: EMA9 exit at next open
            exits.append((remaining, b["o"]))
            break
        if held >= MAX_HOLD_BARS:                # time stop: open of bar 61
            exits.append((remaining, b["o"]))
            break
        # stop first, whatever else the bar did
        if not entry_bar and b["o"] <= stop:
            exits.append((remaining, b["o"]))
            break
        if b["l"] <= stop:
            exits.append((remaining, stop))
            break
        if not half:
            if b["o"] >= t3:
                exits.append((0.5, b["o"]))
                half = True
                if b["o"] >= t5:
                    exits.append((0.5, b["o"]))
                    break
            elif b["h"] >= t3:
                exits.append((0.5, t3))
                half = True
        if half and len(exits) == 1 and b["h"] >= t5:
            exits.append((0.5, max(t5, b["o"])))
            break
        if half and ema9[d] is not None and b["c"] < ema9[d]:
            pending_ema = True
        d += 1

    gross = sum(f * (px - fill) / r for f, px in exits)
    net = gross - _cost_rate(p) * fill / r
    return {
        "signal_date": sig["date"], "entry_date": nxt["date"], "exit_date": bars[min(d, last)]["date"],
        "entry_idx": j, "exit_idx": d, "fill": fill, "stop": stop, "stop_basis": basis,
        "stop_pct": r / fill * 100, "atr_pct": (atr / fill * 100) if atr else None,
        "gross_r": gross, "net_r": net, "hold_bars": d - j,
        "exit_reason": _exit_reason(exits, stop, pending_ema, d - j >= MAX_HOLD_BARS),
        "half_sold": half,
    }


def _exit_reason(exits, stop, pending_ema, timed_out):
    last_px = exits[-1][1]
    if timed_out and not pending_ema:
        return "time"
    if pending_ema:
        return "ema9"
    if last_px <= stop:
        return "stop"
    return "target"


# ------------------------------------------------- run over all signals ----

def run_trades(universe, signals, mode, params=None, excluded=(), control_days=None, rng_seed=SEED):
    """Section 2.3 steps 2-6 over a signal list. Per code: no re-entry before
    exit, and entry bar must be > exit bar + COOLDOWN_BARS. Returns (trades,
    stats). mode "control": one random strong code per day in `control_days`
    ({date: [codes]}) instead of `signals`."""
    excluded = set(excluded)
    index = {c: {b["date"]: k for k, b in enumerate(bars)} for c, bars in universe.items()}
    if mode == "control":
        rng = random.Random(rng_seed)
        picks = []
        for day in sorted(control_days):
            pool = [c for c in control_days[day] if c not in excluded]
            if pool:
                picks.append({"date": day, "code": rng.choice(pool)})
        sigs = picks
    else:
        sigs = [s for s in signals if s["code"] not in excluded]
    sigs = sorted(sigs, key=lambda s: (s["date"], s["code"]))
    blocked = {}                       # code -> last bar index that may NOT start a new entry
    trades, stats = [], {"signals": len(sigs), "no_fill": 0, "blocked": 0, "censored": 0}
    for s in sigs:
        code = s["code"]
        bars = universe[code]
        i = index[code].get(s["date"])
        if i is None:
            continue
        if i + 1 <= blocked.get(code, -1):
            stats["blocked"] += 1
            continue
        t = simulate_trade(bars, i, mode, params)
        if t is None:
            stats["no_fill"] += 1
            continue
        if t.get("censored"):
            stats["censored"] += 1
            continue
        t["code"] = code
        t["retest_first"] = s.get("retest_first")
        blocked[code] = t["exit_idx"] + COOLDOWN_BARS
        trades.append(t)
    return trades, stats


def dedup_monthly(trades):
    """Section 4 item 1: one trade per code per signal month -- the first by
    signal date."""
    seen, out = set(), []
    for t in sorted(trades, key=lambda t: (t["signal_date"], t["code"])):
        key = (t["code"], t["signal_date"][:7])
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


# ----------------------------------------------------------- statistics ----

def _month_buckets(trades):
    m = {}
    for t in trades:
        m.setdefault(t["signal_date"][:7], []).append(t["net_r"])
    return m


def cluster_bootstrap_ci(trades, n=BOOTSTRAP_N, seed=SEED, field="net_r"):
    """95% CI of the mean of `field`, resampling signal MONTHS with
    replacement (section 4 item 3). Returns (low, high) or None when empty."""
    months = {}
    for t in trades:
        months.setdefault(t["signal_date"][:7], []).append(t[field])
    if not months:
        return None
    sums = [sum(v) for v in months.values()]
    cnts = [len(v) for v in months.values()]
    rng = random.Random(seed)
    k = len(sums)
    means = []
    for _ in range(n):
        idx = [rng.randrange(k) for _ in range(k)]
        c = sum(cnts[x] for x in idx)
        means.append(sum(sums[x] for x in idx) / c)
    means.sort()
    return means[int(0.025 * n)], means[int(0.975 * n) - 1]


def paired_diff_ci(pull, ctrl, n=BOOTSTRAP_N, seed=SEED):
    """Section 4 item 4: per signal month, mean(pullback) - mean(control)
    over months present in both; bootstrap that monthly series. Returns
    (mean_diff, low, high) or None."""
    a, b = _month_buckets(pull), _month_buckets(ctrl)
    diffs = [sum(a[m]) / len(a[m]) - sum(b[m]) / len(b[m]) for m in sorted(a) if m in b]
    if not diffs:
        return None
    rng = random.Random(seed)
    k = len(diffs)
    means = sorted(sum(diffs[rng.randrange(k)] for _ in range(k)) / k for _ in range(n))
    return sum(diffs) / k, means[int(0.025 * n)], means[int(0.975 * n) - 1]


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def summarize(trades):
    """Descriptive numbers (reported, not graded): n, mean net_r, win rate,
    mean hold, longest losing streak (by signal date order)."""
    if not trades:
        return {"n": 0}
    ordered = sorted(trades, key=lambda t: (t["signal_date"], t["code"]))
    streak = best = 0
    for t in ordered:
        streak = streak + 1 if t["net_r"] <= 0 else 0
        best = max(best, streak)
    return {
        "n": len(trades), "mean_net_r": _mean([t["net_r"] for t in trades]),
        "mean_gross_r": _mean([t["gross_r"] for t in trades]),
        "win_rate": sum(1 for t in trades if t["net_r"] > 0) / len(trades),
        "mean_hold_bars": _mean([t["hold_bars"] for t in trades]), "max_losing_streak": best,
    }


def evaluate_criteria(pull, ctrl, n=BOOTSTRAP_N, split=SPLIT_DATE):
    """Section 4: the five criteria on de-duplicated trades. `pull` and `ctrl`
    must already be dedup_monthly() output. Returns a dict with each
    criterion's numbers and "verdict" in {"通過", "不通過", "樣本不足"}."""
    out = {"n": len(pull), "n_control": len(ctrl)}
    out["c1_sample"] = len(pull) >= MIN_TRADES
    mean = _mean([t["net_r"] for t in pull])
    out["mean_net_r"] = mean
    out["c2_mean"] = mean is not None and mean >= MIN_MEAN_NET_R
    ci = cluster_bootstrap_ci(pull, n)
    out["ci"] = ci
    out["c3_ci"] = ci is not None and ci[0] > 0
    ctrl_mean = _mean([t["net_r"] for t in ctrl])
    out["control_mean_net_r"] = ctrl_mean
    diff = paired_diff_ci(pull, ctrl, n)
    out["diff"] = diff
    out["c4_vs_control"] = bool(diff and mean is not None and ctrl_mean is not None
                                and mean > ctrl_mean and diff[1] > 0)
    first = [t for t in pull if t["signal_date"] < split]
    second = [t for t in pull if t["signal_date"] >= split]
    out["half_n"] = (len(first), len(second))
    out["half_mean"] = (_mean([t["net_r"] for t in first]), _mean([t["net_r"] for t in second]))
    out["c5_halves"] = (all(h is not None and h > 0 for h in out["half_mean"]))
    insufficient = (not out["c1_sample"]) or min(out["half_n"]) < MIN_HALF_TRADES
    passed = out["c1_sample"] and out["c2_mean"] and out["c3_ci"] and out["c4_vs_control"] and out["c5_halves"]
    out["verdict"] = "樣本不足" if insufficient else ("通過" if passed else "不通過")
    return out


def stop_distance_groups(trades, n=BOOTSTRAP_N):
    """Section 4.1 (descriptive): groups by stop distance %, boundary belongs
    to the lower group; each group also split by stop basis. `trades` must be
    dedup'd and exclusion-filtered. Groups under MIN_GROUP_FOR_CI report only
    n and mean."""
    def label(lo, hi):
        return f"<{hi:g}%" if lo is None else (f">{lo:g}%" if hi is None else f"{lo:g}-{hi:g}%")

    def group_of(x):
        for lo, hi in STOP_BINS:
            if (lo is None or x > lo) and (hi is None or x <= hi):
                return label(lo, hi)
        return None

    def stats(ts):
        if len(ts) < MIN_GROUP_FOR_CI:
            return {"n": len(ts), "mean_net_r": _mean([t["net_r"] for t in ts]), "note": "樣本不足"}
        s = summarize(ts)
        s["ci"] = cluster_bootstrap_ci(ts, n)
        s["basis_share"] = {b: sum(1 for t in ts if t["stop_basis"] == b) / len(ts) for b in ("zone", "atr")}
        s["mean_atr_pct"] = _mean([t["atr_pct"] for t in ts if t["atr_pct"] is not None])
        return s

    groups = {}
    for t in trades:
        g = group_of(t["stop_pct"])
        groups.setdefault(g, []).append(t)
    out = {}
    for lo, hi in STOP_BINS:
        g = label(lo, hi)
        ts = groups.get(g, [])
        out[g] = {"all": stats(ts),
                  "zone": stats([t for t in ts if t["stop_basis"] == "zone"]),
                  "atr": stats([t for t in ts if t["stop_basis"] == "atr"])}
    return out


def benchmark_return(curve, entry_date, exit_date):
    """0050 total-return over the holding period (reported only). `curve` =
    sorted [(date, value)]; uses the last value on/before each date."""
    dates = [d for d, _ in curve]

    def val(day):
        k = bisect.bisect_right(dates, day) - 1
        return curve[k][1] if k >= 0 else None

    a, b = val(entry_date), val(exit_date)
    return None if not a or b is None else b / a - 1
