# -*- coding: utf-8 -*-
"""Re-runs the live multi-factor strategy against 0050 with delisted stocks
added back (docs/delisted_survivorship_preregistration_2026-10.md -- read it
first; the variants, window, metrics and the pass/fail gate are fixed there).

Variants (all: top_n=20, lot_size=0, default ranking weights, monthly):
    V0  cache/factor_run as is, original simulate() behavior
    V1  same universe, delisting mechanics on (stale codes unrankable, forced exit)
    V2  V1 + delisted codes from cache/factor_run_delisted with status "ok"   <- gate
    S_h25 / S_h50   V2 with a 25% / 50% haircut on every forced exit
    S_d75 / S_d90   V2 + price-only ("dark") delisted codes, revenue/ROE rank fixed at 0.75 / 0.90

Refuses to run until the pre-registration carries a lock date.

Usage (from the project root):
    python scripts/run_survivorship_study.py [--out output/survivorship_study.json]
"""
import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import benchmark as bm  # noqa: E402
import metrics as mt  # noqa: E402
import portfolio_backtest as pb  # noqa: E402
import report_factor_ic as rfi  # noqa: E402
import report_vs_benchmark as rvb  # noqa: E402
import report_vs_benchmark_2012_2020 as rvb12  # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DELISTED_DIR = os.path.join(BASE_DIR, "cache", "factor_run_delisted")
PREREG = os.path.join(BASE_DIR, "docs", "delisted_survivorship_preregistration_2026-10.md")

WINDOW_START = "2013-12-02"
# Fixed so the verdict does not depend on the day the script runs (the main cache and
# the 0050 cache both keep growing). 2026-10-16 is the last bar in cache/factor_run
# when this was written; the 0050 cache ends 10-19, so 10-16 is the common end.
WINDOW_END = "2026-10-16"
# V0 at this end date must match docs/multifactor_vs_0050_vs_equalweight_2026-10.md
# (total return 1169.01%, CAGR 21.99%, Sharpe 0.91, MDD -53.11%) -- a wiring check only.
REPRO_END = "2026-09-11"
TRAIN_END = "2019-12-31"
BASE_PARAMS = {"top_n": 20, "lot_size": 0}
MECHANICS = {"max_stale_days": 10, "terminal_exit_days": 30}

VARIANTS = [
    {"id": "V0", "universe": "main", "params": {}},
    {"id": "V1", "universe": "main", "params": MECHANICS},
    {"id": "V2", "universe": "main+ok", "params": MECHANICS},
    {"id": "S_h25", "universe": "main+ok", "params": {**MECHANICS, "terminal_haircut_pct": 25.0}},
    {"id": "S_h50", "universe": "main+ok", "params": {**MECHANICS, "terminal_haircut_pct": 50.0}},
    {"id": "S_d75", "universe": "main+ok+dark", "params": {**MECHANICS, "dark_rank": 0.75}},
    {"id": "S_d90", "universe": "main+ok+dark", "params": {**MECHANICS, "dark_rank": 0.90}},
]


def prereg_is_locked(path=PREREG):
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return False
    return any(line.startswith("鎖定日期：20") for line in text.splitlines())


def load_delisted():
    """{code: cache entry} for every code file in cache/factor_run_delisted/. Raises if
    the manifest (written by fetch_delisted_factor_run.py) lists a candidate that is
    neither in the main cache nor fetched here -- a silently missing code would bias
    the study the same way the survivorship it is meant to fix does."""
    out = {}
    manifest = None
    for path in glob.glob(os.path.join(DELISTED_DIR, "*.json")):
        with open(path, encoding="utf-8") as f:
            entry = json.load(f)
        if os.path.basename(path) == "_manifest.json":
            manifest = entry
        else:
            out[entry["code"]] = entry
    if manifest is None:
        raise RuntimeError("cache/factor_run_delisted/_manifest.json missing; run fetch_delisted_factor_run.py")
    missing = sorted(set(manifest["candidates"]) - set(manifest["in_main_cache"]) - set(out))
    if missing:
        raise RuntimeError(f"{len(missing)} delisted candidates not fetched yet: {missing[:10]}")
    return out


