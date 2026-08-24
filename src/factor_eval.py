# -*- coding: utf-8 -*-
"""Factor evaluation: rank-IC, ICIR, and decay curves for the ranking
strategy's three factors (momentum/revenue-YoY/ROE), reusing this project's
existing rank_ic()/ic_stats() (src/metrics.py, built in Phase 0) and
finlab_factors.py's point-in-time factor plumbing -- see docs/https-finlab-
finance-blog-python-get-tai-memoized-lighthouse.md Phase 4 ("自建 rank-IC
因子評估，取代 Qlib"). Pure functions over an already-fetched
universe_data -- no I/O, no FinMind, no DB access, same split as
backtest.py/analysis.py/portfolio_backtest.py.

This answers the question src/finlab_factors.py's RANKING_WEIGHTS comment
flags as unanswered: whether revenue 0.4 / ROE 0.3 / momentum 0.3 is a
made-up weighting. Per-factor IC/ICIR/decay is evidence either way, not
another guess -- see suggest_weights() for how to turn it into a proposal
(never auto-applied; a human decides whether to adopt it).

Momentum's IC here inherits finlab_factors.momentum_pct()'s KNOWN
LIMITATION (see that module's docstring): it runs on unadjusted close, so
a rebalance date shortly after any universe code's split/large stock
dividend contributes a phantom momentum value to that date's IC.
"""
import finlab_factors as ff
import metrics as m
import portfolio_backtest as pb

DEFAULT_HORIZONS = (1, 5, 20, 60)

# RANKING_WEIGHTS' factor name -> the raw value key _raw_factor_values()
# stores it under (finlab_factors.py names the revenue-YoY factor "yoy" in
# its raw dict but "revenue" in RANKING_WEIGHTS -- this bridges the two).
FACTOR_KEYS = {"revenue": "yoy", "roe": "roe", "momentum": "momentum"}


def _raw_factor_values(universe_data, date):
    """{code: {"momentum", "yoy", "roe", "_idx"}} at `date` -- same
    liquidity-then-availability filter as finlab_factors.composite_scores()
    (filter survivors first, only rank/evaluate within that set; see that
    function's docstring for why this module can't evaluate against "the
    whole market" either). `_idx` is the code's bar index at `date`, kept
    so _forward_return() doesn't need to re-resolve it."""
    raw = {}
    for code, d in universe_data.items():
        bars, bar_dates = d["bars"], d["bar_dates"]
        idx = ff._bar_index_on_or_before(bar_dates, date)
        if idx is None or not ff.liquidity_ok(bars, idx):
            continue
        mom = ff.momentum_pct(bars, idx)
        yoy = ff._as_of(d["revenue_series"], date, "yoy_pct")
        roe = ff._as_of(d["roe_series"], date, "roe_pct")
        if mom is None or yoy is None or roe is None:
            continue
        raw[code] = {"momentum": mom, "yoy": yoy, "roe": roe, "_idx": idx}
    return raw


def _forward_return(bars, idx, horizon):
    """horizon-BAR (not calendar-day) forward return from bars[idx] --
    matches this project's existing bar-count convention (momentum_pct(),
    max_hold_days). None if there aren't `horizon` more bars after idx."""
    if idx + horizon >= len(bars):
        return None
    base = bars[idx]["c"]
    if not base:
        return None
    return (bars[idx + horizon]["c"] - base) / base * 100


def rebalance_dates(universe_data):
    """This module's own rebalance-date source: reuses
    portfolio_backtest.py's exact month-boundary convention (private
    _trading_calendar/_rebalance_dates -- same package, not a public API
    contract) so factor IC is measured at the cadence the strategy actually
    trades on, not an arbitrary daily/weekly grid."""
    calendar = pb._trading_calendar(universe_data)
    return pb._rebalance_dates(calendar)


def factor_ic_series(universe_data, dates, factor_name, horizon, raw_by_date=None):
    """[(date, ic), ...] for one factor at one holding horizon, across
    `dates`. ic is None for a date with no cross-sectional spread (see
    metrics.rank_ic()) or too few codes to correlate.

    raw_by_date: optional {date: _raw_factor_values(universe_data, date)}
    precomputed cache -- _raw_factor_values() doesn't depend on factor_name
    or horizon, so evaluate_factors()'s factor x horizon loop passes one in
    to avoid rescanning the full universe on every (factor, horizon) pair.
    Defaults to None (compute per-date here) so standalone callers/tests
    don't have to build the cache themselves."""
    raw_key = FACTOR_KEYS[factor_name]
    out = []
    for date in dates:
        raw = raw_by_date[date] if raw_by_date is not None else _raw_factor_values(universe_data, date)
        if not raw:
            out.append((date, None))
            continue
        factor_values, forward_returns = [], []
        for code, vals in raw.items():
            fr = _forward_return(universe_data[code]["bars"], vals["_idx"], horizon)
            if fr is None:
                continue
            factor_values.append(vals[raw_key])
            forward_returns.append(fr)
        out.append((date, m.rank_ic(factor_values, forward_returns)))
    return out


