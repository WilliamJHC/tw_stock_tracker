# -*- coding: utf-8 -*-
"""Unit tests for optimize.py, all on hand-computed/synthetic fixtures --
no network, no DB. The exact walk-forward numbers docs/https-finlab-
finance-blog-python-get-tai-memoized-lighthouse.md Phase 5 cites
(exit_composite plateau -60~-30, OOS 294.5% vs 153.1%) were measured BEFORE
Phase 1's price-adjustment fix changed the underlying watchlist prices (see
Phase 3's commit message for the same before/after-Phase-1 baseline shift)
-- asserting those exact numbers here would be testing a stale snapshot,
not this module. These tests instead verify the FRAMEWORK's own logic
(convergence, purging, plateau detection, decision rules) on synthetic
data with a known-correct answer."""
import datetime as dt
import random

import backtest as bt
import optimize as opt
import portfolio_backtest as pb


# --------------------------------------------------- purged walk-forward --

def test_purged_walk_forward_splits_anchored_grows_train_and_purges():
    folds = opt.purged_walk_forward_splits(600, n_folds=3, purge_bars=20, mode="anchored")
    assert len(folds) == 3
    for train_range, test_range in folds:
        assert train_range.start == 0  # anchored: train always starts at bar 0
        assert train_range.stop <= test_range.start - 20  # purge gap honored
    # train grows fold over fold
    assert folds[1][0].stop > folds[0][0].stop
    assert folds[2][0].stop > folds[1][0].stop
    # test ranges are contiguous and non-overlapping, covering the tail
    assert folds[0][1].stop == folds[1][1].start
    assert folds[1][1].stop == folds[2][1].start
    assert folds[2][1].stop == 600


def test_purged_walk_forward_splits_rolling_train_is_fixed_size_window():
    folds = opt.purged_walk_forward_splits(600, n_folds=3, purge_bars=20, mode="rolling")
    assert len(folds) == 3
    train_sizes = [train_range.stop - train_range.start for train_range, _ in folds]
    # rolling: each train window is (at most) one block wide, not growing
    # without bound like anchored's.
    assert max(train_sizes) <= 600 // 4 + 1


def test_purged_walk_forward_splits_too_little_history_returns_empty():
    assert opt.purged_walk_forward_splits(50, n_folds=4, purge_bars=120) == []


def test_purged_walk_forward_splits_purge_excludes_bars_a_trade_could_span():
    # A trade opened just before train_end could still be open past
    # test_start if the gap is smaller than purge_bars -- assert the gap
    # itself, not an indirect proxy.
    folds = opt.purged_walk_forward_splits(600, n_folds=3, purge_bars=50, mode="anchored")
    for train_range, test_range in folds:
        assert test_range.start - train_range.stop >= 50


# --------------------------------------------------------- search engine --

def test_coordinate_descent_finds_synthetic_optimum():
    # f(x, y) = -(x-5)^2 - (y-3)^2, maximized at (5, 3).
    def objective(params):
        return -((params["x"] - 5) ** 2) - ((params["y"] - 3) ** 2)

    grid = {"x": list(range(0, 11)), "y": list(range(0, 11))}
    best, trials = opt.coordinate_descent(objective, grid, init={"x": 0, "y": 0})
    assert best == {"x": 5, "y": 3}
    assert len(trials) > 1
    assert all("params" in t and "score" in t for t in trials)


def test_coordinate_descent_none_objective_never_wins():
    def objective(params):
        return None if params["x"] < 5 else float(params["x"])

    grid = {"x": [0, 1, 2, 3, 4, 5, 6]}
    best, trials = opt.coordinate_descent(objective, grid, init={"x": 0})
    assert best == {"x": 6}
    assert any(t["score"] is None for t in trials)  # the None trials were still recorded


