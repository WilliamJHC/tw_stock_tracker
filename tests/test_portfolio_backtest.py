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


# ---------------------------------------------------------- invest_pct --

def test_resolve_invest_pct_layering():
    assert pb._resolve_invest_pct(None, None) == pb.PORTFOLIO_PARAMS["invest_pct"]

    candidate = {"bear": {"invest_pct": 40}}
    assert pb._resolve_invest_pct(None, "bear", by_regime=candidate) == 40
    assert pb._resolve_invest_pct(None, "bull", by_regime=candidate) == pb.PORTFOLIO_PARAMS["invest_pct"]
    # explicit override wins over the regime-conditional candidate
    assert pb._resolve_invest_pct({"invest_pct": 10}, "bear", by_regime=candidate) == 10

    # by_regime omitted (None) falls back to the module-level table, which
    # ships empty -- so any regime name resolves to the global default.
    assert pb._resolve_invest_pct(None, "bear") == pb.PORTFOLIO_PARAMS["invest_pct"]


def test_resolve_invest_pct_by_regime_layers_on_top_of_module_table(monkeypatch):
    # by_regime overriding invest_pct for a regime must not lose whatever
    # else PORTFOLIO_PARAMS_BY_REGIME already carries for that SAME key on
    # that regime -- layers on top of it instead of replacing the table
    # wholesale (mirrors backtest._resolve_params()'s by_regime contract).
    monkeypatch.setitem(pb.PORTFOLIO_PARAMS_BY_REGIME, "bear", {"invest_pct": 70})
    candidate = {"bear": {"invest_pct": 40}}
    assert pb._resolve_invest_pct(None, "bear", by_regime=candidate) == 40  # by_regime wins over the module table
    assert pb._resolve_invest_pct(None, "bear") == 70  # by_regime omitted -> module table alone


def test_simulate_invest_pct_reduces_deployed_capital():
    universe = {"A": _entry(100, 0.5), "B": _entry(80, 0.4)}
    full = pb.simulate(universe, params={"top_n": 2, "initial_capital": 1_000_000})
    half = pb.simulate(universe, params={"top_n": 2, "initial_capital": 1_000_000, "invest_pct": 50})
    # Half the capital deployed into the same trending-up names -> less
    # profit than fully invested, but still positive (the deployed half
    # still participates in the up-move; the rest just sits idle as cash).
    assert 0 < half["summary"]["total_return_pct"] < full["summary"]["total_return_pct"]


def test_simulate_regime_by_date_resolves_invest_pct_per_rebalance():
    universe = {"A": _entry(100, 0.5), "B": _entry(80, 0.4)}
    regime_by_date = {d: "bear" for d in universe["A"]["bar_dates"]}
    candidate = {"bear": {"invest_pct": 50}}

    regime_result = pb.simulate(universe, params={"top_n": 2}, regime_by_date=regime_by_date, by_regime=candidate)
    flat_50_result = pb.simulate(universe, params={"top_n": 2, "invest_pct": 50})
    # Every date resolves to "bear" -> byte-identical to a flat invest_pct=50
    # override for the whole run, proving the regime lookup actually reaches
    # target_value_each's sizing.
    assert regime_result["summary"] == flat_50_result["summary"]


def test_simulate_explicit_invest_pct_wins_over_by_regime():
    universe = {"A": _entry(100, 0.5), "B": _entry(80, 0.4)}
    regime_by_date = {d: "bear" for d in universe["A"]["bar_dates"]}
    candidate = {"bear": {"invest_pct": 50}}

    out = pb.simulate(universe, params={"top_n": 2, "invest_pct": 80},
                       regime_by_date=regime_by_date, by_regime=candidate)
    flat_80 = pb.simulate(universe, params={"top_n": 2, "invest_pct": 80})
    assert out["summary"] == flat_80["summary"]


