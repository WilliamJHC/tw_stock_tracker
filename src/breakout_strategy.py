# -*- coding: utf-8 -*-
"""Independent short-term breakout-momentum strategy: pure technical
(K-line/volume breakout) + chip (三大法人合力/融資) confirmation.

Deliberately separate from analysis.py's analyze()/chip_analysis()/
compute_attention() (SCORE_WEIGHTS/CHIP_WEIGHTS/CHIP_THRESHOLDS) and
backtest.py's replay_scores()/simulate() (STRATEGY_PARAMS) -- this module
does not touch either, so the existing watchlist cards/推薦面板 pipeline and
backtest engine are unaffected (2026-09-01 architecture decision: a new
strategy stays fully independent rather than extending the existing
今日決策 engine or the全市場排名 engine).

Reuses analysis.py's pure indicator functions (ma_series via
build_backtest_ctx()/donchian_position/big_candle_signal) and backtest.py's
build_bar_index()/metrics.max_drawdown_pct() rather than reimplementing
them -- only the entry/exit fusion rules and params below are new.

Output shape mirrors backtest.simulate()'s {"trades": [...], "summary":
{...}} by deliberate convention, for direct side-by-side comparison against
the existing single-code engine later.
"""
import analysis as an
import backtest as bt
import metrics as mt

BREAKOUT_PARAMS = {
    # -- technical (entry) --
    "donchian_period": 20,        # 20-trading-day breakout window (classic short/mid swing lookback)
    "donchian_breakout_pct": 95,  # donchian_position()'s 0-100 "short" reading must be >= this
                                   # (near/at the 20d high) -- analyze() itself uses a looser 90
                                   # threshold on its own (55d/120d) reading for a related signal
    "vol_mult": 2.0,              # today's volume >= 2x vol20, same threshold BACKTEST_SIGNALS'
                                   # "爆量(>2倍均量)" already uses, for consistency
    "require_big_candle": True,   # also require big_candle_signal()=="bull" (vol>1.5x + body>=3%,
                                   # see SHORT_TERM_THRESHOLDS) -- a real breakout day, not a
                                   # high-volume doji
    # -- chip (entry) --
    "chip_lookback_days": 3,      # trailing net-buy window per bucket -- short-term entry trigger,
                                   # deliberately tighter than chip_analysis()'s 5d holding-score
                                   # window (reactivity over smoothing)
    "min_buy_sides": 2,           # of {foreign, trust, dealer}, how many must net-buy over
                                   # chip_lookback_days ("三大法人合力")
    "margin_lookback_days": 5,    # mirrors CHIP_THRESHOLDS' 5d margin-balance window
    "margin_max_increase_pct": 0.03,  # 融資餘額5日增幅上限 gate -- half of CHIP_THRESHOLDS
                                   # ["margin_chg_full"]=0.05, deliberately stricter since this
                                   # blocks entry outright rather than just discounting a score
    # -- exit (checked in this order, first match wins) --
    "stop_loss_pct": 0.07,
    "take_profit_pct": 0.15,
    "trend_break_ma": 5,          # exit when close < MA5 (short-term momentum lost)
    "max_hold_days": 10,          # short-term cap, ~2 trading weeks -- far shorter than
                                   # backtest.STRATEGY_PARAMS["max_hold_days"]=120
    "fee_pct": 0.6,               # same round-trip cost convention as backtest.STRATEGY_PARAMS
}

# ma60 is the longest moving average this strategy reads (ma_bull needs
# ma5/ma20/ma60 all populated) -- no volume_profile dependency here, so no
# need for replay_scores()'s 120-bar warmup.
WARMUP = 60