def test_coordinate_descent_stops_early_with_no_improvement():
    def objective(params):
        return 0.0  # flat landscape -- first round should find no improvement anywhere

    grid = {"x": [1, 2, 3]}
    best, trials = opt.coordinate_descent(objective, grid, init={"x": 1}, max_rounds=50)
    assert best == {"x": 1}
    assert len(trials) == 1 + len(grid["x"]) - 1  # init + one round's worth, no further rounds


def test_random_restart_escapes_a_local_optimum():
    # Two peaks: a shallow local one near x=1, a taller global one near x=8.
    def objective(params):
        x = params["x"]
        return max(3 - abs(x - 1), 10 - abs(x - 8))

    grid = {"x": list(range(0, 12))}
    rng = random.Random(42)
    best, score, trials = opt.random_restart(objective, grid, n_restarts=8, rng=rng)
    assert best == {"x": 8}
    assert score == 10
    assert len(trials) > len(grid["x"])  # pooled across multiple restarts


# -------------------------------------------------------- plateau report --

def test_detect_plateau_finds_widest_flat_region():
    values = [-60, -50, -40, -30, -20, -10]
    scores = [290, 295, 300, 292, 150, 100]  # flat-ish -60..-30, then a cliff
    plateau = opt.detect_plateau(values, scores, tolerance_frac=0.1)
    assert plateau["range"] == (-60, -30)
    assert plateau["center"] == -45
    assert abs(plateau["width_frac"] - (30 / 50)) < 1e-9


def test_detect_plateau_ignores_none_scores():
    values = [1, 2, 3, 4]
    scores = [None, 10, 10, None]
    plateau = opt.detect_plateau(values, scores)
    assert plateau["range"] == (2, 3)


def test_detect_plateau_insufficient_data_returns_none():
    assert opt.detect_plateau([1], [10]) is None
    assert opt.detect_plateau([], []) is None


# ------------------------------------------------------- decision rules --

def test_evaluate_decision_accepts_when_all_rules_clear():
    plateau = {"width_frac": 0.5}
    decision = opt.evaluate_decision(plateau, pbo_value=0.2, dsr_value=0.99)
    assert decision == {"accept": True, "reasons": []}


def test_evaluate_decision_rejects_narrow_plateau():
    plateau = {"width_frac": 0.05}
    decision = opt.evaluate_decision(plateau, pbo_value=0.1, dsr_value=0.99)
    assert decision["accept"] is False
    assert any("plateau width" in r for r in decision["reasons"])


def test_evaluate_decision_rejects_high_pbo():
    plateau = {"width_frac": 0.5}
    decision = opt.evaluate_decision(plateau, pbo_value=0.6, dsr_value=0.99)
    assert decision["accept"] is False
    assert any("PBO" in r for r in decision["reasons"])


def test_evaluate_decision_rejects_low_dsr():
    plateau = {"width_frac": 0.5}
    decision = opt.evaluate_decision(plateau, pbo_value=0.1, dsr_value=0.5)
    assert decision["accept"] is False
    assert any("DSR" in r for r in decision["reasons"])


def test_evaluate_decision_none_plateau_rejects():
    decision = opt.evaluate_decision(None, pbo_value=None, dsr_value=None)
    assert decision["accept"] is False


# ---------------------------------- resolve_ranking_weights_action (6d) --

def test_resolve_ranking_weights_action_keeps_when_rejected():
    decision = {"accept": False, "reasons": ["PBO too high"]}
    assert opt.resolve_ranking_weights_action(decision, winner="ic_suggested", has_previous=False) == "keep"


def test_resolve_ranking_weights_action_keeps_when_active_wins():
    decision = {"accept": True, "reasons": []}
    assert opt.resolve_ranking_weights_action(decision, winner="active", has_previous=True) == "keep"


def test_resolve_ranking_weights_action_adopts_suggested_when_it_wins():
    decision = {"accept": True, "reasons": []}
    assert opt.resolve_ranking_weights_action(decision, winner="ic_suggested", has_previous=False) == "adopt_suggested"


