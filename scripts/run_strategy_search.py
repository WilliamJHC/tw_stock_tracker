# -*- coding: utf-8 -*-
"""Scheduled STRATEGY-LOGIC search: compares named candidates that add
brand-new factor logic (moving-average cross, volume/price divergence) on
top of the existing revenue/roe/momentum trio, using the same purged
walk-forward + PBO/DSR machinery as scripts/run_ranking_optimize.py --
but, unlike that pipeline, this one NEVER auto-applies its winner.

Why manual review here but not in run_ranking_optimize.py (docs/adr/0002):
ADR 0002's auto-apply/auto-rollback design fits RANKING_WEIGHTS because the
three factor's own significance was already vetted by Phase 4's rank-IC
tests -- reweighting them has no extra degree of freedom a human needs to
judge. ma_cross/vol_price_div are brand-new, never-before-run condition
types with no such prior vetting, so a human looks at both the train-
segment walk-forward result AND a held-out test-segment sanity check before
anything goes live. See docs/adr/0003-manual-review-for-strategy-logic-
search.md.

Each run:
  1. Loads the cached full-market universe (report_factor_ic.py's
     load_cached_universe(), no network) and derives its trading calendar.
  2. src/optimize.py's train_test_split_by_calendar() carves off the last
     6 calendar months as a held-out TEST segment that never enters the
     search -- an extra guard on top of sweep_ranking_weights_walk_forward()'s
     own purged walk-forward folds, which only ever run inside TRAIN.
  3. Runs the fixed named candidates (baseline / with_ma_cross /
     with_vol_price_div / with_both -- see CANDIDATE_WEIGHTS below) through
     sweep_ranking_weights_walk_forward() over the TRAIN segment only, with
     finlab_factors.EXTRA_FACTOR_FUNCS wired in so ma_cross/vol_price_div
     actually get scored.
  4. If a non-baseline candidate wins AND clears evaluate_decision()'s
     DSR/PBO/plateau gates: re-simulates that candidate and baseline over
     the TEST segment (never touched by step 3) for a final, once-only
     sanity comparison, then writes meta.pending_strategy_candidate and
     sends a Telegram summary asking a human to run
     scripts/apply_strategy_candidate.py.
  5. Every TRAIN-segment trial from step 3 is persisted to backtest.db's
     optimization_runs table (run_id="strategy_search_YYYY-MM-DD"), same
     discipline as run_ranking_optimize.py's persist_trials().

Usage:
    python scripts/run_strategy_search.py [--dry-run] [--pbr | --skip21 | --high250]

--pbr swaps the 4 named candidates for baseline vs with_pbr/with_pbr_15
(value_pbr factor) and restricts the calendar to the PBR-data era (2021-10
onward) -- see build_pbr_candidates().
--skip21 swaps them for baseline vs momentum-with-latest-21-bars-skipped
(60/120-bar windows) over the full calendar -- see build_skip21_candidates().
--high250 swaps them for baseline vs 52-week-high proximity replacing or
sharing the momentum weight -- see build_high250_candidates().
Default behavior (no flag) is unchanged.

--dry-run runs the full evaluation and prints what WOULD happen, without
writing meta/optimization_runs or sending a Telegram message.
"""
import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import alerts  # noqa: E402
import db  # noqa: E402
import finlab_factors as ff  # noqa: E402
import optimize as opt  # noqa: E402
import portfolio_backtest as pb  # noqa: E402

# report_factor_ic.py lives in this same scripts/ directory -- see
# scripts/run_factor_backtest.py's matching import of build_adj_factors for
# why this works without an extra sys.path entry.
import report_factor_ic as rfi  # noqa: E402

ACTIVE_META_KEY = "active_ranking_weights"
PENDING_META_KEY = "pending_strategy_candidate"


def build_candidates(active_weights):
    """Fixed named candidates for this cycle's TRAIN-segment sweep -- see
    the strategy-logic-search plan's "具名候選組合" section. These weight
    numbers are a reasonable first guess, not something searched for: this
    round deliberately does not do a continuous search over ma_cross/
    vol_price_div's own internal parameters or over candidate weight
    ratios, same discrete-named-candidate stance ADR 0002 already took for
    RANKING_WEIGHTS itself. "baseline" is whatever's currently live (no new
    factors) so the sweep always has an apples-to-apples reference point,
    and so a "nothing beats what's already running" result is visible
    rather than only ever comparing the new candidates against each
    other."""
    return {
        "baseline": dict(active_weights),
        "with_ma_cross": {"revenue": 0.3, "roe": 0.2, "momentum": 0.2, "ma_cross": 0.3},
        "with_vol_price_div": {"revenue": 0.3, "roe": 0.2, "momentum": 0.2, "vol_price_div": 0.3},
        "with_both": {"revenue": 0.2, "roe": 0.15, "momentum": 0.15, "ma_cross": 0.25, "vol_price_div": 0.25},
    }


