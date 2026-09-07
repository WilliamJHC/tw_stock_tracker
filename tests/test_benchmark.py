# -*- coding: utf-8 -*-
"""Unit tests for benchmark.py, on hand-built synthetic curves (no network,
no cache) -- see src/benchmark.py's module docstring and
docs/regime_switch_vs_0050.md for what these functions are for."""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import benchmark as bm


def _dates(n, start="2021-01-01"):
    d0 = dt.date.fromisoformat(start)
    return [(d0 + dt.timedelta(days=i)).isoformat() for i in range(n)]


def _curve(dates, values):
    return [{"date": d, "value": v} for d, v in zip(dates, values)]


# --------------------------------------------------------- apply_switch_rule --

def _switch_rule_fixture(n=8):
    """Strategy always +1%/day, benchmark always flat (0%/day). Regime
    alternates bull(0-2)/bear(3-5)/bull(6-7) so a bear->bull->bear round
    trip happens inside the window."""
    dates = _dates(n)
    strategy = _curve(dates, [100 * 1.01 ** i for i in range(n)])
    benchmark = _curve(dates, [100.0] * n)
    regimes = ["bull", "bull", "bull", "bear", "bear", "bear", "bull", "bull"]
    regime_by_date = dict(zip(dates, regimes))
    return dates, strategy, benchmark, regime_by_date


def test_switch_rule_decision_is_deferred_one_day_no_lookahead():
    dates, strategy, benchmark, regime_by_date = _switch_rule_fixture()
    result = bm.apply_switch_rule(strategy, benchmark, regime_by_date,
                                   hold_strategy_in={"bull"}, switch_cost_pct=0.0,
                                   start_date=dates[0])
    curve = {row["date"]: row["equity"] for row in result["equity_curve"]}
    # dates[2] is still bull (raw regime) -> dates[3]'s HOLDING is decided by
    # dates[2]'s regime (bull) -> dates[3] still earns the STRATEGY return,
    # even though dates[3] itself is already classified bear.
    assert curve[dates[3]] > curve[dates[2]] * 1.005
    # dates[3] is the first bear day -> dates[4]'s holding switches to
    # benchmark (flat) -- so dates[4]'s equity barely moves off dates[3]'s.
    ret_day4 = curve[dates[4]] / curve[dates[3]] - 1
    assert abs(ret_day4) < 1e-9


def test_switch_rule_counts_one_cost_per_switch():
    dates, strategy, benchmark, regime_by_date = _switch_rule_fixture()
    no_cost = bm.apply_switch_rule(strategy, benchmark, regime_by_date,
                                    hold_strategy_in={"bull"}, switch_cost_pct=0.0,
                                    start_date=dates[0])
    assert no_cost["n_switches"] == 2  # bull->bear at day4, bear->bull at day7

    with_cost = bm.apply_switch_rule(strategy, benchmark, regime_by_date,
                                      hold_strategy_in={"bull"}, switch_cost_pct=1.0,
                                      start_date=dates[0])
    assert with_cost["n_switches"] == 2
    no_cost_final = no_cost["equity_curve"][-1]["equity"]
    with_cost_final = with_cost["equity_curve"][-1]["equity"]
    # 2 switches * 1% cost should compound to roughly a 2% drag (order of
    # magnitude check, not an exact closed form given the interleaved returns).
    assert with_cost_final < no_cost_final * 0.985


def test_switch_rule_none_regime_carries_forward_prior_holding():
    dates, strategy, benchmark, regime_by_date = _switch_rule_fixture()
    regime_by_date[dates[2]] = None  # the day that would decide dates[3]'s holding
    result = bm.apply_switch_rule(strategy, benchmark, regime_by_date,
                                   hold_strategy_in={"bull"}, switch_cost_pct=0.0,
                                   start_date=dates[0])
    curve = {row["date"]: row["equity"] for row in result["equity_curve"]}
    # dates[3] should still hold whatever dates[2] held (strategy, since
    # dates[1] was bull and nothing switched it) despite the None regime.
    ret_day3 = curve[dates[3]] / curve[dates[2]] - 1
    assert ret_day3 > 0.005


def test_switch_rule_no_cost_charged_on_start_day():
    dates, strategy, benchmark, regime_by_date = _switch_rule_fixture()
    result = bm.apply_switch_rule(strategy, benchmark, regime_by_date,
                                   hold_strategy_in={"bull"}, switch_cost_pct=5.0,
                                   start_date=dates[0])
    assert result["equity_curve"][0]["equity"] == 100.0


# ----------------------------------------------------- full_period_comparison --

def test_full_period_comparison_basic():
    dates = _dates(3)
    a = _curve(dates, [100, 110, 121])
    b = _curve(dates, [100, 105, 110.25])
    result = bm.full_period_comparison(a, b)
    assert abs(result["a_ret_pct"] - 21.0) < 1e-9
    assert abs(result["b_ret_pct"] - 10.25) < 1e-9
    assert abs(result["excess_pct"] - 10.75) < 1e-9
    assert result["beat"] is True