def test_resolve_ranking_weights_action_rolls_back_when_previous_wins():
    decision = {"accept": True, "reasons": []}
    assert opt.resolve_ranking_weights_action(decision, winner="previous", has_previous=True) == "rollback_to_previous"


def test_resolve_ranking_weights_action_no_rollback_without_a_real_previous_candidate():
    # winner=="previous" should never happen when has_previous is False (it
    # wasn't even in the candidate pool that cycle) -- guard against it
    # anyway rather than trust the caller.
    decision = {"accept": True, "reasons": []}
    assert opt.resolve_ranking_weights_action(decision, winner="previous", has_previous=False) == "keep"


# ------------------------------------------ sweep_param_walk_forward (e2e) --

def _synthetic_bars(n=600, seed=13, p0=60.0):
    rng = random.Random(seed)
    start = dt.date(2015, 1, 1)
    bars = []
    p = p0
    for i in range(n):
        p *= 1 + rng.uniform(-0.025, 0.028)
        d = (start + dt.timedelta(days=i)).isoformat()
        bars.append({"date": d, "o": p, "h": p * 1.01, "l": p * 0.99, "c": p,
                     "v": rng.randint(1000, 5000) * 1000})
    return bars


def test_sweep_param_walk_forward_shape_and_trial_accounting():
    bars = _synthetic_bars()
    candidates = [-60, -40, -20, -10]
    result = opt.sweep_param_walk_forward(
        bars, [], [], [], [], None,
        param_name="exit_composite", candidate_values=candidates,
        n_folds=3, purge_bars=20,
    )
    assert result is not None
    assert set(result) == {
        "param_name", "plateau", "point_best", "fold_argmax", "dsr", "pbo",
        "n_trials", "vs_buy_hold", "decision", "trials",
    }
    usable_folds = len(result["fold_argmax"])
    assert usable_folds > 0
    assert result["n_trials"] == usable_folds * len(candidates)
    assert len(result["trials"]) == result["n_trials"]
    assert all(t["value"] in candidates for t in result["trials"])
    assert all(f in candidates for f in result["fold_argmax"])
    assert set(result["decision"]) == {"accept", "reasons"}
    assert result["vs_buy_hold"]["buy_hold_return_pct"] is not None


def test_sweep_param_walk_forward_insufficient_history_returns_none():
    bars = _synthetic_bars(n=50)
    result = opt.sweep_param_walk_forward(
        bars, [], [], [], [], None,
        param_name="exit_composite", candidate_values=[-40, -10],
        n_folds=4, purge_bars=120,  # default purge_bars, way more than 50 bars can support
    )
    assert result is None


def test_sweep_param_walk_forward_respects_base_params_override():
    bars = _synthetic_bars(seed=7)
    captured = []
    orig_simulate = bt.simulate

    def spy(score_rows, bars_, params=None, bar_index=None):
        captured.append(dict(params or {}))
        return orig_simulate(score_rows, bars_, params=params, bar_index=bar_index)

    import unittest.mock as mock
    with mock.patch("optimize.bt.simulate", side_effect=spy):
        opt.sweep_param_walk_forward(
            bars, [], [], [], [], None,
            param_name="exit_composite", candidate_values=[-40, -10],
            base_params={"max_hold_days": 30}, n_folds=2, purge_bars=20,
        )
    assert captured  # at least one call happened
    assert all(p.get("max_hold_days") == 30 for p in captured)


