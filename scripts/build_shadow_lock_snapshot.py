# -*- coding: utf-8 -*-
"""Build the locked snapshot of the forward shadow portfolio (spec A1, A5b-5, A5d-6, A5f-6).

Writes two files into --out-dir (default shadow/, the project root, NOT the gitignored cache/):

  universe_<stamp>.json            {"snapshot_date", "source", "count", "codes": [...]}
  industries_snapshot_<stamp>.json {"snapshot_date", "source", "industries": {code: [category, ...]}}

The universe is the set of live codes of cache/factor_run (a cache file that exists and is not
marked "skip"; 4-digit codes only).  Spec A5f-6: the cache that counts is the one the daily job
reads, i.e. the Actions cache, so on the lock day this script is run by
.github/workflows/shadow_lock_snapshot.yml, not on a local copy.  The industries are the raw
``data_sources.get_industries()`` lists restricted to the universe; the primary industry is derived
from them at load time by ``shadow_rules.primary_industry`` (A5b-5), so the stored file stays raw.

With --write-lock <ledger dir> --start-date YYYY-MM-DD it also writes <ledger dir>/meta/lock.json
(start date + SHA-256 of both files, newline-insensitive, as ``run_shadow_portfolio.check_lock``
verifies them).  Nothing here looks at prices or performance (spec A5: no S2 / S3 backtests before
the lock).

With --lock-only the snapshot files already in --out-dir (e.g. the ones the cloud workflow built and
the operator committed) are verified with the daily job's loader and only the lock is written; no
cache is read.

Existing snapshot files are never replaced unless --force is given: a locked snapshot must not be
rewritten by accident.  Run from the project root.
"""
import argparse
import json
import os
import sys
from collections import Counter
from datetime import date

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_shadow_portfolio as rsp  # noqa: E402
import shadow_rules as sr  # noqa: E402

DEFAULT_CACHE_DIR = os.path.join(BASE_DIR, "cache", "factor_run")
DEFAULT_OUT_DIR = os.path.join(BASE_DIR, "shadow")
DEFAULT_STAMP = "2026-11"
DEFAULT_MIN_CODES = 1500        # same floor as the workflow's factor_run check


class SnapshotError(Exception):
    pass


def live_codes(cache_dir):
    """(sorted live codes, stats) of a cache/factor_run directory."""
    codes, skipped, bad = [], 0, 0
    if not os.path.isdir(cache_dir):
        raise SnapshotError(f"cache directory not found: {cache_dir}")
    for name in sorted(os.listdir(cache_dir)):
        code, ext = os.path.splitext(name)
        if ext != ".json" or len(code) != 4 or not code.isdigit():
            continue
        try:
            with open(os.path.join(cache_dir, name), encoding="utf-8") as f:
                entry = json.load(f)
        except (OSError, ValueError):
            bad += 1
            continue
        if entry.get("skip"):
            skipped += 1
            continue
        codes.append(code)
    return codes, {"live": len(codes), "skipped": skipped, "bad_json": bad}


def load_industries(universe, industries_file=None):
    """Raw {code: [category, ...]} restricted to the universe, from a saved JSON file or from
    ``data_sources.get_industries()`` (FinMind, needs FINMIND_TOKEN)."""
    if industries_file:
        with open(industries_file, encoding="utf-8") as f:
            raw = rsp._unwrap(json.load(f), ("industries",))
    else:
        import data_sources as ds
        raw = ds.get_industries(cache=False)
    if not isinstance(raw, dict) or not raw:
        raise SnapshotError("no industry data (get_industries() returned nothing; FINMIND_TOKEN set?)")
    uni = set(universe)
    return {c: sorted(str(x) for x in raw[c]) for c in sorted(raw) if c in uni and raw[c]}


def _dump(path, obj):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=1) + "\n")


def build(cache_dir, out_dir, stamp, snapshot_date, industries_file=None, min_codes=DEFAULT_MIN_CODES,
          force=False):
    """Write both snapshot files; returns {"universe_file", "industries_file", "sha", "stats", ...}."""
    codes, stats = live_codes(cache_dir)
    if len(codes) < min_codes:
        raise SnapshotError(f"only {len(codes)} live codes in {cache_dir} (expected >= {min_codes}): "
                            "the cache is missing or incomplete, refusing to lock a partial universe")
    industries = load_industries(codes, industries_file)
    uni_path = os.path.join(out_dir, f"universe_{stamp}.json")
    ind_path = os.path.join(out_dir, f"industries_snapshot_{stamp}.json")
    for p in (uni_path, ind_path):
        if os.path.exists(p) and not force:
            raise SnapshotError(f"{p} already exists (a locked snapshot is never replaced silently; --force)")
    os.makedirs(out_dir, exist_ok=True)
    iso = snapshot_date.isoformat()
    _dump(uni_path, {"snapshot_date": iso, "source": "cache/factor_run live codes",
                     "count": len(codes), "codes": codes})
    _dump(ind_path, {"snapshot_date": iso, "source": "data_sources.get_industries()",
                     "industries": industries})
    # The same loader the daily job uses: a wrong shape or too many "其他" fails here, not at 07:00.
    universe, primary = rsp.load_snapshots(uni_path, ind_path)
    counts = Counter(primary.values())
    return {"universe_file": uni_path, "industries_file": ind_path,
            "sha": {"universe_sha256": rsp.sha256_file(uni_path), "industries_sha256": rsp.sha256_file(ind_path)},
            "stats": {**stats, "universe": len(universe), "with_industry_entry": len(industries),
                      "primary_other": counts.get(sr.OTHER, 0), "primary_groups": len(counts)}}


