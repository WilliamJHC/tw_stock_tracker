# -*- coding: utf-8 -*-
"""Monthly-rebalanced, multi-position portfolio backtest for the FinMind
port of finlab.finance's AI-agent guide ranking strategy (src/finlab_factors.py).

Pure functions over already-fetched per-code data -- no I/O, no FinMind, no
DB access, same split as backtest.py. backtest.py's simulate() is
single-position/single-code; this module is the portfolio-level
counterpart needed for "hold N names at once, rebalance monthly" (the thing
backtest.py deliberately doesn't do -- see its module docstring).

Simplifications (recorded, not hidden -- see docs/quant_upgrade_plan.md's
convention of naming every threshold as a product-judgment default):
  - Fractional shares. Real trading only fills whole 1000-share lots; this
    engine sizes each name to an equal fraction of portfolio value exactly.
    Mirrors how the FinLab original also backtests on % weights under the
    hood, not literal share counts. Lot rounding is a real future gap, not
    modeled here.
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

PORTFOLIO_PARAMS = {
    "initial_capital": 1_000_000,  # NTD
    "top_n": ff.RANKING_TOP_N,
    "fee_pct": 0.6,  # round-trip, see module docstring
}


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


def _mark_to_market(cash, holdings, prices, date):
    return cash + sum(shares * prices[code].get(date, 0) for code, shares in holdings.items())


def simulate(universe_data, params=None):
    """Monthly-rebalanced, equal-weight top-N portfolio backtest.

    universe_data: {code: {"bars": [{date,c,v,...}, ...], "bar_dates": [...],
    "revenue_series": [...], "roe_series": [...]}} -- same shape
    finlab_factors.composite_scores() expects.

    Returns {equity_curve: [{date, equity}], trades: [...], summary: {...}}.
    summary is {} if there's no usable trading calendar at all.
    """
    p = dict(PORTFOLIO_PARAMS)
    if params:
        p.update(params)
    leg_fee = p["fee_pct"] / 2 / 100

    calendar = _trading_calendar(universe_data)
    if not calendar:
        return {"equity_curve": [], "trades": [], "summary": {}}
    rebalance_dates = set(_rebalance_dates(calendar))

    prices = {code: {b["date"]: b["c"] for b in d["bars"]} for code, d in universe_data.items()}

    cash = p["initial_capital"]
    holdings = {}  # code -> shares (fractional, see module docstring)
    equity_curve, trades = [], []

    for date in calendar:
        if date in rebalance_dates:
            scores = ff.composite_scores(universe_data, date)
            target_codes = set(ff.top_n(scores, p["top_n"]))

            for code in [c for c in holdings if c not in target_codes]:
                px = prices[code].get(date)
                if not px:
                    continue  # halted/no print today -- carry the position, retry next rebalance
                proceeds = holdings[code] * px * (1 - leg_fee)
                cash += proceeds
                trades.append({"date": date, "code": code, "side": "sell", "price": px, "shares": holdings[code]})
                del holdings[code]

            port_value = _mark_to_market(cash, holdings, prices, date)
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
                        buy_shares = diff_value / (px * (1 + leg_fee))
                        cost = buy_shares * px * (1 + leg_fee)
                        if cost <= cash:
                            cash -= cost
                            holdings[code] = current_shares + buy_shares
                            trades.append({"date": date, "code": code, "side": "buy", "price": px, "shares": buy_shares})
                    elif diff_value < 0:
                        sell_shares = -diff_value / px
                        cash += sell_shares * px * (1 - leg_fee)
                        holdings[code] = current_shares - sell_shares
                        trades.append({"date": date, "code": code, "side": "sell", "price": px, "shares": sell_shares})

        equity_curve.append({"date": date, "equity": _mark_to_market(cash, holdings, prices, date)})

    return {
        "equity_curve": equity_curve,
        "trades": trades,
        "summary": _compute_metrics(equity_curve, p["initial_capital"]),
    }


def _compute_metrics(equity_curve, initial_capital):
    """CAGR / Sharpe / max drawdown off the daily equity curve. Sharpe uses
    a 0% risk-free rate (see backtest_plan-style rationale in
    docs/quant_upgrade_plan.md Phase 1: strategy is long-only relative
    comparison, 0 is a conservative, easily-adjusted default)."""
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
    final_over_initial = equities[-1] / initial_capital
    cagr = final_over_initial ** (365 / n_days) - 1 if final_over_initial > 0 else None

    mean_ret = sum(daily_returns) / len(daily_returns) if daily_returns else 0
    variance = sum((r - mean_ret) ** 2 for r in daily_returns) / len(daily_returns) if daily_returns else 0
    std_ret = math.sqrt(variance)
    sharpe = (mean_ret / std_ret * math.sqrt(252)) if std_ret else None

    peak, max_dd = equities[0], 0.0
    for e in equities:
        peak = max(peak, e)
        if peak:
            max_dd = min(max_dd, (e - peak) / peak)

    return {
        "start_date": dates[0],
        "end_date": dates[-1],
        "total_return_pct": (final_over_initial - 1) * 100,
        "cagr_pct": cagr * 100 if cagr is not None else None,
        "sharpe": sharpe,
        "max_drawdown_pct": max_dd * 100,
    }