def test_simulate_without_regime_by_date_unchanged():
    universe = {"A": _entry(100, 0.5), "B": _entry(80, 0.4)}
    baseline = pb.simulate(universe, params={"top_n": 2})
    explicit_none = pb.simulate(universe, params={"top_n": 2}, regime_by_date=None, by_regime=None)
    assert baseline == explicit_none


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


def _daily_calendar(start, days):
    d0 = dt.date.fromisoformat(start)
    return [(d0 + dt.timedelta(days=i)).isoformat() for i in range(days)]


def test_rebalance_dates_default_is_every_month():
    calendar = _daily_calendar("2022-01-01", 200)
    assert pb._rebalance_dates(calendar) == pb._rebalance_dates(calendar, 1)
    assert pb._rebalance_dates(calendar)[:3] == ["2022-01-01", "2022-02-01", "2022-03-01"]


def test_rebalance_dates_every_k_months_anchored_to_calendar_year():
    calendar = _daily_calendar("2022-01-01", 400)
    assert [d[:7] for d in pb._rebalance_dates(calendar, 3)] == ["2022-01", "2022-04", "2022-07", "2022-10", "2023-01"]
    assert [d[:7] for d in pb._rebalance_dates(calendar, 2)][:4] == ["2022-01", "2022-03", "2022-05", "2022-07"]


def test_rebalance_dates_grid_does_not_depend_on_where_calendar_starts():
    # A slice starting in February still lands on Apr/Jul/Oct, not Feb/May/Aug.
    calendar = _daily_calendar("2022-02-10", 300)
    assert [d[:7] for d in pb._rebalance_dates(calendar, 3)] == ["2022-04", "2022-07", "2022-10"]


def test_rebalance_dates_offset_shifts_phase():
    calendar = _daily_calendar("2022-01-01", 200)
    assert [d[:7] for d in pb._rebalance_dates(calendar, 3, offset_months=1)] == ["2022-02", "2022-05"]


def test_rebalance_dates_rejects_non_positive_interval():
    import pytest
    with pytest.raises(ValueError):
        pb._rebalance_dates(_daily_calendar("2022-01-01", 10), 0)


def test_simulate_quarterly_trades_only_on_quarter_start_months():
    universe = {"A": _entry(100, 0.1, days=300), "B": _entry(80, 0.05, days=300)}
    result = pb.simulate(universe, params={"top_n": 2, "rebalance_every_months": 3})
    assert result["trades"]
    assert {t["date"][:7] for t in result["trades"]} <= {"2022-01", "2022-04", "2022-07", "2022-10"}


def test_simulate_default_interval_matches_explicit_monthly():
    universe = {"A": _entry(100, 0.1, days=200), "B": _entry(80, 0.05, days=200)}
    base = pb.simulate(universe, params={"top_n": 2})
    explicit = pb.simulate(universe, params={"top_n": 2, "rebalance_every_months": 1})
    assert base["equity_curve"] == explicit["equity_curve"]
    assert base["trades"] == explicit["trades"]


# ---------------------------------------------------- _rebalance_dates freq --

def test_rebalance_dates_monthly_is_unchanged_default():
    calendar = ["2023-01-02", "2023-01-03", "2023-02-01", "2023-02-02", "2023-03-01"]
    assert pb._rebalance_dates(calendar) == ["2023-01-02", "2023-02-01", "2023-03-01"]
    assert pb._rebalance_dates(calendar, freq="monthly") == pb._rebalance_dates(calendar)


def test_rebalance_dates_weekly_groups_by_iso_week():
    # 2023-01-02 is a Monday (ISO week 1); 2023-01-09 is the next Monday (week 2).
    calendar = ["2023-01-02", "2023-01-03", "2023-01-06", "2023-01-09", "2023-01-10"]
    assert pb._rebalance_dates(calendar, freq="weekly") == ["2023-01-02", "2023-01-09"]


