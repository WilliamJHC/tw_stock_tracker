# -*- coding: utf-8 -*-
"""Daily top-N all-market recommendation ranking (docs/dev_plan_recommendations.md
Phase 1). Recomputes finlab_factors.composite_scores() over whatever
cache/factor_run/*.json currently holds (refreshed weekly by
scripts/update_universe_bars.py, Phase 0) and stores the result under
backtest.db's meta key "daily_recommendations". Cheap -- composite_scores()
is O(survivors * log n), stdlib-only, no network beyond one cached
get_universe() call for code->name lookup -- so this is meant to run daily,
even though the underlying cache/factor_run data itself only refreshes
weekly. The written "as_of" is deliberately the cache's own latest bar
date, not today's date -- the UI must not imply same-day freshness the
data doesn't have (see Phase 0's cost note on why the cache can be up to
~1-2 weeks stale for a given code).

Each run:
  1. report_factor_ic.load_cached_universe() (no network).
  2. Reads meta.active_ranking_weights via db.get_meta_json() (fallback
     finlab_factors.RANKING_WEIGHTS if unset) -- same shared read path
     run_ranking_optimize.py's writer uses, so this ranking always reflects
     whatever weights that job most recently adopted, never a stale local
     copy.
  3. finlab_factors.composite_scores()/factor_ranks()/top_n() over the
     cached universe, "as of" the latest bar date present anywhere in the
     cache. Both calls pass extra_factors=ff.EXTRA_FACTOR_FUNCS so that an
     active_ranking_weights adopted via scripts/apply_strategy_candidate.py
     (one that includes "ma_cross"/"vol_price_div") actually takes effect
     here -- composite_scores() otherwise silently ignores a weight key
     that was never computed (docs/adr/0003-manual-review-for-strategy-
     logic-search.md).
  4. data_sources.get_universe(cache=True) for code->name (one cached
     request total, not per-code).
  5. Writes meta.daily_recommendations as JSON: {as_of, generated_at,
     codes: [{code, name, composite_score, factor_ranks}, ...]}.
  6. Fires one alerts.send_telegram() summary, mirroring run_ranking_optimize.py
     (Phase 6d)'s "every scheduled run posts one event summary" convention.

Usage:
    python scripts/run_daily_recommendations.py [--top-n 5] [--dry-run]

--dry-run computes and prints the payload but never writes meta or sends
Telegram.

If cache/factor_run/*.json is empty (Phase 0 has never run) or has no
usable bar_dates, this prints a message and returns without touching meta
-- same "don't fail the schedule, just say so, and don't clobber whatever
meta already held" precedent as run_ranking_optimize.py's own empty-cache
check.
"""
import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import alerts  # noqa: E402
import data_sources as ds  # noqa: E402
import db  # noqa: E402
import finlab_factors as ff  # noqa: E402

import report_factor_ic as rfi  # noqa: E402

META_KEY = "daily_recommendations"
ACTIVE_META_KEY = "active_ranking_weights"


def _latest_bar_date(universe_data):
    """The single latest bar date seen anywhere across the cached universe
    -- this IS "as_of" (see module docstring on why it's the cache's date,
    not today's)."""
    dates = [d["bar_dates"][-1] for d in universe_data.values() if d.get("bar_dates")]
    return max(dates) if dates else None


def build_recommendations(conn, universe_data, top_n):
    """Returns {"as_of": str, "codes": [...]} or None if universe_data has
    no usable bar_dates at all (nothing to date the ranking by)."""
    as_of = _latest_bar_date(universe_data)
    if as_of is None:
        return None

    weights = db.get_meta_json(conn, ACTIVE_META_KEY, default=dict(ff.RANKING_WEIGHTS))
    # factor_ranks() computed once and reused by composite_scores() below --
    # it's the full liquidity-filter + percentile-rank pass over the whole
    # cached universe, no reason to pay for it twice on every daily run.
    ranks = ff.factor_ranks(universe_data, as_of, extra_factors=ff.EXTRA_FACTOR_FUNCS)
    scores = ff.composite_scores(universe_data, as_of, weights=weights, ranks=ranks, extra_factors=ff.EXTRA_FACTOR_FUNCS)
    top_codes = ff.top_n(scores, top_n)

    universe = ds.get_universe(cache=True) or []
    code_to_name = {u["code"]: u["name"] for u in universe}

    codes_out = []
    for code in top_codes:
        r = ranks.get(code, {})
        codes_out.append({
            "code": code,
            "name": code_to_name.get(code) or code,
            "composite_score": scores[code],
            "factor_ranks": {"revenue": r.get("revenue"), "roe": r.get("roe"), "momentum": r.get("momentum")},
        })
    return {"as_of": as_of, "codes": codes_out}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--top-n", type=int, default=5)
    ap.add_argument("--dry-run", action="store_true", help="計算並印出結果，不寫 meta 也不發 Telegram")
    args = ap.parse_args()

    universe_data = rfi.load_cached_universe()
    print(f"{len(universe_data)} codes loaded from {rfi.RUN_CACHE_DIR}")
    if not universe_data:
        print("nothing cached -- run scripts/update_universe_bars.py "
              "(or scripts/run_factor_backtest.py --universe all) first")
        return

    conn = db.connect()
    db.init_db(conn)
    db.migrate(conn)

    result = build_recommendations(conn, universe_data, args.top_n)
    if result is None:
        print("universe_data 裡沒有任何一檔有 bar_dates -- 無法判斷 as_of，未寫入 meta")
        conn.close()
        return

    payload = {
        "as_of": result["as_of"],
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "codes": result["codes"],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    summary = (
        f"[每日推薦排名] as_of={payload['as_of']} top{args.top_n}=" +
        ", ".join(f"{c['code']}({c['composite_score']:.3f})" for c in payload["codes"])
    )

    if args.dry_run:
        print("（dry-run，未寫入 meta，未發 Telegram）")
        conn.close()
        return

    db.set_meta(conn, META_KEY, json.dumps(payload, ensure_ascii=False))
    conn.close()
    print(summary)
    alerts.send_telegram(summary)


if __name__ == "__main__":
    main()
