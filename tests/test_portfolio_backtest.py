# -*- coding: utf-8 -*-
"""Unit tests for portfolio_backtest.py, all on hand-computed fixtures --
no network, no DB."""
import datetime as dt
import unittest.mock as mock

import finlab_factors as ff
import portfolio_backtest as pb


def _entry(base, drift, days=400, volume=500_000):
    d0 = dt.date(2022, 1, 1)
    bars = [
        {"date": (d0 + dt.timedelta(days=i)).isoformat(), "c": max(base + i * drift, 1.0), "v": volume}
        for i in range(days)
    ]
    return {
        "bars": bars,
        "bar_dates": [b["date"] for b in bars],
        "revenue_series": [{"usable_date": "2022-01-01", "yoy_pct": base}],
        "roe_series": [{"usable_date": "2022-01-01", "roe_pct": base}],
    }


def test_simulate_empty_universe_returns_empty_shell():
    result = pb.simulate({})
    assert result == {"equity_curve": [], "trades": [], "summary": {}}


def test_simulate_produces_one_equity_point_per_calendar_day():
    universe = {"A": _entry(100, 0.1, days=100), "B": _entry(80, 0.05, days=100)}
    result = pb.simulate(universe, params={"top_n": 5})
    assert len(result["equity_curve"]) == 100
    assert result["equity_curve"][0]["date"] == "2022-01-01"
    assert result["equity_curve"][-1]["date"] == universe["A"]["bar_dates"][-1]


def test_simulate_picks_best_ranked_names_and_excludes_others():
    # 3 names, top_n=2: worst-ranked name should never appear in trades.
    universe = {
        "A": _entry(100, 0.30),
        "B": _entry(80, 0.20),
        "C": _entry(50, -0.05),
    }
    result = pb.simulate(universe, params={"top_n": 2})
    traded_codes = {t["code"] for t in result["trades"]}
    assert traded_codes == {"A", "B"}


def test_simulate_ranking_weights_override_changes_selection():
    # A: weak revenue/ROE (base=10) but strong momentum; B: strong
    # revenue/ROE (base=100) but weak momentum. Default RANKING_WEIGHTS
    # (revenue+roe=0.7 outweighs momentum=0.3) picks B; a momentum-only
    # override picks A instead -- proves PORTFOLIO_PARAMS["ranking_weights"]
    # actually reaches finlab_factors.composite_scores() (docs/https-
    # finlab-finance-blog-python-get-tai-memoized-lighthouse.md Phase 5,
    # applied to Phase 4's rank-IC-derived weight suggestions).
    universe = {"A": _entry(10, 2.0), "B": _entry(100, 0.01)}
    default_result = pb.simulate(universe, params={"top_n": 1})
    assert {t["code"] for t in default_result["trades"]} == {"B"}

    override_result = pb.simulate(universe, params={
        "top_n": 1, "ranking_weights": {"revenue": 0.0, "roe": 0.0, "momentum": 1.0},
    })
    assert {t["code"] for t in override_result["trades"]} == {"A"}


def test_simulate_equity_grows_when_selected_names_trend_up():
    universe = {"A": _entry(100, 0.5), "B": _entry(80, 0.4)}
    result = pb.simulate(universe, params={"top_n": 2, "initial_capital": 1_000_000})
    assert result["equity_curve"][-1]["equity"] > 1_000_000
    assert result["summary"]["total_return_pct"] > 0
    assert result["summary"]["cagr_pct"] > 0


def test_simulate_ranks_cache_none_matches_default_results():
    # ranks_cache=None (the default) must produce byte-identical results to
    # omitting it -- src/optimize.py's sweep_ranking_weights_walk_forward()
    # is the only caller that passes a real cache; every other caller
    # (this module's own callers, scripts/report_backtest.py) must see
    # unchanged behavior.
    universe = {"A": _entry(10, 2.0, days=200), "B": _entry(100, 0.01, days=200)}
    without_cache = pb.simulate(universe, params={"top_n": 1})
    with_explicit_none = pb.simulate(universe, params={"top_n": 1}, ranks_cache=None)
    assert with_explicit_none == without_cache


def test_simulate_ranks_cache_does_not_change_results_when_reused():
    universe = {"A": _entry(10, 2.0, days=200), "B": _entry(100, 0.01, days=200)}
    without_cache = pb.simulate(universe, params={"top_n": 1})
    cache = {}
    with_cache = pb.simulate(universe, params={"top_n": 1}, ranks_cache=cache)
    assert with_cache["trades"] == without_cache["trades"]
    assert with_cache["equity_curve"] == without_cache["equity_curve"]
    assert cache  # actually got populated