def evaluate_factors(universe_data, dates=None, horizons=DEFAULT_HORIZONS):
    """{factor_name: {horizon: ic_stats_dict}} for every RANKING_WEIGHTS
    factor x every horizon in `horizons` -- reading one factor across all
    horizons IS its decay curve (docs/.../lighthouse.md Phase 4). `dates`
    defaults to this universe's own month-boundary rebalance dates (see
    rebalance_dates())."""
    dates = dates if dates is not None else rebalance_dates(universe_data)
    # _raw_factor_values() doesn't depend on factor_name/horizon -- compute
    # it once per date and reuse across the full factor x horizon loop below
    # instead of rescanning the whole universe len(FACTOR_KEYS)*len(horizons)
    # times.
    raw_by_date = {date: _raw_factor_values(universe_data, date) for date in dates}
    result = {}
    for factor_name in FACTOR_KEYS:
        result[factor_name] = {}
        for horizon in horizons:
            ic_series = [ic for _, ic in factor_ic_series(universe_data, dates, factor_name, horizon,
                                                            raw_by_date=raw_by_date)]
            result[factor_name][horizon] = m.ic_stats(ic_series)
    return result


def evaluate_candidate_factor(universe_data, series_key, value_key, dates=None, horizons=DEFAULT_HORIZONS):
    """Rank-IC/ICIR/decay for ONE Phase 6b candidate factor (gross/operating
    margin trend, PBR-based value -- see finlab_factors.py's
    gross_margin_trend_series()/operating_margin_trend_series()/
    value_series()) that is NOT one of the required revenue/roe/momentum
    triplet evaluate_factors() above handles. Decoupled from that function
    on purpose: composite_scores()'s "all three factors must be available"
    filter doesn't apply here -- a code simply lacking `series_key` data for
    a given date just doesn't contribute to that date's cross-section,
    rather than being excluded from revenue/roe/momentum's IC too.

    universe_data[code][series_key]: optional [{"usable_date", value_key:
    float}, ...] ascending, same point-in-time shape as revenue_series/
    roe_series -- absent entirely for a code scripts/run_factor_backtest.py
    fetched before this candidate factor existed (Phase 6a-style cache
    entries aren't retroactively upgraded for margin trend -- see that
    script's fetch_universe_data() docstring). `dates` defaults to this
    universe's own month-boundary rebalance dates (see rebalance_dates()).

    Returns {horizon: ic_stats_dict} -- same per-horizon shape as one row of
    evaluate_factors()'s result, so report_factor_ic.py can print it with
    the same _fmt() helper. This function itself never folds a candidate
    into RANKING_WEIGHTS/composite_scores() -- these three Phase 6b
    candidates aren't even wired into suggest_weights()'s FACTOR_KEYS-based
    proposal (that stays revenue/roe/momentum only), so Phase 6d's
    auto-apply pipeline never sees them either; evaluation-only for now."""
    dates = dates if dates is not None else rebalance_dates(universe_data)
    # (bars, idx, val) per liquid/available code, computed ONCE per date and
    # reused across every horizon below -- mirrors evaluate_factors()'s
    # raw_by_date cache; only _forward_return()'s horizon differs per
    # (date, horizon) pair, everything else here is horizon-independent.
    raw_by_date = {}
    for date in dates:
        raw = []
        for d in universe_data.values():
            series = d.get(series_key)
            if not series:
                continue
            bars, bar_dates = d["bars"], d["bar_dates"]
            idx = ff._bar_index_on_or_before(bar_dates, date)
            if idx is None or not ff.liquidity_ok(bars, idx):
                continue
            val = ff._as_of(series, date, value_key)
            if val is None:
                continue
            raw.append((bars, idx, val))
        raw_by_date[date] = raw

    result = {}
    for horizon in horizons:
        ic_series = []
        for date in dates:
            factor_values, forward_returns = [], []
            for bars, idx, val in raw_by_date[date]:
                fr = _forward_return(bars, idx, horizon)
                if fr is None:
                    continue
                factor_values.append(val)
                forward_returns.append(fr)
            ic_series.append(m.rank_ic(factor_values, forward_returns))
        result[horizon] = m.ic_stats(ic_series)
    return result


def suggest_weights(evaluate_result, horizon=20, min_icir=0.0):
    """A RANKING_WEIGHTS proposal, not a decision -- this function itself
    never applies anything. Weights each factor proportional to |ICIR| at
    `horizon` (20d matches this project's other "hold ~1 month" default, see
    backtest.CONFIDENCE_HORIZON) among factors whose |ICIR| clears
    `min_icir`. Returns None if no factor clears the bar -- weighting by
    noise would be worse than keeping the current fixed weights, so this
    function refuses to guess rather than return a misleading answer.

    Phase 6d (docs/https-finlab-finance-blog-python-get-tai-memoized-
    lighthouse.md, docs/adr/0002-auto-apply-ranking-weights.md) DOES apply
    this proposal automatically on a schedule -- scripts/run_ranking_optimize.py
    feeds this function's output into src/optimize.py's
    sweep_ranking_weights_walk_forward() + resolve_ranking_weights_action(),
    which only adopts it after it wins a purged walk-forward comparison AND
    clears DSR/PBO/plateau gates (and can auto-rollback the next cycle). "No
    human in the loop by default" is that pipeline's decision, not this
    function's -- this function stays a pure proposal generator either way."""
    icirs = {}
    for factor_name, by_horizon in evaluate_result.items():
        icir = by_horizon.get(horizon, {}).get("icir")
        if icir is not None and abs(icir) > min_icir:
            icirs[factor_name] = abs(icir)
    total = sum(icirs.values())
    if not total:
        return None
    return {k: v / total for k, v in icirs.items()}
