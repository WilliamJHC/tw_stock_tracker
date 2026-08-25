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
    # 2023-01 revenue's usable_date = that month's end (2023-01-31) + 10
    # days = 2023-02-10, not 2023-01-01 + 10 days -- disclosure deadlines
    # are measured from month-end, not month-start.
    assert out[0] == {"usable_date": "2023-02-10", "yoy_pct": 20.0}


def test_revenue_yoy_series_matches_by_calendar_year_not_position():
    # A gap (missing 2022-06) must not shift the i-12 lookup onto the wrong
    # month -- 2023-06 should be skipped (no exact prior-year match) while
    # every other 2023 month still matches its real prior-year counterpart.
    rows = [{"date": f"2022-{m:02d}-01", "revenue": 100.0} for m in range(1, 13) if m != 6]
    rows += [{"date": f"2023-{m:02d}-01", "revenue": 120.0} for m in range(1, 13)]
    out = ff.revenue_yoy_series(rows)
    assert len(out) == 11
    assert all(row["yoy_pct"] == 20.0 for row in out)


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


def test_roe_series_skips_window_with_non_consecutive_quarter():
    # A missing quarter (get_financials() drops it when income/equity data
    # doesn't intersect) must not silently sum 4 non-adjacent rows as TTM:
    # only the all-2022 window (i=3) is a valid consecutive-quarter window;
    # the window sliding in the gapped 2023-09 row (i=4) must be skipped.
    rows = [{"date": f"2022-{q:02d}-28", "net_income": 10.0, "equity": 200.0} for q in (3, 6, 9, 12)]
    rows += [{"date": "2023-09-28", "net_income": 10.0, "equity": 200.0}]  # gap: skipped 2023-03/06
    out = ff.roe_series(rows)
    assert len(out) == 1
    assert out[0]["usable_date"] == "2023-02-26"  # from the valid 2022-12-28 window only


def test_roe_series_skips_non_positive_equity():
    rows = [{"date": f"2022-{q:02d}-28", "net_income": 10.0, "equity": -50.0} for q in (3, 6, 9, 12)]
    assert ff.roe_series(rows) == []


# ------------------------------------------------------- margin trend series --

def test_gross_margin_trend_series_yoy_change():
    rows = [
        {"date": "2022-12-31", "net_income": 1.0, "equity": 1.0, "revenue": 1000.0, "gross_profit": 300.0, "operating_income": 100.0},
        {"date": "2023-03-31", "net_income": 1.0, "equity": 1.0, "revenue": 100.0, "gross_profit": 100.0, "operating_income": 100.0},
        {"date": "2023-06-30", "net_income": 1.0, "equity": 1.0, "revenue": 100.0, "gross_profit": 100.0, "operating_income": 100.0},
        {"date": "2023-09-30", "net_income": 1.0, "equity": 1.0, "revenue": 100.0, "gross_profit": 100.0, "operating_income": 100.0},
        {"date": "2023-12-31", "net_income": 1.0, "equity": 1.0, "revenue": 1000.0, "gross_profit": 400.0, "operating_income": 100.0},
    ]
    out = ff.gross_margin_trend_series(rows)
    # 2023-12-31 gross margin 40% vs 2022-12-31's 30% -> +10pp
    assert out == [{"usable_date": "2024-02-29", "trend_pct": 10.0}]


def test_operating_margin_trend_series_yoy_change():
    rows = [
        {"date": "2022-12-31", "net_income": 1.0, "equity": 1.0, "revenue": 1000.0, "gross_profit": 100.0, "operating_income": 100.0},
        {"date": "2023-03-31", "net_income": 1.0, "equity": 1.0, "revenue": 100.0, "gross_profit": 100.0, "operating_income": 100.0},
        {"date": "2023-06-30", "net_income": 1.0, "equity": 1.0, "revenue": 100.0, "gross_profit": 100.0, "operating_income": 100.0},
        {"date": "2023-09-30", "net_income": 1.0, "equity": 1.0, "revenue": 100.0, "gross_profit": 100.0, "operating_income": 100.0},
        {"date": "2023-12-31", "net_income": 1.0, "equity": 1.0, "revenue": 1000.0, "gross_profit": 100.0, "operating_income": 150.0},
    ]
    out = ff.operating_margin_trend_series(rows)
    # 2023-12-31 operating margin 15% vs 2022-12-31's 10% -> +5pp
    assert out == [{"usable_date": "2024-02-29", "trend_pct": 5.0}]


