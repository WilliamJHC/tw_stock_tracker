# -*- coding: utf-8 -*-
"""Multi-factor composite ranking, reproducing finlab.finance's AI-agent
guide "ranking method" (排名法多因子) on FinMind data instead of a FinLab
subscription -- see src/finlab_strategy.py for the original FinLab-backed
version this was ported from.

Pure functions over already-fetched per-code series -- no I/O, no FinMind,
no DB access, same split as backtest.py/analysis.py.

Point-in-time discipline: FinMind doesn't expose actual disclosure dates,
only period-end dates, so a fixed lag stands in for "when this number
became public." Taiwan's regulatory deadlines are ~10 days after month-end
for monthly revenue and up to ~45-60 days after quarter-end for quarterly
financials; REVENUE_LAG_DAYS/FINANCIALS_LAG_DAYS use the conservative end
of that range rather than a per-filing lookup.

>>> momentum_pct() prefers ADJUSTED close, falls back to raw <<<
adjust.py's downstream rule (docs/archive/backtest_plan.md Phase 1c) says
momentum must run on split/dividend-adjusted price levels, not raw ones.
Phase 6a (docs/https-finlab-finance-blog-python-get-tai-memoized-lighthouse.md)
closed this gap for this module's (potentially 2000+ code) universe:
scripts/run_factor_backtest.py's fetch_universe_data() now does its own
lightweight per-code Yahoo-ratio adjustment (fetch_yahoo_adj_close() +
compute_yahoo_ratio_series() + detect_steps(), reused from
scripts/build_adj_factors.py, plus this project's own adjust.build_adj_factor())
and stamps a "c_adj" key onto each bar dict -- no TWSE table, no DB write,
just an in-memory factor applied straight onto the bars this module already
receives. momentum_pct() below reads bars[i]["c_adj"] when present, its raw
"c" otherwise. liquidity_ok() is NOT affected either way -- it wants raw
close*volume by design (adjust.py's own rule), not a bug. A caller that
builds `bars` without ever computing adj_factor (e.g. a hand-built fixture,
or a future caller that skips the Yahoo augmentation step) still gets the
old raw-close behavior -- the phantom-return risk near a split only remains
where "c_adj" was never populated.
"""
import bisect
import datetime as dt

REVENUE_LAG_DAYS = 10
FINANCIALS_LAG_DAYS = 60
LIQUIDITY_MIN_TURNOVER = 10_000_000  # NTD, 20-bar avg trading value floor
LIQUIDITY_WINDOW = 20
MOMENTUM_DAYS = 60
RANKING_WEIGHTS = {"revenue": 0.4, "roe": 0.3, "momentum": 0.3}
RANKING_TOP_N = 20


def _shift_date(date_str, days):
    d = dt.datetime.strptime(date_str, "%Y-%m-%d") + dt.timedelta(days=days)
    return d.strftime("%Y-%m-%d")


def _month_end_lag_date(month_start_str, lag_days):
    """month_start_str ("YYYY-MM-01") -> that month's end + lag_days, i.e.
    the 1st of the following month plus (lag_days - 1). Taiwan's monthly
    revenue disclosure deadline is measured from month-END, not month-start
    -- get_monthly_revenue() stores the revenue month's 1st, so shifting
    from that directly (as an earlier version of this module did) understated
    the real-world lag by up to a month."""
    d = dt.datetime.strptime(month_start_str, "%Y-%m-%d")
    next_month_first = d.replace(year=d.year + 1, month=1) if d.month == 12 else d.replace(month=d.month + 1)
    return (next_month_first + dt.timedelta(days=lag_days - 1)).strftime("%Y-%m-%d")


