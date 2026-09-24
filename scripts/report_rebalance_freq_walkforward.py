# -*- coding: utf-8 -*-
"""Purged walk-forward comparison of portfolio_backtest.simulate()'s
rebalance cadence -- docs/factor_timeframe_optimization_directions.md S3.3:
"rebalance 頻率本身從未被系統性測試過". The live pipeline has always
rebalanced monthly (a FinLab-article convention this project inherited, not
something it verified itself); this tests weekly/biweekly against it.

Deliberately does NOT extend src/optimize.py's
sweep_ranking_weights_walk_forward() (which swaps params["ranking_weights"]
per named candidate) to take an arbitrary override key: that function is
live production infrastructure for scripts/run_ranking_optimize.py and
scripts/run_strategy_search.py's ADR 0002/0003 governance pipelines, and
widening its signature to cover an unrelated, unevaluated candidate
dimension is exactly the kind of pipeline surgery
scripts/report_value_walkforward.py's module docstring already argued
against for the same reason. Instead this duplicates that function's
walk-forward loop shape (same purged_calendar_walk_forward_splits() folds,
same OOS-return scoring, same _pbo_and_dsr()/evaluate_decision() gates) with
params["rebalance_freq"] as the swapped key instead of "ranking_weights".
If this ever clears its gates and get adopted, decommissioning THIS script
in favor of a generalized sweep_ranking_weights_walk_forward() would be a
reasonable follow-up refactor -- not done here since nothing has been
adopted yet.

Usage:
    python scripts/report_rebalance_freq_walkforward.py [--folds 4] [--purge-days 60] [--mode anchored]
"""
import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import optimize as opt  # noqa: E402
import portfolio_backtest as pb  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from report_factor_ic import load_cached_universe  # noqa: E402

CANDIDATES = {
    "monthly (現況)": "monthly",
    "biweekly": "biweekly",
    "weekly": "weekly",
}