def test_margin_trend_series_skips_quarter_missing_numerator():
    rows = [
        {"date": "2022-12-31", "net_income": 1.0, "equity": 1.0, "revenue": 1000.0, "gross_profit": None, "operating_income": 100.0},
        {"date": "2023-03-31", "net_income": 1.0, "equity": 1.0, "revenue": 100.0, "gross_profit": 100.0, "operating_income": 100.0},
        {"date": "2023-06-30", "net_income": 1.0, "equity": 1.0, "revenue": 100.0, "gross_profit": 100.0, "operating_income": 100.0},
        {"date": "2023-09-30", "net_income": 1.0, "equity": 1.0, "revenue": 100.0, "gross_profit": 100.0, "operating_income": 100.0},
        {"date": "2023-12-31", "net_income": 1.0, "equity": 1.0, "revenue": 1000.0, "gross_profit": 400.0, "operating_income": 100.0},
    ]
    # 2022-12-31 has no gross_profit -> 2023-12-31's YoY comparison has no
    # prior-year point either, so the whole series is empty (roe_series()
    # over the same rows would still compute fine -- separate concerns).
    assert ff.gross_margin_trend_series(rows) == []


# -------------------------------------------------------------- value_series --

def test_value_series_flips_pbr_sign():
    rows = [{"date": "2024-01-02", "per": 17.17, "pbr": 4.59}, {"date": "2024-01-03", "per": 17.0, "pbr": 4.5}]
    out = ff.value_series(rows)
    assert out == [
        {"usable_date": "2024-01-02", "value_score": -4.59},
        {"usable_date": "2024-01-03", "value_score": -4.5},
    ]


def test_value_series_skips_missing_pbr():
    rows = [{"date": "2024-01-02", "per": 17.17, "pbr": None}]
    assert ff.value_series(rows) == []


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


def test_momentum_pct_prefers_c_adj_over_raw_c():
    # Raw close jumps +300% (a phantom 1:4-split-shaped move); c_adj holds
    # flat -- momentum_pct() must read c_adj, not the raw split artefact
    # (docs/https-finlab-finance-blog-python-get-tai-memoized-lighthouse.md
    # Phase 6a).
    bars = _bars([100.0] * 61)
    bars[60]["c"] = 400.0
    for b in bars:
        b["c_adj"] = 100.0
    assert ff.momentum_pct(bars, 60, days=60) == 0.0


def test_momentum_pct_falls_back_to_raw_c_when_no_c_adj():
    closes = [100.0] * 61
    closes[60] = 110.0
    bars = _bars(closes)
    assert "c_adj" not in bars[0]
    assert ff.momentum_pct(bars, 60, days=60) == 10.0


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


def test_composite_scores_weights_override_can_flip_ranking():
    # A has the best momentum but worst revenue/ROE; B is the reverse.
    # Default RANKING_WEIGHTS (revenue+roe=0.7 vs momentum=0.3) favors B,
    # since B wins 2 of 3 percentile-ranked factors; a momentum-only
    # override flips it to A.
    universe = {
        "A": _universe_entry(10, 2.0),    # weak fundamentals, strong momentum
        "B": _universe_entry(100, 0.01),  # strong fundamentals, weak momentum
    }
    date = universe["A"]["bar_dates"][-1]
    default_scores = ff.composite_scores(universe, date)
    assert default_scores["B"] > default_scores["A"]

    flipped = ff.composite_scores(universe, date, weights={"revenue": 0.0, "roe": 0.0, "momentum": 1.0})
    assert flipped["A"] > flipped["B"]
    # module-level RANKING_WEIGHTS must be untouched by the override
    assert ff.RANKING_WEIGHTS == {"revenue": 0.4, "roe": 0.3, "momentum": 0.3}


def test_composite_scores_reuses_precomputed_ranks_instead_of_recomputing():
    """scripts/run_daily_recommendations.py calls factor_ranks() itself for
    the per-factor breakdown, then passes it into composite_scores() so the
    liquidity-filter + percentile-rank pass isn't paid for twice. Passing
    `ranks` must skip the internal factor_ranks() call and use exactly what
    was given, even if it disagrees with what factor_ranks() would compute."""
    universe = {
        "A": _universe_entry(100, 1.0),
        "B": _universe_entry(50, 0.2),
    }
    date = universe["A"]["bar_dates"][-1]
    real_ranks = ff.factor_ranks(universe, date)

    fake_ranks = {"A": {"revenue": 0.1, "roe": 0.1, "momentum": 0.1},
                  "B": {"revenue": 0.9, "roe": 0.9, "momentum": 0.9}}
    assert fake_ranks != real_ranks

    scores = ff.composite_scores(universe, date, ranks=fake_ranks)
    assert scores["B"] > scores["A"]  # follows fake_ranks, not real_ranks (which favors A)


# -------------------------------------------------------------------- top_n --

def test_top_n_descending_and_truncated():
    scores = {"A": 0.9, "B": 0.5, "C": 0.7, "D": 0.1}
    assert ff.top_n(scores, 2) == ["A", "C"]
