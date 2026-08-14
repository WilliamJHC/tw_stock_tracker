# -*- coding: utf-8 -*-
"""Unit tests for portfolio_backtest.py, all on hand-computed fixtures --
no network, no DB."""
import datetime as dt

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


def test_simulate_equity_grows_when_selected_names_trend_up():
    universe = {"A": _entry(100, 0.5), "B": _entry(80, 0.4)}
    result = pb.simulate(universe, params={"top_n": 2, "initial_capital": 1_000_000})
    assert result["equity_curve"][-1]["equity"] > 1_000_000
    assert result["summary"]["total_return_pct"] > 0
    assert result["summary"]["cagr_pct"] > 0


def test_simulate_fee_reduces_return_vs_zero_fee():
    universe = {"A": _entry(100, 0.5), "B": _entry(80, 0.4)}
    no_fee = pb.simulate(universe, params={"top_n": 2, "fee_pct": 0.0})
    with_fee = pb.simulate(universe, params={"top_n": 2, "fee_pct": 2.0})
    assert with_fee["equity_curve"][-1]["equity"] < no_fee["equity_curve"][-1]["equity"]


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
