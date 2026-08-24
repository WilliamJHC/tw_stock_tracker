# -*- coding: utf-8 -*-
"""Weekly incremental refresh of cache/factor_run/*.json's daily bars
(docs/dev_plan_recommendations.md Phase 0). Fills the gap
.github/workflows/optimize.yml's header comment calls out: that workflow
re-evaluates whatever cache/factor_run/*.json already holds but never
refreshes it; scripts/run_factor_backtest.py only supports a full
~19-22h --universe all re-pull, no incremental mode.

For every code already cached under cache/factor_run/ (skipping
{"skip": true} entries -- these were excluded by the liquidity/data
filters and stay excluded), pulls a small `LOOKBACK_DAYS`-day trailing
window via data_sources.get_daily(), merges it into the existing `bars`
array by date (dedup, new fetch wins on overlap), then re-runs
run_factor_backtest.py's _augment_bars_with_adj_factor() over the FULL
merged history to refresh every bar's "c_adj" key -- that function
recomputes from bars[0]'s date every call (it is NOT incremental), so
this is one extra Yahoo request per code, same shape as a first-time
augment.

Cost note (2026-08-24 decision -- see docs/dev_plan_recommendations.md
Phase 0): _augment_bars_with_adj_factor() shares the SAME _RateLimiter
instance as the get_daily() call (mirrors fetch_universe_data()'s own
call pattern in run_factor_backtest.py), so each code consumes 2 limiter
slots (1 real FinMind request + 1 Yahoo request throttled at the same
550/hr rate), not 1. For ~2000 codes that's roughly 4000 slots / 550
per hr =~ 7.3h -- well past the plan's original "~3.3h, FinMind-only"
paper estimate, and past GitHub-hosted runners' 6h default job ceiling.
The user explicitly chose to keep c_adj refresh despite this cost
(2026-08-24 AskUserQuestion). To still approximate the Phase 0 goal
("bars stay <=1 week stale") under that ceiling, `codes` is rotated by
an ISO-week-based offset before processing, so a run truncated by the
job timeout strands a DIFFERENT tail of codes each week rather than the
same one forever -- worst case for any single code becomes ~2 weeks
stale instead of never-refreshed. This is a mitigation, not a fix: if
the real run confirms ~7h+, the honest fix is either narrowing
LOOKBACK_DAYS's Yahoo cost (out of this script's scope) or splitting
this into two workflow runs -- flagged for the user in the Phase 0
report, not decided unilaterally here.

Other cache keys (revenue_series/roe_series/per_pbr_series/margin trend
series/days) are left untouched -- this script only ever rewrites "bars".

Usage:
    python scripts/update_universe_bars.py [--dry-run] [--limit N]

--dry-run fetches/merges/augments but never writes cache/factor_run/*.json;
prints a per-code before/after bar count and date range instead.
--limit N processes only the first N codes after rotation -- for a quick
local sanity check without burning the full universe's request budget.
"""
import argparse
import datetime as dt
import glob
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import alerts  # noqa: E402
import data_sources as ds  # noqa: E402

import run_factor_backtest as rfb  # noqa: E402

LOOKBACK_DAYS = 30


def _cached_codes():
    paths = sorted(glob.glob(os.path.join(rfb.RUN_CACHE_DIR, "*.json")))
    return [os.path.splitext(os.path.basename(p))[0] for p in paths]


def _rotated(codes, today=None):
    """Rotate the (sorted) code list by an ISO-week-based offset -- see
    the cost note in this module's docstring for why: a run that gets
    truncated by GitHub Actions' 6h job ceiling should strand a
    different tail each week, not the same one every time."""
    if not codes:
        return codes
    today = today or dt.date.today()
    week = today.isocalendar()[1]
    step = max(1, len(codes) * 2 // 3)  # advance ~2/3 of the universe per week
    offset = (week * step) % len(codes)
    return codes[offset:] + codes[:offset]


def _merge_bars(existing_bars, new_bars):
    """existing_bars/new_bars: ascending [{date,c,v}, ...]. New fetch wins
    on a shared date (fresher pull, e.g. late settlement corrections).
    Returns ascending merged list (ISO date strings sort chronologically
    same as calendar order)."""
    by_date = {b["date"]: b for b in existing_bars}
    for b in new_bars:
        by_date[b["date"]] = {"date": b["date"], "c": b["c"], "v": b["v"]}
    return [by_date[d] for d in sorted(by_date)]


def update_code(code, limiter, dry_run):
    """Returns {"status": "updated"|"skipped"|"failed", "detail": str}.
    Never raises -- a per-code failure is best-effort, matching
    fetch_universe_data()'s pattern, so one bad code can't kill the run."""
    try:
        with open(rfb._code_cache_path(code), encoding="utf-8") as f:
            cached = json.load(f)
    except (OSError, ValueError) as e:
        return {"status": "failed", "detail": f"cache read error: {e}"}

    if cached.get("skip"):
        return {"status": "skipped", "detail": "previously marked skip"}

    existing_bars = cached.get("bars") or []
    if not existing_bars:
        return {"status": "skipped", "detail": "no existing bars"}

    limiter.before_request()
    try:
        daily = ds.get_daily(code, days=LOOKBACK_DAYS, cache=False)
    except Exception as e:
        return {"status": "failed", "detail": f"get_daily error: {e}"}
    new_bars = rfb._daily_to_bars(daily)

    merged = _merge_bars(existing_bars, new_bars)
    augmented = rfb._augment_bars_with_adj_factor(code, merged, limiter)
    has_c_adj = bool(augmented) and "c_adj" in augmented[0]

    detail = (f"{len(existing_bars)}->{len(merged)} bars, "
              f"latest {existing_bars[-1]['date']}->{merged[-1]['date']}, "
              f"c_adj={'yes' if has_c_adj else 'fallback(raw close)'}")

    if dry_run:
        return {"status": "updated", "detail": "[dry-run] " + detail}

    cached["bars"] = augmented
    rfb._save_code_cache(code, cached)
    return {"status": "updated", "detail": detail}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="抓取/合併/還原因子照跑，但不寫回 cache/factor_run/*.json")
    ap.add_argument("--limit", type=int, default=None, help="只處理輪替後的前 N 檔，本機小樣本測試用")
    args = ap.parse_args()

    codes = _rotated(_cached_codes())
    if args.limit:
        codes = codes[: args.limit]
    print(f"{len(codes)} cached codes to refresh (lookback={LOOKBACK_DAYS} days, dry_run={args.dry_run})")

    limiter = rfb._RateLimiter()
    counts = {"updated": 0, "skipped": 0, "failed": 0}
    failures = []
    for i, code in enumerate(codes):
        result = update_code(code, limiter, args.dry_run)
        counts[result["status"]] = counts.get(result["status"], 0) + 1
        print(f"[{i + 1}/{len(codes)}] {code}: {result['status']} -- {result['detail']}")
        if result["status"] == "failed":
            failures.append(f"{code}: {result['detail']}")

    summary = (f"[全市場日線增量更新] updated={counts['updated']} "
               f"skipped={counts['skipped']} failed={counts['failed']}")
    if failures:
        summary += "\n失敗清單（前 10）：" + "; ".join(failures[:10])
    print(summary)
    if not args.dry_run:
        alerts.send_telegram(summary)


if __name__ == "__main__":
    main()