def revenue_yoy_series(revenue_rows):
    """[{date, revenue}] (ascending, monthly) -> [{usable_date, yoy_pct}].
    usable_date = revenue month-end + REVENUE_LAG_DAYS. Matches each row
    against the entry exactly 12 calendar months earlier by date (not by
    position), so a gap in the input -- get_monthly_revenue() doesn't
    backfill missing months -- skips that point instead of silently
    comparing non-adjacent months. Needs >=13 months of input to produce a
    single point (one year of comparison + the point itself); shorter input
    yields []."""
    if not revenue_rows or len(revenue_rows) < 13:
        return []
    by_date = {r["date"]: r["revenue"] for r in revenue_rows}
    out = []
    for r in revenue_rows:
        cur_dt = dt.datetime.strptime(r["date"], "%Y-%m-%d")
        prev_key = cur_dt.replace(year=cur_dt.year - 1).strftime("%Y-%m-%d")
        prev = by_date.get(prev_key)
        if not prev:
            continue
        out.append({
            "usable_date": _month_end_lag_date(r["date"], REVENUE_LAG_DAYS),
            "yoy_pct": (r["revenue"] - prev) / abs(prev) * 100,
        })
    return out


def roe_series(financials_rows):
    """[{date, net_income, equity}] (ascending, quarterly) -> [{usable_date,
    roe_pct}]. ROE = trailing-4-quarter net income / latest quarter equity,
    annualized by construction (TTM numerator). usable_date = quarter-end +
    FINANCIALS_LAG_DAYS. Needs >=4 quarters of input. get_financials() joins
    income/equity by date INTERSECTION, so a quarter missing either side is
    dropped rather than backfilled -- each 4-row window is checked for
    ~quarterly (80-100 day) consecutive gaps before use, so a dropped
    quarter skips that point instead of silently summing non-adjacent
    quarters as if they were TTM. Non-positive equity is also skipped:
    ROE is undefined (or sign-flipped and misleading) for negative book
    equity."""
    if not financials_rows or len(financials_rows) < 4:
        return []
    out = []
    for i in range(3, len(financials_rows)):
        window = financials_rows[i - 3:i + 1]
        window_dates = [dt.datetime.strptime(r["date"], "%Y-%m-%d") for r in window]
        if any(not (80 <= (window_dates[j + 1] - window_dates[j]).days <= 100) for j in range(3)):
            continue
        equity = window[-1]["equity"]
        if not equity or equity <= 0:
            continue
        ttm_income = sum(r["net_income"] for r in window)
        out.append({
            "usable_date": _shift_date(window[-1]["date"], FINANCIALS_LAG_DAYS),
            "roe_pct": ttm_income / equity * 100,
        })
    return out


def _margin_trend_series(financials_rows, numerator_key):
    """Shared implementation for gross_margin_trend_series()/
    operating_margin_trend_series() -- financials_rows: [{date, revenue,
    numerator_key, ...}] (ascending, quarterly, get_financials()'s extended
    shape). Trend = this quarter's margin (numerator/revenue, %) minus the
    SAME quarter one year ago's -- cancels seasonality the same way
    revenue_yoy_series() does (exact calendar-year match, not position),
    rather than comparing a raw margin level across quarters with different
    seasonal mix. usable_date = quarter-end + FINANCIALS_LAG_DAYS, same
    disclosure-lag convention as roe_series(). A quarter missing revenue or
    `numerator_key` (get_financials() leaves these None rather than
    dropping the row, so ROE stays computable even when a margin figure is
    absent) is skipped for this factor without affecting roe_series()/
    revenue_yoy_series() at all."""
    if not financials_rows or len(financials_rows) < 5:
        return []
    by_date = {r["date"]: r for r in financials_rows}

    def _margin_pct(row):
        revenue, num = row.get("revenue"), row.get(numerator_key)
        if not revenue or num is None:
            return None
        return num / revenue * 100

    out = []
    for r in financials_rows:
        cur_margin = _margin_pct(r)
        if cur_margin is None:
            continue
        cur_dt = dt.datetime.strptime(r["date"], "%Y-%m-%d")
        prev_key = cur_dt.replace(year=cur_dt.year - 1).strftime("%Y-%m-%d")
        prev = by_date.get(prev_key)
        prev_margin = _margin_pct(prev) if prev else None
        if prev_margin is None:
            continue
        out.append({
            "usable_date": _shift_date(r["date"], FINANCIALS_LAG_DAYS),
            "trend_pct": cur_margin - prev_margin,
        })
    return out


