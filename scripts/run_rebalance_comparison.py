# -*- coding: utf-8 -*-
"""Rebalance-frequency comparison: the SAME ranking weights rebalanced
monthly / every 2 months / quarterly, scored through the project's purged
anchored walk-forward (optimize.sweep_ranking_weights_walk_forward) with the
engine's built-in costs (fee_pct 0.6% round trip + 0.1% one-way slippage).

Pre-registered design (fixed BEFORE the first run, do not edit after seeing
results):
  - Exactly 3 candidates: monthly (k=1), bimonthly (k=2), quarterly (k=3).
    Same weights for all; only portfolio_backtest's rebalance_every_months
    differs. No other interval is tried.
  - Data: the TRAIN segment of optimize.train_test_split_by_calendar() (last
    6 calendar months stay held out and are NOT touched here), 4 purged
    anchored folds, same as scripts/run_strategy_search.py.
  - Decision rule: a non-monthly interval X is worth proposing only if ALL
    hold: X is the point_best (highest mean OOS return), X's mean OOS return
    is strictly above monthly's, X is also the per-fold-argmax majority
    winner, and evaluate_decision() accepts (DSR >= 0.95, PBO <= 0.5).
    Otherwise: keep monthly.
  - Caveat printed with the result: the sweep's DSR is computed for
    point_best, not for "the candidate you wish to adopt", and the 4 folds
    are few; an accept/reject is evidence, not proof. Execution-parameter
    change only -- never applied by this script (no DB writes, no Telegram).

--offset N (diagnostic, NOT part of the decision rule) shifts the month grid
by N months, to see how phase-sensitive a result is.

Usage (from the project root; needs cache/factor_run/):
    python scripts/run_rebalance_comparison.py [--weights '{"revenue":0.4,"roe":0.3,"momentum":0.3}'] [--offset 0]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import finlab_factors as ff  # noqa: E402
import optimize as opt  # noqa: E402
import portfolio_backtest as pb  # noqa: E402
import report_factor_ic as rfi  # noqa: E402

INTERVALS = {"monthly": 1, "bimonthly": 2, "quarterly": 3}


def cost_stats(universe, params):
    """Descriptive cost/turnover numbers for one full-train simulation."""
    result = pb.simulate(universe, params=params)
    curve = result["equity_curve"]
    if len(curve) < 2:
        return {}
    p = dict(pb.PORTFOLIO_PARAMS, **params)
    leg_cost = p["fee_pct"] / 2 / 100 + p["slippage_pct"] / 100
    notional = sum(t["price"] * t["shares"] for t in result["trades"])
    years = (len(curve)) / 245.0
    mean_equity = sum(pt["equity"] for pt in curve) / len(curve)
    first, last = curve[0]["equity"], curve[-1]["equity"]
    return {
        "trades": len(result["trades"]),
        "cagr_pct": ((last / first) ** (1 / years) - 1) * 100 if first and last > 0 else None,
        "cost_drag_pct_per_year": notional * leg_cost / mean_equity / years * 100,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--weights", help="JSON ranking weights; default finlab_factors.RANKING_WEIGHTS")
    ap.add_argument("--offset", type=int, default=0, help="diagnostic: shift the month grid (not part of the rule)")
    args = ap.parse_args()

    weights = json.loads(args.weights) if args.weights else dict(ff.RANKING_WEIGHTS)
    universe_data = rfi.load_cached_universe()
    print(f"{len(universe_data)} codes loaded from {rfi.RUN_CACHE_DIR}")
    if not universe_data:
        print("nothing cached -- run scripts/run_factor_backtest.py --universe all first")
        return

    calendar = pb._trading_calendar(universe_data)
    split = opt.train_test_split_by_calendar(calendar)
    if split is None:
        print("not enough calendar for a train/test split")
        return
    train_start, train_end = split["train"]
    print(f"train={train_start}..{train_end}  (held-out test {split['test'][0]}..{split['test'][1]} untouched)")
    print(f"weights={weights}  offset={args.offset}")
    train_universe = opt._slice_universe_by_date(universe_data, train_start, train_end)

    candidates = {name: dict(weights) for name in INTERVALS}
    candidate_params = {
        name: {"rebalance_every_months": k, "rebalance_offset_months": args.offset} for name, k in INTERVALS.items()
    }
    result = opt.sweep_ranking_weights_walk_forward(train_universe, candidates, candidate_params=candidate_params)
    if result is None:
        print("not enough history for any purged fold")
        return

    by_fold = {}
    for t in result["trials"]:
        by_fold.setdefault(t["fold_idx"], {})[t["name"]] = t["score"]
    print("\nper-fold OOS return %:")
    for fold_idx in sorted(by_fold):
        print(f"  fold {fold_idx}: " + "  ".join(f"{n}={by_fold[fold_idx][n]:+.1f}" for n in INTERVALS))
    print("mean OOS %: " + "  ".join(f"{n}={result['candidate_scores'][n]:+.1f}" for n in INTERVALS))
    print(f"fold_argmax={result['fold_argmax']} point_best={result['point_best']['name']} "
          f"pbo={result['pbo']} dsr={result['dsr']} n_trials={result['n_trials']}")
    print(f"decision={result['decision']}")

    print("\nfull-train descriptive stats (not used by the rule):")
    for name, k in INTERVALS.items():
        stats = cost_stats(train_universe, candidate_params[name])
        print(f"  {name}: {stats}")

    point_best = result["point_best"]["name"]
    winner = result["plateau"]["most_common"]
    mean_monthly = result["candidate_scores"]["monthly"]
    proposes = (
        point_best != "monthly" and winner == point_best
        and result["candidate_scores"][point_best] > mean_monthly and result["decision"]["accept"]
    )
    print(f"\nrule verdict: {'PROPOSE ' + point_best if proposes else 'KEEP monthly'}")
    print("caveat: DSR here belongs to point_best; only 4 folds; evidence, not proof.")


if __name__ == "__main__":
    main()