def build_pbr_candidates(active_weights):
    """--pbr mode's two candidates: the live baseline vs the same
    revenue/roe/momentum trio with value_pbr blended in (weights mirror
    with_ma_cross above -- a first guess, not searched for).

    Kept out of build_candidates() on purpose: ff.used_extra_factors() feeds
    the sweep the UNION of every candidate's extra factors, and factor_ranks()
    drops any code whose extra factor is None -- value_pbr is None for every
    code before ~2021-10, so putting with_pbr in the default 4-candidate race
    would silently empty out the pre-2021 history for ma_cross/vol_price_div
    too."""
    # Two weight levels, fixed up front and both counted in n_trials (so DSR
    # already charges for trying more than one) -- not tuned after seeing
    # fold results.
    return {
        "baseline": dict(active_weights),
        "with_pbr": {"revenue": 0.3, "roe": 0.2, "momentum": 0.2, "value_pbr": 0.3},
        "with_pbr_15": {"revenue": 0.35, "roe": 0.25, "momentum": 0.25, "value_pbr": 0.15},
    }


def build_skip21_candidates(active_weights):
    """--skip21 mode: the live baseline vs swapping ONLY the momentum term for
    a skip-most-recent-21-bars window (60- or 120-bar), everything else
    unchanged -- isolates the formation-window question from
    docs/factor_candidates_momentum_ic_2026-09.md §2. Momentum's own weight
    moves to the skip-21 factor; the plain "momentum" key is left out of
    these weight dicts (composite_scores() skips keys a weight dict lacks).
    Needs no PBR-era cut: both windows only need bars, so the full history
    is usable. Kept out of build_candidates() for the same union-of-extra-
    factors reason as build_pbr_candidates()."""
    base = dict(active_weights)
    momentum_w = base.pop("momentum", 0.0)
    return {
        "baseline": dict(active_weights),
        "mom60_skip21": {**base, "mom60_skip21": momentum_w},
        "mom120_skip21": {**base, "mom120_skip21": momentum_w},
    }


def build_high250_candidates(active_weights):
    """--high250 mode: the live baseline vs 52-week-high proximity
    (ff.high250_ratio) either REPLACING the momentum term or SHARING it
    half/half -- answers "substitute or complement" in one race. Weights are
    fixed up front, not tuned, and both non-baseline candidates count in
    n_trials. Needs no PBR-era cut (bars only) but needs 250 bars of
    history, so every candidate (baseline included) is evaluated on codes
    with at least that much -- see ff.used_extra_factors()'s union
    behavior; baseline numbers here are therefore not comparable to other
    modes'. Kept out of build_candidates() for the same union-of-extra-
    factors reason as build_pbr_candidates()."""
    base = dict(active_weights)
    momentum_w = base.pop("momentum", 0.0)
    return {
        "baseline": dict(active_weights),
        "high250": {**base, "high250": momentum_w},
        "mom_high250_half": {**base, "momentum": momentum_w / 2, "high250": momentum_w / 2},
    }


def pbr_history_start(universe_data):
    """Earliest value_series usable_date across the universe, or None if no
    code has any PBR history."""
    starts = [d["value_series"][0]["usable_date"] for d in universe_data.values() if d.get("value_series")]
    return min(starts) if starts else None


def persist_trials(conn, run_id, result):
    """Same shape as scripts/run_ranking_optimize.py's persist_trials() --
    result["trials"]: [{"fold_idx","name","score"}, ...] from
    sweep_ranking_weights_walk_forward() -> db.upsert_optimization_trials()'s
    row shape."""
    now = dt.datetime.now().isoformat()
    rows = [
        {
            "trial_id": i, "code": None, "param_name": t["name"], "fold_idx": t["fold_idx"],
            "params_json": json.dumps({"score": t["score"]}), "score": t["score"], "computed_at": now,
        }
        for i, t in enumerate(result["trials"])
    ]
    db.upsert_optimization_trials(conn, run_id, rows)


