# -*- coding: utf-8 -*-
"""Periodic refresh of the monthly-revenue and quarterly-financials series
inside cache/factor_run/*.json (.github/workflows/update_fundamentals.yml).

Why this exists: update_universe_bars.py only rewrites `bars`, so revenue_series
(weight 0.4 in the ranking) and roe_series (0.3) -- and the margin-trend /
accrual series -- were frozen at seed time. After one month the live ranking
runs on stale revenue YoY, after two on stale ROE.

How it works
  * Raw FinMind rows are never stored, only the derived series. So per code we
    re-fetch a short recent window (revenue 760 days = 1 request, financials 900
    days = 3 requests), recompute the series from it, and MERGE them into the
    cached full-history series (merge_series). Replacing instead of merging would
    truncate the multi-year history the backtests rely on.
  * "Is this code due?" is decided by a target period, not by a calendar:
      revenue    -> the month whose disclosure deadline (the 10th of the
                    following month) has passed, see revenue_target()
      financials -> the latest quarter whose legal filing deadline (5/15, 8/14,
                    11/14, 3/31) has passed, see financials_target()
    A code counts as refreshed once its fetched rows reach the target period
    (stored in cached["fund_refresh"]). Because of that the job is idempotent and
    resumable: a missed cron, a time-boxed stop or a re-run just picks up the
    codes that are still behind. A code whose rows do NOT reach the target (late
    filer, FinMind lag) is attempted at most once per day, so the relay chain
    cannot loop on it; the next scheduled run retries it.

Cost (FinMind limiter: 550 req/hr): revenue ~1 req/code -> ~1800 codes ~3.3h;
financials 3 req/code -> ~10h, i.e. 2-3 relay slices of --budget-minutes.

Usage:
    python scripts/update_fundamentals.py [--kind auto|revenue|financials]
                                          [--budget-minutes 300] [--limit N]
                                          [--dry-run]

Reports through $GITHUB_OUTPUT (when set): `remaining` (items still due, not yet
attempted today) and `progress` (items this run handled without a fetch
failure); the workflow chains another slice only while both are > 0.
"""
import argparse
import datetime as dt
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import alerts  # noqa: E402
import data_sources as ds  # noqa: E402
import finlab_factors as ff  # noqa: E402

import run_factor_backtest as rfb  # noqa: E402
import update_universe_bars as uub  # noqa: E402

REVENUE_DAYS = 760     # >= 13 months of rows are needed for a single YoY point
FINANCIALS_DAYS = 900  # ~10 quarters: enough lookback for the YoY-style margin trends
MERGE_TOLERANCE_DAYS = 15  # same period's usable_date can differ (announce_date vs fixed lag)
PROBE_CODE = "2330"
MAX_CONSECUTIVE_FAILURES = 10
REVENUE_DEADLINE_DAY = 10
# (quarter-end month/day, filing-deadline month/day, deadline year offset)
_QUARTERS = [((3, 31), (5, 15), 0), ((6, 30), (8, 14), 0), ((9, 30), (11, 14), 0), ((12, 31), (3, 31), 1)]

# series key -> financials-derived builder; all ride on the same financials rows
FINANCIALS_SERIES = {
    "roe_series": ff.roe_series,
    "gross_margin_trend_series": ff.gross_margin_trend_series,
    "operating_margin_trend_series": ff.operating_margin_trend_series,
    "accrual_quality_series": ff.accrual_quality_series,
}


def revenue_target(today):
    """First day ("YYYY-MM-01") of the newest revenue month that should be
    disclosed by `today`: the previous month once the 10th has passed, else the
    month before that."""
    months_back = 1 if today.day > REVENUE_DEADLINE_DAY else 2
    y, m = today.year, today.month - months_back
    while m < 1:
        y, m = y - 1, m + 12
    return f"{y:04d}-{m:02d}-01"


def financials_target(today):
    """Quarter-end date ("YYYY-MM-DD") of the newest quarter whose legal filing
    deadline is strictly before `today`."""
    best = None
    for year in (today.year - 1, today.year):
        for (qm, qd), (dm, dd), offset in _QUARTERS:
            if dt.date(year + offset, dm, dd) < today:
                q = dt.date(year, qm, qd).isoformat()
                if best is None or q > best:
                    best = q
    return best


def first_yoy_lag_date(rows):
    """Fixed-lag usable_date of the first revenue period that yields a YoY point
    (its prior-year month is present). Deterministic -- unlike the fresh series'
    first usable_date, which may be a later announce_date -- so it is a stable
    anchor for where the cached series should be cut."""
    dates = {r["date"] for r in rows}
    for d in sorted(dates):
        if f"{int(d[:4]) - 1:04d}{d[4:]}" in dates:
            return ff._month_end_lag_date(d, ff.REVENUE_LAG_DAYS)
    return None


