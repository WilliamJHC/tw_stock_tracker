# -*- coding: utf-8 -*-
"""Backtest analysis: signal win-rate stats (A) and strategy simulation (B).

Pure functions over daily bars / score rows -- no I/O, no FinMind, no DB
access. run_backtest.py orchestrates data flow between this module and
db.py. See docs/backtest_plan.md Phase 3/4.
"""
import datetime as dt
import json

import analysis as an

DEFAULT_HORIZONS = (5, 10, 20)

# Phase 4 strategy knobs -- product judgment calls, not derived. Serialized
# whole into strategy_runs.params_json so a stored run's numbers stay
# traceable to the parameters that produced them (see docs/backtest_plan.md
# Phase 4). fee_pct is a flat round-trip cost: 0.1425% commission x2
# (buy+sell) + 0.3% securities transaction tax = 0.585%, rounded to 0.6.
STRATEGY_PARAMS = {
    "exit_composite": -10,
    "atr_mult": 2.0,
    "max_hold_days": 60,
    "fee_pct": 0.6,
}

# Phase 5 confidence-engine knobs. horizon=20 matches this project's other
# "hold ~1 month" framing (max_hold_days=60, but 20d is the default backtest
# horizon used elsewhere); min_samples guards against an n=1 or n=2 signal
# occurrence dominating the weighted average with a lucky/unlucky outcome.
CONFIDENCE_HORIZON = 20
MIN_SAMPLES = 8


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


# --------------------------------------------------------- score replay ----

def _epoch(date_str):
    """ISO date -> epoch seconds. analyze() reads daily["t"] to build a
    date-keyed close lookup for OF divergence -- unused here since replay
    always passes minute=None/tick_days=None (OF can't be reconstructed
    2 years back, see docs/backtest_plan.md Phase 4), but the field must
    still be a valid timestamp or analyze() would raise on the conversion."""
    return int(dt.datetime.strptime(date_str, "%Y-%m-%d").timestamp())


def compute_rs_by_date(bars_by_code):
    """5-day relative-strength ranking across the watchlist, recomputed for
    every date at least 2 codes have a bar for (an.rs_ranking() needs >=2 to
    rank). Point-in-time safe: date D's ranking only uses each code's close
    up to and including D -- available after D's close, same as the live
    build_watchlist_payloads() path, so it's safe to act on at D+1's open
    (protection #4 in docs/backtest_plan.md Phase 4).

    bars_by_code: {code: [{date,c,...}, ...]} sorted ascending by date.
    Returns {code: {date: rs_info}}, i.e. per-code slices ready to pass as
    replay_scores()'s rs_by_date argument.
    """
    date_idx_by_code = {code: {b["date"]: i for i, b in enumerate(bars)}
                        for code, bars in bars_by_code.items()}
    all_dates = sorted({b["date"] for bars in bars_by_code.values() for b in bars})

    out = {code: {} for code in bars_by_code}
    for date in all_dates:
        returns_by_code = {}
        for code, bars in bars_by_code.items():
            idx = date_idx_by_code[code].get(date)
            if idx is None or idx < 5:
                continue
            c0 = bars[idx - 5]["c"]
            if c0:
                returns_by_code[code] = (bars[idx]["c"] - c0) / c0 * 100
        if len(returns_by_code) < 2:
            continue
        for code, info in an.rs_ranking(returns_by_code).items():
            out[code][date] = info
    return out


