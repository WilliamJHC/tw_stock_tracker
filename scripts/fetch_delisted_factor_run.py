# -*- coding: utf-8 -*-
"""Backfills delisted / merged-away common stocks into cache/factor_run_delisted/
so the multi-factor backtest can be re-run without survivorship bias
(docs/delisted_survivorship_preregistration_2026-10.md).

Why a separate directory: cache/factor_run/ feeds the live pipeline
(daily_recommendations, optimize, the cloud snapshot). Putting delisted codes
there would make them ranking candidates for real recommendations. The
research scripts load both directories; nothing live reads this one.

Source list: FinMind TaiwanStockDelisting, 4-digit codes delisted on or after
MIN_DELIST_DATE. A code that already has usable bars in cache/factor_run/ is
left there (not re-fetched). Per code this fetches: daily bars, dividend
events (for c_adj), monthly revenue and financials. FinMind keeps price data
for delisted codes but its revenue/financials coverage for them is patchy, so
every code gets a status in its cache file and the run ends with a coverage
table:
    ok          bars + revenue_series + roe_series (rankable)
    dark        bars pass the liquidity bar but revenue or ROE series is empty
                (price-only; not rankable, see the pre-registration)
    illiquid    never passed finlab_factors.liquidity_ok()
    no_price    FinMind returned no price rows
    code_reused bars continue past the delisting date (the code was reissued)

Resumable: a code with a cache file is skipped. Delete the file to retry it.

Usage (from the project root):
    python scripts/fetch_delisted_factor_run.py [--limit N] [--dry-run]
"""
import argparse
import collections
import datetime as dt
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import adjust  # noqa: E402
import data_sources as ds  # noqa: E402
import finlab_factors as ff  # noqa: E402
import run_factor_backtest as rfb  # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DELISTED_DIR = os.path.join(BASE_DIR, "cache", "factor_run_delisted")

MIN_DELIST_DATE = "2013-06-01"
HISTORY_START = "2010-01-01"  # revenue YoY and ROE TTM need lead-in before the 2013-12 backtest start
REUSE_TOLERANCE_DAYS = 10     # bars this far past the delisting date mean the code was reissued
_CODE_RE = re.compile(r"^[1-9]\d{3}$")


def delisted_path(code):
    return os.path.join(DELISTED_DIR, f"{code}.json")


def candidates(delisting_rows):
    """[{code, name, delisted_date}] for 4-digit common-stock codes delisted
    on or after MIN_DELIST_DATE, ascending by code. Tabs/spaces in FinMind's
    names are stripped."""
    by_code = {}
    for r in delisting_rows:
        code = (r.get("stock_id") or "").strip()
        date = r.get("date") or ""
        if _CODE_RE.match(code) and date >= MIN_DELIST_DATE:
            by_code[code] = {"code": code, "name": (r.get("stock_name") or "").strip(), "delisted_date": date}
    return [by_code[c] for c in sorted(by_code)]


def dividend_actions(rows):
    """TaiwanStockDividendResult rows -> adjust.build_adj_factor() actions.
    ratio = reference price / pre-ex-date close, i.e. the ex-date price drop
    that cash and stock dividends cause. Rows with a missing or implausible
    price pair are dropped."""
    actions = []
    for r in rows:
        before, ref = r.get("before_price"), r.get("reference_price")
        if not before or not ref or before <= 0 or ref <= 0:
            continue
        ratio = ref / before
        if 0.3 < ratio <= 1.5:
            actions.append({"date": r["date"], "ratio": ratio})
    return actions


def add_c_adj(bars, actions):
    """bars with a "c_adj" key (total-return back-adjusted close). Falls back
    to raw close, factor 1.0, when adjust.validate_adj_factor() reports a
    violation -- same fallback as run_factor_backtest.augment_bars_with_adj_factor()."""
    dates = [b["date"] for b in bars]
    factors = adjust.build_adj_factor(dates, actions)
    if adjust.validate_adj_factor(dates, factors, [a["date"] for a in actions]):
        return [{**b, "c_adj": b["c"]} for b in bars], False
    return [{**b, "c_adj": b["c"] * f} for b, f in zip(bars, factors)], True


