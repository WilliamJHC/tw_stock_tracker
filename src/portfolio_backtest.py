# -*- coding: utf-8 -*-
"""Monthly-rebalanced, multi-position portfolio backtest for the FinMind
port of finlab.finance's AI-agent guide ranking strategy (src/finlab_factors.py).

Pure functions over already-fetched per-code data -- no I/O, no FinMind, no
DB access, same split as backtest.py. backtest.py's simulate() is
single-position/single-code; this module is the portfolio-level
counterpart needed for "hold N names at once, rebalance monthly" (the thing
backtest.py deliberately doesn't do -- see its module docstring).

Trading/valuation price: c_adj when a bar has it (split+dividend back-
adjusted, same convention as finlab_factors.momentum_pct() -- see adjust.py's
module docstring), else falls back to the raw close. This makes the equity
curve a total-return series: cash and stock dividends are captured as part
of the strategy's own return, not just price appreciation, the same way
they already were for the momentum ranking factor. Whole-lot sizing then
operates on adjusted-price-implied quantities rather than literal share
counts once a code has any historical corporate action behind it -- a minor,
accepted approximation at the lot-rounding granularity (same style of
named simplification as lot_size/slippage_pct below).

Simplifications (recorded, not hidden -- every threshold below is a named
product-judgment default):
  - Whole-lot (張, 1000-share) rounding: PORTFOLIO_PARAMS["lot_size"]. Every
    rebalance buy/sell is floored to the nearest whole lot -- real TWSE/TPEx
    fills don't do fractional lots. Leftover cash from the floor just sits
    idle until the next rebalance rather than chasing the last few shares
    (docs/https-finlab-finance-blog-python-get-tai-memoized-lighthouse.md
    Phase 3 item 5 -- this REPLACES the earlier fractional-share
    simplification the module used to have). Set lot_size=0/None to fall
    back to the old exact-fraction sizing (useful for isolating other
    effects in a test).
  - slippage_pct: one-way market-impact/spread cost, applied to the EXECUTED
    fill price (buys fill above the quoted close, sells fill below it).
    target_value_each/diff_value sizing uses the quoted close, but the BUY
    leg's share-count division uses the fill price (buy_fill), not the
    quoted close -- so the resulting cost lands at diff_value regardless of
    slippage. (Sizing off the quoted close then costing off buy_fill used to
    systematically overspend diff_value by the slip fraction; lot_size>0's
    floor-to-lot rounddown almost always hid this with enough slack, but
    lot_size=0 has none, so a full-cash buy would silently fail the cash
    check every time -- fixed 2026-09-10.) Default 0.1% is a product-
    judgment placeholder (not derived from real fill data); same "明確不做"
    Phase 3 item 5 as lot_size.
  - fee_pct is a flat ROUND-TRIP cost (matches backtest.STRATEGY_PARAMS'
    convention: 0.1425% commission x2 + 0.3% tax ~= 0.6%), split evenly
    across the buy leg and the sell leg (0.3% each) rather than modeled as
    separate commission/tax lines.
  - Rebalance date = the trading calendar's first date in each new calendar
    month, matching finlab.backtest.sim(resample="M")'s month-boundary
    convention.
"""
import datetime as dt
import math

import finlab_factors as ff
import metrics as mt

PORTFOLIO_PARAMS = {
    "initial_capital": 1_000_000,  # NTD
    "top_n": ff.RANKING_TOP_N,
    "fee_pct": 0.6,  # round-trip, see module docstring
    "lot_size": 0,  # 0 = odd-lot sizing (exact fraction, see module docstring); product decision 2026-09-10 to avoid whole-lot rounding distorting results at NTD 1M capital
    "slippage_pct": 0.1,  # one-way, see module docstring
    "ranking_weights": None,  # optional RANKING_WEIGHTS override, see finlab_factors.composite_scores()
    "extra_factors": None,  # optional {name: fn(bars, idx)} override, see finlab_factors.composite_scores()
    "rebalance_freq": "monthly",  # "monthly"/"weekly"/"biweekly", see _rebalance_dates() -- docs/factor_timeframe_optimization_directions.md S3.3 candidate, unevaluated, default preserves the original monthly-only behavior
    # % of current portfolio value actually deployed at each rebalance; the
    # rest sits idle as `cash` until the next rebalance re-evaluates it. 100
    # (invest everything, the pre-existing behavior) everywhere by default --
    # see PORTFOLIO_PARAMS_BY_REGIME and simulate()'s regime_by_date param
    # for how a caller tests a regime-conditional discount without changing
    # this global.
    "invest_pct": 100,
}

