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


def momentum_pct(bars, idx, days=MOMENTUM_DAYS):
    """`days`-bar percent return ending at bars[idx], or None if idx has
    fewer than `days` bars of history before it."""
    if idx - days < 0:
        return None
    base = bars[idx - days]["c"]
    if not base:
        return None
    return (bars[idx]["c"] - base) / base * 100


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


def composite_scores(universe_data, date):
    """universe_data: {code: {"bars": [...], "bar_dates": [...],
    "revenue_series": [...], "roe_series": [...]}} (bar_dates = bars' date
    field pre-extracted, see _bar_index_on_or_before). Returns {code: score}
    for codes that pass the liquidity filter and have all three factors
    available as of `date` -- percentile ranks are computed within this
    survivor set, matching the FinLab original's
    `.where(liquid & close.notna())` (filter before rank, not after)."""
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
        code: (
            yoy_rank[code] * RANKING_WEIGHTS["revenue"]
            + roe_rank[code] * RANKING_WEIGHTS["roe"]
            + mom_rank[code] * RANKING_WEIGHTS["momentum"]
        )
        for code in raw
    }


def top_n(scores, n=RANKING_TOP_N):
    """[code, ...] of the top `n` scores, descending."""
    return [code for code, _ in sorted(scores.items(), key=lambda kv: -kv[1])[:n]]
