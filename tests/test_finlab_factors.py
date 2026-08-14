# -*- coding: utf-8 -*-
"""Unit tests for finlab_factors.py, all on hand-computed fixtures -- no
network, no DB."""
import finlab_factors as ff


def _bars(closes, start="2023-01-01", volume=300_000):
    import datetime as dt
    base = dt.date.fromisoformat(start)
    return [
        {"date": (base + dt.timedelta(days=i)).isoformat(), "c": c, "v": volume}
        for i, c in enumerate(closes)
    ]


# ------------------------------------------------------- revenue_yoy_series --

def test_revenue_yoy_series_hand_computed():
    rows = [{"date": f"2022-{m:02d}-01", "revenue": 100.0} for m in range(1, 13)]
    rows += [{"date": f"2023-{m:02d}-01", "revenue": 120.0} for m in range(1, 13)]
    out = ff.revenue_yoy_series(rows)
    assert len(out) == 12
    assert out[0] == {"usable_date": "2023-01-11", "yoy_pct": 20.0}


def test_revenue_yoy_series_too_short_returns_empty():
    rows = [{"date": f"2023-{m:02d}-01", "revenue": 100.0} for m in range(1, 6)]
    assert ff.revenue_yoy_series(rows) == []


def test_revenue_yoy_series_skips_zero_prior_year():
    rows = [{"date": f"2022-{m:02d}-01", "revenue": 0.0} for m in range(1, 13)]
    rows += [{"date": f"2023-{m:02d}-01", "revenue": 120.0} for m in range(1, 13)]
    assert ff.revenue_yoy_series(rows) == []


# -------------------------------------------------------------- roe_series --

def test_roe_series_ttm_over_latest_equity():
    rows = [{"date": f"2022-{q:02d}-28", "net_income": 10.0, "equity": 200.0} for q in (3, 6, 9, 12)]
    out = ff.roe_series(rows)
    assert len(out) == 1
    assert out[0]["roe_pct"] == 20.0  # (10*4)/200*100
    assert out[0]["usable_date"] == "2023-02-26"  # 2022-12-28 + 60d


def test_roe_series_too_short_returns_empty():
    rows = [{"date": "2022-12-31", "net_income": 10.0, "equity": 200.0}]
    assert ff.roe_series(rows) == []


# ------------------------------------------------------------------ _as_of --

def test_as_of_picks_latest_not_after_date():
    series = [{"usable_date": "2023-01-10", "yoy_pct": 5}, {"usable_date": "2023-04-10", "yoy_pct": 8}]
    assert ff._as_of(series, "2023-03-01", "yoy_pct") == 5
    assert ff._as_of(series, "2023-04-10", "yoy_pct") == 8
    assert ff._as_of(series, "2023-05-01", "yoy_pct") == 8
    assert ff._as_of(series, "2022-01-01", "yoy_pct") is None


# ------------------------------------------------------------ liquidity_ok --

def test_liquidity_ok_true_above_threshold():
    bars = _bars([100.0] * 25, volume=500_000)  # turnover 50M > 10M floor
    assert ff.liquidity_ok(bars, 24) is True


def test_liquidity_ok_false_below_threshold():
    bars = _bars([10.0] * 25, volume=1_000)  # turnover 10k << 10M floor
    assert ff.liquidity_ok(bars, 24) is False


def test_liquidity_ok_false_when_window_not_full():
    bars = _bars([100.0] * 10, volume=500_000)
    assert ff.liquidity_ok(bars, 9) is False


# ----------------------------------------------------------- momentum_pct --

def test_momentum_pct_hand_computed():
    closes = [100.0] * 61
    closes[60] = 110.0
    bars = _bars(closes)
    assert ff.momentum_pct(bars, 60, days=60) == 10.0


def test_momentum_pct_none_when_insufficient_history():
    bars = _bars([100.0] * 10)
    assert ff.momentum_pct(bars, 9, days=60) is None


# ------------------------------------------------------- composite_scores --

def _universe_entry(base, mom_boost, days=90):
    bars = _bars([base + i * mom_boost for i in range(days)])
    return {
        "bars": bars,
        "bar_dates": [b["date"] for b in bars],
        "revenue_series": [{"usable_date": "2023-01-01", "yoy_pct": base}],
        "roe_series": [{"usable_date": "2023-01-01", "roe_pct": base}],
    }


def test_composite_scores_ranks_dominant_stock_highest():
    universe = {
        "A": _universe_entry(100, 1.0),
        "B": _universe_entry(50, 0.2),
        "C": _universe_entry(30, 0.05),
    }
    date = universe["A"]["bar_dates"][-1]
    scores = ff.composite_scores(universe, date)
    assert set(scores) == {"A", "B", "C"}
    assert scores["A"] > scores["B"] > scores["C"]
    assert ff.top_n(scores, 2) == ["A", "B"]


def test_composite_scores_excludes_illiquid_and_short_history():
    universe = {
        "A": _universe_entry(100, 1.0),
        "ILLIQUID": {**_universe_entry(200, 2.0), "bars": _bars([200.0] * 90, volume=1)},
    }
    universe["ILLIQUID"]["bar_dates"] = [b["date"] for b in universe["ILLIQUID"]["bars"]]
    date = universe["A"]["bar_dates"][-1]
    scores = ff.composite_scores(universe, date)
    assert set(scores) == {"A"}


def test_composite_scores_empty_when_no_survivors():
    assert ff.composite_scores({}, "2023-01-01") == {}


# -------------------------------------------------------------------- top_n --

def test_top_n_descending_and_truncated():
    scores = {"A": 0.9, "B": 0.5, "C": 0.7, "D": 0.1}
    assert ff.top_n(scores, 2) == ["A", "C"]
