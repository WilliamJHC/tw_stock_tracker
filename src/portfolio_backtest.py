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
  - slippage_pct: one-way market-impact/spread cost, applied only to the
    EXECUTED fill price (buys fill above the quoted close, sells fill
    below it) -- position SIZING (target_value_each, diff_value, how many
    lots to trade) still uses the quoted close, matching how a real order's
    intended size is decided before the fill price is known. Default 0.1%
    is a product-judgment placeholder (not derived from real fill data);
    same "明確不做" Phase 3 item 5 as lot_size.
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
    "lot_size": 1000,  # whole-share-lot (張) rounding, see module docstring
    "slippage_pct": 0.1,  # one-way, see module docstring
    "ranking_weights": None,  # optional RANKING_WEIGHTS override, see finlab_factors.composite_scores()
    "extra_factors": None,  # optional {name: fn(bars, idx)} override, see finlab_factors.composite_scores()
}


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


def _rebalance_dates(calendar):
    """First trading-calendar date of each calendar month."""
    out, seen_months = [], set()
    for date in calendar:
        ym = date[:7]
        if ym not in seen_months:
            seen_months.add(ym)
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


def simulate(universe_data, params=None, ranks_cache=None):
    """Monthly-rebalanced, equal-weight top-N portfolio backtest.

    universe_data: {code: {"bars": [{date,c,v,...}, ...], "bar_dates": [...],
    "revenue_series": [...], "roe_series": [...]}} -- same shape
    finlab_factors.composite_scores() expects.

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
    rebalance_dates = set(_rebalance_dates(calendar))

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
                target_value_each = port_value / len(target_codes)
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
                        # sizing uses the quoted close px, not buy_fill -- see module
                        # docstring's slippage_pct paragraph (sizing decided before the
                        # fill price is known, matching the sell leg's use of px below).
                        buy_shares = _floor_to_lot(diff_value / (px * (1 + leg_fee)), lot_size)
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