def test_simulate_ranks_cache_avoids_recomputing_factor_ranks():
    # Two simulate() calls sharing one cache dict over the SAME universe
    # must only compute factor_ranks() once per rebalance date total, not
    # once per call -- this is the whole point of threading ranks_cache
    # through (src/optimize.py's sweep_ranking_weights_walk_forward() calls
    # simulate() once per named candidate per fold, all sharing one cache).
    universe = {"A": _entry(100, 0.30, days=90), "B": _entry(80, 0.20, days=90)}
    cache = {}
    with mock.patch("finlab_factors.factor_ranks", wraps=ff.factor_ranks) as spy:
        pb.simulate(universe, params={"top_n": 2}, ranks_cache=cache)
        first_call_count = spy.call_count
        pb.simulate(universe, params={"top_n": 2}, ranks_cache=cache)
        second_call_count = spy.call_count
    assert first_call_count > 0
    assert second_call_count == first_call_count  # second call reused every entry


def test_simulate_ranks_cache_seeded_entry_is_actually_consulted():
    # Stronger than a call-count spy: seed the cache with a deliberately
    # empty ranks dict for the very first rebalance date and confirm
    # simulate() actually used it (no code scoreable that day) instead of
    # silently recomputing the real ranks and ignoring the seeded value.
    universe = {"A": _entry(100, 0.30, days=60), "B": _entry(80, 0.20, days=60)}
    first_date = universe["A"]["bar_dates"][0]
    cache = {first_date: {}}
    result = pb.simulate(universe, params={"top_n": 2}, ranks_cache=cache)
    assert all(t["date"] != first_date for t in result["trades"])


def test_simulate_prices_pnl_off_c_adj_when_present():
    # Raw close never moves (no price-only return); c_adj rises steadily,
    # simulating a total-return series where the gain comes entirely from
    # cash/stock dividends baked into the back-adjustment. If simulate()
    # priced P&L off raw "c" (the pre-fix bug), equity would stay flat.
    d0 = dt.date(2022, 1, 1)
    days = 400
    bars = [
        {
            "date": (d0 + dt.timedelta(days=i)).isoformat(),
            "c": 100.0,
            "c_adj": 100.0 + i * 0.5,
            "v": 500_000,
        }
        for i in range(days)
    ]
    universe = {"A": {
        "bars": bars,
        "bar_dates": [b["date"] for b in bars],
        "revenue_series": [{"usable_date": "2022-01-01", "yoy_pct": 10}],
        "roe_series": [{"usable_date": "2022-01-01", "roe_pct": 10}],
    }}
    result = pb.simulate(universe, params={"top_n": 1, "initial_capital": 1_000_000})
    assert result["summary"]["total_return_pct"] > 10  # c_adj roughly doubles over the window


def test_simulate_falls_back_to_raw_close_when_c_adj_absent():
    # Same shape as the c_adj test above but with no c_adj key at all --
    # must reproduce the pre-fix flat-equity result, not error out.
    universe = {"A": _entry(100, 0.0, days=400)}
    result = pb.simulate(universe, params={"top_n": 1, "initial_capital": 1_000_000})
    assert abs(result["summary"]["total_return_pct"]) < 1


def test_simulate_carries_forward_price_through_a_halt_day():
    # B has no bar on day index 2 (simulated halt/no print). Equity that
    # day must reflect B's last known price, not zero-fill it -- a
    # zero-fill would crash equity by roughly B's whole position value that
    # one day, then phantom-recover once B prints again.
    d0 = dt.date(2022, 1, 1)
    a_dates = [(d0 + dt.timedelta(days=i)).isoformat() for i in range(5)]
    b_dates = [(d0 + dt.timedelta(days=i)).isoformat() for i in (0, 1, 3, 4)]  # skip day 2
    universe = {
        "A": {
            "bars": [{"date": d, "c": 100.0, "v": 500_000} for d in a_dates],
            "bar_dates": a_dates,
            "revenue_series": [{"usable_date": "2022-01-01", "yoy_pct": 100}],
            "roe_series": [{"usable_date": "2022-01-01", "roe_pct": 100}],
        },
        "B": {
            "bars": [{"date": d, "c": 100.0, "v": 500_000} for d in b_dates],
            "bar_dates": b_dates,
            "revenue_series": [{"usable_date": "2022-01-01", "yoy_pct": 100}],
            "roe_series": [{"usable_date": "2022-01-01", "roe_pct": 100}],
        },
    }
    result = pb.simulate(universe, params={"top_n": 2})
    equities = [row["equity"] for row in result["equity_curve"]]
    # both flat at price 100 throughout -- equity should stay ~flat across
    # the halt day, not crash toward roughly half its value.
    assert equities[2] > equities[1] * 0.9


def test_simulate_fee_reduces_return_vs_zero_fee():
    universe = {"A": _entry(100, 0.5), "B": _entry(80, 0.4)}
    no_fee = pb.simulate(universe, params={"top_n": 2, "fee_pct": 0.0})
    with_fee = pb.simulate(universe, params={"top_n": 2, "fee_pct": 2.0})
    assert with_fee["equity_curve"][-1]["equity"] < no_fee["equity_curve"][-1]["equity"]


