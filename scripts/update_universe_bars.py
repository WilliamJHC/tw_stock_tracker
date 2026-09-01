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
call pattern in run_factor_backtest.py), so each LIVE code (one with
actual bars, i.e. not a {"skip": true} entry -- those cost ~0, no
network call at all) consumes 2 limiter slots (1 real FinMind request +
1 Yahoo request, both throttled at the same 550/hr rate), not 1.
Measured against the real cache (2026-08-24): 2148 cached codes, 427
{"skip": true} (excluded by the liquidity/data filters, never touched
here), so 1721 live codes -> ~3442 slots / 550 per hr =~ 6.3h for the
FULL universe in one run -- past GitHub-hosted runners' 6h default job
ceiling (this project's earlier back-of-envelope estimate used the raw
2148 count and got ~7.3h; the real number is a bit better but still
over the line). The user explicitly chose to keep c_adj refresh despite
this cost (2026-08-24 AskUserQuestion), then asked (same day) whether
splitting the run into two -- Saturday/Sunday -- would fix the timeout
risk structurally rather than just mitigate it.

It does: `--half {1,2}` splits the (sorted) cached-code list into two
contiguous halves up front, so each half's run only ever touches ~860
codes =~ 3.1h with the 6.3h-for-everything math above -- ~1.9x safety
margin per run, same total weekly wall-clock/GitHub-Actions-minutes cost
as one 6.3h run would have been, just spread across two scheduled runs
that never individually approach the 6h ceiling. See
.github/workflows/update_universe_bars_part1.yml (Saturday, --half 1)
and _part2.yml (Sunday, --half 2). This replaces an earlier ISO-week
rotation design (kept only in git history) that was a probabilistic
mitigation (worst case ~2 weeks stale for a stranded code) rather than
this deterministic full-coverage-per-week split.

Other cache keys (revenue_series/roe_series/per_pbr_series/margin trend
series/days) are left untouched -- this script only ever rewrites "bars".

Usage:
    python scripts/update_universe_bars.py [--half {1,2}] [--dry-run] [--limit N]

--half {1,2} restricts the run to that contiguous half of the sorted
cached-code list (omit to process the whole universe -- e.g. for a local
full-universe dry run, or a manual one-off catch-up).
--dry-run fetches/merges/augments but never writes cache/factor_run/*.json;
prints a per-code before/after bar count and date range instead.
--limit N processes only the first N codes of the selected half -- for a
quick local sanity check without burning the full half's request budget.
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


def _split_half(codes, half):
    """codes split into two contiguous halves by sorted order; `half` is
    1 or 2 (None -- the whole list, unsplit). Skip/live status isn't
    correlated with code-number order, so a plain positional split keeps
    each half's real (non-skip) workload roughly even without needing to
    inspect cache contents up front."""
    if half is None:
        return codes
    mid = (len(codes) + 1) // 2
    return codes[:mid] if half == 1 else codes[mid:]


def _twse_years_for(codes):
    """Earliest bars[0]["date"] across live (non-skip) cached codes -> the
    year range _fetch_twse_table() must cover so _augment_bars_with_adj_factor()'s
    FULL-history recompute (see module docstring: it is NOT incremental, it
    recomputes c_adj from bars[0] every call) has TWSE dividend/split
    coverage back to the oldest bar any code holds. LOOKBACK_DAYS (30d) is
    only the new-bar fetch window and is NOT enough for this -- a merged
    history commonly spans years. Falls back to a 5y-back default if no
    live code has bars yet (mirrors run_factor_backtest.py's own --days
    1800 default), which shouldn't occur in practice since this script only
    ever runs against an already-populated cache."""
    earliest = None
    for code in codes:
        try:
            with open(rfb._code_cache_path(code), encoding="utf-8") as f:
                cached = json.load(f)
        except (OSError, ValueError):
            continue
        if cached.get("skip"):
            continue
        bars = cached.get("bars") or []
        if not bars:
            continue
        d = bars[0]["date"]
        if earliest is None or d < earliest:
            earliest = d
    start_year = int(earliest[:4]) if earliest else dt.date.today().year - 5
    return range(start_year, dt.date.today().year + 1)


def _merge_bars(existing_bars, new_bars):
    """existing_bars/new_bars: ascending [{date,c,v}, ...]. New fetch wins
    on a shared date (fresher pull, e.g. late settlement corrections).
    Returns ascending merged list (ISO date strings sort chronologically
    same as calendar order)."""
    by_date = {b["date"]: b for b in existing_bars}
    for b in new_bars:
        by_date[b["date"]] = {"date": b["date"], "c": b["c"], "v": b["v"]}
    return [by_date[d] for d in sorted(by_date)]


def update_code(code, limiter, dry_run, market, twse_events):
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
    try:
        augmented = rfb._augment_bars_with_adj_factor(code, merged, limiter, market, twse_events)
    except Exception as e:
        return {"status": "failed", "detail": f"augment error: {e}"}
    # Check the NEWEST bar, not the oldest: _merge_bars() strips c_adj only
    # off newly-merged/overlapping-date bars, so an old untouched bar at
    # augmented[0] keeps a stale c_adj from a prior successful run even when
    # THIS run's augmentation fell back to raw close -- augmented[0] would
    # then misreport "c_adj=yes" while the bars momentum_pct() actually
    # reads (the newest ones) lack it.
    has_c_adj = bool(augmented) and "c_adj" in augmented[-1]

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
    ap.add_argument("--half", type=int, choices=[1, 2], default=None,
                     help="只跑排序後的前半（1）或後半（2）；不給則跑全部")
    ap.add_argument("--dry-run", action="store_true", help="抓取/合併/還原因子照跑，但不寫回 cache/factor_run/*.json")
    ap.add_argument("--limit", type=int, default=None, help="只處理選定範圍的前 N 檔，本機小樣本測試用")
    args = ap.parse_args()

    codes = _split_half(_cached_codes(), args.half)
    if args.limit:
        codes = codes[: args.limit]
    print(f"{len(codes)} cached codes to refresh (half={args.half}, lookback={LOOKBACK_DAYS} days, dry_run={args.dry_run})")

    universe = ds.get_universe(cache=True) or []
    market_by_code = {u["code"]: u["market"] for u in universe}
    years = _twse_years_for(codes)
    print(f"抓取 TWSE 除權息表（市場層級，{years[0]}-{years[-1]}，共 {len(years)} 個 request）...")
    twse_by_code = rfb._fetch_twse_table(years, codes)

    limiter = rfb._RateLimiter()
    counts = {"updated": 0, "skipped": 0, "failed": 0}
    failures = []
    for i, code in enumerate(codes):
        market = market_by_code.get(code, "twse")
        twse_events = twse_by_code.get(code, [])
        result = update_code(code, limiter, args.dry_run, market, twse_events)
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
