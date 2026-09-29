# -*- coding: utf-8 -*-
"""Factor-optimization directions, "immediate" items (docs/factor_timeframe_optimization_directions.md
2.1 and 3.1): NO network, reads cache/factor_run/*.json only, same as report_factor_ic.py.

Sections:
  1. Phase 6b candidate factors (gross/operating margin trend, PBR value) vs the adopted three,
     over ALL rebalance dates and over the candidates' own coverage window (>= --since).
  2. Momentum formation-window comparison (5/20/60/120, skip-most-recent-21-bars variants, 12-1).
  3. Composite-score rank-IC: current RANKING_WEIGHTS vs momentum swapped for a skip-21 window,
     momentum dropped, and PBR added -- weights are ILLUSTRATIVE, evidence only, never applied.

Evaluation only. Anything worth adopting still has to go through ADR 0003's review path
(run_strategy_search.py -> DSR/PBO/plateau -> manual apply).

Usage:
    python scripts/report_factor_candidates_and_momentum.py [--since 2021-12-01] [--horizons 20,60]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import factor_eval as fe  # noqa: E402
import finlab_factors as ff  # noqa: E402
import metrics as m  # noqa: E402
import report_factor_ic as rfi  # noqa: E402


def fmt(s):
    if s["mean_ic"] is None:
        return f"n/a (n={s['n']})"
    return f"IC={s['mean_ic']:+.3f} t={s['t_stat']:+.2f} ICIR={s['icir']:+.2f} n={s['n']}"


def momentum_fn(days, skip=0):
    """`days`-bar return ending `skip` bars before idx (skip=0 == ff.momentum_pct)."""
    def fn(bars, idx):
        j = idx - skip
        if j - days < 0:
            return None
        base = ff._close(bars[j - days])
        return (ff._close(bars[j]) - base) / base * 100 if base else None
    return fn


def variant_ic(U, fn, dates, horizon):
    ics = []
    for date in dates:
        fv, fr = [], []
        for d in U.values():
            bars = d["bars"]
            idx = ff._bar_index_on_or_before(d["bar_dates"], date)
            if idx is None or not ff.liquidity_ok(bars, idx):
                continue
            v = fn(bars, idx)
            f = fe._forward_return(bars, idx, horizon)
            if v is None or f is None:
                continue
            fv.append(v)
            fr.append(f)
        ics.append(m.rank_ic(fv, fr))
    return m.ic_stats(ics)


def composite_ic(U, mom_fn, pbr_weight_used, weights, dates, horizon):
    """weights = (revenue, roe, momentum, pbr) applied to percentile ranks within the liquid survivor set."""
    ics = []
    for date in dates:
        raw = {}
        for code, d in U.items():
            bars = d["bars"]
            idx = ff._bar_index_on_or_before(d["bar_dates"], date)
            if idx is None or not ff.liquidity_ok(bars, idx):
                continue
            vals = (mom_fn(bars, idx), ff._as_of(d["revenue_series"], date, "yoy_pct"),
                    ff._as_of(d["roe_series"], date, "roe_pct"),
                    ff._as_of(d["value_series"], date, "value_score") if pbr_weight_used else 0,
                    fe._forward_return(bars, idx, horizon))
            if any(v is None for v in vals):
                continue
            raw[code] = vals
        if len(raw) < 20:
            ics.append(None)
            continue
        rk = [ff._pct_rank({c: v[i] for c, v in raw.items()}) for i in range(4)]
        w = weights
        score = {c: w[0] * rk[1][c] + w[1] * rk[2][c] + w[2] * rk[0][c] + w[3] * rk[3][c] for c in raw}
        ics.append(m.rank_ic([score[c] for c in raw], [raw[c][4] for c in raw]))
    return m.ic_stats(ics)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", default="2021-12-01", help="start of the candidates' data-coverage window")
    ap.add_argument("--horizons", default="5,20,60")
    args = ap.parse_args()
    H = tuple(int(h) for h in args.horizons.split(","))

    U = rfi.load_cached_universe()
    dates = fe.rebalance_dates(U)
    recent = [d for d in dates if d >= args.since]
    print(f"{len(U)} codes, {len(dates)} rebalance dates ({dates[0]}..{dates[-1]}), {len(recent)} since {args.since}\n")

    print("== 1. baseline factors vs Phase 6b candidates ==")
    for label, ds in (("all dates", dates), (f">= {args.since}", recent)):
        print(f"-- {label}")
        base = fe.evaluate_factors(U, dates=ds, horizons=H)
        for f in base:
            print(f"  {f:<24}" + " | ".join(f"h={h} {fmt(base[f][h])}" for h in H))
        for name, (sk, vk) in {"gross_margin_trend": ("gross_margin_trend_series", "trend_pct"),
                               "operating_margin_trend": ("operating_margin_trend_series", "trend_pct"),
                               "value_pbr": ("value_series", "value_score")}.items():
            r = fe.evaluate_candidate_factor(U, sk, vk, dates=ds, horizons=H)
            print(f"  {name + ' (candidate)':<24}" + " | ".join(f"h={h} {fmt(r[h])}" for h in H))

    print("\n== 2. momentum formation window ==")
    variants = {"mom5": momentum_fn(5), "mom20": momentum_fn(20), "mom60 (current)": momentum_fn(60),
                "mom120": momentum_fn(120), "mom60_skip21": momentum_fn(60, 21),
                "mom120_skip21": momentum_fn(120, 21), "mom231_skip21 (~12-1)": momentum_fn(231, 21)}
    for label, ds in (("all dates", dates), (f">= {args.since}", recent)):
        print(f"-- {label}")
        for name, fn in variants.items():
            print(f"  {name:<24}" + " | ".join(f"h={h} {fmt(variant_ic(U, fn, ds, h))}" for h in H if h >= 20))

    print("\n== 3. composite-score rank-IC (illustrative weights, NOT proposals) ==")
    cfgs = [("current rev.4/roe.3/mom60 .3", momentum_fn(60), False, (.4, .3, .3, 0)),
            ("momentum -> 120_skip21", momentum_fn(120, 21), False, (.4, .3, .3, 0)),
            ("momentum -> 60_skip21", momentum_fn(60, 21), False, (.4, .3, .3, 0)),
            ("drop momentum rev.55/roe.45", momentum_fn(60), False, (.55, .45, 0, 0)),
            ("current + PBR .2", momentum_fn(60), True, (.32, .24, .24, .2)),
            ("skip21 + PBR .2", momentum_fn(120, 21), True, (.32, .24, .24, .2))]
    for name, mf, pbr, w in cfgs:
        print(name)
        for label, ds in (("all dates", dates), (f">= {args.since}", recent)):
            if pbr and label == "all dates":
                continue  # PBR series only exists from ~2021-10
            print(f"   {label:<12}" + " | ".join(f"h={h} {fmt(composite_ic(U, mf, pbr, w, ds, h))}" for h in H if h >= 20))


if __name__ == "__main__":
    main()
