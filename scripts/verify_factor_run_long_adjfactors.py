# -*- coding: utf-8 -*-
"""One-off verification: does cache/factor_run_long/*.json's stored c_adj
(an orphaned directory, 2148 codes back to 2012-05, structurally identical
to cache/factor_run/*.json but never wired into the live pipeline) agree
with what the CURRENT adjustment logic (src/adjust.py +
scripts/build_adj_factors.py's resolve_adjustment_actions()) recomputes
from scratch?

Why this exists: the 2021-08-onward overlap between factor_run_long and
factor_run has already been spot-checked elsewhere and matches exactly, so
that segment is trusted. But 2012-01 ~ 2021-08 is history that exists
ONLY in factor_run_long -- there's no independent second source to compare
it against, and it might have been produced by an older/buggier version of
the adjustment code. Before merging it into the live backtest sample, this
script recomputes adj_factor for every historical bar the current code way
and diffs against the stored c_adj, so a stale/buggy computation gets
caught instead of silently trusted.

This script does NOT modify cache/factor_run_long/ or cache/factor_run/ --
read-only against both, writes only to its own JSONL output.

Usage:
    python scripts/verify_factor_run_long_adjfactors.py
    python scripts/verify_factor_run_long_adjfactors.py --codes 2330,2317
    python scripts/verify_factor_run_long_adjfactors.py --resume
"""
import argparse
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import adjust  # noqa: E402
import data_sources as ds  # noqa: E402

# scripts/ is auto-added to sys.path by Python (this file lives there too),
# same trick run_factor_backtest.py uses to import build_adj_factors.py.
import build_adj_factors as baf  # noqa: E402
import run_factor_backtest as rfb  # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LONG_CACHE_DIR = os.path.join(BASE_DIR, "cache", "factor_run_long")
DEFAULT_OUT = os.path.join(BASE_DIR, "cache", "factor_run_long_verify.jsonl")

# Same tolerance build_adj_factors.py uses to decide whether a day-over-day
# ratio jump is a real corporate action vs. float noise -- reused here as
# the mismatch threshold between recomputed and stored c_adj, since both
# questions are "is this difference bigger than plausible rounding".
MISMATCH_THRESHOLD = baf.STEP_THRESHOLD


def _codes_from_cache_dir():
    paths = glob.glob(os.path.join(LONG_CACHE_DIR, "*.json"))
    return sorted(os.path.splitext(os.path.basename(p))[0] for p in paths)


def _load_long_cache(code):
    with open(os.path.join(LONG_CACHE_DIR, f"{code}.json"), encoding="utf-8") as f:
        return json.load(f)