def _test_window_summary(sim_result, test_start, test_end):
    """portfolio_backtest.simulate()'s own "summary" is relative to the
    WHOLE simulated run, but that run is deliberately started earlier than
    test_start (see main()'s buffered_start) purely so momentum/ma_cross/
    vol_price_div have enough lookback history at the test segment's first
    rebalance dates -- reusing sim_result["summary"] as-is would blend a
    few months of pre-test-segment performance into what's supposed to be
    a held-out-only number. Recomputes the same metrics
    (portfolio_backtest._compute_metrics()) restricted to the equity-curve
    points actually inside [test_start, test_end], re-based off that
    window's own starting equity. {} if fewer than 2 points fall in that
    window."""
    pts = [pt for pt in sim_result["equity_curve"] if test_start <= pt["date"] <= test_end]
    if len(pts) < 2:
        return {}
    return pb._compute_metrics(pts, pts[0]["equity"])


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="評估但不寫 meta/optimization_runs，也不發 Telegram")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--pbr", action="store_true",
                      help="改跑 baseline vs with_pbr（兩個權重），且只用 PBR 有資料的區段（見 build_pbr_candidates）")
    mode.add_argument("--skip21", action="store_true",
                      help="改跑 baseline vs 動能換成跳過最近 21 根的 60/120 視窗（見 build_skip21_candidates）")
    mode.add_argument("--high250", action="store_true",
                      help="改跑 baseline vs 52 週高點接近度（取代動能／與動能對半，見 build_high250_candidates）")
    args = ap.parse_args()

    universe_data = rfi.load_cached_universe()
    print(f"{len(universe_data)} codes loaded from {rfi.RUN_CACHE_DIR}")
    if not universe_data:
        print("nothing cached -- run scripts/run_factor_backtest.py --universe all first")
        return

    if args.pbr:
        # Before the first PBR row every code scores None for value_pbr, so
        # every candidate (baseline included) would hold an empty universe and
        # tie at 0% -- and ties go to the first-listed candidate ("baseline")
        # in the per-fold argmax. Cut the calendar to the PBR era so every
        # fold compares real portfolios.
        pbr_start = pbr_history_start(universe_data)
        if pbr_start is None:
            print("[策略搜尋] 快取內沒有任何 PBR 資料，--pbr 本次略過")
            return
        universe_data = opt._slice_universe_by_date(universe_data, pbr_start, "9999-12-31")
        print(f"--pbr: universe cut to bars from {pbr_start} (first PBR date)")

    conn = db.connect()
    db.init_db(conn)
    db.migrate(conn)

    calendar = pb._trading_calendar(universe_data)
    split = opt.train_test_split_by_calendar(calendar)
    if split is None:
        msg = "[策略搜尋] 資料量不足以切出 train/test 區段，本次略過"
        print(msg)
        if not args.dry_run:
            alerts.send_telegram(msg)
        conn.close()
        return

    train_start, train_end = split["train"]
    test_start, test_end = split["test"]
    print(f"train={train_start}..{train_end}  test={test_start}..{test_end}")

    train_universe = opt._slice_universe_by_date(universe_data, train_start, train_end)

    active_weights = db.get_meta_json(conn, ACTIVE_META_KEY, default=dict(ff.RANKING_WEIGHTS))
    if args.pbr:
        candidates = build_pbr_candidates(active_weights)
    elif args.skip21:
        candidates = build_skip21_candidates(active_weights)
    elif args.high250:
        candidates = build_high250_candidates(active_weights)
    else:
        candidates = build_candidates(active_weights)
    print(f"candidates: {list(candidates)}")

    # Union of extra factors actually referenced across THIS cycle's 4 named
    # candidates (currently == ff.EXTRA_FACTOR_FUNCS, since between them
    # with_ma_cross/with_vol_price_div/with_both already cover both keys --
    # ff.used_extra_factors() keeps this correct automatically if
    # build_candidates()'s fixed dict ever changes instead of needing a
    # matching manual update here).
    result = opt.sweep_ranking_weights_walk_forward(
        train_universe, candidates, extra_factors=ff.used_extra_factors(candidates.values())
    )
    if result is None:
        msg = "[策略搜尋] train 段資料量不足以切出任何一個 purged walk-forward fold，本次略過"
        print(msg)
        if not args.dry_run:
            alerts.send_telegram(msg)
        conn.close()
        return

    # plateau["most_common"] (per-fold-argmax majority winner), not
    # point_best["name"] -- same reasoning as run_ranking_optimize.py's
    # matching comment: evaluate_decision() validated plateau's own
    # stability, not point_best's.
    winner = result["plateau"]["most_common"]
    decision = result["decision"]
    print(f"winner={winner} (point_best={result['point_best']['name']}) decision={decision}")
    # Per-fold OOS returns and the aggregate stats print on EVERY path, not
    # only when a candidate wins -- a "baseline held" verdict is otherwise
    # unreadable (a near-tie and a blowout look identical).
    for t in result["trials"]:
        print(f"  fold {t['fold_idx']} {t['name']}: {t['score']}")
    print(f"  candidate_scores={result['candidate_scores']} fold_argmax={result['fold_argmax']} "
          f"pbo={result['pbo']} dsr={result['dsr']} n_trials={result['n_trials']}")

    run_id = f"strategy_search{'_pbr' if args.pbr else '_skip21' if args.skip21 else '_high250' if args.high250 else ''}_{dt.date.today().isoformat()}"
    if not args.dry_run:
        persist_trials(conn, run_id, result)

    if not decision["accept"] or winner is None or winner == "baseline":
        msg = (
            f"[策略搜尋] train 段沒有新候選勝出（winner={winner}, accept={decision['accept']}），"
            f"維持現況，本次不產生待審候選"
        )
        print(msg)
        if not args.dry_run:
            alerts.send_telegram(msg)
        conn.close()
        return

    # Held-out TEST segment: winner (train-selected) vs baseline, run once,
    # never fed back into the search above. Buffered start mirrors
    # sweep_ranking_weights_walk_forward()'s mode="rolling" buffer -- lets
    # momentum_pct()/ma_cross_signal()/volume_price_divergence() already
    # have enough lookback history at the test segment's own first
    # rebalance dates instead of returning None there.
    buffered_start = (dt.date.fromisoformat(test_start)
                       - dt.timedelta(days=opt.ROLLING_MOMENTUM_LOOKBACK_BUFFER_DAYS)).isoformat()
    test_sim_start = max(calendar[0], buffered_start)
    test_universe = opt._slice_universe_by_date(universe_data, test_sim_start, test_end)

    winner_weights = candidates[winner]
    # Only the extra factor(s) winner/baseline actually use -- NOT the full
    # ff.EXTRA_FACTOR_FUNCS. Using the full set here would apply
    # vol_price_div's eligibility filter to baseline_sim (and to winner_sim
    # if winner is "with_ma_cross") even though neither candidate's weights
    # reference it, artificially narrowing the held-out test-segment
    # universe relative to how each candidate actually behaves in
    # production (used_extra_factors([weights]) there too, see
    # run_daily_recommendations.py) -- exactly the bias this human-facing
    # apply/reject report must not have.
    test_extra_factors = ff.used_extra_factors([winner_weights, candidates["baseline"]])
    winner_sim = pb.simulate(test_universe, params={"ranking_weights": winner_weights, "extra_factors": test_extra_factors})
    baseline_sim = pb.simulate(test_universe, params={"ranking_weights": candidates["baseline"], "extra_factors": test_extra_factors})
    winner_test_summary = _test_window_summary(winner_sim, test_start, test_end)
    baseline_test_summary = _test_window_summary(baseline_sim, test_start, test_end)

    train_summary = {
        "dsr": result["dsr"],
        "pbo": result["pbo"],
        "plateau": result["plateau"],
        "candidate_scores": result["candidate_scores"],
        "n_trials": result["n_trials"],
    }
    test_summary = {"winner": winner_test_summary, "baseline": baseline_test_summary}

    payload = {
        "run_id": run_id,
        "candidate_name": winner,
        "weights": winner_weights,
        "train_summary": train_summary,
        "test_summary": test_summary,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    # db.set_meta() below is a plain overwrite (meta is a key-value table,
    # no history) -- if a PRIOR cycle's candidate is still sitting there
    # unreviewed (nobody ran apply_strategy_candidate.py before this cycle
    # fired again), it's about to be silently replaced with no trace beyond
    # its trials already persisted to optimization_runs under its own
    # run_id. Surface that explicitly rather than let it vanish quietly --
    # a human who was mid-review of the old one needs to know it's gone.
    overwritten = db.get_meta_json(conn, PENDING_META_KEY)
    overwritten_note = (
        f"\n（覆蓋掉尚未處理的候選 run_id={overwritten['run_id']} candidate_name={overwritten['candidate_name']}，"
        f"未執行 apply/--reject 就被下一輪蓋掉；試驗紀錄仍在 optimization_runs 裡）"
        if overwritten else ""
    )

    message = (
        f"[策略搜尋] train 段候選 {winner} 勝出並通過 DSR/PBO/高原門檻\n"
        f"train: dsr={result['dsr']} pbo={result['pbo']} plateau={result['plateau']}\n"
        f"test(vs baseline): {winner}={winner_test_summary} baseline={baseline_test_summary}\n"
        # apply_strategy_candidate.py must run via its own GitHub Actions
        # workflow (workflow_dispatch), not as a raw local command -- a
        # local run would silently write to an unrelated throwaway sqlite
        # file instead of the real backtest.db every scheduled pipeline
        # reads (see that script's own docstring). Telling a human to run
        # `python scripts/apply_strategy_candidate.py ...` here would send
        # them straight into that trap.
        f"人工確認後到 GitHub Actions 手動觸發 apply_strategy_candidate workflow"
        f"（run_id={run_id}），或勾選 reject 捨棄"
        f"{overwritten_note}"
    )
    print(message)

    if args.dry_run:
        print("（dry-run，未寫入 meta.pending_strategy_candidate，未發 Telegram）")
        conn.close()
        return

    db.set_meta(conn, PENDING_META_KEY, json.dumps(payload, ensure_ascii=False))
    conn.close()
    alerts.send_telegram(message)


if __name__ == "__main__":
    main()