def test_rebalance_dates_biweekly_pairs_consecutive_iso_weeks():
    # 2023-01-09/16/23 are ISO weeks 2/3/4 -> week//2 groups (2,3) into pair 1
    # and 4 alone into pair 2 (grouping is by absolute week parity, not by
    # position within this calendar -- see _rebalance_dates() docstring).
    calendar = ["2023-01-09", "2023-01-16", "2023-01-23"]
    assert pb._rebalance_dates(calendar, freq="biweekly") == ["2023-01-09", "2023-01-23"]


def test_rebalance_dates_unknown_freq_raises():
    try:
        pb._rebalance_dates(["2023-01-02"], freq="daily")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_simulate_accepts_rebalance_freq_param_without_error():
    universe = {"A": _entry(100, 0.1)}
    result = pb.simulate(universe, params={"rebalance_freq": "weekly", "top_n": 1})
    assert result["equity_curve"]


def test_rebalance_dates_month_interval_only_applies_to_monthly_freq():
    # every_months/offset_months (merged from the interval work) and freq
    # (weekly/biweekly) are different dimensions; mixing them is ambiguous.
    import pytest
    calendar = _daily_calendar("2023-01-02", 60)
    with pytest.raises(ValueError):
        pb._rebalance_dates(calendar, 3, freq="weekly")
    with pytest.raises(ValueError):
        pb._rebalance_dates(calendar, offset_months=1, freq="biweekly")
    assert pb._rebalance_dates(calendar, 1, freq="weekly") == pb._rebalance_dates(calendar, freq="weekly")


# ---- delisting handling (terminal_exit_days / max_stale_days) ----

_DELIST_PARAMS = {"top_n": 1, "slippage_pct": 0.0, "fee_pct": 0.0}


def _delisting_universe():
    # B outranks A on every factor but stops printing after 200 days; A keeps going.
    return {"A": _entry(50, 0.1, days=400), "B": _entry(100, 0.3, days=200)}


def test_simulate_default_keeps_delisted_position_frozen_and_never_redeploys():
    result = pb.simulate(_delisting_universe(), params=_DELIST_PARAMS)
    assert {t["code"] for t in result["trades"]} == {"B"}
    assert [t for t in result["trades"] if t["side"] == "sell"] == []


def test_simulate_terminal_exit_sells_on_day_after_last_bar_then_redeploys():
    universe = _delisting_universe()
    last_bar = universe["B"]["bar_dates"][-1]
    result = pb.simulate(universe, params={**_DELIST_PARAMS, "terminal_exit_days": 30, "max_stale_days": 10})
    terminal = [t for t in result["trades"] if t.get("terminal")]
    assert len(terminal) == 1
    assert terminal[0]["code"] == "B" and terminal[0]["side"] == "sell"
    assert terminal[0]["date"] == (dt.date.fromisoformat(last_bar) + dt.timedelta(days=1)).isoformat()
    later_buys = [t for t in result["trades"] if t["side"] == "buy" and t["date"] > terminal[0]["date"]]
    assert later_buys and {t["code"] for t in later_buys} == {"A"}


def test_simulate_terminal_exit_applies_haircut_slippage_and_fee():
    universe = _delisting_universe()
    last_px = universe["B"]["bars"][-1]["c"]
    result = pb.simulate(universe, params={
        **_DELIST_PARAMS, "terminal_exit_days": 30, "max_stale_days": 10,
        "terminal_haircut_pct": 50.0, "slippage_pct": 1.0, "fee_pct": 0.6,
    })
    terminal = [t for t in result["trades"] if t.get("terminal")][0]
    assert abs(terminal["price"] - last_px * 0.5 * 0.99) < 1e-9


def test_simulate_terminal_exit_ignores_code_that_is_still_printing():
    universe = {"A": _entry(50, 0.1, days=400), "B": _entry(100, 0.3, days=400)}
    result = pb.simulate(universe, params={**_DELIST_PARAMS, "terminal_exit_days": 30, "max_stale_days": 10})
    assert [t for t in result["trades"] if t.get("terminal")] == []
