# -*- coding: utf-8 -*-
"""Fetch full-history institutional net-buy (法人買賣超) for the cached
universe and store a per-code net-ratio series for factor IC evaluation
(docs/factor_timeframe_optimization_directions.md §2.2).

One FinMind request per code (TaiwanStockInstitutionalInvestorsBuySell,
whole history in one response -- probed 2026-09-30: 2330 returns 2012-05-02
onward, ~15.6k rows). Output goes to cache/institutional_series/<code>.json,
a directory of its own: cache/factor_run/*.json belongs to the weekly
update_universe_bars workflows and is never touched here.

Sharing the quota: FINMIND_TOKEN is the same token the scheduled workflows
use, so the default rate (300 requests/hour) is well under the project's own
550/hr RateLimiter ceiling, leaving headroom. A full 1821-code run at that
rate is ~6h; --every N takes a systematic 1-in-N sample of the sorted code
list instead (--every 8 -> ~228 codes, ~46 min). Resumable: a code whose file
already exists is skipped, so re-running with a smaller --every (or 1) only
fetches what's missing -- note that --every K then re-selects codes by the
same sorted-index rule, so use --every 1 to fill in everything.

Needs FINMIND_TOKEN (environment variable, or .env at the project root -- a
git worktree has no .env, so export the variable there); without one
data_sources falls back to the much smaller free-tier quota.

Stops immediately on an HTTP 402/429 (quota) response rather than retrying;
other per-code failures are logged and left without a file so the next run
retries them.

Usage:
    python scripts/fetch_institutional_series.py [--every 8] [--per-hour 300] [--limit N] [--plan]

--plan prints how many codes would be fetched/skipped and exits without any
network call.
"""
import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import data_sources as ds  # noqa: E402
import finlab_factors as ff  # noqa: E402
import report_factor_ic as rfi  # noqa: E402
import run_factor_backtest as rfb  # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(BASE_DIR, "cache", "institutional_series")
DATASET = "TaiwanStockInstitutionalInvestorsBuySell"
QUOTA_MARKERS = ("HTTP 402", "HTTP 429")


def out_path(code):
    return os.path.join(OUT_DIR, f"{code}.json")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--every", type=int, default=8, help="take every Nth code of the sorted cached-code list")
    ap.add_argument("--per-hour", type=int, default=300, help="request ceiling per rolling hour")
    ap.add_argument("--limit", type=int, default=0, help="stop after N fetches (0 = no cap)")
    ap.add_argument("--plan", action="store_true", help="show the plan and exit, no network")
    args = ap.parse_args()

    universe = rfi.load_cached_universe()
    codes = sorted(universe)[::max(args.every, 1)]
    todo = [c for c in codes if not os.path.exists(out_path(c))]
    print(f"{len(universe)} cached codes -> {len(codes)} selected (every {args.every}), "
          f"{len(codes) - len(todo)} already fetched, {len(todo)} to fetch")
    if args.plan:
        return
    if args.limit:
        todo = todo[:args.limit]
    os.makedirs(OUT_DIR, exist_ok=True)

    limiter = rfb.RateLimiter(limit=args.per_hour)
    fetched = failed = 0
    for n, code in enumerate(todo, 1):
        bars = universe[code]["bars"]
        limiter.before_request()
        try:
            rows = ds._finmind(DATASET, code, bars[0]["date"])
        except ds.SourceError as e:
            print(f"[{n}/{len(todo)}] {code} FAILED: {e}")
            failed += 1
            if any(m in str(e) for m in QUOTA_MARKERS):
                print("quota/rate-limit response -- stopping so the shared token isn't hammered")
                break
            continue
        series = ff.institutional_net_ratio_series(ff.institutional_net_by_date(rows), bars)
        with open(out_path(code), "w", encoding="utf-8") as f:
            json.dump({"code": code, "fetched_on": dt.date.today().isoformat(), "n_rows": len(rows),
                       "series": series}, f)
        fetched += 1
        if n % 25 == 0 or n == len(todo):
            print(f"[{n}/{len(todo)}] {code} rows={len(rows)} series={len(series)}")
    print(f"done: fetched={fetched} failed={failed}")


if __name__ == "__main__":
    main()
