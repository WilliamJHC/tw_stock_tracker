# -*- coding: utf-8 -*-
"""One-off merge: splice cache/factor_run_long/*.json's pre-2021-08 history
(2012-05 onward, an orphaned directory structurally identical to
cache/factor_run/*.json but never wired into the live pipeline) onto the
FRONT of cache/factor_run/*.json's existing bars/revenue_series/roe_series,
extending the multi-factor backtest's usable sample from ~5 years to
~14 years for codes where cache/factor_run_long_verify.jsonl already
confirmed the recomputed c_adj matches the stored one exactly.

Eligibility (a code is merged only if ALL of these hold):
  1. cache/factor_run_long_verify.jsonl has a row for it with
     error is None and n_mismatch == 0 (verified clean against the
     CURRENT adjustment logic -- see verify_factor_run_long_adjfactors.py).
  2. cache/factor_run/{code}.json is currently enabled (no "skip" key,
     has a "bars" list) -- codes currently skipped are a separate "skip
     revival" question, left untouched here regardless of how clean their
     factor_run_long verification is.
  3. Not already merged by a previous run of this script (idempotency --
     see "long_history_merged" below).

Merge, per eligible code:
  - cutoff_date = active["bars"][0]["date"]; prepend every long["bars"] row
    with date < cutoff_date (strict, so the join has no duplicate date).
  - Same idea for revenue_series/roe_series, keyed on "usable_date"
    instead of "date".
  - per_pbr_series/gross_margin_trend_series/operating_margin_trend_series/
    fundamentals_days/days/skip are left exactly as in `active` --
    factor_run_long doesn't have these keys at all.
  - Stamps "long_history_merged": true and "long_history_start_date" onto
    the written record so a re-run of this script is a no-op for that code.

This script does NOT touch cache/factor_run_long/ (read-only) and backs up
the whole cache/factor_run/ directory to cache/factor_run_backup_pre_longmerge/
before writing anything (see --force-backup).

Usage:
    python scripts/merge_factor_run_long_history.py --dry-run
    python scripts/merge_factor_run_long_history.py --codes 2330,2317 --limit 999
    python scripts/merge_factor_run_long_history.py
"""
import argparse
import glob
import json
import os
import shutil
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACTIVE_DIR = os.path.join(BASE_DIR, "cache", "factor_run")
LONG_DIR = os.path.join(BASE_DIR, "cache", "factor_run_long")
VERIFY_PATH = os.path.join(BASE_DIR, "cache", "factor_run_long_verify.jsonl")
BACKUP_DIR = os.path.join(BASE_DIR, "cache", "factor_run_backup_pre_longmerge")


def load_verify_rows(path):
    """code -> parsed JSONL row from verify_factor_run_long_adjfactors.py's
    output. Not present in the dict at all == factor_run_long has no file
    for that code (never fetched into the long-history run)."""
    rows = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            rows[row["code"]] = row
    return rows