def technical_signal(ctx, highs, lows, i, params=None):
    """ctx: an.build_backtest_ctx()'s output (closes/opens/vols/ma5/ma20/
    ma60/vol20, precomputed ONCE over full history -- same convention
    analysis.py's own BACKTEST_SIGNALS functions use, avoids an O(n^2)
    per-bar MA rebuild). highs/lows: this code's full daily arrays (not
    part of ctx -- analyze() itself computes donchian_position() straight
    off daily highs/lows, not through ctx).

    Returns {"ma_bull": bool, "donchian_pct": float|None, "breakout": bool,
    "vol_surge": bool, "big_candle": "bull"|"bear"|None, "confirmed": bool}.
    confirmed requires ma_bull AND breakout AND vol_surge AND (big_candle
    == "bull" if params["require_big_candle"] else True)."""
    p = dict(BREAKOUT_PARAMS)
    if params:
        p.update(params)

    ma5, ma20, ma60, vol20 = ctx["ma5"], ctx["ma20"], ctx["ma60"], ctx["vol20"]
    if ma5[i] is None or ma20[i] is None or ma60[i] is None or not vol20[i]:
        return {"ma_bull": False, "donchian_pct": None, "breakout": False,
                "vol_surge": False, "big_candle": None, "confirmed": False}

    ma_bull = ma5[i] > ma20[i] > ma60[i]
    donch = an.donchian_position(highs[:i + 1], lows[:i + 1], ctx["closes"][:i + 1],
                                  period_short=p["donchian_period"], period_long=p["donchian_period"])
    donchian_pct = donch["short"] if donch else None
    breakout = donchian_pct is not None and donchian_pct >= p["donchian_breakout_pct"]
    vol_surge = ctx["vols"][i] >= p["vol_mult"] * vol20[i]
    big_candle = an.big_candle_signal(ctx["opens"], ctx["closes"], ctx["vols"], vol20, i)
    candle_ok = (big_candle == "bull") if p["require_big_candle"] else True

    confirmed = ma_bull and breakout and vol_surge and candle_ok
    return {"ma_bull": ma_bull, "donchian_pct": donchian_pct, "breakout": breakout,
            "vol_surge": vol_surge, "big_candle": big_candle, "confirmed": confirmed}


def chip_signal(inst_window, margin_window, params=None):
    """inst_window: this code's institutional-net rows for the trailing
    `chip_lookback_days` window, ascending [{date, foreign, trust, dealer},
    ...] (already date-cutoff by the caller -- see replay_breakout()'s
    anti-lookahead rule). margin_window: trailing `margin_lookback_days`
    window, ascending [{date, margin_balance, ...}, ...].

    "合力" (buy_sides): of {foreign, trust, dealer}, how many net to a
    positive sum over inst_window. Missing/empty inst_window -> 0 sides,
    never defaults to a pass -- this is the strategy's primary chip
    confirmation, so absent data must not silently satisfy it.

    margin_ok is an entry GATE against margin-fueled over-extension, not a
    confirmation -- absent margin data (e.g. ETFs, see data_sources.py's
    get_margin() docstring) does not block entry, only an actually-measured
    excessive increase does.

    Returns {"buy_sides": int, "sides": {"foreign": bool, "trust": bool,
    "dealer": bool}, "margin_chg_pct": float|None, "margin_ok": bool,
    "confirmed": bool}. confirmed = buy_sides >= min_buy_sides AND margin_ok."""
    p = dict(BREAKOUT_PARAMS)
    if params:
        p.update(params)

    sides = {}
    for key in ("foreign", "trust", "dealer"):
        vals = [r[key] for r in (inst_window or []) if r.get(key) is not None]
        sides[key] = bool(vals) and sum(vals) > 0
    buy_sides = sum(1 for v in sides.values() if v)

    margin_chg_pct = None
    if margin_window:
        bal_first = margin_window[0].get("margin_balance")
        bal_last = margin_window[-1].get("margin_balance")
        if bal_first:
            margin_chg_pct = (bal_last - bal_first) / bal_first
    margin_ok = margin_chg_pct is None or margin_chg_pct <= p["margin_max_increase_pct"]

    confirmed = buy_sides >= p["min_buy_sides"] and margin_ok
    return {"buy_sides": buy_sides, "sides": sides, "margin_chg_pct": margin_chg_pct,
            "margin_ok": margin_ok, "confirmed": confirmed}


def entry_signal(tech, chip):
    """True only when both tech["confirmed"] and chip["confirmed"] hold --
    a technical+chip FUSION strategy per the architecture decision, not
    either/or. chip=None (or tech=None) never defaults to a pass."""
    if not tech or not chip:
        return False
    return bool(tech["confirmed"] and chip["confirmed"])


def exit_signal(position, i, closes, ma5, params=None):
    """position: {entry_idx, entry_price}. Checked in this order every bar
    while holding (first match wins): stop_loss, take_profit, trend_break
    (today's close < MA5), max_hold_days. Returns the exit_reason str, or
    None while still held."""
    p = dict(BREAKOUT_PARAMS)
    if params:
        p.update(params)

    price = closes[i]
    ret = price / position["entry_price"] - 1
    if ret <= -p["stop_loss_pct"]:
        return "stop_loss"
    if ret >= p["take_profit_pct"]:
        return "take_profit"
    if ma5[i] is not None and price < ma5[i]:
        return "trend_break"
    if (i - position["entry_idx"]) >= p["max_hold_days"]:
        return "max_hold"
    return None


