# -*- coding: utf-8 -*-
"""Formal purged walk-forward test for the accrual_quality candidate factor
that passed pooled rank-IC significance in scripts/report_factor_ic.py
(h=5 t=-2.92, h=20 t=-3.83, h=60 t=-4.78, all icir magnitude > 0.4 -- see
docs/factor_ic_candidate_and_momentum_window_report.md S2.3). Same
motivation/method as scripts/report_value_walkforward.py: a pooled IC can't
rule out a lucky sub-period, so this reruns it as an honest OOS test across
purged, non-overlapping time folds through src/optimize.py's existing
DSR/PBO overfitting guards.

Sign: finlab_factors.accrual_quality_series() deliberately does NOT flip
accrual_ratio's sign (see its docstring -- the accrual anomaly's sign in
this market was unevaluated at the time it was written). The pooled test
above found it NEGATIVE (higher accrual_ratio = worse forward return,
matching the Sloan (1996) accrual-anomaly direction), so this script scores
the candidate on -accrual_ratio (higher score = better), the same
"flip-then-rank" convention value_series() already applies to -pbr
internally.

Deliberately does NOT reuse portfolio_backtest.simulate(), for the same
reason report_value_walkforward.py gives (accrual_ratio is a date-keyed
lookup via _as_of(), not a bars/idx-only extra_factor).

Usage:
    python scripts/report_accrual_walkforward.py [--horizon 60] [--folds 4]
"""
import argparse
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import factor_eval as fe  # noqa: E402
import finlab_factors as ff  # noqa: E402
import metrics as m  # noqa: E402
import optimize as opt  # noqa: E402
import portfolio_backtest as pb  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from report_factor_ic import load_cached_universe  # noqa: E402

# (name, {"revenue","roe","momentum","accrual"} weights summing to 1.0) --
# baseline is the live RANKING_WEIGHTS unchanged; the other two are the
# smallest deviations that test "does adding accrual_quality help" (same
# framing as report_value_walkforward.py's CANDIDATES).
CANDIDATES = {
    "baseline (無 accrual)": {"revenue": 0.4, "roe": 0.3, "momentum": 0.3},
    "accrual 15% (等比例稀釋)": {"revenue": 0.34, "roe": 0.255, "momentum": 0.255, "accrual": 0.15},
    "accrual 30% (等比例稀釋)": {"revenue": 0.28, "roe": 0.21, "momentum": 0.21, "accrual": 0.3},
}

N_FOLDS = 4
PURGE_DAYS = 60  # same as optimize.purged_calendar_walk_forward_splits() default


def _raw_with_accrual(universe_data, date):
    """{code: {"revenue","roe","momentum","accrual": raw float}} at `date`
    -- same liquidity filter as finlab_factors.factor_ranks(), additionally
    requires accrual_quality_series availability so every CANDIDATES entry
    is scored against the identical cross-section."""
    raw = {}
    for code, d in universe_data.items():
        bars, bar_dates = d["bars"], d["bar_dates"]
        idx = ff._bar_index_on_or_before(bar_dates, date)
        if idx is None or not ff.liquidity_ok(bars, idx):
            continue
        mom = ff.momentum_pct(bars, idx)
        yoy = ff._as_of(d.get("revenue_series", []), date, "yoy_pct")
        roe = ff._as_of(d.get("roe_series", []), date, "roe_pct")
        accrual_ratio = ff._as_of(d.get("accrual_quality_series", []), date, "accrual_ratio")
        if mom is None or yoy is None or roe is None or accrual_ratio is None:
            continue
        # flip sign here -- see module docstring
        raw[code] = {"revenue": yoy, "roe": roe, "momentum": mom, "accrual": -accrual_ratio, "bars": bars, "idx": idx}
    return raw


def _composite_scores_at(raw, weights):
    """{code: weighted percentile-rank score} -- same pct-rank-then-weight
    order as finlab_factors.composite_scores()."""
    ranks = {}
    for key in ("revenue", "roe", "momentum", "accrual"):
        if key not in weights:
            continue
        ranks[key] = ff._pct_rank({c: v[key] for c, v in raw.items()})
    return {
        code: sum(ranks[key][code] * w for key, w in weights.items())
        for code in raw
    }