def gross_margin_trend_series(financials_rows):
    """YoY change in gross margin (GrossProfit/Revenue, pct points) -- see
    _margin_trend_series(). Candidate quality factor, Phase 6b
    (docs/https-finlab-finance-blog-python-get-tai-memoized-lighthouse.md);
    NOT part of RANKING_WEIGHTS/composite_scores() -- evaluate via
    factor_eval.evaluate_candidate_factor() first, see that function's
    docstring on why adoption is never automatic."""
    return _margin_trend_series(financials_rows, "gross_profit")


def operating_margin_trend_series(financials_rows):
    """YoY change in operating margin (OperatingIncome/Revenue, pct points)
    -- see _margin_trend_series(). Same candidate-factor status as
    gross_margin_trend_series()."""
    return _margin_trend_series(financials_rows, "operating_income")


def value_series(per_pbr_rows):
    """data_sources.get_per_pbr()'s [{date, per, pbr}] (ascending) ->
    [{usable_date, value_score}]. value_score = -pbr so this factor's sign
    convention matches every other factor here (higher raw value = more
    attractive, fed straight into _pct_rank()) -- a LOWER price-to-book is
    the conventionally "cheaper" (better value) stock, so its sign is
    flipped. usable_date == date directly: FinMind's TaiwanStockPER series
    is already trading-date-indexed (see get_per_pbr()'s docstring on why
    no REVENUE_LAG_DAYS/FINANCIALS_LAG_DAYS-style lag is added here). Skips
    a row with no pbr. Candidate value factor, Phase 6b -- same
    evaluate-before-adopt status as the margin-trend functions above."""
    return [{"usable_date": r["date"], "value_score": -r["pbr"]}
            for r in per_pbr_rows if r.get("pbr")]


def _as_of(series, date, key):
    """Latest `series` entry with usable_date <= date, or None. series must
    be sorted ascending by usable_date (both revenue_yoy_series() and
    roe_series() preserve their input's ascending order)."""
    dates = [row["usable_date"] for row in series]
    idx = bisect.bisect_right(dates, date) - 1
    return series[idx][key] if idx >= 0 else None


def _bar_index_on_or_before(dates, date):
    """Index of the latest bar with date <= `date`, or None. `dates` is a
    bars list's date field pre-extracted by the caller so repeated lookups
    across many rebalance dates don't re-walk the list each time."""
    idx = bisect.bisect_right(dates, date) - 1
    return idx if idx >= 0 else None


def liquidity_ok(bars, idx, min_turnover=LIQUIDITY_MIN_TURNOVER, window=LIQUIDITY_WINDOW):
    """True if the `window`-bar average turnover (close*volume) ending at
    bars[idx] exceeds min_turnover. False if fewer than `window` bars
    precede idx."""
    if idx + 1 < window:
        return False
    chunk = bars[idx + 1 - window: idx + 1]
    avg_turnover = sum(b["c"] * b["v"] for b in chunk) / window
    return avg_turnover > min_turnover


def _close(bar):
    """bar["c_adj"] (split/dividend-adjusted) if present, else raw bar["c"]
    -- see this module's docstring on who populates c_adj and why."""
    c_adj = bar.get("c_adj")
    return c_adj if c_adj is not None else bar["c"]


def momentum_pct(bars, idx, days=MOMENTUM_DAYS):
    """`days`-bar percent return ending at bars[idx], or None if idx has
    fewer than `days` bars of history before it.

    Prefers bars[i]["c_adj"] (see this module's docstring), falling back to
    raw "c" for any bar that doesn't carry one -- phantom near a split/large
    stock dividend only for the latter."""
    if idx - days < 0:
        return None
    base = _close(bars[idx - days])
    if not base:
        return None
    return (_close(bars[idx]) - base) / base * 100


def _pct_rank(values):
    """{code: number} -> {code: percentile in (0, 1]}, average-rank ties
    (matches pandas .rank(pct=True), which the original FinLab strategy
    uses)."""
    ordered = sorted(values.items(), key=lambda kv: kv[1])
    n = len(ordered)
    ranks = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        avg_rank_pct = ((i + j) / 2 + 1) / n
        for k in range(i, j + 1):
            ranks[ordered[k][0]] = avg_rank_pct
        i = j + 1
    return ranks