def load_active_cache(code):
    path = os.path.join(ACTIVE_DIR, f"{code}.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_long_cache(code):
    path = os.path.join(LONG_DIR, f"{code}.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_active_cache(code, data):
    """Same style as run_factor_backtest.py's save_code_cache() -- compact
    (no ensure_ascii=False, no indent) to match the existing 2148 cache
    files' on-disk format."""
    path = os.path.join(ACTIVE_DIR, f"{code}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def classify_codes(verify_rows):
    """Buckets every code that appears in cache/factor_run/ into disjoint
    sets, purely for reporting -- doesn't load cache/factor_run_long/ at
    all (that happens later, per selected code).

    Returns a dict of code -> bucket name, one of:
      "eligible"        clean verify + active enabled + not yet merged
      "already_merged"  clean verify + active enabled + already merged
      "mismatch"        active enabled but verify found n_mismatch > 0
      "skip"            clean verify but active is currently skip-state
      "no_data"         active enabled/skip but no clean long-history data
                         (missing from verify_rows, or verify errored --
                         i.e. factor_run_long itself has {"skip": true})
    """
    active_paths = glob.glob(os.path.join(ACTIVE_DIR, "*.json"))
    buckets = {}
    for path in active_paths:
        code = os.path.splitext(os.path.basename(path))[0]
        with open(path, encoding="utf-8") as f:
            active = json.load(f)
        is_enabled = "bars" in active
        row = verify_rows.get(code)
        # A row only counts as "clean" if it also has no structural adj_factor
        # violations and wasn't verified under a guessed (possibly wrong)
        # market -- n_mismatch==0 alone isn't sufficient (see code review).
        row_trustworthy = row is not None and row["error"] is None and not row.get("violations") and not row.get("market_guessed")
        clean = row_trustworthy and row["n_mismatch"] == 0
        mismatched = row_trustworthy and row["n_mismatch"] > 0

        if not is_enabled:
            buckets[code] = "skip" if clean else "no_data"
        elif mismatched:
            buckets[code] = "mismatch"
        elif clean:
            buckets[code] = "already_merged" if active.get("long_history_merged") else "eligible"
        else:
            buckets[code] = "no_data"
    return buckets


def merge_one(code):
    """Returns (new_data, stats) on success, or raises ValueError on a
    data problem the caller should log-and-skip rather than let crash the
    whole batch (a strict-increasing-date assertion failure)."""
    active = load_active_cache(code)
    long_data = load_long_cache(code)

    cutoff_date = active["bars"][0]["date"]
    prefix_bars = [b for b in long_data["bars"] if b["date"] < cutoff_date]
    new_bars = prefix_bars + active["bars"]
    if not all(new_bars[i]["date"] < new_bars[i + 1]["date"] for i in range(len(new_bars) - 1)):
        raise ValueError("merged bars are not strictly increasing / has a duplicate date")

    stats = {"bars_added": len(prefix_bars), "revenue_added": 0, "roe_added": 0}
    new_data = dict(active)  # per_pbr_series/gross_margin_trend_series/operating_margin_trend_series/
    new_data["bars"] = new_bars  # fundamentals_days/days/skip all carried over untouched via dict(active)

    for field, date_key in (("revenue_series", "usable_date"), ("roe_series", "usable_date")):
        active_series = active.get(field)
        if not active_series:
            print(f"  {code}: 警告 -- active['{field}'] 是空的，略過這個欄位的合併")
            continue
        # NOTE: no strict-increasing assertion here (unlike bars above) --
        # active's OWN revenue_series/roe_series can already contain a
        # duplicate usable_date from an upstream data revision (observed
        # in 2330: two 2026-04-21 rows), unrelated to this merge. The
        # prefix/active split point itself can't introduce a duplicate
        # (prefix is strictly < cutoff, active starts at cutoff), so
        # asserting here would only reject good merges over a pre-existing,
        # out-of-scope data quality issue.
        cutoff = active_series[0][date_key]
        prefix = [r for r in long_data.get(field, []) if r[date_key] < cutoff]
        new_series = prefix + active_series
        new_data[field] = new_series
        stats["revenue_added" if field == "revenue_series" else "roe_added"] = len(prefix)

    new_data["long_history_merged"] = True
    new_data["long_history_start_date"] = new_bars[0]["date"]
    return new_data, stats


def backup_active_dir(force):
    if os.path.exists(BACKUP_DIR):
        if not force:
            print(f"備份目錄已存在：{BACKUP_DIR}，中止（避免覆蓋掉可能是先前已做過的備份）。"
                  f"確認要重建請加 --force-backup。")
            sys.exit(1)
        print(f"--force-backup：刪除既有備份目錄 {BACKUP_DIR} 並重建")
        shutil.rmtree(BACKUP_DIR)
    print(f"備份 {ACTIVE_DIR} -> {BACKUP_DIR} ...")
    shutil.copytree(ACTIVE_DIR, BACKUP_DIR)
    print("備份完成。")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="只印統計，不寫入任何檔案、不做備份")
    ap.add_argument("--limit", type=int, default=None, help="只處理前 N 個符合資格的代碼（依代碼字串排序）")
    ap.add_argument("--codes", type=str, default=None, help="逗號分隔的代碼子集，只處理這幾檔")
    ap.add_argument("--force-backup", action="store_true", help="備份目錄已存在時，先刪除再重建")
    args = ap.parse_args()

    verify_rows = load_verify_rows(VERIFY_PATH)
    buckets = classify_codes(verify_rows)

    counts = {}
    for bucket in buckets.values():
        counts[bucket] = counts.get(bucket, 0) + 1
    print("=== 資格分類統計（全體 cache/factor_run/ 代碼） ===")
    print(f"  eligible（乾淨且啟用中，本次會處理）      : {counts.get('eligible', 0)}")
    print(f"  already_merged（先前已合併過，本次略過）  : {counts.get('already_merged', 0)}")
    print(f"  mismatch（啟用中但驗證有 mismatch，排除） : {counts.get('mismatch', 0)}")
    print(f"  skip（驗證乾淨但目前是 skip 狀態，不動）  : {counts.get('skip', 0)}")
    print(f"  no_data（兩邊都沒有真正資料可用）         : {counts.get('no_data', 0)}")
    print(f"  合計代碼數：{sum(counts.values())}")

    eligible_codes = sorted(c for c, b in buckets.items() if b == "eligible")

    if args.codes:
        requested = args.codes.split(",")
        selected = []
        for c in requested:
            if c not in buckets:
                print(f"  警告：{c} 不在 cache/factor_run/ 底下，略過")
            elif buckets[c] != "eligible":
                print(f"  警告：{c} 不符合合併資格（分類：{buckets[c]}），略過")
            else:
                selected.append(c)
    else:
        selected = eligible_codes

    if args.limit is not None:
        selected = selected[:args.limit]

    print(f"\n=== 本次選定要處理的代碼數：{len(selected)} ===")

    if args.dry_run:
        total_bars = total_rev = total_roe = 0
        for code in selected:
            try:
                active = load_active_cache(code)
                long_data = load_long_cache(code)
                cutoff_date = active["bars"][0]["date"]
                n_bars_add = sum(1 for b in long_data["bars"] if b["date"] < cutoff_date)
                n_rev_add = 0
                if active.get("revenue_series"):
                    cutoff = active["revenue_series"][0]["usable_date"]
                    n_rev_add = sum(1 for r in long_data.get("revenue_series", []) if r["usable_date"] < cutoff)
                n_roe_add = 0
                if active.get("roe_series"):
                    cutoff = active["roe_series"][0]["usable_date"]
                    n_roe_add = sum(1 for r in long_data.get("roe_series", []) if r["usable_date"] < cutoff)
            except (OSError, ValueError, KeyError, IndexError) as e:
                # Same exception set as merge_one()'s real-run path, so a
                # code that would be logged-and-skipped for real doesn't
                # instead crash the whole dry-run preview.
                print(f"  {code}: 讀取/計算失敗（{type(e).__name__}: {e}），略過")
                continue
            total_bars += n_bars_add
            total_rev += n_rev_add
            total_roe += n_roe_add
            print(f"  {code}: +{n_bars_add} bars, +{n_rev_add} revenue, +{n_roe_add} roe "
                  f"(現有 bars[0]={active['bars'][0]['date']} -> long bars[0]={long_data['bars'][0]['date']})")
        print(f"\n[dry-run] 合計會新增：{total_bars} bars, {total_rev} revenue rows, {total_roe} roe rows"
              f"（{len(selected)} 檔）。未寫入任何檔案，未做備份。")
        return

    backup_active_dir(args.force_backup)

    n_ok = 0
    n_error = 0
    for code in selected:
        try:
            new_data, stats = merge_one(code)
        except (OSError, ValueError, KeyError, IndexError) as e:
            print(f"  {code}: 合併失敗（{type(e).__name__}: {e}），略過，未寫入")
            n_error += 1
            continue
        save_active_cache(code, new_data)
        n_ok += 1
        print(f"  {code}: OK, +{stats['bars_added']} bars, +{stats['revenue_added']} revenue, "
              f"+{stats['roe_added']} roe -> long_history_start_date={new_data['long_history_start_date']}")

    print(f"\n完成：{n_ok} 檔合併成功寫入，{n_error} 檔失敗略過（原檔案未變動）。"
          f"備份在 {BACKUP_DIR}")


if __name__ == "__main__":
    main()
