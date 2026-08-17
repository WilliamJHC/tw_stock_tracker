# -*- coding: utf-8 -*-
"""Shared backtest performance metrics (pure stdlib).

Consolidates the max-drawdown/return-summary logic duplicated between
backtest.simulate() and portfolio_backtest._compute_metrics(), and adds
the metrics neither of those needed yet: Sharpe/Sortino, exposure
(time-in-market), turnover, rank-IC (for Phase 4's factor evaluation),
and Deflated Sharpe Ratio / PBO (for Phase 5's overfitting guardrails).
See docs/backtest_plan.md Phase 0.

Only math/statistics/itertools -- math.erf gives the normal CDF, Newton's
method inverts it (no scipy), matching the project's stdlib-only policy.
"""
import itertools
import math
import statistics

EULER_MASCHERONI = 0.5772156649


# ------------------------------------------------------------- returns ----

def max_drawdown_pct(equities):
    """Largest peak-to-trough decline (%, negative or 0.0) over a value
    series (equity curve or cumulative-return curve). 0.0 if the series
    never dips below its running peak, or has fewer than 2 points."""
    if len(equities) < 2:
        return 0.0
    peak = equities[0]
    max_dd = 0.0
    for e in equities:
        peak = max(peak, e)
        if peak:
            max_dd = min(max_dd, (e - peak) / peak * 100)
    return max_dd


def cagr_pct(start_value, end_value, n_days):
    """Compound annual growth rate (%) over `n_days` calendar days. None
    if the inputs can't produce a meaningful growth rate (non-positive
    value or span)."""
    if start_value <= 0 or n_days <= 0:
        return None
    ratio = end_value / start_value
    if ratio <= 0:
        return None
    return (ratio ** (365 / n_days) - 1) * 100


def sharpe_ratio(returns, rf=0.0, periods_per_year=252):
    """Annualized Sharpe ratio off a period-return series (fractions, e.g.
    0.01 = 1%, not percent). Population std (matches
    portfolio_backtest._compute_metrics' prior inline version). None if
    there are fewer than 2 observations or no variance to divide by."""
    excess = [r - rf for r in returns]
    if len(excess) < 2:
        return None
    mean = sum(excess) / len(excess)
    std = statistics.pstdev(excess)
    if not std:
        return None
    return mean / std * math.sqrt(periods_per_year)


def sortino_ratio(returns, rf=0.0, periods_per_year=252):
    """Like sharpe_ratio() but only penalizes downside deviation (excess
    returns below zero). None if there are fewer than 2 observations or
    no downside deviation to divide by (e.g. every period beat `rf`)."""
    excess = [r - rf for r in returns]
    if len(excess) < 2:
        return None
    downside = [min(0.0, r) for r in excess]
    downside_dev = math.sqrt(sum(d * d for d in downside) / len(downside))
    if not downside_dev:
        return None
    mean = sum(excess) / len(excess)
    return mean / downside_dev * math.sqrt(periods_per_year)


def exposure_pct(days_in_position, total_days):
    """% of the available trading days a strategy held a position (0 =
    always flat, 100 = always in a position). None if total_days is 0."""
    if not total_days:
        return None
    return days_in_position / total_days * 100


def turnover_pct(traded_value, avg_portfolio_value):
    """Sum of buy+sell notional / average portfolio value, as %. A single
    round-trip on the whole portfolio in one period = 200%. None if
    avg_portfolio_value is 0."""
    if not avg_portfolio_value:
        return None
    return traded_value / avg_portfolio_value * 100


# --------------------------------------------------------------- rank-IC ----

def _pct_rank(values):
    """Percentile rank (0..1) of each value within `values`; ties share
    the average rank. Same convention as finlab_factors._pct_rank so
    Phase 4 can reuse either implementation interchangeably."""
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [0.0]
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank / (n - 1)
        i = j + 1
    return ranks


def _pearson(xs, ys):
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx == 0 or vy == 0:
        return None
    return cov / math.sqrt(vx * vy)


def rank_ic(factor_values, forward_returns):
    """Spearman rank correlation between a factor's cross-sectional values
    and forward returns at one rebalance date -- the IC (Information
    Coefficient) for that date. None if there's not enough spread to
    correlate (n<2, or every factor/return value tied)."""
    if len(factor_values) != len(forward_returns) or len(factor_values) < 2:
        return None
    fr = _pct_rank(factor_values)
    rr = _pct_rank(forward_returns)
    if statistics.pstdev(fr) == 0 or statistics.pstdev(rr) == 0:
        return None
    return _pearson(fr, rr)


def ic_stats(ic_series):
    """Mean IC, t-stat, and ICIR (mean / std) across a series of per-date
    rank_ic() values. Drops None entries (dates with no cross-sectional
    spread). n<2 surviving values -> all None (can't estimate a std)."""
    ics = [x for x in ic_series if x is not None]
    n = len(ics)
    if n < 2:
        return {"mean_ic": None, "t_stat": None, "icir": None, "n": n}
    mean_ic = sum(ics) / n
    std_ic = statistics.stdev(ics)
    t_stat = mean_ic / (std_ic / math.sqrt(n)) if std_ic else None
    icir = mean_ic / std_ic if std_ic else None
    return {"mean_ic": mean_ic, "t_stat": t_stat, "icir": icir, "n": n}


