# -*- coding: utf-8 -*-
"""Strategy-vs-0050 comparison and regime-conditional switch-rule evaluation
(see docs/regime_switch_vs_0050.md and plans/0050-all-time-optimized-wolf.md
for the /grill-me-derived design this implements).

Pure functions over already-computed value curves -- no I/O, no FinMind/
yfinance calls, same split as portfolio_backtest.py/market_regime.py. A
"curve" here is always ascending-by-date [{"date": "YYYY-MM-DD", "value":
float}, ...]. Callers normalize portfolio_backtest.simulate()'s "equity"
curve and a benchmark price/total-return series onto this shared "value"
key before passing them in here -- this module never cares whether a value
is NTD equity or a share price index, only its date-over-date ratio.

This module answers "does a fixed, PRE-DETERMINED switch rule (act on
today's market regime, not on hindsight) actually beat holding 0050
outright, once realistic switch costs are charged" -- see
apply_switch_rule()'s no-lookahead discipline below. full_period_comparison/
rolling_window_comparisons/regime_bucket_comparison are the descriptive
statistics layered on top of that (or on a plain strategy/benchmark curve,
for background context); adoption_verdict() turns three of those into a
single pre-registered pass/fail so the reporting script doesn't get to
eyeball its way to a favorable conclusion after the fact.
"""
import bisect
import calendar as cal
import datetime as dt
import math
import statistics

import metrics as mt


def _as_map(curve):
    return {row["date"]: row["value"] for row in curve}


def common_trading_days(curve_a, curve_b):
    """Sorted date strings present in both curves -- the only days a
    curve-to-curve comparison can honestly use."""
    dates_a = {row["date"] for row in curve_a}
    dates_b = {row["date"] for row in curve_b}
    return sorted(dates_a & dates_b)


def _returns_over_days(value_map, days):
    """{date: simple return vs. the previous entry in `days`} for days[1:].
    Keyed to an explicit shared day sequence (e.g. two curves' common
    trading days) rather than either curve's own internal date list, so a
    date one curve has and the other doesn't can't silently misalign which
    prior value a return gets computed against -- see callers below, which
    both need "same-side" (strategy-vs-strategy, benchmark-vs-benchmark)
    returns lined up against one shared date axis. A date missing from
    `value_map` (curve has a gap on a day the other curve traded) yields no
    return entry for that date rather than a guessed 0.0."""
    out = {}
    for i in range(1, len(days)):
        prev_v = value_map.get(days[i - 1])
        cur_v = value_map.get(days[i])
        if prev_v is None or cur_v is None:
            continue
        out[days[i]] = cur_v / prev_v - 1
    return out


def apply_switch_rule(strategy_curve, benchmark_curve, regime_by_date, hold_strategy_in,
                       switch_cost_pct, start_date):
    """Replays a fixed, no-lookahead switch rule as a real equity curve:
    the regime OBSERVED as of trading day t decides which side earns day
    t+1's return -- never the regime as of the day whose return is being
    realized. This is what makes the rule something you could actually have
    traded, not a rule discovered by looking at how the two sides performed
    and picking whichever fit.

    strategy_curve/benchmark_curve: value curves (see module docstring).
    regime_by_date: {date: "bull"/"bear"/"consolidation"/None} -- pass
    market_regime.effective_regime_series() (DEBOUNCED), not the raw
    series: this is the "what do we act on" operational use, the same
    distinction ADR 0004 decisions 4/5 draw for STRATEGY_PARAMS_BY_REGIME.
    hold_strategy_in: set of regime names that mean "hold the strategy
    side" (e.g. {"bull", "consolidation"} for Rule A) -- every other named
    regime means "hold the benchmark side".
    switch_cost_pct: % of portfolio value charged on any day the held side
    changes (a full round-trip cost, not halved per leg -- see
    scripts/report_vs_benchmark.py's --switch-cost-pct default for how this
    is estimated).
    start_date: first date to replay (inclusive). Days before this are not
    part of the trading-day universe at all -- there is no "before start"
    equity to report, unlike a warm-up period that's included but flat.

    A day whose DECIDING regime (see below) is None carries forward
    whatever side was already held -- market_regime only returns None
    during TAIEX's own MA warmup, and callers are expected to pick
    start_date at or after the first non-None regime day, so this mostly
    matters at exactly `start_date` if regime data is thinner than the
    curves.

    Returns {"equity_curve": [{date, equity}], "n_switches": int,
    "days_in_strategy": int, "days_in_benchmark": int}. equity_curve starts
    at an arbitrary base of 100.0 (index, not NTD) since the two input
    curves are generally in unrelated units (portfolio NTD vs. an ETF
    price) -- only the SHAPE of this curve (via full_period_comparison etc.)
    is meaningful, not its absolute level."""
    days = [d for d in common_trading_days(strategy_curve, benchmark_curve) if d >= start_date]
    if not days:
        return {"equity_curve": [], "n_switches": 0, "days_in_strategy": 0, "days_in_benchmark": 0}

    strat_ret = _returns_over_days(_as_map(strategy_curve), days)
    bench_ret = _returns_over_days(_as_map(benchmark_curve), days)
    cost = switch_cost_pct / 100

    equity = 100.0
    equity_curve = []
    n_switches = 0
    days_in_strategy = 0
    days_in_benchmark = 0
    current_side = None

    for i, date in enumerate(days):
        if i == 0:
            # No prior trading day in this replay to defer to -- seed the
            # initial holding straight from start_date's own regime (per
            # the plan's "起點當天依盤勢決定初始持有，不扣成本") rather than
            # inventing a day-before-start regime that was never observed.
            deciding_regime = regime_by_date.get(date)
            desired_side = "strategy" if deciding_regime in hold_strategy_in else "benchmark"
            current_side = desired_side
            charge_cost = False
        else:
            deciding_regime = regime_by_date.get(days[i - 1])
            if deciding_regime is None:
                desired_side = current_side
            else:
                desired_side = "strategy" if deciding_regime in hold_strategy_in else "benchmark"
            charge_cost = desired_side != current_side
            if charge_cost:
                n_switches += 1
            current_side = desired_side

        if i > 0:
            ret = strat_ret.get(date, 0.0) if current_side == "strategy" else bench_ret.get(date, 0.0)
            equity *= (1 + ret)
            if charge_cost:
                equity *= (1 - cost)

        if current_side == "strategy":
            days_in_strategy += 1
        else:
            days_in_benchmark += 1
        equity_curve.append({"date": date, "equity": equity})

    return {
        "equity_curve": equity_curve,
        "n_switches": n_switches,
        "days_in_strategy": days_in_strategy,
        "days_in_benchmark": days_in_benchmark,
    }


