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

Phase 6a (docs/https-finlab-finance-blog-python-get-tai-memoized-lighthouse.md)
adds one more request per surviving code: a Yahoo Adj Close pull, ratioed
against this module's own FinMind raw close, to stamp a "c_adj" key onto
every bar (see augment_bars_with_adj_factor()) -- Yahoo-ratio only, no TWSE
table, no backtest.db write. finlab_factors.momentum_pct() reads c_adj when
present. Best-effort: a code where the Yahoo pull fails, or where the
detected steps fail adjust.py's invariant checks, just keeps raw close for
that code (see augment_bars_with_adj_factor()'s docstring) rather than
aborting the run.

Phase 6b adds one more Stage-2 request per surviving code (get_per_pbr()) --
gross/operating margin trend ride along on the financials request already
made (zero extra cost), but the PBR-based value candidate needs its own
FinMind dataset. universe_data now also carries "value_series"/
"gross_margin_trend_series"/"operating_margin_trend_series" (see
finlab_factors.py's value_series()/gross_margin_trend_series()/
operating_margin_trend_series()) for scripts/report_factor_ic.py to
evaluate via factor_eval.evaluate_candidate_factor() -- these are NOT part
of RANKING_WEIGHTS/composite_scores(), evaluation only.
"""
import argparse
import collections
import datetime as dt
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import adjust  # noqa: E402
import data_sources as ds  # noqa: E402
import finlab_factors as ff  # noqa: E402
import portfolio_backtest as pb  # noqa: E402

# build_adj_factors.py lives in this same scripts/ directory (not src/ --
# its Yahoo/yfinance path is the one place in the project allowed to pull
# pandas, see that module's docstring) -- Python auto-adds a script's own
# directory to sys.path, so this import works the same way "import adjust"
# above does for src/.
import build_adj_factors as baf  # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_CACHE_DIR = os.path.join(BASE_DIR, "cache", "factor_run")
FINMIND_HOURLY_LIMIT = 600
RATE_LIMIT_BUFFER = 550  # stay under the real cap with headroom for the app's own calls


class RateLimiter:
    """Leaky-bucket over a rolling 1h window, tracked by wall-clock request
    timestamps. Sleeps just enough to keep the count under
    RATE_LIMIT_BUFFER -- avoids _finmind()'s fail-fast-on-429 path (see
    data_sources.py's comment on why 429 isn't retried).

    Public interface: also used directly by scripts/update_universe_bars.py
    (import run_factor_backtest as rfb) -- see that script's module
    docstring for why the two scripts must stay in sync (8a504f0)."""

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


def code_cache_path(code):
    """Public interface: also used directly by scripts/update_universe_bars.py."""
    return os.path.join(RUN_CACHE_DIR, f"{code}.json")


def _load_code_cache(code, days):
    """Returns None (cache miss) if the file is missing/corrupt OR if it was
    fetched under a different --days window -- otherwise a resumed run with
    a longer/shorter --days would silently reuse bars/fundamentals (or a
    cached "skip" verdict) computed for the wrong history length."""
    try:
        with open(code_cache_path(code), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if data.get("days") != days:
        return None
    return data


def save_code_cache(code, data):
    """Public interface: also used directly by scripts/update_universe_bars.py."""
    os.makedirs(RUN_CACHE_DIR, exist_ok=True)
    with open(code_cache_path(code), "w", encoding="utf-8") as f:
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


def daily_to_bars(daily):
    """data_sources.get_daily()'s {t,o,h,l,c,v} -> [{date,c,v}, ...] ascending.
    Public interface: also used directly by scripts/update_universe_bars.py."""
    import datetime as dt
    return [
        {"date": dt.datetime.fromtimestamp(daily["t"][i]).date().isoformat(), "c": daily["c"][i], "v": daily["v"][i]}
        for i in range(len(daily["t"]))
    ]


def fetch_twse_table(years, codes):
    """One TWSE request per calendar year for the WHOLE market's corporate
    actions (baf.fetch_twse_year()), filtered down to `codes`. O(years)
    requests, not O(codes) -- unlike every other per-code fetch in this
    module, so this is cheap even at full-universe scale. Not subject to
    the FinMind RateLimiter (this hits TWSE's own site, a separate quota).
    Returns baf.parse_twse_rows()'s {code: [{date, pre_close, ref_price,
    ratio, kind}, ...]} shape, ready for baf.resolve_adjustment_actions().

    Public interface: also used directly by scripts/update_universe_bars.py."""
    all_rows = []
    for year in years:
        rows = baf.fetch_twse_year(year)
        all_rows.extend(rows)
        print(f"  TWSE {year}: {len(rows)} 筆")
        time.sleep(0.3)
    return baf.parse_twse_rows(all_rows, set(codes))


def augment_bars_with_adj_factor(code, bars, limiter, market, twse_events):
    """Public interface: also used directly by scripts/update_universe_bars.py
    (import run_factor_backtest as rfb) -- its exact 5-arg call shape must
    stay in sync with this signature, see that script's module docstring
    (commit 8a504f0: a prior 3-vs-5-arg drift crashed the weekly refresh).

    bars (ascending [{date,c,v}]) -> same-length list with a "c_adj" key
    added to every entry -- a TRUE total-return-style back-adjustment (both
    splits AND cash/stock dividends), combining `twse_events` (this code's
    rows from fetch_twse_table(), authoritative for TWSE-listed codes) with
    a Yahoo-ratio cross-check via baf.resolve_adjustment_actions() -- the
    same combine-and-validate logic scripts/build_adj_factors.py uses for
    the watchlist/backtest.db pipeline, applied here without a DB write.
    finlab_factors.momentum_pct() reads c_adj when present; liquidity_ok()
    keeps reading raw "c"/"v" untouched (same bars list, both fields
    present). portfolio_backtest.simulate() also trades on c_adj -- see its
    own docstring -- so this is the one place feeding both the ranking
    factor AND the P&L.

    Best-effort, with one exception that must NOT be treated as "resolved":
    when this code has no TWSE coverage (TPEX, or a TWSE code with no event
    rows this window) the adjustment depends entirely on the Yahoo pull, and
    fetch_universe_data()'s cache-backfill re-augments a code only while its
    cached bars still lack a "c_adj" key at all (`if bars and "c_adj" not in
    bars[0]`) -- so a Yahoo call that failed here (network blip, rate limit)
    must return `bars` UNCHANGED, not a bars list that already carries a
    (wrongly empty) "c_adj", or the failure gets cached as a permanent,
    silent "confirmed no adjustment" that a later successful run can never
    correct. An adj_factor invariant violation (adjust.py's
    validate_adj_factor()) is a different, already-resolved case -- it DOES
    fall back to raw close for this code (c_adj == c, factor 1.0
    throughout) rather than aborting the whole run, since that failure mode
    doesn't depend on a transient network call succeeding next time."""
    limiter.before_request()
    actions, counters = baf.resolve_adjustment_actions(code, market, bars, twse_events)
    relies_solely_on_yahoo = market == "tpex" or not twse_events
    if relies_solely_on_yahoo and not counters["yahoo_ok"]:
        return bars
    bar_dates = [b["date"] for b in bars]
    factors = adjust.build_adj_factor(bar_dates, actions)
    violations = adjust.validate_adj_factor(bar_dates, factors, [a["date"] for a in actions])
    if violations:
        print(f"  ⚠ {code} adj_factor 不變量違反（{len(violations)} 筆），本檔退回原始價："
              f"{violations[:3]}")
        return [{**b, "c_adj": b["c"]} for b in bars]
    return [{**b, "c_adj": b["c"] * f} for b, f in zip(bars, factors)]


