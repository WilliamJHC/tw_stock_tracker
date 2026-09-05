# -*- coding: utf-8 -*-
"""Backtest analysis: signal win-rate stats (A) and strategy simulation (B).

Pure functions over daily bars / score rows -- no I/O, no FinMind, no DB
access. run_backtest.py orchestrates data flow between this module and
db.py. See docs/backtest_plan.md Phase 3/4.

>>> What this engine is for (docs/https-finlab-finance-blog-python-get-tai-
memoized-lighthouse.md Phase 3 item 4) <<<
This single-code, single-position engine is the dashboard's entry/reduce
ALERT and risk-control layer, not a return-generating strategy -- it is not
meant to, and (per the Phase 0 evaluation baseline, see below) structurally
does not, beat plain buy & hold. Every parameter combination in
STRATEGY_PARAMS' space was walk-forward tested (docs/.../lighthouse.md
Phase 0/3); none closes the gap. Phase 0's real-data numbers (23-code
watchlist, 10y, pre-adjustment prices):
    mean strategy total_return 177.3% vs mean buy&hold 2520.4%, beat 1/23
    time in market (exposure) 33.5%
After Phase 1's price-adjustment fix and this phase's parameter changes,
mean total_return moves to the ~300% range (see scripts/report_backtest.py)
but buy&hold still wins on most codes -- expected and reported honestly, not
chased by further tuning (docs/.../lighthouse.md explicitly warns against
optimizing toward beat_buy&hold; see its "明確不做" table). Alpha generation
is this project's OTHER engine's job: portfolio_backtest.py's multi-factor,
multi-position ranking strategy (src/finlab_factors.py), meant to be
validated at Phase 4's full-market scale where a single-code timing engine
structurally cannot express diversification-driven edge.
"""
import datetime as dt
import json

import analysis as an
import metrics as mt

DEFAULT_HORIZONS = (5, 10, 20)

# Phase 3/4 strategy knobs -- product judgment calls, not derived. Serialized
# whole into strategy_runs.params_json so a stored run's numbers stay
# traceable to the parameters that produced them (see docs/backtest_plan.md
# Phase 4). fee_pct is a flat round-trip cost: 0.1425% commission x2
# (buy+sell) + 0.3% securities transaction tax = 0.585%, rounded to 0.6.
#
# Phase 3 changes (docs/https-finlab-finance-blog-python-get-tai-memoized-
# lighthouse.md), each backed by a walk-forward measurement on the Phase 0
# baseline, not a guess:
#   exit_composite -10 -> -40: walk-forward 4/4 folds beat the -10 default
#     (mean OOS 294.5% vs 153.1%), and -60..-30 is a stable plateau across
#     folds (argmax wobbles between -50/-50/-50/-30, but staying in-plateau
#     matters more than chasing the single best argmax). -40 is the plateau
#     CENTER, deliberately not the full-sample argmax of -30 (that argmax is
#     plateau + noise -- unstable per-fold, see the plan doc).
#   max_hold_days 60 -> 120: return saturates by 120 (60->177.3%, 90->183.6,
#     120->188.4, 250->188.7) -- 120 captures nearly all of the benefit
#     without holding into the flat tail.
#   atr_mult 2.0 -> 100 (effectively disabled): the ATR stop as implemented
#     is a single band fixed at entry, never trailed (see simulate()'s
#     docstring) -- of 193 stop_loss exits, 0% won, mean -8.29%. Turning it
#     off (atr_mult=100 means the stop sits ~50x too far to ever fire in
#     practice) measured 184.7% vs the old default's 177.3%. A real trailing
#     stop would need to re-derive ATR and ratchet the stop daily while
#     held -- a genuinely bigger change for a knob that measured net-negative
#     as implemented; simplest correct fix is turning it off, not building
#     more machinery around a losing rule.
STRATEGY_PARAMS = {
    "exit_composite": -40,
    "atr_mult": 100,
    "max_hold_days": 120,
    "fee_pct": 0.6,
}

