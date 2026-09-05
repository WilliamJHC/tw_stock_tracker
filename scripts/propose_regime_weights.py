# -*- coding: utf-8 -*-
"""Human-reviewed candidate step for regime-conditional RANKING_WEIGHTS
(docs/regime_strategy_research.md, docs/adr/0004-regime-adaptive-strategy.md)
-- same manual-review posture as scripts/run_strategy_search.py's
docs/adr/0003-manual-review-for-strategy-logic-search.md, deliberately NOT
docs/adr/0002-auto-apply-ranking-weights.md's auto-apply: this is a brand-new,
not-yet-walk-forward-validated logic type, same reasoning ADR 0003 already
applied to the ma_cross/vol_price_div strategy-search candidates.

Computes, per market regime (src/market_regime.py), a factor_eval.
suggest_weights()-style proposal from evaluate_factors_by_regime() over the
current cache/factor_run/*.json universe + a live TAIEX fetch, and writes it
to meta.pending_regime_weights_candidate for a human to review and apply via
scripts/apply_regime_weights_candidate.py.

A regime whose bucket has fewer than --min-sample-dates rebalance dates is
skipped entirely rather than proposing weights derived from too few points
to mean anything -- docs/regime_strategy_research.md's Phase B found bear's
h=20 bucket had only 2-3 rebalance dates in the current cache, nowhere near
enough to trust. This gate lives in code, not just in that doc's prose, so a
future run with more accumulated history (particularly more bear-market
months) re-evaluates automatically instead of requiring someone to remember
the caveat and skip it by hand.

Usage:
    python scripts/propose_regime_weights.py [--horizon 20] [--min-sample-dates 20]
"""
import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import data_sources as ds  # noqa: E402
import db  # noqa: E402
import factor_eval as fe  # noqa: E402
import finlab_factors as ff  # noqa: E402
import market_regime as mr  # noqa: E402

import report_factor_ic as rfi  # noqa: E402

PENDING_META_KEY = "pending_regime_weights_candidate"
DEFAULT_MIN_SAMPLE_DATES = 20


def build_candidate(universe_data, raw_regime_series, horizon=20, min_sample_dates=DEFAULT_MIN_SAMPLE_DATES):
    """{"weights_by_regime": {regime: weights}, "skipped": {regime: reason}}
    -- see this module's docstring for the sample-size gate. A regime that
    clears the sample gate but whose suggest_weights() still returns None
    (no factor's |ICIR| clears that function's own min_icir bar) or is
    missing a REQUIRED_FACTOR_KEYS entry is also skipped, with a reason
    recorded either way -- apply_regime_weights_candidate.py surfaces
    `skipped` to whoever reviews the candidate so a thin bucket is never
    silently absent."""
    dates = fe.rebalance_dates(universe_data)
    result = fe.evaluate_factors_by_regime(universe_data, raw_regime_series, dates=dates)
    weights_by_regime, skipped = {}, {}
    for regime in fe.REGIMES:
        bucket = result[regime]
        if bucket["sample_dates"] < min_sample_dates:
            skipped[regime] = f"sample_dates={bucket['sample_dates']} < min_sample_dates={min_sample_dates}"
            continue
        suggestion = fe.suggest_weights(bucket["factors"], horizon=horizon)
        if suggestion is None:
            skipped[regime] = "no factor cleared min_icir -- suggest_weights() returned None"
            continue
        missing = ff.REQUIRED_FACTOR_KEYS - set(suggestion)
        if missing:
            skipped[regime] = f"suggestion missing required factor(s) {sorted(missing)}"
            continue
        weights_by_regime[regime] = suggestion
    return {"weights_by_regime": weights_by_regime, "skipped": skipped}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--horizon", type=int, default=20)
    ap.add_argument("--min-sample-dates", type=int, default=DEFAULT_MIN_SAMPLE_DATES)
    args = ap.parse_args()

    print("抓取 TAIEX 日線 ...")
    taiex = ds.get_index_daily("TAIEX")
    raw_regime_series = mr.regime_series(taiex)

    universe_data = rfi.load_cached_universe()
    print(f"{len(universe_data)} codes loaded from {rfi.RUN_CACHE_DIR}")
    if not universe_data:
        print("nothing cached -- run scripts/run_factor_backtest.py --universe all first")
        return

    candidate = build_candidate(
        universe_data, raw_regime_series, horizon=args.horizon, min_sample_dates=args.min_sample_dates,
    )
    for regime, reason in candidate["skipped"].items():
        print(f"[skip] {regime}: {reason}")
    if not candidate["weights_by_regime"]:
        print("\n沒有任何盤勢通過樣本數門檻，沒有候選可提案（見 docs/regime_strategy_research.md）")
        return

    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    payload = {
        "run_id": run_id,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "horizon": args.horizon,
        "min_sample_dates": args.min_sample_dates,
        "weights_by_regime": candidate["weights_by_regime"],
        "skipped_regimes": candidate["skipped"],
    }
    conn = db.connect()
    db.init_db(conn)
    db.migrate(conn)
    db.set_meta(conn, PENDING_META_KEY, json.dumps(payload, ensure_ascii=False))
    conn.close()

    print(f"\n已寫入待審候選 run_id={run_id}：")
    for regime, weights in candidate["weights_by_regime"].items():
        print(f"  {regime}: {weights}")
    print("\n人工審核後執行 scripts/apply_regime_weights_candidate.py 套用或 --reject 捨棄"
          "（正式環境走 apply_regime_weights_candidate.yml workflow_dispatch，見該腳本說明）")


if __name__ == "__main__":
    main()
