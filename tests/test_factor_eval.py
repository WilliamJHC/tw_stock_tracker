# -*- coding: utf-8 -*-
"""Unit tests for factor_eval.py, all on hand-computed/synthetic fixtures --
no network, no DB."""
import datetime as dt

import factor_eval as fe


def _code_series(code_rank, days=400, base=100.0, volume=500_000):
    """A synthetic single-code universe entry where momentum/revenue-YoY/ROE
    ALL scale with `code_rank` (0-indexed, higher = better on every factor)
    -- daily drift, monthly revenue growth, and quarterly ROE are all tied
    to the same rank so a real rank-IC signal exists to detect. Enough bars
    (400) to clear liquidity_ok's 20-bar window and momentum_pct's 60-day
    lookback with room for a 60-bar forward-return horizon too."""
    drift = 0.002 + code_rank * 0.001  # daily price drift, higher rank -> faster growth
    d0 = dt.date(2022, 1, 1)
    bars = []
    price = base
    for i in range(days):
        price *= 1 + drift
        bars.append({"date": (d0 + dt.timedelta(days=i)).isoformat(), "c": price, "v": volume})

    # 24 months of revenue, YoY growth also scaled by code_rank (higher rank
    # -> bigger YoY jump), usable from day 0 of the price series.
    revenue_rows = []
    rev = 1_000_000.0
    rev_growth = 1.001 + code_rank * 0.002
    for m in range(24):
        month_date = (dt.date(2020, 1, 1) + dt.timedelta(days=30 * m)).replace(day=1).isoformat()
        revenue_rows.append({"date": month_date, "revenue": rev})
        rev *= rev_growth
    import finlab_factors as ff
    revenue_series = ff.revenue_yoy_series(revenue_rows)
    for r in revenue_series:
        r["usable_date"] = "2022-01-01"  # force-available from day 0 for test simplicity

    # 5 quarters of financials -> 2 ROE points via roe_series(), ROE level
    # also scaled by code_rank.
    roe_pct = 5.0 + code_rank * 3.0
    financials_rows = []
    q0 = dt.date(2020, 1, 1)
    for q in range(5):
        qd = (q0 + dt.timedelta(days=91 * q)).isoformat()
        financials_rows.append({"date": qd, "net_income": roe_pct / 4 * 1_000_000, "equity": 1_000_000})
    roe_series = ff.roe_series(financials_rows)
    for r in roe_series:
        r["usable_date"] = "2022-01-01"

    return {
        "bars": bars,
        "bar_dates": [b["date"] for b in bars],
        "revenue_series": revenue_series,
        "roe_series": roe_series,
    }


def _synthetic_universe(n_codes=6, days=400):
    return {f"C{i}": _code_series(i, days=days) for i in range(n_codes)}


# ------------------------------------------------------- private helpers ----

def test_forward_return_hand_computed():
    bars = [{"c": 100.0}, {"c": 110.0}, {"c": 121.0}]
    assert abs(fe._forward_return(bars, 0, 2) - 21.0) < 1e-9
    assert fe._forward_return(bars, 1, 2) is None  # not enough bars ahead
    assert fe._forward_return(bars, 2, 1) is None


def test_raw_factor_values_filters_illiquid_codes():
    universe = _synthetic_universe(n_codes=3, days=400)
    universe["thin"] = {
        "bars": [{"date": b["date"], "c": 100.0, "v": 10} for b in universe["C0"]["bars"]],  # tiny volume
        "bar_dates": universe["C0"]["bar_dates"],
        "revenue_series": universe["C0"]["revenue_series"],
        "roe_series": universe["C0"]["roe_series"],
    }
    raw = fe._raw_factor_values(universe, universe["C0"]["bar_dates"][300])
    assert "thin" not in raw
    assert "C0" in raw and "C1" in raw and "C2" in raw
    assert set(raw["C0"]) == {"momentum", "yoy", "roe", "_idx"}


# ------------------------------------------------------------- rank-IC ----

def test_factor_ic_series_detects_strong_positive_momentum_signal():
    universe = _synthetic_universe(n_codes=6, days=400)
    dates = universe["C0"]["bar_dates"][300:340:10]  # a few dates, all with 60 bars of forward room
    series = fe.factor_ic_series(universe, dates, "momentum", horizon=20)
    ics = [ic for _, ic in series if ic is not None]
    assert ics  # got at least one measurable date
    # every code's future return keeps compounding at its OWN fixed drift
    # rate, and higher-rank codes have both higher historical momentum and
    # higher forward returns by construction -> should be ~perfectly ranked.
    assert all(ic > 0.9 for ic in ics)


def test_evaluate_factors_returns_stats_for_every_factor_and_horizon():
    universe = _synthetic_universe(n_codes=6, days=400)
    dates = universe["C0"]["bar_dates"][300:340:10]
    result = fe.evaluate_factors(universe, dates=dates, horizons=(5, 20))
    assert set(result) == set(fe.FACTOR_KEYS)
    for factor_name, by_horizon in result.items():
        assert set(by_horizon) == {5, 20}
        for stats in by_horizon.values():
            assert set(stats) == {"mean_ic", "t_stat", "icir", "n"}


def test_rebalance_dates_matches_portfolio_backtest_convention():
    import portfolio_backtest as pb
    universe = _synthetic_universe(n_codes=2, days=100)
    calendar = pb._trading_calendar(universe)
    assert fe.rebalance_dates(universe) == pb._rebalance_dates(calendar)


