# -*- coding: utf-8 -*-
"""One-off: force a real re-evaluation of the 193 "skip revival candidate"
codes identified in docs/dev_plan_data_integrity.md's Priority 5 follow-up
item #1 -- codes currently `{"skip": true}` in cache/factor_run/ whose
cache/factor_run_long/ history verified clean (merge_factor_run_long_history.
classify_codes()'s "skip" bucket).

Why this needs its own script instead of just rerunning
run_factor_backtest.py: fetch_universe_data()'s cache-hit path does
`if cached.get("skip"): continue` unconditionally -- once a code is cached
skip:true it is NEVER re-fetched again at the same --days window, no matter
how much the fundamentals lead-in window has widened since. The only way to
get a genuine re-evaluation under the CURRENT fundamentals_days window (not
factor_run_long's narrower one -- see the dev plan doc on why that history
can't just be copied over) is to clear those codes' cache entries first so
fetch_universe_data() takes its normal cache-miss/full-fetch path.

This is a live mutation of cache/factor_run/, not a dry-run preview: any
code that now passes liquidity + fundamentals is genuinely revived (its
cache entry gets real bars/revenue/roe data and drops "skip") the moment
this script finishes that code, exactly like a normal run_factor_backtest.py
pass would. Resumable like the rest of the pipeline -- fetch_universe_data()
saves each code's cache entry as it goes, so a killed/interrupted run just
leaves not-yet-processed codes with no cache file, which a rerun of this
script naturally re-fetches (see fetch_universe_data()'s own docstring on
this pattern).

Usage:
    python scripts/reevaluate_skip_revival_candidates.py --dry-run
    python scripts/reevaluate_skip_revival_candidates.py
"""
import argparse
import datetime as dt
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import data_sources as ds  # noqa: E402
import merge_factor_run_long_history as mflh  # noqa: E402
import run_factor_backtest as rfb  # noqa: E402

DAYS = 1800  # matches run_factor_backtest.py main()'s default -- the window every
             # already-active cache entry (including the 1708 long-history-merged
             # ones) is currently stored at.
BACKUP_DIR = os.path.join(rfb.BASE_DIR, "cache", "factor_run_backup_pre_skip_reeval")


def get_revival_candidates():
    """The "skip" bucket from classify_codes(): clean factor_run_long verify
    + currently skip:true in the live cache. Same set docs/
    dev_plan_data_integrity.md counted as 193."""
    verify_rows = mflh.load_verify_rows(mflh.VERIFY_PATH)
    buckets = mflh.classify_codes(verify_rows)
    return sorted(c for c, b in buckets.items() if b == "skip")


def backup_and_clear(codes):
    """Copies each candidate's current cache/factor_run/{code}.json into
    BACKUP_DIR (so the original skip:true verdict + days is recoverable),
    then removes it from the live dir so fetch_universe_data() takes its
    cache-miss path. Codes already missing a cache file (shouldn't happen --
    classify_codes() only puts a code in the "skip" bucket if it read one --
    are logged and left alone)."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    cleared = []
    for code in codes:
        src = rfb.code_cache_path(code)
        if not os.path.exists(src):
            print(f"  {code}: 警告 -- 快取檔不存在，略過（不應該發生）")
            continue
        shutil.copy2(src, os.path.join(BACKUP_DIR, f"{code}.json"))
        os.remove(src)
        cleared.append(code)
    return cleared


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="只列出候選代碼與數量，不清快取、不連網")
    args = ap.parse_args()

    codes = get_revival_candidates()
    print(f"=== 復活候選（factor_run_long 驗證乾淨但目前 skip:true）：{len(codes)} 檔 ===")
    print(codes)

    if args.dry_run:
        print("\n[dry-run] 未清除任何快取、未連網。")
        return

    cleared = backup_and_clear(codes)
    print(f"\n已備份並清除 {len(cleared)} 檔快取到 {BACKUP_DIR}，開始重新評估（days={DAYS}）...")

    universe = ds.get_universe(cache=True) or []
    market_by_code = {u["code"]: u["market"] for u in universe}
    start_year = (dt.date.today() - dt.timedelta(days=DAYS)).year
    years = range(start_year, dt.date.today().year + 1)
    print(f"抓取 TWSE 除權息表（{years[0]}-{years[-1]}，共 {len(years)} 個 request）...")
    twse_by_code = rfb.fetch_twse_table(years, cleared)

    limiter = rfb.RateLimiter()
    universe_data = rfb.fetch_universe_data(cleared, DAYS, limiter, market_by_code, twse_by_code)

    revived = sorted(universe_data.keys())
    still_skip = sorted(set(cleared) - set(revived))
    print(f"\n=== 結果：{len(revived)}/{len(cleared)} 檔正式復活，{len(still_skip)} 檔仍不符資格 ===")
    print(f"復活：{revived}")
    print(f"仍 skip：{still_skip}")

    report_path = os.path.join(rfb.BASE_DIR, "cache", "skip_reeval_result.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({"revived": revived, "still_skip": still_skip, "days": DAYS,
                    "run_at": dt.datetime.now().isoformat()}, f, ensure_ascii=False, indent=2)
    print(f"結果已寫入 {report_path}")


if __name__ == "__main__":
    main()