def merge_series(old, new, tolerance_days=MERGE_TOLERANCE_DAYS, anchor_date=None):
    """Append the freshly recomputed `new` series to the cached `old` one.
    Old points from the first refetched period onward are dropped (the fresh
    fetch wins, e.g. restated figures). That period is located by `anchor_date`
    when given (revenue: first_yoy_lag_date()), else by new[0]'s usable_date --
    exact for the quarterly series, whose usable_date is quarter-end + 60d in
    every computation. The tolerance absorbs a period's usable_date moving a few
    days between computations. usable_date is then clamped to be non-decreasing
    -- _as_of() bisects assuming ascending order. An empty `new` leaves `old`
    untouched."""
    if not new:
        return old or []
    cut = ff._shift_date(anchor_date or new[0]["usable_date"], -tolerance_days)
    merged = [p for p in (old or []) if p["usable_date"] < cut] + list(new)
    out, last = [], None
    for p in merged:
        if last is not None and p["usable_date"] < last:
            p = {**p, "usable_date": last}
        last = p["usable_date"]
        out.append(p)
    return out


def is_due(cached, kind, target, today):
    """True when this cache entry still needs a `kind` refresh towards `target`
    and has not already been attempted today."""
    if cached.get("skip") or not cached.get("bars"):
        return False
    fr = cached.get("fund_refresh") or {}
    return fr.get(kind) != target and fr.get(f"{kind}_attempt") != today.isoformat()


def refresh_code(code, kind, target, today, dry_run, limiter, fetch_revenue=None, fetch_financials=None):
    """Returns {"status": "updated"|"stale"|"insufficient"|"failed"|"skipped",
    "detail": str}.
      updated      rows reached the target period (marked done)
      stale        fetched and merged, but the target period is not published
                   yet (retried at most once per day)
      insufficient rows came back but are too few to build the series (new
                   listing, gaps) -- terminal for this period, so it is marked
                   done instead of burning requests on every run
      failed       no rows at all: a fetch error and "no data" look the same
                   (get_*() return None for both), so nothing is written and
                   no marker is set -- a transient 429/timeout is retried by
                   the next slice/run rather than waiting a whole period
    Never raises."""
    fetch_revenue = fetch_revenue or ds.get_monthly_revenue
    fetch_financials = fetch_financials or ds.get_financials
    try:
        with open(rfb.code_cache_path(code), encoding="utf-8") as f:
            cached = json.load(f)
    except (OSError, ValueError) as e:
        return {"status": "failed", "detail": f"cache read error: {e}"}
    if not is_due(cached, kind, target, today):
        return {"status": "skipped", "detail": "not due"}

    fr = dict(cached.get("fund_refresh") or {})
    anchors = {}
    try:
        if kind == "revenue":
            limiter.before_request()
            rows = fetch_revenue(code, days=REVENUE_DAYS, cache=False)
            fresh = {"revenue_series": ff.revenue_yoy_series(rows or [])}
            anchors["revenue_series"] = first_yoy_lag_date(rows or [])
            ok = bool(fresh["revenue_series"])
        else:
            for _ in range(3):  # get_financials() issues 3 requests (income/balance/cashflow)
                limiter.before_request()
            rows = fetch_financials(code, days=FINANCIALS_DAYS, cache=False)
            fresh = {key: fn(rows or []) for key, fn in FINANCIALS_SERIES.items()}
            ok = bool(fresh["roe_series"])
    except Exception as e:  # noqa: BLE001 -- one bad code must not kill the run
        return {"status": "failed", "detail": f"{kind} fetch error: {e}"}

    if not rows:
        return {"status": "failed", "detail": f"{kind}: no rows (fetch error or no data)"}

    fr[f"{kind}_attempt"] = today.isoformat()
    if not ok:
        fr[kind] = target
        cached["fund_refresh"] = fr
        if not dry_run:
            rfb.save_code_cache(code, cached)
        return {"status": "insufficient", "detail": f"{kind}: {len(rows)} rows, too few for a series"}

    latest = rows[-1]["date"]
    reached = latest >= target
    if kind == "financials":
        # The cash-flow request is swallowed inside get_financials(): if it alone
        # failed, the newest quarter has no operating_cash_flow and
        # accrual_quality_series silently lacks it. Don't mark done in that case.
        reached = reached and rows[-1].get("operating_cash_flow") is not None
    for key, series in fresh.items():
        cached[key] = merge_series(cached.get(key), series, anchor_date=anchors.get(key))
    if reached:
        fr[kind] = target
    cached["fund_refresh"] = fr
    detail = f"{kind} latest={latest} target={target}"
    if not dry_run:
        rfb.save_code_cache(code, cached)
    status = "updated" if reached else "stale"
    return {"status": status, "detail": ("[dry-run] " if dry_run else "") + detail}


def build_todo(codes, kinds, today):
    """[(kind, code, target)] in `kinds` order (revenue first: higher weight and
    cheaper). Reads each cache file (multi-MB with bars) once."""
    targets = {"revenue": revenue_target(today), "financials": financials_target(today)}
    due = {kind: [] for kind in kinds}
    for code in codes:
        try:
            with open(rfb.code_cache_path(code), encoding="utf-8") as f:
                cached = json.load(f)
        except (OSError, ValueError):
            continue
        for kind in kinds:
            if is_due(cached, kind, targets[kind], today):
                due[kind].append((kind, code, targets[kind]))
    return [item for kind in kinds for item in due[kind]]


