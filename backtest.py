# -*- coding: utf-8 -*-
"""Backtest analysis: signal win-rate stats (A) and strategy simulation (B).

Pure functions over daily bars / score rows -- no I/O, no FinMind, no DB
access. run_backtest.py orchestrates data flow between this module and
db.py. See docs/backtest_plan.md Phase 3/4.
"""
import analysis as an

DEFAULT_HORIZONS = (5, 10, 20)


def signal_win_rates(daily, horizons=DEFAULT_HORIZONS):
    """Win rate + avg return for every analysis.BACKTEST_SIGNALS signal, at
    each of `horizons` holding days.

    daily: {c, v, o, ...} -- only the arrays build_backtest_ctx() reads
        (closes/vols/opens) are required.

    Returns a flat list of {signal, horizon, n, wins, win_rate, avg_ret}
    rows, one per (signal, horizon) pair, ready for db.upsert_signal_stats().
    `wins`/`win_rate`/`avg_ret` are None when n == 0 (no historical
    occurrence survived the gap=5 overlap filter -- see backtest_signal()).

    VP/OF-derived signals aren't in BACKTEST_SIGNALS (their data window is
    too short / recomputation too heavy to backtest -- see its docstring in
    analysis.py) so they're simply absent from the output; the front end
    labels those "回測不適用" instead.
    """
    ctx = an.build_backtest_ctx(daily)
    rows = []
    for signal, fn in an.BACKTEST_SIGNALS.items():
        for horizon in horizons:
            r = an.backtest_signal(ctx, fn, horizon=horizon, gap=5)
            rows.append({
                "signal": signal,
                "horizon": horizon,
                "n": r["n"],
                "wins": r.get("wins"),
                "win_rate": r.get("win_rate"),
                "avg_ret": r.get("avg_ret"),
            })
    return rows
