# -*- coding: utf-8 -*-
"""Scheduled RANKING_WEIGHTS re-evaluation + auto-apply/rollback driver
(docs/https-finlab-finance-blog-python-get-tai-memoized-lighthouse.md
Phase 6c/6d, docs/adr/0002-auto-apply-ranking-weights.md). Meant to run
monthly off .github/workflows/optimize.yml -- no CLI flags needed for that
path, everything it needs comes from cache/factor_run/*.json (scripts/
run_factor_backtest.py's cache, kept current by a separate incremental-
update step) and backtest.db's `meta` table.

Each run:
  1. Loads the cached full-market universe (report_factor_ic.py's
     load_cached_universe(), no network).
  2. Computes this cycle's fresh rank-IC/ICIR per factor and derives an
     "ic_suggested" RANKING_WEIGHTS candidate (factor_eval.suggest_weights(),
     None if nothing clears the bar).
  3. Runs a purged walk-forward comparison (src/optimize.py's
     sweep_ranking_weights_walk_forward()) across up to 3 named candidates:
     "active" (whatever backtest.db's meta.active_ranking_weights currently
     holds, or finlab_factors.RANKING_WEIGHTS if never set),
     "previous" (only present the cycle right after a prior switch -- see
     meta.previous_ranking_weights), "ic_suggested" (from step 2, omitted
     entirely if None).
  4. src/optimize.py's resolve_ranking_weights_action() turns that result's
     decision/winner into "keep" / "adopt_suggested" / "rollback_to_previous"
     -- see that function's docstring for the full rule set. A switch
     updates meta.active_ranking_weights (and meta.previous_ranking_weights
     to whatever was just replaced, so a rollback is possible next cycle);
     a rollback restores meta.active_ranking_weights from
     meta.previous_ranking_weights and clears it (one rollback undoes one
     switch, it does not chain further back).
  5. Every trial from step 3 is persisted to backtest.db's optimization_runs
     table (run_id="ranking_weights_YYYY-MM-DD") so n_trials/DSR can be
     recomputed later from real stored rows, same discipline as
     scripts/run_optimize.py's Phase 5 usage of that table.
  6. Fires exactly one alerts.send_telegram() event summary regardless of
     outcome (keep/adopt/rollback) -- Phase 6d: "每次排程跑完...都呼叫
     alerts.py 現成的 send_telegram() 發一則事件摘要", observability, not a
     gate.

Usage:
    python scripts/run_ranking_optimize.py [--horizon 20] [--dry-run]

--dry-run runs the full evaluation and prints what WOULD happen, without
writing meta/optimization_runs or sending a Telegram message -- useful to
sanity-check a cache locally before wiring the schedule.
"""
import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import alerts  # noqa: E402
import db  # noqa: E402
import factor_eval as fe  # noqa: E402
import finlab_factors as ff  # noqa: E402
import optimize as opt  # noqa: E402

# report_factor_ic.py lives in this same scripts/ directory -- see
# scripts/run_factor_backtest.py's matching import of build_adj_factors for
# why this works without an extra sys.path entry.
import report_factor_ic as rfi  # noqa: E402

ACTIVE_META_KEY = "active_ranking_weights"
PREVIOUS_META_KEY = "previous_ranking_weights"
DEFAULT_HORIZON = 20  # matches finlab_factors' "hold ~1 month" rebalance cadence


def build_candidates(conn, universe_data, horizon=DEFAULT_HORIZON):
    """{name: weights_dict} for this cycle's sweep_ranking_weights_walk_forward()
    call, plus the raw factor_result (for logging/inspection) -- "active" is
    always present, "previous"/"ic_suggested" only when real. Returns
    (candidates, factor_result, has_previous)."""
    active = db.get_meta_json(conn, ACTIVE_META_KEY, default=dict(ff.RANKING_WEIGHTS))
    previous = db.get_meta_json(conn, PREVIOUS_META_KEY)

    dates = fe.rebalance_dates(universe_data)
    factor_result = fe.evaluate_factors(universe_data, dates=dates)
    suggested = fe.suggest_weights(factor_result, horizon=horizon)
    # suggest_weights() only includes factors clearing its min_icir bar --
    # a legitimate 1- or 2-factor dict for a human reading the report, but
    # finlab_factors.composite_scores() indexes w["revenue"]/w["roe"]/
    # w["momentum"] unconditionally and would KeyError on a partial dict.
    # Every OTHER candidate here ("active"/"previous") is always a complete
    # RANKING_WEIGHTS-shaped dict (persisted via meta or the module default),
    # so this is the one place a partial dict could enter the walk-forward
    # sweep -- skip it rather than crash the whole scheduled run.
    if suggested is not None and set(suggested) != set(ff.RANKING_WEIGHTS):
        print(f"  ic_suggested 缺少因子（{set(ff.RANKING_WEIGHTS) - set(suggested)}），本輪不納入候選")
        suggested = None

    candidates = {"active": active}
    has_previous = previous is not None
    if has_previous:
        candidates["previous"] = previous
    if suggested is not None:
        candidates["ic_suggested"] = suggested
    return candidates, factor_result, has_previous


