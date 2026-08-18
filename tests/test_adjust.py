# -*- coding: utf-8 -*-
"""Unit tests for adjust.py's pure adjustment-factor functions. No network,
no DB -- see scripts/build_adj_factors.py for the offline batch job that
feeds this module real TWSE/Yahoo data (validated by hand-running
`--verify` against the real backtest DB, not unit tests, since it's a
network-touching script outside src/, matching this repo's existing
convention of not unit-testing scripts/*.py)."""
import adjust


def test_build_adj_factor_no_actions_is_all_ones():
    dates = ["2024-01-01", "2024-01-02", "2024-01-03"]
    assert adjust.build_adj_factor(dates, []) == [1.0, 1.0, 1.0]


def test_build_adj_factor_single_split_applies_before_not_on_ex_date():
    dates = ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"]
    actions = [{"date": "2024-01-03", "ratio": 0.25}]
    factors = adjust.build_adj_factor(dates, actions)
    assert factors == [0.25, 0.25, 1.0, 1.0]  # ex-date itself already post-split


def test_build_adj_factor_compounds_multiple_actions_going_backward():
    dates = ["2024-01-01", "2024-06-01", "2024-12-01", "2025-06-01"]
    actions = [
        {"date": "2024-06-01", "ratio": 0.9},
        {"date": "2025-06-01", "ratio": 0.5},
    ]
    factors = adjust.build_adj_factor(dates, actions)
    # 2024-01-01: before both actions -> 0.9 * 0.5
    # 2024-06-01: on the first action's ex-date -> only the second applies
    # 2024-12-01: after the first, before the second -> 0.5
    # 2025-06-01: on the second action's ex-date -> nothing left to apply
    assert factors == [0.9 * 0.5, 0.5, 0.5, 1.0]


def test_build_adj_factor_unsorted_actions_same_result():
    dates = ["2024-01-01", "2024-06-01", "2024-12-01"]
    actions_sorted = [{"date": "2024-06-01", "ratio": 0.8}]
    actions_unsorted = list(reversed(actions_sorted))  # single item, but exercises the sort path
    assert (adjust.build_adj_factor(dates, actions_sorted)
            == adjust.build_adj_factor(dates, actions_unsorted))


def test_build_adj_factor_ignores_action_dated_after_last_bar():
    # TWSE publishes ex-dividend dates in advance -- an action dated past
    # the last fetched bar (e.g. today's session hasn't closed/been
    # fetched yet, but is already on TWSE's table) hasn't happened within
    # the observed price history and must not touch any bar, including the
    # last one (discovered via a full-market sample run against code 3022,
    # see docs/https-finlab-finance-blog-python-get-tai-memoized-
    # lighthouse.md Phase 4).
    dates = ["2024-01-01", "2024-01-02", "2024-01-03"]
    actions = [{"date": "2024-01-04", "ratio": 0.9}]  # one day past the last bar
    factors = adjust.build_adj_factor(dates, actions)
    assert factors == [1.0, 1.0, 1.0]


def test_build_adj_factor_action_exactly_on_last_bar_still_excluded():
    # An action dated exactly on the last bar means that bar already trades
    # post-action (TWSE convention, see the ex-date-itself rule above) --
    # it must not scale the last bar down either.
    dates = ["2024-01-01", "2024-01-02"]
    actions = [{"date": "2024-01-02", "ratio": 0.9}]
    factors = adjust.build_adj_factor(dates, actions)
    assert factors == [0.9, 1.0]


def test_build_adj_factor_future_action_does_not_affect_earlier_ones():
    # A genuine, already-effective action before the last bar must still
    # apply normally even when a not-yet-effective one exists further out.
    dates = ["2024-01-01", "2024-01-02", "2024-01-03"]
    actions = [
        {"date": "2024-01-02", "ratio": 0.8},   # already happened
        {"date": "2024-01-10", "ratio": 0.5},   # scheduled, past the last bar
    ]
    factors = adjust.build_adj_factor(dates, actions)
    assert factors == [0.8, 1.0, 1.0]


def test_build_adj_factor_empty_bar_dates_returns_empty():
    assert adjust.build_adj_factor([], [{"date": "2024-01-01", "ratio": 0.5}]) == []


def test_apply_adjustment_scales_price_fields_not_volume():
    bars = [{"date": "2024-01-01", "o": 100, "h": 110, "l": 90, "c": 105, "v": 5000}]
    out = adjust.apply_adjustment(bars, [0.5])
    assert out[0]["o_adj"] == 50
    assert out[0]["h_adj"] == 55
    assert out[0]["l_adj"] == 45
    assert out[0]["c_adj"] == 52.5
    assert out[0]["v"] == 5000  # untouched -- see module docstring's downstream rule
    assert out[0]["adj_factor"] == 0.5


def test_apply_adjustment_does_not_mutate_input_bars():
    bars = [{"date": "2024-01-01", "o": 100, "h": 110, "l": 90, "c": 105, "v": 5000}]
    adjust.apply_adjustment(bars, [0.5])
    assert "c_adj" not in bars[0]


def test_apply_adjustment_length_mismatch_raises():
    bars = [{"date": "2024-01-01", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]
    try:
        adjust.apply_adjustment(bars, [1.0, 1.0])
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_validate_adj_factor_passes_for_correct_series():
    dates = ["2024-01-01", "2024-01-02", "2024-01-03"]
    actions = [{"date": "2024-01-03", "ratio": 0.5}]
    factors = adjust.build_adj_factor(dates, actions)
    assert adjust.validate_adj_factor(dates, factors, [a["date"] for a in actions]) == []


def test_validate_adj_factor_flags_unexplained_step():
    dates = ["2024-01-01", "2024-01-02", "2024-01-03"]
    factors = [0.5, 0.5, 1.0]  # a step at 2024-01-03 with no corresponding action
    violations = adjust.validate_adj_factor(dates, factors, [])
    assert len(violations) == 1
    assert "2024-01-03" in violations[0]


def test_validate_adj_factor_flags_wrong_final_factor():
    dates = ["2024-01-01", "2024-01-02"]
    factors = [1.0, 0.9]  # last bar must be 1.0
    violations = adjust.validate_adj_factor(dates, factors, ["2024-01-02"])
    assert any("!= 1.0" in v for v in violations)


def test_validate_adj_factor_allows_action_inside_a_bar_gap():
    # No bar exactly on the action's ex-date (e.g. a trading halt spanning
    # it) -- the step is still explained because the action date falls
    # inside (prev_bar_date, this_bar_date].
    dates = ["2024-01-01", "2024-01-05"]  # action on 01-03, bars skip 01-02..01-04
    factors = [0.5, 1.0]
    violations = adjust.validate_adj_factor(dates, factors, ["2024-01-03"])
    assert violations == []


def test_validate_adj_factor_allows_reverse_consolidation_ratio_above_one():
    # A capital reduction (減資) raises the nominal price -- ratio > 1 is
    # legitimate and must not be flagged just for breaking monotonicity
    # (see 2327's real 2016/2017/2022 capital actions, discovered while
    # building this module against the project's own backtest DB).
    dates = ["2024-01-01", "2024-01-02", "2024-01-03"]
    actions = [{"date": "2024-01-02", "ratio": 1.25}]
    factors = adjust.build_adj_factor(dates, actions)
    assert factors == [1.25, 1.0, 1.0]
    assert adjust.validate_adj_factor(dates, factors, [a["date"] for a in actions]) == []


def test_validate_adj_factor_empty_factors_is_noop():
    assert adjust.validate_adj_factor([], [], []) == []