def existing_snapshot(out_dir, stamp):
    """Result dict (same shape as ``build``) for snapshot files that already exist in out_dir, plus the
    snapshot date stored in the universe file.  Verified with the daily job's loader."""
    uni_path = os.path.join(out_dir, f"universe_{stamp}.json")
    ind_path = os.path.join(out_dir, f"industries_snapshot_{stamp}.json")
    for p in (uni_path, ind_path):
        if not os.path.isfile(p):
            raise SnapshotError(f"snapshot file not found: {p}")
    with open(uni_path, encoding="utf-8") as f:
        snap = date.fromisoformat(json.load(f)["snapshot_date"])
    universe, primary = rsp.load_snapshots(uni_path, ind_path)
    counts = Counter(primary.values())
    return ({"universe_file": uni_path, "industries_file": ind_path,
             "sha": {"universe_sha256": rsp.sha256_file(uni_path), "industries_sha256": rsp.sha256_file(ind_path)},
             "stats": {"universe": len(universe), "primary_other": counts.get(sr.OTHER, 0),
                       "primary_groups": len(counts)}}, snap)


def write_lock(ledger_dir, start_date, result, snapshot_date):
    """Write <ledger dir>/meta/lock.json (never replaces an existing lock)."""
    path = os.path.join(ledger_dir, "meta", "lock.json")
    if os.path.exists(path):
        raise SnapshotError(f"{path} already exists: a lock is written once")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _dump(path, {"start_date": start_date.isoformat(), "snapshot_date": snapshot_date.isoformat(),
                 "universe_file": os.path.basename(result["universe_file"]),
                 "industries_file": os.path.basename(result["industries_file"]),
                 **result["sha"]})
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR)
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    ap.add_argument("--stamp", default=DEFAULT_STAMP, help="file-name stamp (the daily job reads 2026-11)")
    ap.add_argument("--snapshot-date", default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--industries-file", default=None, help="saved get_industries() JSON instead of FinMind")
    ap.add_argument("--min-codes", type=int, default=DEFAULT_MIN_CODES)
    ap.add_argument("--force", action="store_true", help="replace existing snapshot files")
    ap.add_argument("--write-lock", metavar="LEDGER_DIR", default=None, help="also write LEDGER_DIR/meta/lock.json")
    ap.add_argument("--start-date", default=None, help="start date for lock.json (required with --write-lock)")
    ap.add_argument("--lock-only", action="store_true",
                    help="do not build: verify the snapshot files already in --out-dir and write the lock")
    args = ap.parse_args(argv)
    if bool(args.write_lock) != bool(args.start_date):
        ap.error("--write-lock and --start-date go together")
    if args.lock_only and not args.write_lock:
        ap.error("--lock-only needs --write-lock and --start-date")
    snap = date.fromisoformat(args.snapshot_date) if args.snapshot_date else date.today()
    try:
        if args.lock_only:
            res, snap = existing_snapshot(args.out_dir, args.stamp)
            print(f"existing snapshot of {snap}: {res['stats']}")
            for k, v in res["sha"].items():
                print(f"{k}: {v}")
            print("lock:", write_lock(args.write_lock, date.fromisoformat(args.start_date), res, snap))
            return 0
        res = build(args.cache_dir, args.out_dir, args.stamp, snap, args.industries_file, args.min_codes, args.force)
        print(f"universe: {res['stats']['universe']} codes -> {res['universe_file']}")
        print(f"industries: {res['stats']['with_industry_entry']} codes with a category -> {res['industries_file']}")
        print(f"primary industries: {res['stats']['primary_groups']} groups, "
              f"{res['stats']['primary_other']} codes in 其他; cache: {res['stats']}")
        for k, v in res["sha"].items():
            print(f"{k}: {v}")
        if args.write_lock:
            print("lock:", write_lock(args.write_lock, date.fromisoformat(args.start_date), res, snap))
    except (SnapshotError, rsp.ShadowError, OSError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