# ------------------------------------------------- overfitting guardrails ----

def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _norm_ppf(p, tol=1e-10, max_iter=100):
    """Inverse standard normal CDF via Newton's method (no scipy)."""
    if not 0 < p < 1:
        raise ValueError("p must be in (0, 1)")
    x = 0.0
    for _ in range(max_iter):
        err = _norm_cdf(x) - p
        if abs(err) < tol:
            break
        density = math.exp(-x * x / 2) / math.sqrt(2 * math.pi)
        if density < 1e-12:
            break
        x -= err / density
    return x


def deflated_sharpe_ratio(observed_sharpe, n_trials, n_obs, skew=0.0, kurtosis=3.0,
                           trial_sharpe_std=1.0):
    """Probabilistic Sharpe Ratio of `observed_sharpe` against the expected
    maximum Sharpe of `n_trials` random/noise strategies (Bailey & Lopez de
    Prado 2014's Deflated Sharpe Ratio). Returns a probability in [0, 1]
    that observed_sharpe genuinely beats a skill-less null, after
    accounting for having picked the best of `n_trials` tries -- more
    trials raises the bar (see docs/backtest_plan.md Phase 5's "少試就是
    少罰"). None if there's fewer than 1 trial or 2 observations.

    trial_sharpe_std: cross-trial std of Sharpe ratios (in the same units
    as observed_sharpe); defaults to 1.0 for an uncalibrated null when the
    actual distribution of trial Sharpes isn't available. Pass the real
    value (std of Sharpes across all `n_trials` runs) for a calibrated
    result -- see optimization_runs in Phase 5.
    """
    if n_trials < 1 or n_obs < 2:
        return None
    if n_trials == 1:
        sr0 = 0.0
    else:
        sr0 = trial_sharpe_std * (
            (1 - EULER_MASCHERONI) * _norm_ppf(1 - 1 / n_trials)
            + EULER_MASCHERONI * _norm_ppf(1 - 1 / (n_trials * math.e))
        )
    sigma_sr_sq = (
        1 - skew * observed_sharpe + (kurtosis - 1) / 4 * observed_sharpe ** 2
    ) / (n_obs - 1)
    if sigma_sr_sq <= 0:
        return None
    sigma_sr = math.sqrt(sigma_sr_sq)
    return _norm_cdf((observed_sharpe - sr0) / sigma_sr)


def pbo(returns_matrix, n_blocks=8):
    """Probability of Backtest Overfitting via Combinatorially Symmetric
    Cross-Validation (Bailey, Borwein, Lopez de Prado & Zhu 2015).

    returns_matrix: {trial_id: [period_return, ...]} -- every trial's
    return series, same length, aligned in time (e.g. one column per
    parameter combo tried in Phase 5's optimizer).

    Splits the timeline into `n_blocks` contiguous blocks. For each way of
    choosing half the blocks as in-sample (C(n_blocks, n_blocks/2)
    combinations): ranks trials by in-sample Sharpe, takes the IS winner,
    then checks that winner's Sharpe rank on the complementary out-of-
    sample half. If the IS winner ranks below the OOS median, that
    combination is flagged as overfit (the in-sample "best" pick didn't
    hold up out of sample).

    Returns flagged / total combinations, in [0, 1]. None if fewer than
    2 trials, or the series is too short to split into `n_blocks` blocks.
    """
    trial_ids = list(returns_matrix)
    if len(trial_ids) < 2:
        return None
    length = len(returns_matrix[trial_ids[0]])
    if any(len(returns_matrix[t]) != length for t in trial_ids):
        raise ValueError("all trial return series must be the same length")
    block_size = length // n_blocks
    if block_size < 1:
        return None
    blocks = [
        range(i * block_size, (i + 1) * block_size if i < n_blocks - 1 else length)
        for i in range(n_blocks)
    ]

    combos = list(itertools.combinations(range(n_blocks), n_blocks // 2))
    if not combos:
        return None

    flagged = 0
    for is_blocks in combos:
        is_idx = [i for b in is_blocks for i in blocks[b]]
        oos_idx = [i for b in range(n_blocks) if b not in is_blocks for i in blocks[b]]

        is_sharpe = {}
        for t in trial_ids:
            s = sharpe_ratio([returns_matrix[t][i] for i in is_idx])
            if s is not None:
                is_sharpe[t] = s
        if not is_sharpe:
            continue
        winner = max(is_sharpe.items(), key=lambda kv: kv[1])[0]

        oos_sharpe = {}
        for t in trial_ids:
            s = sharpe_ratio([returns_matrix[t][i] for i in oos_idx])
            oos_sharpe[t] = s if s is not None else float("-inf")
        oos_ranked = sorted(trial_ids, key=lambda t: oos_sharpe[t])
        winner_rank = oos_ranked.index(winner)
        relative_rank = winner_rank / (len(trial_ids) - 1) if len(trial_ids) > 1 else 0.5
        if relative_rank < 0.5:
            flagged += 1
    return flagged / len(combos)