def apply_action(conn, action, candidates):
    """Mutates meta per resolve_ranking_weights_action()'s verdict. Returns
    a human-readable summary line for the Telegram event."""
    if action == "adopt_suggested":
        db.set_meta(conn, PREVIOUS_META_KEY, json.dumps(candidates["active"]))
        db.set_meta(conn, ACTIVE_META_KEY, json.dumps(candidates["ic_suggested"]))
        return f"採用新候選 ic_suggested={candidates['ic_suggested']}"
    if action == "rollback_to_previous":
        db.set_meta(conn, ACTIVE_META_KEY, json.dumps(candidates["previous"]))
        db.set_meta(conn, PREVIOUS_META_KEY, "")  # one rollback undoes one switch, doesn't chain further back
        return f"rollback，退回 previous={candidates['previous']}"
    return f"維持現況 active={candidates['active']}"


def persist_trials(conn, run_id, result):
    """result["trials"]: [{"fold_idx","name","score"}, ...] from
    sweep_ranking_weights_walk_forward() -> db.upsert_optimization_trials()'s
    row shape. code is left None (this sweep isn't per-code); param_name
    carries the candidate name so a later query can group by it."""
    now = dt.datetime.now().isoformat()
    rows = [
        {
            "trial_id": i, "code": None, "param_name": t["name"], "fold_idx": t["fold_idx"],
            "params_json": json.dumps({"score": t["score"]}), "score": t["score"], "computed_at": now,
        }
        for i, t in enumerate(result["trials"])
    ]
    db.upsert_optimization_trials(conn, run_id, rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--horizon", type=int, default=DEFAULT_HORIZON)
    ap.add_argument("--dry-run", action="store_true", help="評估但不寫 meta/optimization_runs，也不發 Telegram")
    args = ap.parse_args()

    universe_data = rfi.load_cached_universe()
    print(f"{len(universe_data)} codes loaded from {rfi.RUN_CACHE_DIR}")
    if not universe_data:
        print("nothing cached -- run scripts/run_factor_backtest.py --universe all first")
        return

    conn = db.connect()
    db.init_db(conn)
    db.migrate(conn)

    candidates, factor_result, has_previous = build_candidates(conn, universe_data, horizon=args.horizon)
    print(f"candidates: {list(candidates)}")
    for factor_name, by_horizon in factor_result.items():
        icir = by_horizon.get(args.horizon, {}).get("icir")
        print(f"  rank-IC h={args.horizon} {factor_name}: icir={icir}")

    result = opt.sweep_ranking_weights_walk_forward(universe_data, candidates)
    if result is None:
        msg = "[排程最佳化] 資料量不足以切出任何一個 purged walk-forward fold，本次維持現況"
        print(msg)
        if not args.dry_run:
            alerts.send_telegram(msg)
        conn.close()
        return

    # plateau["most_common"] (the per-fold-argmax MAJORITY winner), not
    # point_best["name"] (the highest-MEAN-OOS-return winner) -- decision
    # was computed by evaluate_decision(plateau, ...), which validates
    # plateau's stability (width_frac = fold win rate vs the chance-adjusted
    # threshold), so "did the winner clear the gate" only holds together
    # when "winner" IS the candidate that plateau describes. point_best can
    # differ (one big-margin fold win can out-mean a candidate that lost
    # every other fold by a little) and would then carry a decision that
    # was never actually validated for it. See src/optimize.py's
    # resolve_ranking_weights_action() docstring.
    winner = result["plateau"]["most_common"]
    decision = result["decision"]
    action = opt.resolve_ranking_weights_action(decision, winner, has_previous)
    print(f"winner={winner} (point_best={result['point_best']['name']}) decision={decision} action={action}")

    run_id = f"ranking_weights_{dt.date.today().isoformat()}"
    if args.dry_run:
        summary = f"（dry-run，未寫入）action={action}"
    else:
        persist_trials(conn, run_id, result)
        summary = apply_action(conn, action, candidates)

    message = (
        f"[排程最佳化] {summary}\n"
        f"winner={winner}  dsr={result['dsr']}  pbo={result['pbo']}  n_trials={result['n_trials']}\n"
        f"vs_buy_hold={result['vs_buy_hold']}"
    )
    print(message)
    if not args.dry_run:
        alerts.send_telegram(message)
    conn.close()


if __name__ == "__main__":
    main()