def test_simulate_passes_extra_factors_through_to_composite_scores():
    # Regression/positive test for PORTFOLIO_PARAMS["extra_factors"] ->
    # composite_scores()'s extra_factors kwarg (src/optimize.py's
    # strategy-logic search relies on this to score ma_cross/vol_price_div
    # candidates through simulate()).
    universe = {"A": _entry(10, 2.0), "B": _entry(100, 0.01)}
    marker = {"fake": lambda bars, idx: 1.0}
    captured = []
    orig_composite_scores = ff.composite_scores

    def spy(universe_data, date, weights=None, ranks=None, extra_factors=None):
        captured.append(extra_factors)
        return orig_composite_scores(universe_data, date, weights=weights, ranks=ranks, extra_factors=extra_factors)

    with mock.patch("portfolio_backtest.ff.composite_scores", side_effect=spy):
        pb.simulate(universe, params={"top_n": 1, "extra_factors": marker})
    assert captured
    assert all(ef is marker for ef in captured)


def test_simulate_default_extra_factors_is_none():
    universe = {"A": _entry(10, 2.0), "B": _entry(100, 0.01)}
    captured = []
    orig_composite_scores = ff.composite_scores

    def spy(universe_data, date, weights=None, ranks=None, extra_factors=None):
        captured.append(extra_factors)
        return orig_composite_scores(universe_data, date, weights=weights, ranks=ranks, extra_factors=extra_factors)

    with mock.patch("portfolio_backtest.ff.composite_scores", side_effect=spy):
        pb.simulate(universe, params={"top_n": 1})
    assert captured
    assert all(ef is None for ef in captured)


# ------------------------------------------ Phase 3 item 5: lot rounding/slippage --

def test_floor_to_lot_hand_computed():
    assert pb._floor_to_lot(2999, 1000) == 2000
    assert pb._floor_to_lot(3000, 1000) == 3000
    assert pb._floor_to_lot(500, 1000) == 0
    assert pb._floor_to_lot(1234.5, 0) == 1234.5  # lot_size falsy -> no rounding
    assert pb._floor_to_lot(1234.5, None) == 1234.5


def test_simulate_lot_rounding_produces_whole_lot_share_counts():
    universe = {"A": _entry(100, 0.5), "B": _entry(80, 0.4)}
    result = pb.simulate(universe, params={"top_n": 2, "lot_size": 1000})
    buys = [t for t in result["trades"] if t["side"] == "buy"]
    assert buys  # at least one buy happened
    for t in buys:
        assert t["shares"] % 1000 == 0


def test_simulate_lot_rounding_disabled_allows_fractional_shares():
    universe = {"A": _entry(100, 0.5), "B": _entry(80, 0.4)}
    result = pb.simulate(universe, params={"top_n": 2, "lot_size": 0, "slippage_pct": 0.0})
    buys = [t for t in result["trades"] if t["side"] == "buy"]
    assert buys
    # with lot rounding off, target_value_each is hit ~exactly -> shares are
    # essentially never an exact multiple of 1000 for these hand-picked prices
    assert any(t["shares"] % 1000 != 0 for t in buys)


def test_simulate_slippage_reduces_return_vs_zero_slippage():
    universe = {"A": _entry(100, 0.5), "B": _entry(80, 0.4)}
    no_slip = pb.simulate(universe, params={"top_n": 2, "slippage_pct": 0.0})
    with_slip = pb.simulate(universe, params={"top_n": 2, "slippage_pct": 5.0})
    assert with_slip["equity_curve"][-1]["equity"] < no_slip["equity_curve"][-1]["equity"]


def test_simulate_slippage_moves_trade_fill_price_away_from_quote():
    universe = {"A": _entry(100, 0.5), "B": _entry(80, 0.4)}
    result = pb.simulate(universe, params={"top_n": 2, "slippage_pct": 1.0, "lot_size": 0})
    buy = next(t for t in result["trades"] if t["side"] == "buy")
    quote = next(b["c"] for b in universe[buy["code"]]["bars"] if b["date"] == buy["date"])
    assert abs(buy["price"] - quote * 1.01) < 1e-9  # buys fill above the quote


# --------------------------------------------------------- _compute_metrics --

def test_compute_metrics_hand_computed_flat_curve():
    curve = [{"date": "2023-01-01", "equity": 1_000_000}, {"date": "2024-01-01", "equity": 1_000_000}]
    summary = pb._compute_metrics(curve, 1_000_000)
    assert summary["total_return_pct"] == 0.0
    assert summary["max_drawdown_pct"] == 0.0
    assert summary["sharpe"] is None  # zero variance -> undefined, not divide-by-zero


def test_compute_metrics_drawdown_hand_computed():
    curve = [
        {"date": "2023-01-01", "equity": 1_000_000},
        {"date": "2023-02-01", "equity": 1_200_000},
        {"date": "2023-03-01", "equity": 900_000},
        {"date": "2023-04-01", "equity": 1_000_000},
    ]
    summary = pb._compute_metrics(curve, 1_000_000)
    assert round(summary["max_drawdown_pct"], 4) == round((900_000 / 1_200_000 - 1) * 100, 4)


def test_compute_metrics_too_short_returns_empty():
    assert pb._compute_metrics([{"date": "2023-01-01", "equity": 1_000_000}], 1_000_000) == {}