def test_sweep_param_walk_forward_bounds_simulate_to_each_folds_test_range():
    """A position still open when a fold's score rows run out must be
    force-closed within that fold's own bars, not against the full (many
    years later) `bars` array -- otherwise total_return leaks future price
    action from outside the fold. Regression test: bt.simulate() used to
    receive the full, unsliced `bars` for every fold."""
    bars = _synthetic_bars(n=600, seed=7)
    candidate_values = [-40, -10]
    folds = opt.purged_walk_forward_splits(len(bars), n_folds=3, purge_bars=20, mode="anchored")
    assert len(folds) > 1  # need at least one non-final fold to prove the bound

    captured_lens = []
    orig_simulate = bt.simulate

    def spy(score_rows, bars_, params=None, bar_index=None):
        captured_lens.append(len(bars_))
        return orig_simulate(score_rows, bars_, params=params, bar_index=bar_index)

    import unittest.mock as mock
    with mock.patch("optimize.bt.simulate", side_effect=spy):
        opt.sweep_param_walk_forward(
            bars, [], [], [], [], None,
            param_name="exit_composite", candidate_values=candidate_values,
            n_folds=3, purge_bars=20,
        )

    # Calls are grouped fold-by-fold, len(candidate_values) calls per fold,
    # in fold order -- every call within a fold shares that fold's bars.
    assert len(captured_lens) == len(folds) * len(candidate_values)
    for fold_idx, (_, test_range) in enumerate(folds):
        expected_len = test_range[-1] + 1
        chunk = captured_lens[fold_idx * len(candidate_values):(fold_idx + 1) * len(candidate_values)]
        assert chunk == [expected_len] * len(candidate_values)
    # The non-final folds' bound must fall strictly short of the full
    # dataset -- this is what the bug let leak through.
    assert captured_lens[0] < len(bars)


# ---------------------- purged_calendar_walk_forward_splits / RANKING_WEIGHTS --

def _calendar(n=600, start=dt.date(2020, 1, 1)):
    return [(start + dt.timedelta(days=i)).isoformat() for i in range(n)]


def test_purged_calendar_walk_forward_splits_anchored_and_purge_gap():
    calendar = _calendar(600)
    folds = opt.purged_calendar_walk_forward_splits(calendar, n_folds=3, purge_days=60, mode="anchored")
    assert len(folds) == 3
    for fold in folds:
        assert fold["train"][0] == calendar[0]  # anchored: train always starts at calendar[0]
        train_end = dt.date.fromisoformat(fold["train"][1])
        test_start = dt.date.fromisoformat(fold["test"][0])
        assert (test_start - train_end).days >= 60
    # test windows are non-overlapping and cover the tail
    assert folds[0]["test"][1] < folds[1]["test"][0]
    assert folds[-1]["test"][1] == calendar[-1]


def test_purged_calendar_walk_forward_splits_too_little_calendar_returns_empty():
    assert opt.purged_calendar_walk_forward_splits(_calendar(20), n_folds=4, purge_days=60) == []


def test_slice_universe_by_date_filters_bars_keeps_factor_series():
    universe = {
        "A": {
            "bars": [{"date": d, "c": 100.0, "v": 1000} for d in _calendar(10)],
            "bar_dates": _calendar(10),
            "revenue_series": [{"usable_date": "2020-01-01", "yoy_pct": 5.0}],
            "roe_series": [{"usable_date": "2020-01-01", "roe_pct": 10.0}],
        }
    }
    sliced = opt._slice_universe_by_date(universe, "2020-01-03", "2020-01-05")
    assert sliced["A"]["bar_dates"] == ["2020-01-03", "2020-01-04", "2020-01-05"]
    assert sliced["A"]["revenue_series"] == universe["A"]["revenue_series"]  # untouched
    assert sliced["A"]["roe_series"] == universe["A"]["roe_series"]


def test_slice_universe_by_date_drops_codes_with_no_bars_in_range():
    universe = {"A": {"bars": [{"date": "2020-01-01", "c": 100.0, "v": 1000}],
                      "bar_dates": ["2020-01-01"], "revenue_series": [], "roe_series": []}}
    assert opt._slice_universe_by_date(universe, "2021-01-01", "2021-06-01") == {}


