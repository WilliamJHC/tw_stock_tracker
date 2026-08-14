# -*- coding: utf-8 -*-
"""Multi-factor ranking strategy backtest on FinMind data (no FinLab
subscription needed) -- the src/finlab_strategy.py idea ported onto
src/finlab_factors.py + src/portfolio_backtest.py.

Usage:
    python scripts/run_factor_backtest.py --universe watchlist [--top-n 20]
    python scripts/run_factor_backtest.py --universe all --days 1800

--universe all pulls FinMind's whole 2162-name TWSE/TPEX universe. FinMind's
free tier is 600 req/hr and can't batch-query (see get_universe()'s
docstring on why); fetching every name's daily bars + revenue + financials
is ~8600 requests, hours even with a two-stage fetch. Two-stage: Stage 1
pulls daily bars only (1 req/code) and applies the liquidity filter once
up front (a "did this name ever clear the bar in the window" proxy, not the
per-rebalance-date exact check finlab_factors.liquidity_ok() does inside
the backtest); only survivors get revenue+financials (3 more req/code) in
Stage 2. A per-code JSON cache under cache/factor_run/ makes a killed run
resumable -- rerun the same command and already-fetched codes are skipped.
"""
import argparse
import collections
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import data_sources as ds  # noqa: E402
import finlab_factors as ff  # noqa: E402
import portfolio_backtest as pb  # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_CACHE_DIR = os.path.join(BASE_DIR, "cache", "factor_run")
FINMIND_HOURLY_LIMIT = 600
RATE_LIMIT_BUFFER = 550  # stay under the real cap with headroom for the app's own calls


class _RateLimiter:
    """Leaky-bucket over a rolling 1h window, tracked by wall-clock request
    timestamps. Sleeps just enough to keep the count under
    RATE_LIMIT_BUFFER -- avoids _finmind()'s fail-fast-on-429 path (see
    data_sources.py's comment on why 429 isn't retried)."""

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


def _code_cache_path(code):
    return os.path.join(RUN_CACHE_DIR, f"{code}.json")


def _load_code_cache(code, days):
    """Returns None (cache miss) if the file is missing/corrupt OR if it was
    fetched under a different --days window -- otherwise a resumed run with
    a longer/shorter --days would silently reuse bars/fundamentals (or a
    cached "skip" verdict) computed for the wrong history length."""
    try:
        with open(_code_cache_path(code), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if data.get("days") != days:
        return None
    return data


def _save_code_cache(code, data):
    os.makedirs(RUN_CACHE_DIR, exist_ok=True)
    with open(_code_cache_path(code), "w", encoding="utf-8") as f:
        json.dump(data, f)


def _load_universe(name):
    if name == "watchlist":
        with open(os.path.join(BASE_DIR, "watchlist.json"), encoding="utf-8") as f:
            rows = json.load(f)
        return [r["code"] for r in rows if r["code"] != "0050"]  # ETF, no fundamentals
    if name == "all":
        universe = ds.get_universe(cache=True)
        if not universe:
            raise RuntimeError("get_universe() returned nothing -- check FINMIND_TOKEN / connectivity")
        return [u["code"] for u in universe if not u["code"].startswith("00")]  # exclude ETFs/ETNs
    raise ValueError(f"unknown universe: {name!r}")


def _daily_to_bars(daily):
    """data_sources.get_daily()'s {t,o,h,l,c,v} -> [{date,c,v}, ...] ascending."""
    import datetime as dt
    return [
        {"date": dt.datetime.fromtimestamp(daily["t"][i]).date().isoformat(), "c": daily["c"][i], "v": daily["v"][i]}
        for i in range(len(daily["t"]))
    ]


def _passed_liquidity_ever(bars):
    """Cheap Stage-1 proxy: did this name's turnover ever clear the bar
    across its whole history? Filters out names that would never survive
    finlab_factors.liquidity_ok() at any rebalance date, without needing
    revenue/financials data to know that."""
    for i in range(len(bars)):
        if ff.liquidity_ok(bars, i):
            return True
    return False


def fetch_universe_data(codes, days, limiter):
    """Two-stage fetch. Returns {code: {bars, bar_dates, revenue_series,
    roe_series}}, skipping codes with insufficient data at any stage."""
    universe_data = {}
    n = len(codes)
    for i, code in enumerate(codes):
        cached = _load_code_cache(code, days)
        if cached is not None:
            if cached.get("skip"):
                continue
            universe_data[code] = {
                "bars": cached["bars"],
                "bar_dates": [b["date"] for b in cached["bars"]],
                "revenue_series": cached["revenue_series"],
                "roe_series": cached["roe_series"],
            }
            continue

        print(f"[{i + 1}/{n}] {code}")
        limiter.before_request()
        try:
            daily = ds.get_daily(code, days=days, cache=False)
        except Exception as e:
            print(f"  daily FAILED: {e}")
            _save_code_cache(code, {"skip": True, "days": days})
            continue
        bars = _daily_to_bars(daily)

        if not _passed_liquidity_ever(bars):
            print("  never liquid -- skipping fundamentals fetch")
            _save_code_cache(code, {"skip": True, "days": days})
            continue

        limiter.before_request()
        revenue_rows = ds.get_monthly_revenue(code, days=days, cache=False)
        limiter.before_request()
        limiter.before_request()  # get_financials() issues 2 requests internally
        financials_rows = ds.get_financials(code, days=days, cache=False)

        revenue_series = ff.revenue_yoy_series(revenue_rows or [])
        roe_series_ = ff.roe_series(financials_rows or [])
        if not revenue_series or not roe_series_:
            print("  insufficient revenue/financials history -- skipping")
            _save_code_cache(code, {"skip": True, "days": days})
            continue

        _save_code_cache(code, {"bars": bars, "revenue_series": revenue_series, "roe_series": roe_series_, "days": days})
        universe_data[code] = {
            "bars": bars, "bar_dates": [b["date"] for b in bars],
            "revenue_series": revenue_series, "roe_series": roe_series_,
        }
    return universe_data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", choices=["watchlist", "all"], default="watchlist")
    parser.add_argument("--days", type=int, default=1800, help="daily-bar history window (default ~5y)")
    parser.add_argument("--top-n", type=int, default=ff.RANKING_TOP_N)
    parser.add_argument("--capital", type=float, default=pb.PORTFOLIO_PARAMS["initial_capital"])
    args = parser.parse_args()

    codes = _load_universe(args.universe)
    print(f"universe={args.universe} ({len(codes)} codes), days={args.days}")

    limiter = _RateLimiter()
    universe_data = fetch_universe_data(codes, args.days, limiter)
    print(f"\n{len(universe_data)}/{len(codes)} codes usable after liquidity + fundamentals filters")

    if not universe_data:
        print("nothing usable -- aborting")
        return

    result = pb.simulate(universe_data, params={"top_n": args.top_n, "initial_capital": args.capital})
    print(f"\n=== 排名法多因子（FinMind, universe={args.universe}） ===")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    print(f"trades: {len(result['trades'])}")


if __name__ == "__main__":
    main()