# Regime-conditional overrides on top of PORTFOLIO_PARAMS, same layering
# convention as backtest.STRATEGY_PARAMS_BY_REGIME (see that module for the
# rationale: only a regime with real walk-forward evidence to deviate gets an
# entry here). Empty until a candidate invest_pct discount actually clears
# src/optimize.py's DSR/PBO/plateau gates for some regime -- see
# docs/regime_strategy_research.md's methodology and simulate()'s
# `by_regime` parameter for how a candidate is tested first without editing
# this table.
PORTFOLIO_PARAMS_BY_REGIME = {}


def _floor_to_lot(shares, lot_size):
    """Round down to the nearest whole lot. lot_size falsy (0/None) skips
    rounding -- see module docstring's fractional-sizing fallback."""
    if not lot_size:
        return shares
    return math.floor(shares / lot_size) * lot_size


def _trading_calendar(universe_data):
    """Union of all bar dates across the universe, sorted ascending."""
    dates = set()
    for d in universe_data.values():
        dates.update(d["bar_dates"])
    return sorted(dates)


def _rebalance_dates(calendar, freq="monthly"):
    """First trading-calendar date of each period. freq="monthly" (default,
    original behavior): calendar month (y-m). freq="weekly": ISO calendar
    week (year, week). freq="biweekly": every other ISO calendar week,
    grouped by (year, week // 2) -- a deterministic function of the
    calendar, not a rolling N-trading-day counter, so it lines up with the
    same "period boundary" idea monthly already uses rather than being
    sensitive to holidays shifting a fixed-count window. See
    docs/factor_timeframe_optimization_directions.md S3.3 -- this is an
    unevaluated candidate dimension, not a default change; simulate()'s
    PORTFOLIO_PARAMS["rebalance_freq"] stays "monthly" until a walk-forward
    report justifies otherwise."""
    if freq == "monthly":
        key_fn = lambda d: d[:7]
    elif freq == "weekly":
        key_fn = lambda d: dt.date.fromisoformat(d).isocalendar()[:2]
    elif freq == "biweekly":
        def key_fn(d):
            iso_year, iso_week, _ = dt.date.fromisoformat(d).isocalendar()
            return (iso_year, iso_week // 2)
    else:
        raise ValueError(f"_rebalance_dates: unknown freq {freq!r}")
    out, seen = [], set()
    for date in calendar:
        key = key_fn(date)
        if key not in seen:
            seen.add(key)
            out.append(date)
    return out


def _mark_to_market(cash, holdings, prices, bar_dates, date):
    """Values each holding at its latest price on or before `date`, not at
    `date` exactly -- a held code with no print today (halt, thin trading)
    would otherwise be zero-filled, producing a one-day equity crash (and a
    matching phantom rebound once it prints again) that corrupts
    daily_returns/Sharpe/max_drawdown, and, on a rebalance date, corrupts
    port_value/target_value_each sizing for every other name that rebalance
    too."""
    value = cash
    for code, shares in holdings.items():
        idx = ff._bar_index_on_or_before(bar_dates[code], date)
        if idx is None:
            continue
        value += shares * prices[code][bar_dates[code][idx]]
    return value


def _resolve_invest_pct(explicit_override, regime=None, by_regime=None):
    """PORTFOLIO_PARAMS["invest_pct"], layered with
    PORTFOLIO_PARAMS_BY_REGIME[regime]["invest_pct"] (if `regime` names one
    with that key), then by_regime[regime]["invest_pct"] as an ADDITIONAL
    layer on top of that (if given and it also names one), and then
    explicit_override["invest_pct"] (if given) -- same layering precedence
    as backtest._resolve_params()'s by_regime, scoped to just this one knob
    since every other PORTFOLIO_PARAMS key stays fixed for the whole
    simulation (see simulate()'s `p` built once up top).

    by_regime: a caller passes its own dict here to test a candidate discount
    without mutating the module global (mirrors backtest.simulate()'s
    `by_regime` parameter) -- layered on top of, not instead of,
    PORTFOLIO_PARAMS_BY_REGIME, so a candidate can override this one knob
    without needing to also restate whatever else that table might carry
    for the same regime in the future."""
    value = PORTFOLIO_PARAMS["invest_pct"]
    if regime and regime in PORTFOLIO_PARAMS_BY_REGIME and "invest_pct" in PORTFOLIO_PARAMS_BY_REGIME[regime]:
        value = PORTFOLIO_PARAMS_BY_REGIME[regime]["invest_pct"]
    if regime and by_regime and regime in by_regime and "invest_pct" in by_regime[regime]:
        value = by_regime[regime]["invest_pct"]
    if explicit_override and "invest_pct" in explicit_override:
        value = explicit_override["invest_pct"]
    return value


def simulate(universe_data, params=None, ranks_cache=None, regime_by_date=None, by_regime=None):
    """Monthly-rebalanced, equal-weight top-N portfolio backtest.

    universe_data: {code: {"bars": [{date,c,v,...}, ...], "bar_dates": [...],
    "revenue_series": [...], "roe_series": [...]}} -- same shape
    finlab_factors.composite_scores() expects.

    regime_by_date: optional {date: regime} (e.g. dict(market_regime.
    regime_series(taiex_bars)) -- see src/market_regime.py) -- when given,
    EVERY rebalance resolves its invest_pct via _resolve_invest_pct(params,
    regime, by_regime) using THAT rebalance date's regime, instead of one
    fixed invest_pct for the whole run. None (the default, unchanged
    behavior) invests params["invest_pct"] (100 unless overridden) at every
    rebalance regardless of regime.

    by_regime: forwarded to _resolve_invest_pct() in place of the module-
    level PORTFOLIO_PARAMS_BY_REGIME -- lets a walk-forward sweep test a
    candidate regime-conditional invest_pct without mutating that global.
    None (the default) uses PORTFOLIO_PARAMS_BY_REGIME, unchanged behavior.

    ranks_cache: optional {date: factor_ranks(universe_data, date,
    extra_factors=...) result} dict, mutated in place as a memoization
    cache. finlab_factors.factor_ranks() doesn't depend on
    params["ranking_weights"] at all (only on universe_data/date/
    extra_factors) -- src/optimize.py's sweep_ranking_weights_walk_forward()
    calls this function once per NAMED CANDIDATE per fold, all sharing the
    same universe_data/extra_factors for that fold, so without this cache
    every candidate independently recomputes the identical liquidity-filter
    + percentile-rank pass at every rebalance date. A caller passes one
    shared dict across those calls to reuse it; None (the default) disables
    caching entirely, unchanged behavior for every other caller (this
    module's own tests, scripts/report_backtest.py, etc.) and safe for a
    caller whose universe_data/extra_factors DIFFER between simulate()
    calls (a shared cache would silently be wrong there -- see
    sweep_ranking_weights_walk_forward()'s per-fold scoping, which creates
    a fresh cache per fold rather than reusing one across folds, since
    "rolling" mode slices universe_data differently per fold).

    Returns {equity_curve: [{date, equity}], trades: [...], summary: {...}}.
    summary is {} if there's no usable trading calendar at all.
    """
    p = dict(PORTFOLIO_PARAMS)
    if params:
        p.update(params)
    leg_fee = p["fee_pct"] / 2 / 100
    slip = p.get("slippage_pct", 0.0) / 100
    lot_size = p.get("lot_size", 0)

    calendar = _trading_calendar(universe_data)
    if not calendar:
        return {"equity_curve": [], "trades": [], "summary": {}}
    rebalance_dates = set(_rebalance_dates(calendar, freq=p.get("rebalance_freq", "monthly")))

    prices = {
        code: {b["date"]: (b["c_adj"] if b.get("c_adj") is not None else b["c"]) for b in d["bars"]}
        for code, d in universe_data.items()
    }
    bar_dates = {code: d["bar_dates"] for code, d in universe_data.items()}

    cash = p["initial_capital"]
    holdings = {}  # code -> shares, whole-lot multiples once lot_size>0 (see module docstring)
    equity_curve, trades = [], []

    for date in calendar:
        if date in rebalance_dates:
            ranks = None
            if ranks_cache is not None:
                ranks = ranks_cache.get(date)
                if ranks is None:
                    ranks = ff.factor_ranks(universe_data, date, extra_factors=p.get("extra_factors"))
                    ranks_cache[date] = ranks
            scores = ff.composite_scores(
                universe_data, date, weights=p["ranking_weights"], ranks=ranks, extra_factors=p.get("extra_factors")
            )
            target_codes = set(ff.top_n(scores, p["top_n"]))

            for code in [c for c in holdings if c not in target_codes]:
                px = prices[code].get(date)
                if not px:
                    continue  # halted/no print today -- carry the position, retry next rebalance
                sell_fill = px * (1 - slip)
                proceeds = holdings[code] * sell_fill * (1 - leg_fee)
                cash += proceeds
                trades.append({"date": date, "code": code, "side": "sell", "price": sell_fill, "shares": holdings[code]})
                del holdings[code]

            port_value = _mark_to_market(cash, holdings, prices, bar_dates, date)
            if target_codes:
                # No regime_by_date -> no regime ever resolves -> neither
                # PORTFOLIO_PARAMS_BY_REGIME nor by_regime could apply
                # regardless, so p["invest_pct"] (already resolved once, up
                # top) is exactly what _resolve_invest_pct() would return
                # here -- skip the redundant per-rebalance lookup.
                invest_pct = (_resolve_invest_pct(params, regime_by_date.get(date), by_regime)
                              if regime_by_date is not None else p["invest_pct"])
                target_value_each = port_value * (invest_pct / 100) / len(target_codes)
                # Sorted, not set-order: a plain `for code in target_codes`
                # iterates in Python's per-run-randomized string hash order,
                # so later-iterated codes could get silently starved of cash
                # by the fee spent on earlier ones -- same target_value_each
                # would then buy a different, non-reproducible subset of
                # names each run. buy_shares below also divides by (1 +
                # leg_fee) so cost lands exactly on target_value_each
                # instead of overshooting it by the fee amount, which was
                # the actual source of the cash shortfall.
                for code in sorted(target_codes):
                    px = prices[code].get(date)
                    if not px:
                        continue
                    current_shares = holdings.get(code, 0)
                    diff_value = target_value_each - current_shares * px
                    if diff_value > 0:
                        buy_fill = px * (1 + slip)
                        # Sizing divides by buy_fill (not px) so cost below lands at
                        # diff_value regardless of slippage. Dividing by px here used to
                        # systematically overspend diff_value by the slip fraction (cost
                        # uses buy_fill, sizing didn't) -- with lot_size>0 the floor-to-
                        # lot rounddown almost always left enough slack to absorb that,
                        # but lot_size=0 has no such slack: a full-cash buy (e.g. small
                        # top_n, or a later code in the same rebalance left little cash
                        # by an earlier one's overspend) would then fail the cost<=cash
                        # check below and get silently skipped every time -- see tests
                        # this bug surfaced when lot_size=0 became the default.
                        buy_shares = _floor_to_lot(diff_value / (buy_fill * (1 + leg_fee)), lot_size)
                        if buy_shares <= 0:
                            continue  # too small for even one lot -- cash sits idle till next rebalance
                        cost = buy_shares * buy_fill * (1 + leg_fee)
                        if cost <= cash:
                            cash -= cost
                            holdings[code] = current_shares + buy_shares
                            trades.append({"date": date, "code": code, "side": "buy", "price": buy_fill, "shares": buy_shares})
                    elif diff_value < 0:
                        sell_fill = px * (1 - slip)
                        sell_shares = min(_floor_to_lot(-diff_value / px, lot_size), current_shares)
                        if sell_shares <= 0:
                            continue
                        cash += sell_shares * sell_fill * (1 - leg_fee)
                        holdings[code] = current_shares - sell_shares
                        trades.append({"date": date, "code": code, "side": "sell", "price": sell_fill, "shares": sell_shares})

        equity_curve.append({"date": date, "equity": _mark_to_market(cash, holdings, prices, bar_dates, date)})

    return {
        "equity_curve": equity_curve,
        "trades": trades,
        "summary": _compute_metrics(equity_curve, p["initial_capital"]),
    }


def _compute_metrics(equity_curve, initial_capital):
    """CAGR / Sharpe / max drawdown off the daily equity curve, via
    metrics.py's shared implementations (no more duplicating that logic
    here -- see metrics.py's module docstring). Sharpe uses a 0% risk-free
    rate: strategy is a long-only relative comparison, 0 is a conservative,
    easily-adjusted default (matches backtest.py's convention)."""
    if len(equity_curve) < 2:
        return {}
    equities = [row["equity"] for row in equity_curve]
    dates = [row["date"] for row in equity_curve]

    daily_returns = [
        equities[i] / equities[i - 1] - 1
        for i in range(1, len(equities)) if equities[i - 1]
    ]

    start = dt.datetime.strptime(dates[0], "%Y-%m-%d")
    end = dt.datetime.strptime(dates[-1], "%Y-%m-%d")
    n_days = (end - start).days or 1
    cagr = mt.cagr_pct(initial_capital, equities[-1], n_days)

    return {
        "start_date": dates[0],
        "end_date": dates[-1],
        "total_return_pct": (equities[-1] / initial_capital - 1) * 100,
        "cagr_pct": cagr,
        "sharpe": mt.sharpe_ratio(daily_returns),
        "max_drawdown_pct": mt.max_drawdown_pct(equities),
    }