def probe_finmind():
    """True when a real FinMind request succeeds. Deliberately NOT
    ds.get_daily(): that falls back to Fugle/TWSE/yfinance, so it would pass
    with a bad FINMIND_TOKEN while every revenue/financials request fails."""
    try:
        start = (dt.date.today() - dt.timedelta(days=30)).isoformat()
        return bool(ds._finmind("TaiwanStockPrice", PROBE_CODE, start))
    except Exception as e:  # noqa: BLE001 -- any failure means "do not trust this run"
        print(f"probe {PROBE_CODE} failed: {e}")
        return False


def run(todo, today, dry_run, budget_seconds, limiter, refresh=refresh_code, probe=probe_finmind,
        clock=time.monotonic):
    """Process `todo` until the budget is spent. Returns (counts, attempted,
    failures). Raises RuntimeError after MAX_CONSECUTIVE_FAILURES failures in a
    row when the probe request also fails (bad token / outage): the workflow then
    discards this slice instead of saving it."""
    counts = {"updated": 0, "stale": 0, "insufficient": 0, "failed": 0, "skipped": 0}
    failures, attempted, streak = [], 0, 0
    deadline = clock() + budget_seconds
    for i, (kind, code, target) in enumerate(todo):
        if clock() >= deadline:
            print(f"time budget used up after {i} of {len(todo)} items -- stopping")
            break
        result = refresh(code, kind, target, today, dry_run, limiter)
        counts[result["status"]] = counts.get(result["status"], 0) + 1
        if result["status"] != "skipped":
            attempted += 1
        print(f"[{i + 1}/{len(todo)}] {kind} {code}: {result['status']} -- {result['detail']}")
        if result["status"] == "failed":
            failures.append(f"{kind} {code}: {result['detail']}")
            streak += 1
            if streak >= MAX_CONSECUTIVE_FAILURES:
                if not probe():
                    raise RuntimeError(f"{streak} consecutive failures and the probe request failed "
                                       "-- likely a bad FINMIND_TOKEN or an outage; aborting")
                streak = 0
        else:
            streak = 0
    return counts, attempted, failures


def write_outputs(remaining, progress):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"remaining={remaining}\nprogress={progress}\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kind", choices=["auto", "revenue", "financials"], default="auto",
                    help="auto = every kind that is due (revenue first)")
    ap.add_argument("--budget-minutes", type=float, default=300, help="stop starting new codes after this long")
    ap.add_argument("--limit", type=int, default=None, help="only the first N cached codes (local smoke test)")
    ap.add_argument("--dry-run", action="store_true", help="fetch and merge but write nothing back")
    args = ap.parse_args()

    override = os.environ.get("CACHE_DIR_OVERRIDE")
    if override:
        rfb.RUN_CACHE_DIR = override

    all_codes = uub._cached_codes()
    if len(all_codes) < uub.MIN_CACHED_CODES and not args.limit:
        msg = (f"[基本面增量更新] 中止：cache/factor_run/ 只找到 {len(all_codes)} 檔快取"
               f"（預期 >= {uub.MIN_CACHED_CODES}），疑似 cache miss，不繼續。")
        print(msg)
        if not args.dry_run:
            alerts.send_telegram(msg)
        sys.exit(1)
    codes = all_codes[: args.limit] if args.limit else all_codes

    today = dt.date.today()
    kinds = ["revenue", "financials"] if args.kind == "auto" else [args.kind]
    todo = build_todo(codes, kinds, today)
    print(f"{len(codes)} cached codes, {len(todo)} items due "
          f"(revenue target {revenue_target(today)}, financials target {financials_target(today)})")
    if not todo:
        write_outputs(0, 0)
        print("nothing due -- already up to date")
        return
    if not probe_finmind():
        print(f"probe request for {PROBE_CODE} failed -- check FINMIND_TOKEN / connectivity; nothing written")
        sys.exit(1)

    counts, attempted, failures = run(todo, today, args.dry_run, args.budget_minutes * 60, rfb.RateLimiter())
    remaining = len(build_todo(codes, kinds, today)) if not args.dry_run else len(todo) - attempted
    # Failed items stay due (no marker), so they are not "progress": a slice that
    # only re-tried failures reports 0 and ends the relay chain.
    progress = attempted - counts["failed"]
    msg = (f"[基本面增量更新] 本次處理 {attempted} 項：updated={counts['updated']} "
           f"stale={counts['stale']}（目標期尚未公布）insufficient={counts['insufficient']}（歷史不足）"
           f"failed={counts['failed']}（無資料或抓取錯誤，下次重試）；尚餘 {remaining} 項")
    if failures:
        msg += "\n失敗清單（前 10）：" + "; ".join(failures[:10])
    print(msg)
    write_outputs(remaining, progress)
    if not args.dry_run:
        try:
            alerts.send_telegram(msg)
        except Exception as e:  # noqa: BLE001 -- a failed notice must not fail the refresh
            print(f"telegram notice failed: {e}")


if __name__ == "__main__":
    main()
