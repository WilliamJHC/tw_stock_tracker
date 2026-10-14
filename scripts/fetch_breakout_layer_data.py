# -*- coding: utf-8 -*-
"""Data prep for docs/factor_timeframe_optimization_directions.md S3.2:
fetches the full-OHLC daily bars + raw daily institutional net-buy rows that
src/breakout_strategy.py's technical_signal()/chip_signal() need, for the
bounded set of codes that ever appeared in the monthly top-N selection
across cache/factor_run/'s full rebalance-date history -- NOT the whole
1821-code universe (see docs/breakout_entry_layer_report.md's "資料範圍"
section for why the bound exists and how big it turned out to be, ~616
codes as of 2026-09-24).

Why this is a separate fetch from cache/factor_run/: when this was written
that cache's `bars` were close+volume only ({date,c,v,c_adj}; daily_to_bars()
dropped o/h/l -- it was only ever built for momentum/liquidity factors).
Since then daily_to_bars() stores raw o/h/l too, but only for dates fetched
after that change plus whatever `update_universe_bars.py --lookback-days`
backfilled, so coverage can be partial; the chip data below is still absent
from it either way. technical_signal() needs full OHLC
(Donchian breakout needs highs/lows, big_candle_signal() needs opens), and
chip_signal() needs raw per-investor-type daily net buy/sell (inst_window),
not the aggregated inst_net_5d docs/institutional_and_accrual_factor_plan.md
S2.2 already cached. Neither exists anywhere in this repo's cache yet.

Uses cache=False on both data_sources.get_daily()/get_institutional() calls
and its own on-disk cache under cache/breakout_layer/ instead of those
functions' built-in caches -- get_daily()'s own docstring explicitly warns
that its f"daily_{code}" cache key carries no `days` dimension, so writing
a multi-year fetch through it would corrupt the short ~2y window the live
app's UI reads back from that same key (same reason
scripts/run_factor_backtest.py's fetch_universe_data() already uses
cache=False for both these calls).

Resumable the same way cache/factor_run/ is: a per-code JSON under
cache/breakout_layer/ makes a killed run pick back up without re-fetching
already-done codes.

Usage:
    python scripts/fetch_breakout_layer_data.py [--days 5500]
"""
import argparse
import collections
import datetime as dt
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import data_sources as ds  # noqa: E402
import finlab_factors as ff  # noqa: E402
import portfolio_backtest as pb  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from report_factor_ic import load_cached_universe  # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "cache", "breakout_layer")
RATE_LIMIT_BUFFER = 550  # same headroom as scripts/run_factor_backtest.py's RateLimiter


class RateLimiter:
    """Copy of scripts/run_factor_backtest.py's RateLimiter -- not imported
    from there to avoid coupling this standalone data-prep script to that
    module's other (factor-run-specific) globals/side effects."""

    def __init__(self, limit=RATE_LIMIT_BUFFER, window=3600):
        self.limit = limit
        self.window = window
        self.timestamps = collections.deque()

    def before_request(self):
        now = time.monotonic()
        while self.timestamps and now - self.timestamps[0] > self.window:
            self.timestamps.popleft()
        if len(self.timestamps) >= self.limit:
            sleep_for = self.window - (now - self.timestamps[0]) + 1
            print(f"  [rate limit] sleeping {sleep_for:.0f}s ({len(self.timestamps)}/{self.limit} in window)")
            time.sleep(max(sleep_for, 0))
        self.timestamps.append(time.monotonic())


def _cache_path(code):
    return os.path.join(CACHE_DIR, f"{code}.json")


def _load_cache(code):
    try:
        with open(_cache_path(code), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _save_cache(code, data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(_cache_path(code), "w", encoding="utf-8") as f:
        json.dump(data, f)


def candidate_codes(universe_data):
    """Every code that was ever in the monthly top-N across
    cache/factor_run/'s FULL rebalance-date history (not just some walk-
    forward fold's test window) -- anchored-mode simulate() replays from
    calendar[0] through a fold's test_end, so a code selected during an
    earlier fold's train period can already be held (or have been a missed
    entry) by the time a later fold's test window starts; bounding this to
    only test-window dates would silently miss those."""
    calendar = pb._trading_calendar(universe_data)
    codes = set()
    for date in pb._rebalance_dates(calendar):
        scores = ff.composite_scores(universe_data, date)
        codes.update(ff.top_n(scores, pb.PORTFOLIO_PARAMS["top_n"]))
    return sorted(codes)


def daily_to_ohlcv_bars(daily):
    """data_sources.get_daily()'s {t,o,h,l,c,v} -> [{date,o,h,l,c,v}, ...]
    ascending -- unlike scripts/run_factor_backtest.py's daily_to_bars(),
    keeps o/h/l (technical_signal() needs them; that other function's
    universe never did)."""
    return [
        {"date": dt.datetime.fromtimestamp(daily["t"][i]).date().isoformat(),
         "o": daily["o"][i], "h": daily["h"][i], "l": daily["l"][i],
         "c": daily["c"][i], "v": daily["v"][i]}
        for i in range(len(daily["t"]))
    ]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=5500, help="history window, default ~15y (back past cache/factor_run/'s 2012-05-02 calendar start)")
    args = ap.parse_args()

    universe_data = load_cached_universe()
    print(f"{len(universe_data)} codes loaded from cache/factor_run/")
    codes = candidate_codes(universe_data)
    print(f"{len(codes)} candidate codes (ever in monthly top-N across full rebalance history)")

    limiter = RateLimiter()
    done = skipped = failed = 0
    for i, code in enumerate(codes, 1):
        if _load_cache(code) is not None:
            skipped += 1
            continue
        try:
            limiter.before_request()
            daily = ds.get_daily(code, days=args.days, cache=False)
            limiter.before_request()
            inst_rows = ds.get_institutional(code, days=args.days, cache=False)
        except Exception as e:  # noqa: BLE001 -- best-effort per-code, same tolerance as run_factor_backtest.py
            print(f"[{i}/{len(codes)}] {code}: fetch failed ({e!r}), skipping")
            failed += 1
            continue
        bars = daily_to_ohlcv_bars(daily) if daily and daily.get("t") else []
        _save_cache(code, {"bars": bars, "inst_rows": inst_rows or [], "days": args.days})
        done += 1
        if i % 20 == 0 or i == len(codes):
            print(f"[{i}/{len(codes)}] done={done} skipped(resumed)={skipped} failed={failed}")

    print(f"\nfinished: done={done} skipped(resumed)={skipped} failed={failed} total={len(codes)}")


if __name__ == "__main__":
    main()