def full_period_comparison(curve_a, curve_b, start=None, end=None):
    """First-vs-last-value comparison of two curves over their common
    trading days (optionally clipped to [start, end], inclusive). Returns
    {} if fewer than 2 common days survive the clip -- there is no return
    to compute from a single point."""
    days = common_trading_days(curve_a, curve_b)
    if start is not None:
        days = [d for d in days if d >= start]
    if end is not None:
        days = [d for d in days if d <= end]
    if len(days) < 2:
        return {}

    a_map, b_map = _as_map(curve_a), _as_map(curve_b)
    a_vals = [a_map[d] for d in days]
    b_vals = [b_map[d] for d in days]
    a_ret = (a_vals[-1] / a_vals[0] - 1) * 100
    b_ret = (b_vals[-1] / b_vals[0] - 1) * 100
    return {
        "start": days[0], "end": days[-1],
        "a_ret_pct": a_ret, "b_ret_pct": b_ret,
        "excess_pct": a_ret - b_ret, "beat": a_ret > b_ret,
        "a_mdd_pct": mt.max_drawdown_pct(a_vals), "b_mdd_pct": mt.max_drawdown_pct(b_vals),
    }


def _add_months(date_str, months):
    """date_str + `months` calendar months, clamping day-of-month down when
    the target month is shorter (e.g. Aug 31 + 6 -> Feb 28/29). Same
    month-arithmetic convention as optimize.train_test_split_by_calendar()."""
    d = dt.date.fromisoformat(date_str)
    total = d.year * 12 + (d.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    day = min(d.day, cal.monthrange(year, month)[1])
    return dt.date(year, month, day).isoformat()


def rolling_window_comparisons(curve_a, curve_b, window_months=(12, 36), start=None, end=None):
    """For each N in `window_months`: one comparison window per calendar
    month present in the common trading days, starting at that month's
    first common trading day and ending at the first common trading day
    on or after (start + N months). A window whose end falls past the
    available data is dropped (each N independently -- a 36-month window
    can run out of room while 12-month windows from the same start dates
    still fit).

    Returns {N: {"windows": [full_period_comparison() result, ...],
    "summary": {...}}}. summary is {} when no window fit; otherwise
    {"n_windows", "n_beat", "win_rate_pct", "excess_median_pct",
    "excess_min": {"excess_pct", "start", "end"}, "excess_max": {...}}."""
    days = common_trading_days(curve_a, curve_b)
    if start is not None:
        days = [d for d in days if d >= start]
    if end is not None:
        days = [d for d in days if d <= end]

    starts = []
    seen_months = set()
    for d in days:
        ym = d[:7]
        if ym not in seen_months:
            seen_months.add(ym)
            starts.append(d)

    out = {}
    for n in window_months:
        windows = []
        for s in starts:
            target = _add_months(s, n)
            idx = bisect.bisect_left(days, target)
            if idx >= len(days):
                continue
            cmp_result = full_period_comparison(curve_a, curve_b, start=s, end=days[idx])
            if cmp_result:
                windows.append(cmp_result)
        if windows:
            excesses = [w["excess_pct"] for w in windows]
            n_beat = sum(1 for w in windows if w["beat"])
            worst = min(windows, key=lambda w: w["excess_pct"])
            best = max(windows, key=lambda w: w["excess_pct"])
            summary = {
                "n_windows": len(windows),
                "n_beat": n_beat,
                "win_rate_pct": n_beat / len(windows) * 100,
                "excess_median_pct": statistics.median(excesses),
                "excess_min": {"excess_pct": worst["excess_pct"], "start": worst["start"], "end": worst["end"]},
                "excess_max": {"excess_pct": best["excess_pct"], "start": best["start"], "end": best["end"]},
            }
        else:
            summary = {}
        out[n] = {"windows": windows, "summary": summary}
    return out


def regime_bucket_comparison(curve_a, curve_b, raw_regime_by_date):
    """Background statistic, NOT a switch-rule evaluation: cumulative and
    annualized return of each curve, bucketed by the RAW (undebounced)
    regime on each day -- same "statistics want raw, operations want
    debounced" split as ADR 0004 decision 5. A day whose regime isn't one
    of the three named buckets (None, or any unrecognized value), or whose
    day-over-day return is undefined (a curve's first common day), is
    skipped rather than folded into "consolidation" -- conflating "no
    data" with "assessed as range-bound" would bias that bucket.

    Returns {"bull"/"bear"/"consolidation": {"n_days", "a_cum_ret_pct",
    "b_cum_ret_pct", "excess_pct", "a_annualized_pct", "b_annualized_pct",
    "excess_annualized_pct"}}, all values None when n_days == 0 for that
    bucket."""
    days = common_trading_days(curve_a, curve_b)
    a_ret = _returns_over_days(_as_map(curve_a), days)
    b_ret = _returns_over_days(_as_map(curve_b), days)
    log_sums = {r: {"n_days": 0, "a_log": 0.0, "b_log": 0.0} for r in ("bull", "bear", "consolidation")}

    for date in days:
        regime = raw_regime_by_date.get(date)
        if regime not in log_sums:
            continue
        ar, br = a_ret.get(date), b_ret.get(date)
        if ar is None or br is None or ar <= -1 or br <= -1:
            continue
        agg = log_sums[regime]
        agg["n_days"] += 1
        agg["a_log"] += math.log(1 + ar)
        agg["b_log"] += math.log(1 + br)

    out = {}
    for regime, agg in log_sums.items():
        n = agg["n_days"]
        if n == 0:
            out[regime] = {
                "n_days": 0, "a_cum_ret_pct": None, "b_cum_ret_pct": None, "excess_pct": None,
                "a_annualized_pct": None, "b_annualized_pct": None, "excess_annualized_pct": None,
            }
            continue
        a_cum = (math.exp(agg["a_log"]) - 1) * 100
        b_cum = (math.exp(agg["b_log"]) - 1) * 100
        a_ann = (math.exp(agg["a_log"] / n * 252) - 1) * 100
        b_ann = (math.exp(agg["b_log"] / n * 252) - 1) * 100
        out[regime] = {
            "n_days": n, "a_cum_ret_pct": a_cum, "b_cum_ret_pct": b_cum, "excess_pct": a_cum - b_cum,
            "a_annualized_pct": a_ann, "b_annualized_pct": b_ann, "excess_annualized_pct": a_ann - b_ann,
        }
    return out


def adoption_verdict(full, windows12, test_full, min_win_rate_pct=60.0):
    """Pre-registered three-part pass/fail (see the plan's "採用門檻"):
    (1) the rule beats 0050 over the whole period, (2) at least
    `min_win_rate_pct` of 12-month rolling windows beat 0050, (3) the
    held-out final test period also beats 0050. All three must pass --
    picking any two would let a caller retroactively justify whichever
    switch rule happened to look better, exactly what a pre-registered
    threshold is meant to prevent.

    full/test_full: full_period_comparison() results (or {} if unavailable
    -- treated as a fail, not skipped). windows12: rolling_window_comparisons()'s
    result for window=12 specifically, i.e. that function's return value
    indexed by 12 (e.g. `rolling_window_comparisons(...)[12]`), not the
    whole {N: ...} dict.

    Returns {"pass": bool, "checks": {"full_period_beats", "rolling_12m_win_rate", "test_period_beats"}}."""
    check_full = bool(full) and full.get("beat", False)
    win_rate = (windows12.get("summary") or {}).get("win_rate_pct")
    check_rolling = win_rate is not None and win_rate >= min_win_rate_pct
    check_test = bool(test_full) and test_full.get("beat", False)
    checks = {
        "full_period_beats": check_full,
        "rolling_12m_win_rate": check_rolling,
        "test_period_beats": check_test,
    }
    return {"pass": check_full and check_rolling and check_test, "checks": checks}