# Regime-conditional overrides on top of STRATEGY_PARAMS (docs/
# regime_strategy_research.md Phase B, backed by pooled watchlist signal
# win-rates from scripts/report_regime_factor_ic.py -- NOT the same
# walk-forward/PBO/DSR rigor RANKING_WEIGHTS gets, see that doc's
# methodology section for why a lighter bar is appropriate here). Only a
# regime with real evidence to deviate gets an entry; a regime absent here
# (or a key absent from its dict) falls through to STRATEGY_PARAMS
# unchanged -- see simulate()'s regime_by_date parameter.
#
# bear: pooled h=20 signal stats showed "跌破MA60" (trend-continuation
# entry) at a 48.1% win rate with ~0% avg_ret (worst of all regime/signal
# pairs measured -- continuation has no real edge once the market itself is
# already trending down), while "爆量長紅" (reversal) was the single best
# performer of any regime at 70.6%/+9.02%. Shorter max_hold_days locks in a
# bounce before the broader downtrend reasserts itself; a tighter
# exit_composite cuts a deteriorating position sooner instead of waiting for
# the same -40 threshold a bull/consolidation market would use. atr_mult and
# fee_pct are left at the global default -- the research only evaluated
# holding-period and composite-exit behavior, not the ATR stop itself (that
# stop is already disabled globally, see the atr_mult=100 note above; a
# regime-specific re-evaluation of it is future work, not backed by this
# data).
# bull/consolidation: the same data showed no clear divergence from the
# global default worth acting on (docs/regime_strategy_research.md's
# "訊號勝率分盤勢結果" table) -- deliberately left unlisted here rather than
# repeating STRATEGY_PARAMS's values, so a future change to STRATEGY_PARAMS
# doesn't also require updating an unchanged duplicate.
STRATEGY_PARAMS_BY_REGIME = {
    "bear": {"exit_composite": -25, "max_hold_days": 60},
}

# Phase 5 confidence-engine knobs. horizon=20 matches this project's other
# "hold ~1 month" framing (20d is the default backtest horizon used
# elsewhere -- max_hold_days moved to 120 in Phase 3, but 20d's own
# rationale doesn't depend on that value); min_samples guards against an
# n=1 or n=2 signal occurrence dominating the weighted average with a
# lucky/unlucky outcome.
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


REGIMES = ("bull", "bear", "consolidation")


def signal_win_rates_by_regime(daily, raw_regime_series, horizons=DEFAULT_HORIZONS):
    """Same shape as signal_win_rates() above, computed separately for each
    of market_regime.py's three market regimes: {"bull": [...], "bear":
    [...], "consolidation": [...]}, each value the same flat list of
    {signal, horizon, n, wins, win_rate, avg_ret} rows, restricted to bars
    whose calendar date fell in that regime (docs/regime_strategy_research.md
    Phase B).

    raw_regime_series: market_regime.regime_series()'s [(date, regime_or_
    None), ...] for the MARKET INDEX (TAIEX), not `daily`'s own code -- a
    single stock's own MA alignment is a different concept from the
    market-wide regime this buckets by (see src/market_regime.py's
    docstring). Matched against `daily["t"]` by calendar date; a bar with no
    matching regime entry (e.g. before TAIEX's own history starts) is
    excluded from every bucket rather than guessed at -- same "just don't
    count it" stance as an.liquidity_ok()-style filters elsewhere in this
    project.

    Deliberately reuses an.backtest_signal()'s `allowed_indices` filter
    instead of re-deriving win rates from scratch per regime -- see that
    parameter's docstring."""
    dates = [dt.datetime.fromtimestamp(t).date().isoformat() for t in daily["t"]]
    regime_by_date = dict(raw_regime_series)
    buckets = {regime: set() for regime in REGIMES}
    for i, date in enumerate(dates):
        regime = regime_by_date.get(date)
        if regime in buckets:
            buckets[regime].add(i)

    ctx = an.build_backtest_ctx(daily)
    result = {}
    for regime, allowed in buckets.items():
        rows = []
        for signal, fn in an.BACKTEST_SIGNALS.items():
            for horizon in horizons:
                r = an.backtest_signal(ctx, fn, horizon=horizon, gap=5, allowed_indices=allowed)
                rows.append({
                    "signal": signal,
                    "horizon": horizon,
                    "n": r["n"],
                    "wins": r.get("wins"),
                    "win_rate": r.get("win_rate"),
                    "avg_ret": r.get("avg_ret"),
                })
        result[regime] = rows
    return result


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
    action, signals_json, order_flow_override, ...c_* components} rows,
    ready for db.upsert_score_history(). Empty if there aren't more than
    `warmup` bars.

    order_flow_override is always 0.0 here (this function always calls
    an.analyze() with minute=None/tick_days=None, so its divergence check
    never fires -- see run_backtest.py's of_excluded meta flag) but is
    still persisted per-row so db.upsert_score_history()'s stored rows stay
    correct once/if a caller starts replaying with real intraday data.
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
            "order_flow_override": result["order_flow_override"],
        })
    return rows