def _fold_ic(universe_data, dates, weights, horizon):
    """Mean rank-IC of one candidate's composite score against `horizon`-bar
    forward return, pooled across `dates` (one fold's test-window rebalance
    dates)."""
    ic_series = []
    for date in dates:
        raw = _raw_with_accrual(universe_data, date)
        if not raw:
            continue
        scores = _composite_scores_at(raw, weights)
        factor_values, forward_returns = [], []
        for code, score in scores.items():
            fr = fe._forward_return(raw[code]["bars"], raw[code]["idx"], horizon)
            if fr is None:
                continue
            factor_values.append(score)
            forward_returns.append(fr)
        ic_series.append(m.rank_ic(factor_values, forward_returns))
    return m.ic_stats(ic_series)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--horizon", type=int, default=60)
    ap.add_argument("--folds", type=int, default=N_FOLDS)
    args = ap.parse_args()

    universe_data = load_cached_universe()
    print(f"{len(universe_data)} codes loaded")
    if not universe_data:
        print("nothing cached yet -- run scripts/run_factor_backtest.py --universe all first")
        return

    calendar = pb._trading_calendar(universe_data)
    folds = opt.purged_calendar_walk_forward_splits(calendar, n_folds=args.folds, purge_days=PURGE_DAYS, mode="anchored")
    if not folds:
        print("calendar too short for even one purged fold")
        return

    all_dates = fe.rebalance_dates(universe_data)
    names = list(CANDIDATES)
    scores_by_name = {name: [] for name in names}
    fold_argmax = []
    n_trials = 0

    print(f"\n{args.folds} purged folds (anchored, purge_days={PURGE_DAYS}), horizon={args.horizon}\n")
    for fold_idx, fold in enumerate(folds):
        test_start, test_end = fold["test"]
        fold_dates = [d for d in all_dates if test_start <= d <= test_end]
        print(f"fold {fold_idx}: test={test_start}..{test_end} ({len(fold_dates)} rebalance dates)")
        if not fold_dates:
            continue
        fold_scores = {}
        fold_best_name, fold_best_ic = None, float("-inf")
        for name in names:
            stats = _fold_ic(universe_data, fold_dates, CANDIDATES[name], args.horizon)
            ic = stats["mean_ic"]
            n_trials += 1
            fold_scores[name] = ic
            print(f"  {name:<28} mean_ic={ic if ic is None else round(ic, 4)}  n={stats['n']}")
            ic_for_argmax = ic if ic is not None else float("-inf")
            if ic_for_argmax > fold_best_ic:
                fold_best_name, fold_best_ic = name, ic_for_argmax
        if fold_best_name is not None:
            fold_argmax.append(fold_best_name)
            for name in names:
                scores_by_name[name].append(fold_scores[name])

    usable_folds = len(fold_argmax)
    print(f"\nusable_folds={usable_folds}")
    if usable_folds < 2:
        print("usable_folds < 2 -- can't compute PBO/DSR (see metrics.pbo()/deflated_sharpe_ratio())")
        return

    mean_scores = {}
    for name in names:
        valid = [s for s in scores_by_name[name] if s is not None]
        mean_scores[name] = sum(valid) / len(valid) if valid else None
    print("\nOOS mean IC per candidate (across usable folds):")
    for name in names:
        print(f"  {name:<28} {mean_scores[name]}")

    argmax_counts = {}
    for name in fold_argmax:
        argmax_counts[name] = argmax_counts.get(name, 0) + 1
    winner, winner_count = max(argmax_counts.items(), key=lambda kv: kv[1])
    print(f"\nper-fold winner counts: {argmax_counts}")
    print(f"plateau: '{winner}' won {winner_count}/{usable_folds} folds ({winner_count / usable_folds:.0%})")

    returns_matrix = {name: [(s if s is not None else 0.0) for s in scores_by_name[name]] for name in names}
    pbo_value = m.pbo(returns_matrix, n_blocks=usable_folds)
    chosen_returns = returns_matrix[winner]
    sharpe = m.sharpe_ratio(chosen_returns, periods_per_year=usable_folds)
    trial_sharpes = [s for s in (
        m.sharpe_ratio(r, periods_per_year=usable_folds) for r in returns_matrix.values()
    ) if s is not None]
    trial_std = statistics.pstdev(trial_sharpes) if len(trial_sharpes) > 1 else 1.0
    dsr_value = None
    if sharpe is not None:
        dsr_value = m.deflated_sharpe_ratio(sharpe, n_trials=n_trials, n_obs=usable_folds, trial_sharpe_std=trial_std or 1.0)

    print(f"\nPBO (probability of overfitting, over {usable_folds} blocks): {pbo_value}")
    print(f"DSR (deflated Sharpe of winner '{winner}', n_trials={n_trials}, n_obs={usable_folds}): {dsr_value}")
    print(f"\ndecision gate (src/optimize.py's PBO_MAX={opt.PBO_MAX}, DSR_MIN_SIGNIFICANT={opt.DSR_MIN_SIGNIFICANT}):")
    reasons = []
    if pbo_value is not None and pbo_value > opt.PBO_MAX:
        reasons.append(f"PBO {pbo_value:.2f} > {opt.PBO_MAX} -- overfitting probability too high")
    if dsr_value is not None and dsr_value < opt.DSR_MIN_SIGNIFICANT:
        reasons.append(f"DSR {dsr_value:.2f} < {opt.DSR_MIN_SIGNIFICANT} -- not significant after trial-count penalty")
    if reasons:
        print("REJECT:")
        for r in reasons:
            print(f"  - {r}")
    else:
        print("PASS -- clears both gates")


if __name__ == "__main__":
    main()