def test_full_period_comparison_too_few_common_days_is_empty():
    dates = _dates(1)
    a = _curve(dates, [100])
    b = _curve(dates, [100])
    assert bm.full_period_comparison(a, b) == {}


# --------------------------------------------------- rolling_window_comparisons --

def test_rolling_window_win_rate_when_strategy_always_ahead():
    n = 800
    dates = _dates(n)
    strategy = _curve(dates, [100 * 1.0004 ** i for i in range(n)])
    benchmark = _curve(dates, [100 * 1.0002 ** i for i in range(n)])
    result = bm.rolling_window_comparisons(strategy, benchmark, window_months=(12,))
    summary = result[12]["summary"]
    assert summary["n_windows"] > 0
    assert summary["win_rate_pct"] == 100.0


def test_rolling_window_36_month_summary_empty_when_data_too_short():
    n = 700  # roughly 23 months of daily data -- not enough for a 36m window
    dates = _dates(n)
    strategy = _curve(dates, [100.0] * n)
    benchmark = _curve(dates, [100.0] * n)
    result = bm.rolling_window_comparisons(strategy, benchmark, window_months=(36,))
    assert result[36]["windows"] == []
    assert result[36]["summary"] == {}


def test_rolling_window_flags_a_losing_window():
    n = 800
    dates = _dates(n)
    strategy_vals, val = [], 100.0
    for i in range(n):
        month_idx = i // 30
        daily = -0.001 if 12 <= month_idx < 15 else 0.0004
        val *= (1 + daily)
        strategy_vals.append(val)
    strategy = _curve(dates, strategy_vals)
    benchmark = _curve(dates, [100 * 1.0002 ** i for i in range(n)])
    result = bm.rolling_window_comparisons(strategy, benchmark, window_months=(12,))
    assert any(not w["beat"] for w in result[12]["windows"])


# ---------------------------------------------------- regime_bucket_comparison --

def test_regime_bucket_comparison_hand_computed():
    dates = _dates(10)
    # value curves: a always +1%/day, b always +0%/day (once a return exists)
    a = _curve(dates, [100 * 1.01 ** i for i in range(10)])
    b = _curve(dates, [100.0] * 10)
    regimes = [None, "bull", "bull", "bear", "bear", "consolidation",
               "consolidation", "bull", None, "bear"]
    regime_by_date = dict(zip(dates, regimes))
    result = bm.regime_bucket_comparison(a, b, regime_by_date)
    # bull days with a defined return: dates[1],[2],[7] -> 3 days
    assert result["bull"]["n_days"] == 3
    assert result["bull"]["b_cum_ret_pct"] == 0.0
    assert result["bull"]["a_cum_ret_pct"] > 0
    # bear days with a defined return: dates[3],[4],[9] -> 3
    assert result["bear"]["n_days"] == 3
    assert result["consolidation"]["n_days"] == 2


def test_regime_bucket_comparison_all_none_is_zero_days():
    dates = _dates(5)
    a = _curve(dates, [100, 101, 102, 103, 104])
    b = _curve(dates, [100, 100, 100, 100, 100])
    result = bm.regime_bucket_comparison(a, b, {d: None for d in dates})
    for regime in ("bull", "bear", "consolidation"):
        assert result[regime]["n_days"] == 0
        assert result[regime]["a_cum_ret_pct"] is None


# ---------------------------------------------------------- adoption_verdict --

def test_adoption_verdict_passes_when_all_three_beat():
    full = {"beat": True}
    windows12 = {"summary": {"win_rate_pct": 75.0}}
    test_full = {"beat": True}
    result = bm.adoption_verdict(full, windows12, test_full, min_win_rate_pct=60.0)
    assert result["pass"] is True
    assert all(result["checks"].values())


def test_adoption_verdict_fails_when_rolling_win_rate_below_threshold():
    full = {"beat": True}
    windows12 = {"summary": {"win_rate_pct": 50.0}}
    test_full = {"beat": True}
    result = bm.adoption_verdict(full, windows12, test_full, min_win_rate_pct=60.0)
    assert result["pass"] is False
    assert result["checks"]["full_period_beats"] is True
    assert result["checks"]["rolling_12m_win_rate"] is False
    assert result["checks"]["test_period_beats"] is True


def test_adoption_verdict_fails_when_test_period_loses():
    full = {"beat": True}
    windows12 = {"summary": {"win_rate_pct": 90.0}}
    test_full = {"beat": False}
    result = bm.adoption_verdict(full, windows12, test_full)
    assert result["pass"] is False
    assert result["checks"]["test_period_beats"] is False