def data_digest(main_universe, delisted):
    """Short fingerprint of the inputs (code, bar count, last bar), recorded in the output
    so a changed cache is detectable against the value written in the pre-registration."""
    h = hashlib.sha256()
    for label, u in (("main", main_universe), ("delisted", {c: {"bar_dates": [b["date"] for b in e.get("bars", [])]} for c, e in delisted.items()})):
        for code in sorted(u):
            dates = u[code]["bar_dates"]
            h.update(f"{label}:{code}:{len(dates)}:{dates[-1] if dates else ''};".encode())
    return h.hexdigest()[:16]


def build_universe(kind, main, delisted):
    """kind: "main" | "main+ok" | "main+ok+dark". An "ok" delisted entry replaces a main-cache
    entry for the same code (those are late, short-history entries -- see
    fetch_delisted_factor_run._already_in_factor_run); a "dark" entry is only added where
    the main cache has nothing for the code."""
    universe = dict(main)
    if kind == "main":
        return universe
    for code, e in delisted.items():
        if e["status"] not in ("ok", "dark"):
            continue
        if e["status"] == "dark" and (kind != "main+ok+dark" or code in universe):
            continue
        universe[code] = {
            "bars": e["bars"], "bar_dates": [b["date"] for b in e["bars"]],
            "revenue_series": e["revenue_series"], "roe_series": e["roe_series"],
            "dark": e["status"] == "dark",
        }
    return universe


def curve_stats(values, dates):
    """total return %, CAGR %, Sharpe, MDD %, annualized vol % over an aligned value series."""
    rets = [values[i] / values[i - 1] - 1 for i in range(1, len(values))]
    n_days = (dt.date.fromisoformat(dates[-1]) - dt.date.fromisoformat(dates[0])).days or 1
    return {
        "start": dates[0], "end": dates[-1], "n_days": len(values),
        "total_return_pct": (values[-1] / values[0] - 1) * 100,
        "cagr_pct": mt.cagr_pct(values[0], values[-1], n_days),
        "sharpe": mt.sharpe_ratio(rets),
        "mdd_pct": mt.max_drawdown_pct(values),
        "vol_pct": statistics.pstdev(rets) * 252 ** 0.5 * 100 if len(rets) > 1 else None,
    }


def aligned(curve_a, curve_b, start, end):
    days = [d for d in bm.common_trading_days(curve_a, curve_b) if start <= d <= end]
    a, b = bm.as_map(curve_a), bm.as_map(curve_b)
    return days, [a[d] for d in days], [b[d] for d in days]


def compare(strategy_curve, bench_curve, start, end):
    days, a, b = aligned(strategy_curve, bench_curve, start, end)
    if len(days) < 2:
        return None
    return {"strategy": curve_stats(a, days), "benchmark": curve_stats(b, days)}


def gate(v2_vs_bench):
    """The pre-registered pass/fail (section 4): CAGR and Sharpe both >= 0050's."""
    s, b = v2_vs_bench["strategy"], v2_vs_bench["benchmark"]
    cagr_ok = s["cagr_pct"] >= b["cagr_pct"]
    sharpe_ok = s["sharpe"] >= b["sharpe"]
    return {"cagr_ok": cagr_ok, "sharpe_ok": sharpe_ok, "pass": cagr_ok and sharpe_ok}


def terminal_codes(universe, days=MECHANICS["terminal_exit_days"]):
    """Codes simulate() treats as delisted for this universe: last bar more than `days`
    calendar days before the universe's last bar date (same rule as simulate())."""
    end = max(u["bar_dates"][-1] for u in universe.values() if u["bar_dates"])
    cutoff = (dt.date.fromisoformat(end) - dt.timedelta(days=days)).isoformat()
    return {c for c, u in universe.items() if u["bar_dates"] and u["bar_dates"][-1] < cutoff}