def _ranking_universe(n_codes=6, days=500, seed=3):
    """Codes whose momentum/revenue-YoY/ROE all scale with rank (same
    construction as tests/test_factor_eval.py's fixture) -- gives
    sweep_ranking_weights_walk_forward() a real signal to distinguish a
    momentum-heavy candidate from a fundamentals-heavy one."""
    import finlab_factors as ff
    rng = random.Random(seed)
    universe = {}
    for rank in range(n_codes):
        drift = 0.0015 + rank * 0.0012
        d0 = dt.date(2019, 1, 1)
        bars = []
        price = 60.0
        for i in range(days):
            price *= 1 + drift + rng.uniform(-0.01, 0.01)
            bars.append({"date": (d0 + dt.timedelta(days=i)).isoformat(), "c": price, "v": 800_000})
        revenue_rows = []
        rev, growth = 1_000_000.0, 1.001 + rank * 0.0015
        for m in range(30):
            month_date = (dt.date(2017, 1, 1) + dt.timedelta(days=30 * m)).replace(day=1).isoformat()
            revenue_rows.append({"date": month_date, "revenue": rev})
            rev *= growth
        revenue_series = ff.revenue_yoy_series(revenue_rows)
        for r in revenue_series:
            r["usable_date"] = "2019-01-01"
        roe_pct = 4.0 + rank * 2.5
        financials_rows = []
        q0 = dt.date(2017, 1, 1)
        for q in range(6):
            qd = (q0 + dt.timedelta(days=91 * q)).isoformat()
            financials_rows.append({"date": qd, "net_income": roe_pct / 4 * 1_000_000, "equity": 1_000_000})
        roe_series = ff.roe_series(financials_rows)
        for r in roe_series:
            r["usable_date"] = "2019-01-01"
        universe[f"C{rank}"] = {
            "bars": bars, "bar_dates": [b["date"] for b in bars],
            "revenue_series": revenue_series, "roe_series": roe_series,
        }
    return universe


def test_sweep_ranking_weights_walk_forward_shape_and_trial_accounting():
    universe = _ranking_universe()
    candidates = {
        "current": {"revenue": 0.4, "roe": 0.3, "momentum": 0.3},
        "momentum_only": {"revenue": 0.0, "roe": 0.0, "momentum": 1.0},
    }
    result = opt.sweep_ranking_weights_walk_forward(universe, candidates, n_folds=3, purge_days=60)
    assert result is not None
    assert set(result) == {
        "candidates", "plateau", "point_best", "fold_argmax", "candidate_scores",
        "dsr", "pbo", "n_trials", "vs_buy_hold", "decision", "trials",
    }
    usable_folds = len(result["fold_argmax"])
    assert usable_folds > 0
    assert result["n_trials"] == usable_folds * len(candidates)
    assert len(result["trials"]) == result["n_trials"]
    assert all(t["name"] in candidates for t in result["trials"])
    assert all(f in candidates for f in result["fold_argmax"])
    assert set(result["candidate_scores"]) == set(candidates)
    assert set(result["decision"]) == {"accept", "reasons"}


def test_sweep_ranking_weights_walk_forward_insufficient_history_returns_none():
    universe = _ranking_universe(days=60)
    result = opt.sweep_ranking_weights_walk_forward(
        universe, {"a": {"revenue": 1, "roe": 0, "momentum": 0}}, n_folds=4, purge_days=60,
    )
    assert result is None


def test_sweep_ranking_weights_walk_forward_passes_sim_params_through():
    universe = _ranking_universe(n_codes=4, days=500)
    captured = []
    orig_simulate = pb.simulate

    def spy(universe_data, params=None):
        captured.append(dict(params or {}))
        return orig_simulate(universe_data, params=params)

    import unittest.mock as mock
    with mock.patch("optimize.pb.simulate", side_effect=spy):
        opt.sweep_ranking_weights_walk_forward(
            universe, {"a": {"revenue": 1, "roe": 0, "momentum": 0}},
            n_folds=2, purge_days=60, sim_params={"top_n": 2},
        )
    assert captured
    assert all(p.get("top_n") == 2 for p in captured)
    assert all(p.get("ranking_weights") == {"revenue": 1, "roe": 0, "momentum": 0} for p in captured)
