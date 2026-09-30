# -*- coding: utf-8 -*-
"""Rank-IC of the institutional net-buy ratio factor (cache/
institutional_series/, written by scripts/fetch_institutional_series.py) next
to the adopted revenue/roe/momentum factors, on the SAME sub-universe (only
codes that have a fetched series) so the comparison is apples-to-apples.
Pure cache read, no network. Evaluation only -- adopting it would still go
through ADR 0003's review path.

Usage:
    python scripts/report_institutional_ic.py [--since 2021-12-01] [--horizons 20,60]
"""
import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import factor_eval as fe  # noqa: E402
import report_factor_ic as rfi  # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IN_DIR = os.path.join(BASE_DIR, "cache", "institutional_series")


def fmt(s):
    if s["mean_ic"] is None:
        return f"n/a (n={s['n']})"
    return f"IC={s['mean_ic']:+.3f} t={s['t_stat']:+.2f} ICIR={s['icir']:+.2f} n={s['n']}"


def load_sub_universe():
    universe = rfi.load_cached_universe()
    sub = {}
    for path in glob.glob(os.path.join(IN_DIR, "*.json")):
        code = os.path.splitext(os.path.basename(path))[0]
        if code not in universe:
            continue
        with open(path, encoding="utf-8") as f:
            series = json.load(f).get("series") or []
        if series:
            sub[code] = {**universe[code], "inst_series": series}
    return sub


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", default="2021-12-01")
    ap.add_argument("--horizons", default="20,60")
    args = ap.parse_args()
    H = tuple(int(h) for h in args.horizons.split(","))

    U = load_sub_universe()
    if not U:
        print(f"no series under {IN_DIR} -- run scripts/fetch_institutional_series.py first")
        return
    dates = fe.rebalance_dates(U)
    windows = (("all dates", dates),
               (f"< {args.since}", [d for d in dates if d < args.since]),
               (f">= {args.since}", [d for d in dates if d >= args.since]))
    print(f"{len(U)} codes with an institutional series, {len(dates)} rebalance dates ({dates[0]}..{dates[-1]})")
    print("(sub-universe = fetched sample; baseline factors below are evaluated on the same codes)\n")
    for label, ds in windows:
        print(f"-- {label} ({len(ds)} dates)")
        base = fe.evaluate_factors(U, dates=ds, horizons=H)
        for f in sorted(base):
            print(f"  {f:<22}" + " | ".join(f"h={h} {fmt(base[f][h])}" for h in H))
        r = fe.evaluate_candidate_factor(U, "inst_series", "net_ratio", dates=ds, horizons=H)
        print(f"  {'inst_net_ratio (cand)':<22}" + " | ".join(f"h={h} {fmt(r[h])}" for h in H))


if __name__ == "__main__":
    main()