def build_entry(cand, fetch_prices, fetch_dividends, fetch_revenue, fetch_financials):
    """One code's cache entry. fetch_* are zero-arg-after-code callables so
    tests can inject fixtures; each returns the same shape as the data_sources
    function it wraps. Revenue/financials are only requested for codes whose
    bars clear the liquidity bar, to save FinMind quota."""
    entry = {"code": cand["code"], "name": cand["name"], "delisted_date": cand["delisted_date"]}
    rows = fetch_prices(cand["code"])
    hist = ds._finmind_price_rows_to_hist(rows) if rows else None
    if not hist:
        return {**entry, "status": "no_price"}
    bars = rfb.daily_to_bars(hist)
    last = bars[-1]["date"]
    cutoff = (dt.date.fromisoformat(cand["delisted_date"]) + dt.timedelta(days=REUSE_TOLERANCE_DAYS)).isoformat()
    if last > cutoff:
        return {**entry, "status": "code_reused", "last_bar": last}
    if not rfb._passed_liquidity_ever(bars):
        return {**entry, "status": "illiquid", "last_bar": last}

    bars, adj_ok = add_c_adj(bars, dividend_actions(fetch_dividends(cand["code"]) or []))
    revenue_series = ff.revenue_yoy_series(fetch_revenue(cand["code"]) or [])
    roe_series = ff.roe_series(fetch_financials(cand["code"]) or [])
    return {
        **entry,
        "status": "ok" if revenue_series and roe_series else "dark",
        "last_bar": last, "adj_ok": adj_ok, "bars": bars,
        "revenue_series": revenue_series, "roe_series": roe_series,
    }


def _already_in_factor_run(code):
    """True when cache/factor_run/ already holds usable (non-skip) bars for `code`."""
    try:
        with open(rfb.code_cache_path(code), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    return not data.get("skip") and bool(data.get("bars"))


def _save(code, entry):
    os.makedirs(DELISTED_DIR, exist_ok=True)
    tmp = delisted_path(code) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entry, f)
    os.replace(tmp, delisted_path(code))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0, help="fetch at most N codes this run (0 = all)")
    ap.add_argument("--dry-run", action="store_true", help="list what would be fetched, no per-code requests")
    args = ap.parse_args()

    cands = candidates(ds._finmind("TaiwanStockDelisting"))
    todo, in_main = [], 0
    for c in cands:
        if _already_in_factor_run(c["code"]):
            in_main += 1
        elif not os.path.exists(delisted_path(c["code"])):
            todo.append(c)
    print(f"delisted candidates (>= {MIN_DELIST_DATE}): {len(cands)}; already usable in factor_run: {in_main}; "
          f"cached here: {len(cands) - in_main - len(todo)}; to fetch: {len(todo)}")
    if args.dry_run:
        return
    if args.limit:
        todo = todo[: args.limit]

    limiter = rfb.RateLimiter()
    days = (dt.date.today() - dt.date.fromisoformat(HISTORY_START)).days

    def prices(code):
        limiter.before_request()
        return ds._finmind("TaiwanStockPrice", code, HISTORY_START)

    def dividends(code):
        limiter.before_request()
        return ds._finmind("TaiwanStockDividendResult", code, HISTORY_START)

    def revenue(code):
        limiter.before_request()
        return ds.get_monthly_revenue(code, days=days, cache=False)

    def financials(code):
        for _ in range(3):  # get_financials() issues 3 requests internally
            limiter.before_request()
        return ds.get_financials(code, days=days, cache=False)

    counts = collections.Counter()
    for i, cand in enumerate(todo, 1):
        try:
            entry = build_entry(cand, prices, dividends, revenue, financials)
        except (ds.SourceError, OSError) as e:
            print(f"[{i}/{len(todo)}] {cand['code']} {cand['name']}: FETCH FAILED ({e}); will retry next run")
            continue
        _save(cand["code"], entry)
        counts[entry["status"]] += 1
        print(f"[{i}/{len(todo)}] {cand['code']} {cand['name']} delisted {cand['delisted_date']}: {entry['status']}")
    print("done this run:", dict(counts))


if __name__ == "__main__":
    main()