# ----------------------------------------------------- strategy simulation ----

def build_bar_index(bars):
    """(bar_idx_by_date, highs, lows, closes) for `bars` -- simulate()'s own
    per-call setup, factored out so a caller running simulate() many times
    against the SAME bars array (e.g. optimize.py's walk-forward grid
    search: one fold's bars, swept across every candidate parameter value)
    can build this once and pass it in via simulate()'s `bar_index` arg
    instead of paying an O(len(bars)) rebuild on every call."""
    return (
        {b["date"]: i for i, b in enumerate(bars)},
        [b["h"] for b in bars],
        [b["l"] for b in bars],
        [b["c"] for b in bars],
    )


def _resolve_params(explicit_override, regime=None):
    """STRATEGY_PARAMS, layered with STRATEGY_PARAMS_BY_REGIME[regime] (if
    `regime` names one) and then `explicit_override` (if given) -- in that
    order, so an explicit caller override always wins over a regime default
    for any key both specify, and a regime default always wins over the
    global default. None (unlisted) regime, or a regime not in
    STRATEGY_PARAMS_BY_REGIME, changes nothing (pure global default +
    explicit override, byte-identical to this function's behavior before
    regime-awareness existed)."""
    p = dict(STRATEGY_PARAMS)
    if regime and regime in STRATEGY_PARAMS_BY_REGIME:
        p.update(STRATEGY_PARAMS_BY_REGIME[regime])
    if explicit_override:
        p.update(explicit_override)
    return p


def simulate(score_rows, bars, params=None, bar_index=None, regime_by_date=None):
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

    bar_index: optional (bar_idx_by_date, highs, lows, closes) precomputed
    by build_bar_index(bars) -- pass it in when calling simulate() many
    times against the same `bars` array to skip rebuilding it every call.
    Must actually correspond to `bars`; not verified here.

    regime_by_date: optional {date: regime} (e.g. dict(market_regime.
    regime_series(taiex_bars)), NOT this code's own bars -- see
    src/market_regime.py) -- when given, EVERY entry/exit decision on a
    given row resolves its params via _resolve_params(params, regime) using
    THAT row's date's regime, instead of one fixed params dict for the whole
    simulation. A regime change mid-hold takes effect immediately on the
    next row (e.g. a position opened in a bull regime gets bear's tighter
    exit_composite the day the market turns), matching how a caller would
    actually want risk parameters to respond to a live regime shift. None
    (the default) uses one _resolve_params(params, None) for the entire
    simulation, byte-identical to this function's behavior before regime-
    awareness existed."""
    default_p = _resolve_params(params, None)
    p = default_p

    if bar_index is not None:
        bar_idx_by_date, highs, lows, closes = bar_index
    else:
        bar_idx_by_date, highs, lows, closes = build_bar_index(bars)

    trades = []
    position = None  # {entry_idx, entry_date, entry_price, stop_price}

    for row in score_rows:
        idx = bar_idx_by_date.get(row["date"])
        if idx is None or idx + 1 >= len(bars):
            continue  # no next bar to execute on yet (or ever, for the last row)
        next_bar = bars[idx + 1]
        p = _resolve_params(params, regime_by_date.get(row["date"])) if regime_by_date is not None else default_p

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
    equities = [1.0]
    for t in trades:
        equities.append(equities[-1] * (1 + t["ret_pct"] / 100))
    equity = equities[-1]
    max_dd = mt.max_drawdown_pct(equities)

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