def factor_ranks(universe_data, date):
    """universe_data/date: same shape as composite_scores() below. Returns
    {code: {"revenue": pct, "roe": pct, "momentum": pct}} -- the per-factor
    percentile ranks (see _pct_rank()) for every code that passes the
    liquidity filter and has all three factors available as of `date`.
    composite_scores() is a weighted sum of exactly these three numbers;
    factored out into its own function so a caller that wants the
    per-factor breakdown (e.g. a recommendation card's "why this stock
    ranked" detail, scripts/run_daily_recommendations.py) doesn't have to
    duplicate this filter+rank logic."""
    raw = {}
    for code, d in universe_data.items():
        bars, bar_dates = d["bars"], d["bar_dates"]
        idx = _bar_index_on_or_before(bar_dates, date)
        if idx is None or not liquidity_ok(bars, idx):
            continue
        mom = momentum_pct(bars, idx)
        yoy = _as_of(d["revenue_series"], date, "yoy_pct")
        roe = _as_of(d["roe_series"], date, "roe_pct")
        if mom is None or yoy is None or roe is None:
            continue
        raw[code] = {"momentum": mom, "yoy": yoy, "roe": roe}

    if not raw:
        return {}

    mom_rank = _pct_rank({c: v["momentum"] for c, v in raw.items()})
    yoy_rank = _pct_rank({c: v["yoy"] for c, v in raw.items()})
    roe_rank = _pct_rank({c: v["roe"] for c, v in raw.items()})
    return {
        code: {"revenue": yoy_rank[code], "roe": roe_rank[code], "momentum": mom_rank[code]}
        for code in raw
    }


def composite_scores(universe_data, date, weights=None, ranks=None):
    """universe_data: {code: {"bars": [...], "bar_dates": [...],
    "revenue_series": [...], "roe_series": [...]}} (bar_dates = bars' date
    field pre-extracted, see _bar_index_on_or_before). Returns {code: score}
    for codes that pass the liquidity filter and have all three factors
    available as of `date` -- percentile ranks are computed within this
    survivor set (filter, then rank) via factor_ranks() above, then
    weighted-summed here.

    weights: optional {"revenue","roe","momentum"} override for the module-
    level RANKING_WEIGHTS -- lets a caller compare candidate weightings
    (e.g. src/optimize.py's sweep_ranking_weights_walk_forward(), Phase 5
    applied to Phase 4's rank-IC-derived suggestions) without mutating
    global state. None (the default) uses RANKING_WEIGHTS, unchanged
    behavior for every existing caller.

    ranks: optional pre-computed factor_ranks(universe_data, date) result --
    lets a caller that also needs the per-factor breakdown (e.g.
    scripts/run_daily_recommendations.py's recommendation cards) reuse the
    same liquidity-filter + percentile-rank pass instead of this function
    recomputing it. None (the default) computes it here, unchanged behavior
    for every existing caller.

    This is NOT the same rank order as finlab_strategy.py's FinLab-backed
    ranking_position(), which ranks over the WHOLE fetched universe first
    and applies the liquidity/availability filter after
    (`composite.where(liquid & close.notna())`) -- percentile rank of the
    same liquid stock differs depending on whether the denominator is the
    full market or only the survivor set. This module can't reproduce
    rank-then-filter exactly anyway: scripts/run_factor_backtest.py's
    two-stage fetch already drops non-survivors before they ever reach
    universe_data, so a "whole universe" to rank against doesn't exist
    here. Treat the two strategies as comparable in spirit, not bit-for-bit
    identical rankings."""
    ranks = ranks if ranks is not None else factor_ranks(universe_data, date)
    if not ranks:
        return {}
    w = weights if weights is not None else RANKING_WEIGHTS
    return {
        code: r["revenue"] * w["revenue"] + r["roe"] * w["roe"] + r["momentum"] * w["momentum"]
        for code, r in ranks.items()
    }


def top_n(scores, n=RANKING_TOP_N):
    """[code, ...] of the top `n` scores, descending."""
    return [code for code, _ in sorted(scores.items(), key=lambda kv: -kv[1])[:n]]