def _load_resume_codes(out_path):
    """Codes that already have a result line in `out_path` -- skipped on
    --resume so a re-run only fills in what didn't finish last time."""
    done = set()
    if not os.path.exists(out_path):
        return done
    with open(out_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if "code" in row:
                done.add(row["code"])
    return done


def verify_code(code, market, market_guessed, bars, twse_events):
    """Recomputes adj_factor for `bars` (factor_run_long's stored
    {date,c,v,c_adj} rows) via the CURRENT resolve_adjustment_actions() +
    build_adj_factor(), and diffs the recomputed c_adj against the stored
    one. Returns one result dict (this script's JSONL row shape) --
    never raises; caller wraps this in try/except as a second line of
    defense, but resolve_adjustment_actions()/build_adj_factor() are
    themselves best-effort against network failures already."""
    bar_dates = [b["date"] for b in bars]
    actions, counters = baf.resolve_adjustment_actions(code, market, bars, twse_events)
    factors = adjust.build_adj_factor(bar_dates, actions)
    violations = adjust.validate_adj_factor(bar_dates, factors, [a["date"] for a in actions])

    mismatch_dates = []
    n_mismatch = 0
    max_rel_diff = 0.0
    for bar, factor in zip(bars, factors):
        stored = bar.get("c_adj")
        if not stored:  # None or 0 -- nothing to divide by, skip
            continue
        recomputed = bar["c"] * factor
        rel_diff = abs(recomputed - stored) / stored
        if rel_diff > max_rel_diff:
            max_rel_diff = rel_diff
        if rel_diff > MISMATCH_THRESHOLD:
            n_mismatch += 1
            if len(mismatch_dates) < 5:
                mismatch_dates.append({
                    "date": bar["date"], "stored": stored,
                    "recomputed": recomputed, "rel_diff": rel_diff,
                })

    return {
        "code": code,
        "n_bars": len(bars),
        "n_events": counters["n_events"],
        "n_mismatch": n_mismatch,
        "max_rel_diff": max_rel_diff,
        "mismatch_dates": mismatch_dates,
        "violations": violations,
        "yahoo_ok": counters["yahoo_ok"],
        "market_guessed": market_guessed,
        "error": None,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--codes", type=str, default=None,
                     help="逗號分隔的代碼子集，預設跑 cache/factor_run_long/ 底下所有代碼")
    ap.add_argument("--start-year", type=int, default=None,
                     help="TWSE 除權息表起始年份，預設自動抓所有待驗證代碼裡最早的 bar 日期所在年份")
    ap.add_argument("--out", type=str, default=DEFAULT_OUT, help="輸出 JSONL 路徑")
    ap.add_argument("--resume", action="store_true",
                     help="讀取既有 --out 內容，跳過已有結果的代碼，續接寫入")
    ap.add_argument("--sleep", type=float, default=0.15, help="每檔跑完後 sleep 幾秒（禮貌對待 Yahoo）")
    args = ap.parse_args()

    all_codes = _codes_from_cache_dir()
    if args.codes:
        codes = args.codes.split(",")
        missing = [c for c in codes if c not in set(all_codes)]
        if missing:
            print(f"警告：cache/factor_run_long/ 找不到這些代碼，略過：{missing}")
        codes = [c for c in codes if c in set(all_codes)]
    else:
        codes = all_codes
    print(f"待驗證代碼數：{len(codes)}")

    universe = ds.get_universe(cache=True) or []
    market_by_code = {u["code"]: u["market"] for u in universe}

    # Pre-load every code's cached bars once -- needed both to pick
    # --start-year automatically and to drive the per-code verification
    # loop, so no point reading each file twice.
    long_cache_by_code = {}
    for code in list(codes):
        try:
            long_cache_by_code[code] = _load_long_cache(code)
        except (OSError, ValueError) as e:
            print(f"  {code}: 讀取 cache/factor_run_long/{code}.json 失敗（{e}），略過")
            codes.remove(code)

    if args.start_year is not None:
        start_year = args.start_year
    else:
        min_dates = [
            data["bars"][0]["date"] for data in long_cache_by_code.values() if data.get("bars")
        ]
        if not min_dates:
            print("沒有任何可用的 bars，中止")
            sys.exit(1)
        start_year = int(min(min_dates)[:4])
    import datetime as dt
    years = range(start_year, dt.date.today().year + 1)
    print(f"抓取 TWSE 除權息表（{years[0]}-{years[-1]}，共 {len(years)} 個 request）...")
    twse_by_code = rfb.fetch_twse_table(years, codes)

    resume_codes = _load_resume_codes(args.out) if args.resume else set()
    if resume_codes:
        print(f"--resume：略過已完成的 {len(resume_codes)} 檔")

    mode = "a" if args.resume else "w"
    n_market_guessed = 0
    n_done = 0
    n_error = 0
    with open(args.out, mode, encoding="utf-8") as out_f:
        for i, code in enumerate(codes, 1):
            if code in resume_codes:
                continue
            data = long_cache_by_code[code]
            bars = data.get("bars") or []
            market_guessed = code not in market_by_code
            if market_guessed:
                n_market_guessed += 1
            market = market_by_code.get(code, "twse")
            twse_events = twse_by_code.get(code, [])
            try:
                if not bars:
                    raise ValueError("empty bars")
                result = verify_code(code, market, market_guessed, bars, twse_events)
            except Exception as e:
                result = {
                    "code": code, "n_bars": len(bars), "n_events": None,
                    "n_mismatch": None, "max_rel_diff": None, "mismatch_dates": [],
                    "violations": [], "yahoo_ok": None, "market_guessed": market_guessed,
                    "error": f"{type(e).__name__}: {e}",
                }
                n_error += 1
            out_f.write(json.dumps(result, ensure_ascii=False) + "\n")
            out_f.flush()
            n_done += 1
            status = "OK" if result["error"] is None else f"ERROR: {result['error']}"
            print(f"[{i}/{len(codes)}] {code} ({market}"
                  f"{'*猜測*' if market_guessed else ''}): "
                  f"n_mismatch={result['n_mismatch']} {status}")
            time.sleep(args.sleep)

    print(f"\n完成：{n_done} 檔寫入 {args.out}（{n_error} 檔 error，"
          f"{n_market_guessed} 檔 market 是用預設值猜的，不在目前 universe 名單內）")


if __name__ == "__main__":
    main()