def _passed_liquidity_ever(bars):
    """Cheap Stage-1 proxy: did this name's turnover ever clear the bar
    across its whole history? Filters out names that would never survive
    finlab_factors.liquidity_ok() at any rebalance date, without needing
    revenue/financials data to know that."""
    for i in range(len(bars)):
        if ff.liquidity_ok(bars, i):
            return True
    return False


def fetch_universe_data(codes, days, limiter, market_by_code, twse_by_code):
    """Two-stage fetch. Returns {code: {bars, bar_dates, revenue_series,
    roe_series, value_series, gross_margin_trend_series,
    operating_margin_trend_series}}, skipping codes with insufficient data
    at any stage. The three Phase 6b keys can be empty lists (no signal
    computable, e.g. margin data missing) but are always present -- only a
    code loaded from a pre-Phase-6b cache entry that hasn't gone through
    fetch/backfill yet could differ, and that only for the margin-trend
    keys (see the "per_pbr_series"/c_adj backfill comments above).

    market_by_code/twse_by_code: main()'s ds.get_universe()-derived market
    lookup and fetch_twse_table()'s market-wide dividend/split events,
    threaded through to augment_bars_with_adj_factor() so it can combine
    TWSE's authoritative table with the per-code Yahoo cross-check."""
    universe_data = {}
    n = len(codes)
    for i, code in enumerate(codes):
        market = market_by_code.get(code, "twse")
        twse_events = twse_by_code.get(code, [])
        cached = _load_code_cache(code, days)
        if cached is not None:
            if cached.get("skip"):
                continue
            bars = cached["bars"]
            cache_dirty = False
            # A cache entry from before c_adj became a TWSE-table+Yahoo
            # combine (or before Phase 6a at all) never got a real "c_adj"
            # -- augment/upgrade it in place (one extra Yahoo request, no
            # re-fetch of daily/revenue/financials) and persist the result
            # so this only happens once per code.
            if bars and "c_adj" not in bars[0]:
                print(f"[{i + 1}/{n}] {code}（快取補算 adj_factor）")
                bars = augment_bars_with_adj_factor(code, bars, limiter, market, twse_events)
                cached = {**cached, "bars": bars}
                cache_dirty = True
            # per_pbr_series is a standalone 1-request fetch (unlike margin
            # trend below, which rides on financials_rows already fetched
            # only at first-fetch time) -- cheap enough to backfill into an
            # old cache entry the same way c_adj is above.
            if "per_pbr_series" not in cached:
                limiter.before_request()
                per_pbr_rows = ds.get_per_pbr(code, days=days, cache=False)
                cached = {**cached, "per_pbr_series": ff.value_series(per_pbr_rows or [])}
                cache_dirty = True
            if cache_dirty:
                save_code_cache(code, cached)
            universe_data[code] = {
                "bars": bars,
                "bar_dates": [b["date"] for b in bars],
                "revenue_series": cached["revenue_series"],
                "roe_series": cached["roe_series"],
                "value_series": cached["per_pbr_series"],
                # margin_trend_series is only ever populated at first-fetch
                # time below (needs financials_rows re-fetched, not zero-
                # cost) -- absent on any cache entry from before Phase 6b or
                # never backfilled, factor_eval.evaluate_candidate_factor()
                # tolerates a missing key per code (see that function's
                # docstring), so this .get() default is enough.
                "gross_margin_trend_series": cached.get("gross_margin_trend_series", []),
                "operating_margin_trend_series": cached.get("operating_margin_trend_series", []),
            }
            continue

        print(f"[{i + 1}/{n}] {code}")
        limiter.before_request()
        try:
            daily = ds.get_daily(code, days=days, cache=False)
        except Exception as e:
            print(f"  daily FAILED: {e}")
            save_code_cache(code, {"skip": True, "days": days})
            continue
        bars = daily_to_bars(daily)

        if not _passed_liquidity_ever(bars):
            print("  never liquid -- skipping fundamentals fetch")
            save_code_cache(code, {"skip": True, "days": days})
            continue

        bars = augment_bars_with_adj_factor(code, bars, limiter, market, twse_events)

        limiter.before_request()
        revenue_rows = ds.get_monthly_revenue(code, days=days, cache=False)
        limiter.before_request()
        limiter.before_request()  # get_financials() issues 2 requests internally
        financials_rows = ds.get_financials(code, days=days, cache=False)
        limiter.before_request()
        per_pbr_rows = ds.get_per_pbr(code, days=days, cache=False)

        revenue_series = ff.revenue_yoy_series(revenue_rows or [])
        roe_series_ = ff.roe_series(financials_rows or [])
        if not revenue_series or not roe_series_:
            print("  insufficient revenue/financials history -- skipping")
            save_code_cache(code, {"skip": True, "days": days})
            continue

        # Zero extra requests -- financials_rows was already fetched above
        # for roe_series() (docs/https-finlab-finance-blog-python-get-tai-
        # memoized-lighthouse.md Phase 6b).
        gross_margin_trend = ff.gross_margin_trend_series(financials_rows or [])
        operating_margin_trend = ff.operating_margin_trend_series(financials_rows or [])
        value_series_ = ff.value_series(per_pbr_rows or [])

        save_code_cache(code, {
            "bars": bars, "revenue_series": revenue_series, "roe_series": roe_series_,
            "per_pbr_series": value_series_, "gross_margin_trend_series": gross_margin_trend,
            "operating_margin_trend_series": operating_margin_trend, "days": days,
        })
        universe_data[code] = {
            "bars": bars, "bar_dates": [b["date"] for b in bars],
            "revenue_series": revenue_series, "roe_series": roe_series_,
            "value_series": value_series_, "gross_margin_trend_series": gross_margin_trend,
            "operating_margin_trend_series": operating_margin_trend,
        }
    return universe_data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", choices=["watchlist", "all"], default="watchlist")
    parser.add_argument("--days", type=int, default=1800, help="daily-bar history window (default ~5y)")
    parser.add_argument("--top-n", type=int, default=ff.RANKING_TOP_N)
    parser.add_argument("--capital", type=float, default=pb.PORTFOLIO_PARAMS["initial_capital"])
    args = parser.parse_args()
    if args.capital <= 0:
        parser.error("--capital must be positive")

    codes = _load_universe(args.universe)
    print(f"universe={args.universe} ({len(codes)} codes), days={args.days}")
    print("提醒：c_adj（動能因子排名 + 損益計算都用它）結合 TWSE 除權息表與 Yahoo 比值交叉驗證，"
          "逐檔盡力而為——任一代碼若不變量檢查未過，該檔會退回原始價（見執行過程中的 ⚠ 警告）。")

    universe = ds.get_universe(cache=True) or []
    market_by_code = {u["code"]: u["market"] for u in universe}
    start_year = (dt.date.today() - dt.timedelta(days=args.days)).year
    years = range(start_year, dt.date.today().year + 1)
    print(f"抓取 TWSE 除權息表（市場層級，{years[0]}-{years[-1]}，共 {len(years)} 個 request）...")
    twse_by_code = fetch_twse_table(years, codes)

    limiter = RateLimiter()
    universe_data = fetch_universe_data(codes, args.days, limiter, market_by_code, twse_by_code)
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
