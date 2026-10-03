# -*- coding: utf-8 -*-
"""Seed cache/factor_run/ from scratch inside GitHub Actions
(.github/workflows/seed_factor_run.yml).

Why this exists: cache/ is gitignored and the only way to build it was a
manual ~19-22h `run_factor_backtest.py --universe all` on a dev machine, so the
Actions cache lineage `update-universe-bars-state-*` that optimize.yml,
strategy_search.yml, daily_recommendations.yml and update_universe_bars_part1/2
all restore from was never created (the weekly refresh aborts on an empty dir,
see update_universe_bars.MIN_CACHED_CODES). This script does the same full pull
(it reuses run_factor_backtest.fetch_universe_data(), so per-code cache files
are byte-compatible with the local ones) but in time-boxed slices: a GitHub-
hosted job is capped at 6h and the FinMind limiter (550 req/hr) makes the whole
universe ~20h+, so each run fetches until --budget-minutes is used up, then
stops at a batch boundary. fetch_universe_data() skips every code that already
has a valid cache file, so rerunning picks up where the last slice ended.

Usage:
    python scripts/seed_factor_run.py [--days 5300] [--budget-minutes 300]
                                      [--batch-size 10] [--limit N]

--limit N only considers the first N codes of the universe (local smoke test;
point RUN_CACHE_DIR at a scratch dir via CACHE_DIR_OVERRIDE=<path>).

Reports through $GITHUB_OUTPUT (when set): `remaining` (codes still without a
valid cache file after this run) and `progress` (codes completed by this run).
The workflow chains another run only while progress > 0.

Safety: fetch_universe_data() writes {"skip": true} for a code whose daily
request fails -- with a bad/expired FINMIND_TOKEN that would silently mark the
whole universe as permanently skipped. So (1) a probe request must succeed
before anything is written, and (2) a batch where EVERY code came back skip
triggers a re-probe; if the probe fails the run exits non-zero, and because the
workflow only saves the cache on success, that run's skip files are discarded.
"""
import argparse
import datetime as dt
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import alerts  # noqa: E402
import data_sources as ds  # noqa: E402

import run_factor_backtest as rfb  # noqa: E402

PROBE_CODE = "2330"


def remaining_codes(codes, days):
    """Codes with no valid cache file for this `days` window (missing, corrupt
    or fetched under a different window) -- exactly the ones
    fetch_universe_data() would fetch rather than reuse."""
    return [c for c in codes if rfb.load_code_cache(c, days) is None]


def count_skipped(codes, days):
    n = 0
    for c in codes:
        cached = rfb.load_code_cache(c, days)
        if cached is not None and cached.get("skip"):
            n += 1
    return n


def probe_finmind():
    """True when one real daily-bars request returns rows (token + network ok)."""
    try:
        return bool(ds.get_daily(PROBE_CODE, days=10, cache=False))
    except Exception as e:  # noqa: BLE001 -- any failure means "do not trust this run"
        print(f"probe {PROBE_CODE} failed: {e}")
        return False


def seed(codes, days, budget_seconds, batch_size, fetch=None, probe=None, clock=time.monotonic):
    """Fetch batches of still-missing codes until the budget is spent or none
    remain. Returns (remaining_before, remaining_after). `fetch`/`probe`/`clock`
    are injectable for tests. Raises RuntimeError when a fully-skipped batch
    coincides with a failing probe (see module docstring)."""
    fetch = fetch or _fetch_batch
    probe = probe or probe_finmind
    todo = remaining_codes(codes, days)
    before = len(todo)
    deadline = clock() + budget_seconds
    for i in range(0, len(todo), batch_size):
        if clock() >= deadline:
            print(f"time budget used up after {i} codes of this run -- stopping at a batch boundary")
            break
        batch = todo[i:i + batch_size]
        fetch(batch)
        if count_skipped(batch, days) == len(batch) and not probe():
            raise RuntimeError(
                f"batch {batch[0]}..{batch[-1]} came back entirely skip and the probe request failed "
                "-- likely a bad FINMIND_TOKEN or an outage; aborting so this run's cache is not saved")
    return before, len(remaining_codes(codes, days))


_fetch_ctx = {}


def _fetch_batch(batch):
    rfb.fetch_universe_data(batch, _fetch_ctx["days"], _fetch_ctx["limiter"],
                            _fetch_ctx["market_by_code"], _fetch_ctx["twse_by_code"])


def write_outputs(remaining, progress):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"remaining={remaining}\nprogress={progress}\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=5300,
                    help="daily-bar history window, same meaning as run_factor_backtest.py --days "
                         "(5300 ~ 14.5y: verified 2026-10-02 to reproduce the local 2012-start cache exactly on the "
                         "overlapping dates; 1800 gives only ~5y)")
    ap.add_argument("--budget-minutes", type=float, default=300, help="stop starting new batches after this long")
    ap.add_argument("--batch-size", type=int, default=10)
    ap.add_argument("--limit", type=int, default=None, help="only the first N universe codes (local smoke test)")
    args = ap.parse_args()

    override = os.environ.get("CACHE_DIR_OVERRIDE")
    if override:
        rfb.RUN_CACHE_DIR = override

    codes = rfb.load_universe("all")
    if args.limit:
        codes = codes[: args.limit]
    todo_now = remaining_codes(codes, args.days)
    print(f"universe={len(codes)} codes, {len(todo_now)} still need fetching (days={args.days})")
    if not todo_now:
        write_outputs(0, 0)
        print("nothing to fetch -- already complete")
        return

    if not probe_finmind():
        print(f"probe request for {PROBE_CODE} failed -- check FINMIND_TOKEN / connectivity; nothing written")
        sys.exit(1)

    universe = ds.get_universe(cache=True) or []
    start_year = (dt.date.today() - dt.timedelta(days=args.days)).year
    years = range(start_year, dt.date.today().year + 1)
    print(f"抓取 TWSE 除權息表（{years[0]}-{years[-1]}，共 {len(years)} 個 request）...")
    _fetch_ctx.update({
        "days": args.days,
        "limiter": rfb.RateLimiter(),
        "market_by_code": {u["code"]: u["market"] for u in universe},
        "twse_by_code": rfb.fetch_twse_table(years, codes),
    })

    before, after = seed(codes, args.days, args.budget_minutes * 60, args.batch_size)
    progress = before - after
    skipped = count_skipped(codes, args.days)
    live = len(codes) - after - skipped
    msg = (f"[全市場快取播種] 本次完成 {progress} 檔；尚餘 {after} 檔；"
           f"累計 live {live} 檔、skip {skipped} 檔（共 {len(codes)} 檔）")
    print(msg)
    write_outputs(after, progress)
    try:
        alerts.send_telegram(msg)
    except Exception as e:  # noqa: BLE001 -- a failed notice must not fail the seed
        print(f"telegram notice failed: {e}")


if __name__ == "__main__":
    main()