# --------------------------------------------------- evaluate_factors_by_regime --

def test_regime_as_of_picks_latest_entry_on_or_before_date():
    raw = [("2022-01-01", "bull"), ("2022-03-01", "bear"), ("2022-06-01", "consolidation")]
    assert fe._regime_as_of(raw, "2022-01-15") == "bull"  # between entries -> latest <=
    assert fe._regime_as_of(raw, "2022-03-01") == "bear"  # exact match
    assert fe._regime_as_of(raw, "2022-12-31") == "consolidation"  # after last entry
    assert fe._regime_as_of(raw, "2021-01-01") is None  # before first entry


def test_evaluate_factors_by_regime_partitions_dates_and_matches_direct_call():
    universe = _synthetic_universe(n_codes=6, days=400)
    dates = universe["C0"]["bar_dates"][300:340:10]  # 4 rebalance dates
    assert len(dates) == 4
    # First half of `dates` in the "bull" bucket, second half "bear" -- the
    # regime series itself only needs one entry per date since _regime_as_of()
    # does an exact/as-of lookup, not interpolation.
    raw_regime_series = [(dates[0], "bull"), (dates[1], "bull"), (dates[2], "bear"), (dates[3], "bear")]

    result = fe.evaluate_factors_by_regime(universe, raw_regime_series, dates=dates, horizons=(5, 20))

    assert set(result) == set(fe.REGIMES)
    assert result["bull"]["sample_dates"] == 2
    assert result["bear"]["sample_dates"] == 2
    assert result["consolidation"]["sample_dates"] == 0
    assert result["consolidation"]["factors"] == {}

    # Bucketed "bull" factors must equal calling evaluate_factors() directly
    # with just the bull-bucketed dates -- bucketing must not change the
    # per-bucket evaluation logic itself.
    direct = fe.evaluate_factors(universe, dates=dates[:2], horizons=(5, 20))
    assert result["bull"]["factors"] == direct


def test_evaluate_factors_by_regime_excludes_dates_with_no_regime_coverage():
    universe = _synthetic_universe(n_codes=6, days=400)
    dates = universe["C0"]["bar_dates"][300:320:10]  # 2 rebalance dates
    # Regime series starts AFTER both rebalance dates -- neither has any
    # coverage, so every bucket should be empty rather than misattributed.
    raw_regime_series = [(dates[-1] + "z", "bull")]  # lexically after every real date here
    result = fe.evaluate_factors_by_regime(universe, raw_regime_series, dates=dates)
    assert all(v["sample_dates"] == 0 for v in result.values())


# --------------------------------------------------------- suggest_weights --

def test_suggest_weights_normalizes_and_favors_higher_icir():
    result = {
        "revenue": {20: {"icir": 0.2}},
        "roe": {20: {"icir": -0.6}},   # sign shouldn't matter, only magnitude
        "momentum": {20: {"icir": None}},  # excluded (no signal)
    }
    weights = fe.suggest_weights(result, horizon=20)
    assert weights is not None
    assert abs(sum(weights.values()) - 1.0) < 1e-9
    assert "momentum" not in weights
    assert weights["roe"] > weights["revenue"]


def test_suggest_weights_returns_none_when_nothing_clears_bar():
    result = {"revenue": {20: {"icir": None}}, "roe": {20: {"icir": None}}, "momentum": {20: {"icir": None}}}
    assert fe.suggest_weights(result, horizon=20) is None


def test_suggest_weights_min_icir_filter():
    result = {"revenue": {20: {"icir": 0.05}}, "roe": {20: {"icir": 0.8}}, "momentum": {20: {"icir": None}}}
    weights = fe.suggest_weights(result, horizon=20, min_icir=0.1)
    assert weights == {"roe": 1.0}


# --------------------------------------------------- evaluate_candidate_factor --

def test_evaluate_candidate_factor_detects_strong_signal():
    universe = _synthetic_universe(n_codes=6, days=400)
    for code, d in universe.items():
        code_rank = int(code[1:])
        d["value_series"] = [{"usable_date": "2022-01-01", "value_score": code_rank * 10.0}]
    dates = universe["C0"]["bar_dates"][300:340:10]
    result = fe.evaluate_candidate_factor(universe, "value_series", "value_score", dates=dates, horizons=(5, 20))
    assert set(result) == {5, 20}
    for stats in result.values():
        assert set(stats) == {"mean_ic", "t_stat", "icir", "n"}
    ics_20 = [ic for ic in [result[20]["mean_ic"]] if ic is not None]
    assert ics_20 and ics_20[0] > 0.9  # same construction as momentum's strong-signal test above


def test_evaluate_candidate_factor_skips_codes_without_the_series():
    universe = _synthetic_universe(n_codes=3, days=400)
    universe["C0"]["value_series"] = [{"usable_date": "2022-01-01", "value_score": 1.0}]
    # C1/C2 never got a "value_series" key at all -- e.g. cache entries
    # fetched before Phase 6b existed.
    dates = universe["C0"]["bar_dates"][300:310]
    result = fe.evaluate_candidate_factor(universe, "value_series", "value_score", dates=dates, horizons=(20,))
    # A single-code cross-section has no spread to rank-IC on, so every date
    # comes back None -- the point of this test is that it doesn't crash on
    # C1/C2's missing key, not that it finds a signal.
    assert result[20]["mean_ic"] is None