def delisting_diagnostics(sim, delisted_codes):
    """Which delisted codes the strategy actually bought, and how the forced exits went."""
    bought = {t["code"] for t in sim["trades"] if t["side"] == "buy"}
    terminal = [t for t in sim["trades"] if t.get("terminal")]
    return {
        "delisted_codes_bought": len(bought & delisted_codes),
        "delisted_codes_available": len(delisted_codes),
        "terminal_exits": len(terminal),
        "terminal_exit_codes": sorted({t["code"] for t in terminal}),
    }


def load_benchmark():
    curve, _price, _ticker = rvb.fetch_benchmark_curves(
        start_date="2012-01-01", cache_path=os.path.join(BASE_DIR, "cache", "benchmark_0050_2012.json"), symbol="0050",
    )
    curve, factor = rvb12._fix_yfinance_0050_scale_artifact(curve)
    if factor is None:
        raise RuntimeError("0050 scale-artifact fix did not apply; refusing to compare on an unfixed curve")
    return curve


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(BASE_DIR, "output", "survivorship_study.json"))
    args = ap.parse_args()
    if not prereg_is_locked():
        sys.exit(f"pre-registration not locked ({PREREG}); fill in its section 8 first")

    main_universe = rfi.load_cached_universe()
    delisted = load_delisted()
    bench = load_benchmark()
    print(f"main universe: {len(main_universe)} codes; delisted cache: {len(delisted)} files")

    results = {"window": [WINDOW_START, WINDOW_END], "data_digest": data_digest(main_universe, delisted)}
    caches = {}
    for v in VARIANTS:
        t0 = time.time()
        universe = build_universe(v["universe"], main_universe, delisted)
        # ranks depend on (universe, max_stale_days, dark_rank), not on haircut: share across variants
        cache_key = (v["universe"], v["params"].get("max_stale_days"), v["params"].get("dark_rank"))
        sim = pb.simulate(universe, params={**BASE_PARAMS, **v["params"]}, ranks_cache=caches.setdefault(cache_key, {}))
        curve = bm.value_curve(sim["equity_curve"])
        end = WINDOW_END
        full = compare(curve, bench, WINDOW_START, end)
        if full is None:
            sys.exit(f"[{v['id']}] strategy and 0050 curves share fewer than 2 days in {WINDOW_START}..{end} "
                     f"(0050 cache ends {bench[-1]['date']}); refresh cache/benchmark_0050_2012.json")
        results[v["id"]] = {
            "codes": len(universe), "n_trades": len(sim["trades"]),
            "full": full,
            "train": compare(curve, bench, WINDOW_START, TRAIN_END),
            "test": compare(curve, bench, "2020-01-02", end),
            "rolling_12m": bm.rolling_window_comparisons(curve, bench, window_months=(12,), start=WINDOW_START, end=end)[12]["summary"],
            "rolling_36m": bm.rolling_window_comparisons(curve, bench, window_months=(36,), start=WINDOW_START, end=end)[36]["summary"],
            "delisting": delisting_diagnostics(sim, terminal_codes(universe)),
        }
        if v["id"] == "V2":
            results["V2_curve"] = [r for r in curve if WINDOW_START <= r["date"] <= WINDOW_END]
        if v["id"] == "V0":
            results["V0_repro_to_" + REPRO_END] = compare(curve, bench, WINDOW_START, REPRO_END)
        print(f"[{v['id']}] {len(universe)} codes, {len(sim['trades'])} trades, {time.time() - t0:.0f}s  "
              f"CAGR {full['strategy']['cagr_pct']:.1f}% vs 0050 {full['benchmark']['cagr_pct']:.1f}%  "
              f"Sharpe {full['strategy']['sharpe']:.2f} vs {full['benchmark']['sharpe']:.2f}")

    results["gate_V2"] = gate(results["V2"]["full"])
    print("gate (V2):", results["gate_V2"])
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    print("wrote", args.out)


if __name__ == "__main__":
    main()