def sweep_rebalance_freq_walk_forward(universe_data, candidates, n_folds=4, purge_days=60, mode="anchored"):
    """Same shape/return contract as
    optimize.sweep_ranking_weights_walk_forward(), just swapping
    params["rebalance_freq"] per named candidate instead of
    params["ranking_weights"]. See module docstring for why this isn't a
    generalization of that function."""
    calendar = pb._trading_calendar(universe_data)
    folds = opt.purged_calendar_walk_forward_splits(calendar, n_folds=n_folds, purge_days=purge_days, mode=mode)
    if not folds:
        return None

    names = list(candidates)
    scores_by_name = {name: [] for name in names}
    fold_argmax = []
    trials = []
    n_trials = 0
    for fold_idx, fold in enumerate(folds):
        train_start, _ = fold["train"]
        test_start, test_end = fold["test"]
        if mode == "anchored":
            sim_start = calendar[0]
        else:
            buffered = (dt.date.fromisoformat(train_start)
                        - dt.timedelta(days=opt.ROLLING_MOMENTUM_LOOKBACK_BUFFER_DAYS)).isoformat()
            sim_start = max(calendar[0], buffered)
        sliced = opt._slice_universe_by_date(universe_data, sim_start, test_end)
        # A fresh ranks_cache per fold works here same as
        # sweep_ranking_weights_walk_forward()'s -- and rebalance_freq
        # candidates additionally each visit a DIFFERENT set of rebalance
        # dates (weekly's dates are a superset of monthly's), so unlike
        # that function's candidates (which all share the exact same
        # monthly rebalance dates and only differ in weights), sharing one
        # cache ACROSS this fold's `names` loop is still correct
        # (factor_ranks() is keyed by date and doesn't care which candidate
        # asked for a date first) but buys less reuse than in the ranking-
        # weights case, since a monthly candidate's rebalance dates are a
        # strict subset of weekly's.
        ranks_cache = {}
        fold_scores = {}
        fold_best_name, fold_best_score = None, float("-inf")
        for name in names:
            p = {"rebalance_freq": candidates[name]}
            result = pb.simulate(sliced, params=p, ranks_cache=ranks_cache)
            oos_points = [pt for pt in result["equity_curve"] if test_start <= pt["date"] <= test_end]
            score = None
            if len(oos_points) >= 2 and oos_points[0]["equity"]:
                score = (oos_points[-1]["equity"] / oos_points[0]["equity"] - 1) * 100
            n_trials += 1
            trials.append({"fold_idx": fold_idx, "name": name, "score": score})
            fold_scores[name] = score
            score_for_argmax = score if score is not None else float("-inf")
            if score_for_argmax > fold_best_score:
                fold_best_name, fold_best_score = name, score_for_argmax
        if fold_best_name is not None:
            fold_argmax.append(fold_best_name)
            for name in names:
                scores_by_name[name].append(fold_scores[name])

    usable_folds = len(fold_argmax)
    if usable_folds == 0:
        return None

    mean_scores = {}
    for name in names:
        valid = [s for s in scores_by_name[name] if s is not None]
        mean_scores[name] = sum(valid) / len(valid) if valid else None
    valid_means = {n: s for n, s in mean_scores.items() if s is not None}
    point_best_name = max(valid_means, key=valid_means.get) if valid_means else None

    argmax_counts = {}
    for name in fold_argmax:
        argmax_counts[name] = argmax_counts.get(name, 0) + 1
    winner, winner_count = max(argmax_counts.items(), key=lambda kv: kv[1]) if argmax_counts else (None, 0)
    stability = winner_count / usable_folds if usable_folds else 0.0
    plateau = {"most_common": winner, "width_frac": stability}
    chance_threshold = max(opt.PLATEAU_WIDTH_MIN_FRAC, 1 / len(names)) if names else opt.PLATEAU_WIDTH_MIN_FRAC

    pbo_value, dsr_value = opt._pbo_and_dsr(scores_by_name, usable_folds, point_best_name, n_trials)

    return {
        "candidates": names,
        "plateau": plateau,
        "point_best": {"name": point_best_name, "score": valid_means.get(point_best_name)},
        "fold_argmax": fold_argmax,
        "candidate_scores": mean_scores,
        "dsr": dsr_value,
        "pbo": pbo_value,
        "n_trials": n_trials,
        "decision": opt.evaluate_decision(plateau, pbo_value, dsr_value, plateau_width_min_frac=chance_threshold),
        "trials": trials,
        "folds": [{"test": f["test"]} for f in folds],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--purge-days", type=int, default=60)
    ap.add_argument("--mode", choices=["anchored", "rolling"], default="anchored")
    args = ap.parse_args()

    universe_data = load_cached_universe()
    print(f"{len(universe_data)} codes loaded")
    if not universe_data:
        print("nothing cached yet -- run scripts/run_factor_backtest.py --universe all first")
        return

    result = sweep_rebalance_freq_walk_forward(
        universe_data, CANDIDATES, n_folds=args.folds, purge_days=args.purge_days, mode=args.mode)
    if result is None:
        print("calendar too short for even one purged fold")
        return

    print(f"\n{args.folds} purged folds (mode={args.mode}, purge_days={args.purge_days})\n")
    for fold_idx, f in enumerate(result["folds"]):
        print(f"fold {fold_idx}: test={f['test'][0]}..{f['test'][1]}")
        for t in result["trials"]:
            if t["fold_idx"] == fold_idx:
                score = t["score"]
                print(f"  {t['name']:<18} score={score if score is None else round(score, 2)}")

    print(f"\nusable_folds={len(result['fold_argmax'])}")
    print("\nOOS mean return% per candidate (across usable folds):")
    for name in result["candidates"]:
        print(f"  {name:<18} {result['candidate_scores'][name]}")

    argmax_counts = {}
    for name in result["fold_argmax"]:
        argmax_counts[name] = argmax_counts.get(name, 0) + 1
    print(f"\nper-fold winner counts: {argmax_counts}")
    print(f"plateau: '{result['plateau']['most_common']}' won {result['plateau']['width_frac']:.0%} of usable folds")

    print(f"\nPBO (probability of overfitting): {result['pbo']}")
    print(f"DSR (deflated Sharpe of winner '{result['point_best']['name']}', n_trials={result['n_trials']}): {result['dsr']}")
    print(f"\ndecision gate (src/optimize.py's PBO_MAX={opt.PBO_MAX}, DSR_MIN_SIGNIFICANT={opt.DSR_MIN_SIGNIFICANT}):")
    if result["decision"]["accept"]:
        print("PASS -- clears both gates")
    else:
        print("REJECT:")
        for r in result["decision"]["reasons"]:
            print(f"  - {r}")


if __name__ == "__main__":
    main()