def replay_scores(bars, inst, margin, shareholding, sbl, rs_by_date=None, warmup=120):
    """Recompute tech/chip/composite scores bar-by-bar over history for one
    code (docs/backtest_plan.md Phase 4). Every anti-lookahead rule lives
    here so it can't be bypassed by a caller:

    - bar i's daily series is sliced to [:i+1] (protection #1) -- analyze()
      only ever reads index -1 as "today", so this slice is the only input
      an.analyze() gets to see for that bar. The one exception is
      ma5/ma20/ma60/vol20/quadrant: an.build_backtest_ctx() is built ONCE
      here over the code's full history and passed to every an.analyze()
      call via ctx=/at=i, instead of being rebuilt from an ever-growing
      prefix on every bar -- those 5 series are causal (only read index <=
      i), so this is bit-identical to the old per-bar rebuild while cutting
      replay_scores() from O(n^2) to O(n) in n (docs/https-finlab-finance-
      blog-python-get-tai-memoized-lighthouse.md Phase 2a). Every other
      indicator in analyze() still reads off daily_slice's own length, so
      the [:i+1] slice above is still what keeps them lookahead-free.
    - chip rows (inst/margin/shareholding/sbl) are cut off at the PREVIOUS
      bar's date, not bar i's own date (protection #2): a trading day's
      institutional/margin/shareholding/SBL figures are published after
      that day's own close, so using them to score that same day is
      look-ahead. rs_by_date is keyed by bar i's own date, which is safe
      (protection #4 -- see compute_rs_by_date()'s docstring).
    - minute=None, tick_days=None always: Order Flow can't be reconstructed
      2 years back (yfinance only gives ~60d of 5m bars, FinMind tick is
      paid + one-day-at-a-time), so `of` is naturally None throughout the
      replay -- its 15/100 SCORE_WEIGHTS contribution reads as 0. Confirm
      with db.get_meta(conn, "of_excluded") == "1" downstream.
    - with_backtest=False: analyze() would otherwise rescan full history
      via backtest_signal() for every triggered signal on every bar,
      making the whole replay O(n^2); signal win-rates are computed
      separately (see signal_win_rates()), not per-bar.

    bars: this code's full daily bars, ascending, [{date,o,h,l,c,v}, ...].
    inst/margin/shareholding/sbl: this code's full chip history, ascending,
        in the same shape as data_sources.py's getters (e.g.
        [{date, foreign, trust}, ...] for inst).
    rs_by_date: optional {date: rs_info} for this code (one entry of
        compute_rs_by_date()'s output).
    warmup: bars before this index aren't scored (ma60 + volume_profile
        need ~120 bars of history to be meaningful -- see Phase 2 "抓多久").

    Returns a list of {date, tech_score, chip_score, composite, priority,
    action, signals_json} rows, ready for db.upsert_score_history(). Empty
    if there aren't more than `warmup` bars.
    """
    n = len(bars)
    dates = [b["date"] for b in bars]
    times = [_epoch(d) for d in dates]
    opens = [b["o"] for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    closes = [b["c"] for b in bars]
    vols = [b["v"] for b in bars]

    full_daily = {"t": times, "o": opens, "h": highs, "l": lows, "c": closes, "v": vols}
    ctx = an.build_backtest_ctx(full_daily)  # built once, see docstring above

    rows = []
    for i in range(warmup, n):
        date = dates[i]
        daily_slice = {
            "t": times[:i + 1], "o": opens[:i + 1], "h": highs[:i + 1],
            "l": lows[:i + 1], "c": closes[:i + 1], "v": vols[:i + 1],
        }
        cutoff = dates[i - 1]
        inst_slice = [r for r in inst if r["date"] <= cutoff]
        margin_slice = [r for r in margin if r["date"] <= cutoff]
        shareholding_slice = [r for r in shareholding if r["date"] <= cutoff]
        sbl_slice = [r for r in sbl if r["date"] <= cutoff]
        rs_info = (rs_by_date or {}).get(date)

        result = an.analyze(daily_slice, minute=None, tick_days=None,
                            inst=inst_slice, margin=margin_slice,
                            shareholding=shareholding_slice, sbl=sbl_slice,
                            rs_info=rs_info, with_backtest=False,
                            ctx=ctx, at=i)
        if result is None:
            continue
        attention = an.compute_attention(result["score"], result.get("chip"), result)
        chip_score = result["chip"]["score"] if result.get("chip") else None
        triggered = [s["bt"] for s in result["signals"] if s.get("bt")]
        components = result["components"]
        rows.append({
            "date": date,
            "tech_score": result["score"],
            "chip_score": chip_score,
            "composite": attention["composite"],
            "priority": attention["priority"],
            "action": attention["action"],
            "signals_json": json.dumps(triggered, ensure_ascii=False),
            "c_maPosition": components["maPosition"],
            "c_maAlign": components["maAlign"],
            "c_volumePrice": components["volumePrice"],
            "c_trend": components["trend"],
            "c_vpPosition": components["vpPosition"],
            "c_orderFlow": components["orderFlow"],
            "c_gapTracker": components["gapTracker"],
            "c_donchianPosition": components["donchianPosition"],
            "c_rsRanking": components["rsRanking"],
        })
    return rows


# ----------------------------------------------------- strategy simulation ----

def simulate(score_rows, bars, params=None):
    """Long-only, single-position, no-leverage strategy simulation over one
    code's replayed scores (docs/backtest_plan.md Phase 4). Pure function --
    no I/O.

    Entry: `action == "準備進場"` while flat -> buy at the *next* bar's open
    (protection #3: a signal from D's close can't fill before D+1's open).
    Exit, first match wins (checked in this order every day while holding):
      1. `action == "減碼警示"`
      2. `composite <= params["exit_composite"]`
      3. stop-loss: bar's close <= entry_price - atr_mult * ATR-at-entry
         (ATR computed once at entry from data up to the signal day, not
         recomputed/trailed daily)
      4. held >= params["max_hold_days"] trading days
    Any exit fires -> sell at the next bar's open. A position still open
    when score_rows runs out is closed at the last bar's close (marked
    exit_reason="end_of_data") so summary stats reflect it.

    fee_pct is a flat round-trip cost subtracted (in percentage points,
    not compounded) from each trade's gross return, and once from
    buy_hold_return over the same period -- see STRATEGY_PARAMS.

    max_drawdown is computed over the *realized*-trade equity curve only
    (equity changes at each trade's close, flat between trades) -- it does
    not reflect unrealized mark-to-market swings while a position is open.
    This is a simplification: a true intraday-drawdown curve would need
    daily mark-to-market of open positions, which the plan doesn't call for
    and would add meaningfully more complexity for a number the strategy
    doesn't otherwise use (no trailing stop depends on it).

    Returns {"trades": [...], "summary": {n_trades, win_rate, avg_ret,
    total_return, buy_hold_return, max_drawdown, start_date, end_date}}.
    """
    p = dict(STRATEGY_PARAMS)
    if params:
        p.update(params)

    bar_idx_by_date = {b["date"]: i for i, b in enumerate(bars)}
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    closes = [b["c"] for b in bars]

    trades = []
    position = None  # {entry_idx, entry_date, entry_price, stop_price}

    for row in score_rows:
        idx = bar_idx_by_date.get(row["date"])
        if idx is None or idx + 1 >= len(bars):
            continue  # no next bar to execute on yet (or ever, for the last row)
        next_bar = bars[idx + 1]

        if position is None:
            if row["action"] == "準備進場":
                atr_val = an.atr(highs[:idx + 1], lows[:idx + 1], closes[:idx + 1],
                                  an.SHORT_TERM_THRESHOLDS["atr_period"])
                entry_price = next_bar["o"]
                position = {
                    "entry_idx": idx + 1,
                    "entry_date": next_bar["date"],
                    "entry_price": entry_price,
                    "stop_price": (entry_price - p["atr_mult"] * atr_val) if atr_val else None,
                }
            continue

        hold_days = idx - position["entry_idx"]
        exit_reason = None
        if row["action"] == "減碼警示":
            exit_reason = "action_sell"
        elif row["composite"] <= p["exit_composite"]:
            exit_reason = "composite_exit"
        elif position["stop_price"] is not None and closes[idx] <= position["stop_price"]:
            exit_reason = "stop_loss"
        elif hold_days >= p["max_hold_days"]:
            exit_reason = "max_hold"

        if exit_reason:
            exit_price = next_bar["o"]
            gross_ret = (exit_price / position["entry_price"] - 1) * 100
            trades.append({
                "entry_date": position["entry_date"], "entry_price": position["entry_price"],
                "exit_date": next_bar["date"], "exit_price": exit_price,
                "ret_pct": gross_ret - p["fee_pct"], "exit_reason": exit_reason,
                "hold_days": (idx + 1) - position["entry_idx"],
            })
            position = None

    if position is not None:
        last_idx = len(bars) - 1
        exit_price = closes[last_idx]
        gross_ret = (exit_price / position["entry_price"] - 1) * 100
        trades.append({
            "entry_date": position["entry_date"], "entry_price": position["entry_price"],
            "exit_date": bars[last_idx]["date"], "exit_price": exit_price,
            "ret_pct": gross_ret - p["fee_pct"], "exit_reason": "end_of_data",
            "hold_days": last_idx - position["entry_idx"],
        })

    n_trades = len(trades)
    wins = sum(1 for t in trades if t["ret_pct"] > 0)
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for t in trades:
        equity *= 1 + t["ret_pct"] / 100
        peak = max(peak, equity)
        max_dd = min(max_dd, (equity - peak) / peak * 100)

    start_date = score_rows[0]["date"] if score_rows else None
    end_date = score_rows[-1]["date"] if score_rows else None
    buy_hold_return = None
    if start_date and end_date:
        c0 = closes[bar_idx_by_date[start_date]]
        c1 = closes[bar_idx_by_date[end_date]]
        if c0:
            buy_hold_return = (c1 / c0 - 1) * 100 - p["fee_pct"]

    summary = {
        "n_trades": n_trades,
        "win_rate": (wins / n_trades * 100) if n_trades else None,
        "avg_ret": (sum(t["ret_pct"] for t in trades) / n_trades) if n_trades else None,
        "total_return": (equity - 1) * 100 if n_trades else None,
        "buy_hold_return": buy_hold_return,
        "max_drawdown": max_dd if n_trades else None,
        "start_date": start_date,
        "end_date": end_date,
    }
    return {"trades": trades, "summary": summary}


# --------------------------------------------------------- confidence ----

def compute_confidence(signals, signal_stats, horizon=CONFIDENCE_HORIZON, min_samples=MIN_SAMPLES):
    """Sample-weighted confidence that today's triggered signals are backed
    by real historical edge (docs/backtest_plan.md Phase 5).

    signals: today's result["signals"] list ({..., "bt": str|None}). VP/OF
        signals have bt=None (no backtest evidence exists for them -- see
        BACKTEST_SIGNALS' docstring in analysis.py) and are excluded here,
        not by the caller.
    signal_stats: this code's db.query_signal_stats() rows (any mix of
        horizons is fine; only rows matching `horizon` are considered).

    confidence = Σ(nᵢ·win_rateᵢ)/Σnᵢ, expected_ret = Σ(nᵢ·avg_retᵢ)/Σnᵢ --
    weighted by sample size so a signal with n=2 can't dominate one with
    n=80. A signal contributes only if its stored n >= min_samples.

    Returns {"confidence": float|None, "expected_ret": float|None,
    "sample_n": int, "note": str|None, "evidence": [{signal, horizon, n,
    win_rate, avg_ret}]}. confidence is None (never 0 or a guess) when
    nothing meets the sample bar -- note is "樣本不足" in that case.
    """
    seen = set()
    triggered = []
    for s in signals:
        key = s.get("bt")
        if key and key not in seen:
            seen.add(key)
            triggered.append(key)

    stats_by_signal = {r["signal"]: r for r in signal_stats if r["horizon"] == horizon}

    evidence = []
    total_n = 0
    weighted_win = 0.0
    weighted_ret = 0.0
    for sig in triggered:
        row = stats_by_signal.get(sig)
        if not row or not row["n"] or row["n"] < min_samples:
            continue
        evidence.append({"signal": sig, "horizon": horizon, "n": row["n"],
                         "win_rate": row["win_rate"], "avg_ret": row["avg_ret"]})
        total_n += row["n"]
        weighted_win += row["n"] * row["win_rate"]
        weighted_ret += row["n"] * row["avg_ret"]

    if total_n == 0:
        return {"confidence": None, "expected_ret": None, "sample_n": 0,
                "note": "樣本不足", "evidence": []}
    return {"confidence": weighted_win / total_n, "expected_ret": weighted_ret / total_n,
            "sample_n": total_n, "note": None, "evidence": evidence}