def replay_breakout(bars, inst, margin, warmup=WARMUP, params=None):
    """Bar-by-bar over one code's full history, mirroring backtest.
    replay_scores()'s anti-lookahead protections: ma5/ma20/ma60/vol20 are
    built ONCE over the full arrays via an.build_backtest_ctx() (protection
    #1's ctx/at pattern); inst/margin windows are cut off at bar i's
    PREVIOUS date, not bar i's own date (protection #2 -- a trading day's
    institutional/margin figures are published after that day's own
    close, so using them to score that same day is look-ahead).

    bars: this code's full daily bars, ascending, [{date,o,h,l,c,v}, ...].
    inst/margin: this code's full chip history, ascending, in the same
        shape as data_sources.py's getters ([{date, foreign, trust,
        dealer}, ...] / [{date, margin_balance, ...}, ...]).

    Returns [{date, tech, chip, entry} ...] rows, one per bar >= warmup.
    Empty if there aren't more than `warmup` bars."""
    n = len(bars)
    dates = [b["date"] for b in bars]
    opens = [b["o"] for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    closes = [b["c"] for b in bars]
    vols = [b["v"] for b in bars]

    ctx = an.build_backtest_ctx({"c": closes, "v": vols, "o": opens})

    p = dict(BREAKOUT_PARAMS)
    if params:
        p.update(params)
    chip_days = p["chip_lookback_days"]
    margin_days = p["margin_lookback_days"]

    rows = []
    for i in range(warmup, n):
        tech = technical_signal(ctx, highs, lows, i, params=p)

        cutoff = dates[i - 1]
        inst_window = [r for r in inst if r["date"] <= cutoff][-chip_days:]
        margin_window = [r for r in margin if r["date"] <= cutoff][-margin_days:]
        chip = chip_signal(inst_window, margin_window, params=p)

        rows.append({
            "date": dates[i],
            "tech": tech,
            "chip": chip,
            "entry": entry_signal(tech, chip),
        })
    return rows


def simulate_breakout(rows, bars, params=None, bar_index=None):
    """Long-only, single-position simulation over replay_breakout()'s rows.
    Entry: `entry=True` while flat -> buy at the *next* bar's open (same
    signal-day-D/fill-at-D+1-open timing as backtest.simulate()). Exit:
    first exit_signal() match -> sell at the next bar's open. A position
    still open when `rows` runs out is closed at the last bar's close
    (exit_reason="end_of_data").

    Reuses backtest.build_bar_index()/metrics.max_drawdown_pct() rather
    than reimplementing them. Returns {"trades": [...], "summary":
    {n_trades, win_rate, avg_ret, total_return, buy_hold_return,
    max_drawdown, start_date, end_date}} -- same shape as backtest.
    simulate()'s output by deliberate convention.

    bar_index: optional (bar_idx_by_date, highs, lows, closes) precomputed
    by backtest.build_bar_index(bars); must correspond to `bars`, not
    verified here."""
    p = dict(BREAKOUT_PARAMS)
    if params:
        p.update(params)

    if bar_index is not None:
        bar_idx_by_date, _, _, closes = bar_index
    else:
        bar_idx_by_date, _, _, closes = bt.build_bar_index(bars)

    ma5 = an.ma_series(closes, p["trend_break_ma"])

    trades = []
    position = None  # {entry_idx, entry_date, entry_price}

    for row in rows:
        idx = bar_idx_by_date.get(row["date"])
        if idx is None or idx + 1 >= len(bars):
            continue  # no next bar to execute on yet (or ever, for the last row)
        next_bar = bars[idx + 1]

        if position is None:
            if row["entry"]:
                position = {
                    "entry_idx": idx + 1,
                    "entry_date": next_bar["date"],
                    "entry_price": next_bar["o"],
                }
            continue

        reason = exit_signal(position, idx, closes, ma5, params=p)
        if reason:
            exit_price = next_bar["o"]
            gross_ret = (exit_price / position["entry_price"] - 1) * 100
            trades.append({
                "entry_date": position["entry_date"], "entry_price": position["entry_price"],
                "exit_date": next_bar["date"], "exit_price": exit_price,
                "ret_pct": gross_ret - p["fee_pct"], "exit_reason": reason,
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

    start_date = rows[0]["date"] if rows else None
    end_date = rows[-1]["date"] if rows else None
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
