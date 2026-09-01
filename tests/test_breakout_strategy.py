# -*- coding: utf-8 -*-
"""Unit tests for breakout_strategy.py's pure signal functions and the
replay/simulate pair, using small hand-computed fixtures (no network, no DB)."""
import datetime as dt

import breakout_strategy as bs


def _ctx(closes, opens, vols, ma5, ma20, ma60, vol20):
    return {"closes": closes, "opens": opens, "vols": vols,
            "ma5": ma5, "ma20": ma20, "ma60": ma60, "vol20": vol20}


# --------------------------------------------------------- technical_signal --

def test_technical_signal_false_before_ma_warmup():
    ctx = _ctx([100], [100], [1000], [None], [None], [None], [1000])
    out = bs.technical_signal(ctx, [100], [100], 0)
    assert out["confirmed"] is False
    assert out["ma_bull"] is False


def _base_confirmed_args(**overrides):
    """A minimal 4-bar scenario where ma_bull/breakout/vol_surge/big_candle
    are ALL true at i=3 (donchian_period overridden to 3 so a 4-bar array
    is enough). Each test below flips exactly one input to isolate one
    condition."""
    args = dict(
        closes=[100, 100, 100, 106], opens=[100, 100, 100, 100],
        vols=[1000, 1000, 1000, 3000],
        ma5=[None, None, None, 12], ma20=[None, None, None, 11], ma60=[None, None, None, 10],
        vol20=[None, None, None, 1000],
        highs=[100, 100, 100, 106], lows=[100, 100, 100, 100],
    )
    args.update(overrides)
    return args


def _run_technical(args, params=None):
    p = {"donchian_period": 3}
    if params:
        p.update(params)
    ctx = _ctx(args["closes"], args["opens"], args["vols"], args["ma5"], args["ma20"],
               args["ma60"], args["vol20"])
    return bs.technical_signal(ctx, args["highs"], args["lows"], 3, params=p)


def test_technical_signal_all_confirmed_base_case():
    out = _run_technical(_base_confirmed_args())
    assert out == {"ma_bull": True, "donchian_pct": 100.0, "breakout": True,
                    "vol_surge": True, "big_candle": "bull", "confirmed": True}


def test_technical_signal_ma_bull_false_breaks_confirmed():
    args = _base_confirmed_args(ma5=[None, None, None, 10], ma20=[None, None, None, 11],
                                 ma60=[None, None, None, 12])
    out = _run_technical(args)
    assert out["ma_bull"] is False
    assert out["confirmed"] is False


def test_technical_signal_breakout_false_below_donchian_threshold():
    # widen the window's high so today's close lands below the 95% cutoff
    args = _base_confirmed_args(highs=[100, 100, 100, 107])
    out = _run_technical(args)
    assert out["breakout"] is False
    assert out["confirmed"] is False


def test_technical_signal_vol_surge_false_below_vol_mult():
    args = _base_confirmed_args(vols=[1000, 1000, 1000, 1800])  # < 2x vol20(1000)=2000
    out = _run_technical(args)
    assert out["vol_surge"] is False
    assert out["confirmed"] is False


def test_technical_signal_big_candle_none_blocks_confirmed_when_required():
    # body_pct 2% < SHORT_TERM_THRESHOLDS["big_candle_body_pct"]=3%
    args = _base_confirmed_args(closes=[100, 100, 100, 102], highs=[100, 100, 100, 102])
    out = _run_technical(args)
    assert out["big_candle"] is None
    assert out["confirmed"] is False


def test_technical_signal_require_big_candle_false_ignores_candle():
    args = _base_confirmed_args(closes=[100, 100, 100, 102], highs=[100, 100, 100, 102])
    out = _run_technical(args, params={"require_big_candle": False})
    assert out["big_candle"] is None
    assert out["confirmed"] is True  # breakout/vol_surge/ma_bull alone are enough now


# -------------------------------------------------------------- chip_signal --

def _inst(foreign, trust, dealer):
    row = {"date": "2026-01-01"}
    if foreign is not None:
        row["foreign"] = foreign
    if trust is not None:
        row["trust"] = trust
    if dealer is not None:
        row["dealer"] = dealer
    return [row]


def test_chip_signal_three_of_three_buy_sides():
    out = bs.chip_signal(_inst(100, 100, 100), None)
    assert out["buy_sides"] == 3
    assert out["confirmed"] is True


def test_chip_signal_two_of_three_meets_default_min_buy_sides():
    out = bs.chip_signal(_inst(100, 100, -100), None)
    assert out["buy_sides"] == 2
    assert out["sides"] == {"foreign": True, "trust": True, "dealer": False}
    assert out["confirmed"] is True


def test_chip_signal_one_of_three_below_min_buy_sides():
    out = bs.chip_signal(_inst(100, -100, -100), None)
    assert out["buy_sides"] == 1
    assert out["confirmed"] is False


def test_chip_signal_empty_window_never_defaults_to_pass():
    out = bs.chip_signal([], None)
    assert out["buy_sides"] == 0
    assert out["confirmed"] is False


def test_chip_signal_missing_dealer_key_does_not_raise_and_reads_as_no_side():
    """A pre-fix cached inst row (from before the dealer split) lacks the
    "dealer" key entirely -- must not crash, and dealer must read as an
    unconfirmed side, not silently pass."""
    out = bs.chip_signal([{"date": "2026-01-01", "foreign": 100, "trust": 100}], None)
    assert out["sides"]["dealer"] is False
    assert out["buy_sides"] == 2
    assert out["confirmed"] is True  # foreign+trust alone still clear min_buy_sides=2


def test_chip_signal_margin_increase_at_threshold_still_ok():
    margin = [{"date": "d1", "margin_balance": 100}, {"date": "d2", "margin_balance": 103}]
    out = bs.chip_signal(_inst(100, 100, 100), margin)
    assert out["margin_chg_pct"] == 0.03
    assert out["margin_ok"] is True
    assert out["confirmed"] is True


def test_chip_signal_margin_increase_just_over_threshold_blocks_entry():
    margin = [{"date": "d1", "margin_balance": 100}, {"date": "d2", "margin_balance": 103.01}]
    out = bs.chip_signal(_inst(100, 100, 100), margin)
    assert out["margin_ok"] is False
    assert out["confirmed"] is False


def test_chip_signal_missing_margin_data_does_not_block_entry():
    out = bs.chip_signal(_inst(100, 100, 100), None)
    assert out["margin_chg_pct"] is None
    assert out["margin_ok"] is True


# -------------------------------------------------------------- entry_signal --

def test_entry_signal_requires_both_tech_and_chip_confirmed():
    tech_yes = {"confirmed": True}
    tech_no = {"confirmed": False}
    chip_yes = {"confirmed": True}
    chip_no = {"confirmed": False}
    assert bs.entry_signal(tech_yes, chip_yes) is True
    assert bs.entry_signal(tech_yes, chip_no) is False
    assert bs.entry_signal(tech_no, chip_yes) is False


def test_entry_signal_none_chip_never_defaults_to_tech_only_pass():
    assert bs.entry_signal({"confirmed": True}, None) is False


# --------------------------------------------------------------- exit_signal --

def test_exit_signal_priority_stop_loss_over_others():
    pos = {"entry_idx": 0, "entry_price": 100}
    closes = [None] * 11
    ma5 = [None] * 11
    closes[10], ma5[10] = 92, 95  # ret=-8% (stop), also below ma5, also hold_days>=10
    assert bs.exit_signal(pos, 10, closes, ma5) == "stop_loss"


def test_exit_signal_priority_take_profit_over_trend_break_and_max_hold():
    pos = {"entry_idx": 0, "entry_price": 100}
    closes = [None] * 11
    ma5 = [None] * 11
    closes[10], ma5[10] = 116, 120  # ret=+16% (take-profit), also below ma5, hold_days>=10
    assert bs.exit_signal(pos, 10, closes, ma5) == "take_profit"


def test_exit_signal_priority_trend_break_over_max_hold():
    pos = {"entry_idx": 0, "entry_price": 100}
    closes = [None] * 11
    ma5 = [None] * 11
    closes[10], ma5[10] = 101, 105  # neither stop nor take-profit; below ma5; hold_days>=10
    assert bs.exit_signal(pos, 10, closes, ma5) == "trend_break"


def test_exit_signal_max_hold_days_boundary():
    pos = {"entry_idx": 0, "entry_price": 100}
    closes = [None] * 11
    ma5 = [None] * 11
    closes[10], ma5[10] = 101, 100  # above ma5, no stop/take-profit
    assert bs.exit_signal(pos, 10, closes, ma5) == "max_hold"  # hold_days == 10, boundary
    closes[9], ma5[9] = 101, 100
    assert bs.exit_signal(pos, 9, closes, ma5) is None  # hold_days == 9, one bar under


# --------------------------------------------- replay_breakout / simulate_breakout --

def _flat_then_breakout_bars(n=70, jump_days=(65, 66), pct=0.06, flat_price=100.0):
    """n daily bars: flat at flat_price, then a close-at-the-high up move of
    `pct` on each date in jump_days (no upper wick, so the close itself
    sets the new high), volume spiking on those same days."""
    bars = []
    d0 = dt.date(2026, 1, 1)
    price = flat_price
    for i in range(n):
        date = (d0 + dt.timedelta(days=i)).isoformat()
        if i in jump_days:
            o = price
            c = price * (1 + pct)
            h = c
            l = o
            v = 5000
        else:
            o = h = l = c = price
            v = 1200 if jump_days and i > max(jump_days) else 1000
        bars.append({"date": date, "o": o, "h": h, "l": l, "c": c, "v": v})
        if i in jump_days:
            price = c
    return bars


def test_replay_breakout_entry_and_next_bar_open_fill():
    """A clean breakout day (tech confirmed) with immediate chip confirmation
    (dates are on/before the signal day, so no cutoff issue) must fire
    entry=True the same day, and simulate_breakout() must fill the trade at
    the *next* bar's open, not the signal day's own price."""
    bars = _flat_then_breakout_bars(jump_days=(65,))
    inst = [{"date": b["date"], "foreign": 1000, "trust": 1000, "dealer": 1000} for b in bars]
    margin = [{"date": b["date"], "margin_balance": 100000} for b in bars]

    rows = bs.replay_breakout(bars, inst, margin)
    signal_row = next(r for r in rows if r["entry"])
    signal_idx = next(i for i, b in enumerate(bars) if b["date"] == signal_row["date"])

    sim = bs.simulate_breakout(rows, bars)
    assert len(sim["trades"]) == 1
    trade = sim["trades"][0]
    assert trade["entry_date"] == bars[signal_idx + 1]["date"]
    assert trade["entry_price"] == bars[signal_idx + 1]["o"]
    assert set(sim.keys()) == {"trades", "summary"}
    assert set(sim["summary"].keys()) == {
        "n_trades", "win_rate", "avg_ret", "total_return", "buy_hold_return",
        "max_drawdown", "start_date", "end_date",
    }


def test_replay_breakout_chip_cutoff_blocks_same_day_lookahead():
    """Chip data that only turns positive starting on the breakout day
    itself must NOT confirm entry that same day (the window is cut off at
    the PREVIOUS day's date) -- but must confirm the following day once
    that positive row is available. Two consecutive tech-confirmed days are
    used so the effect is observable on both sides of the cutoff."""
    bars = _flat_then_breakout_bars(jump_days=(65, 66))
    inst = [{"date": b["date"],
             "foreign": (1000 if i >= 65 else 0),
             "trust": (1000 if i >= 65 else 0),
             "dealer": (1000 if i >= 65 else 0)}
            for i, b in enumerate(bars)]
    margin = [{"date": b["date"], "margin_balance": 100000} for b in bars]

    rows = bs.replay_breakout(bars, inst, margin)
    by_date = {r["date"]: r for r in rows}
    day65, day66 = bars[65]["date"], bars[66]["date"]

    assert by_date[day65]["tech"]["confirmed"] is True
    assert by_date[day65]["chip"]["confirmed"] is False  # cutoff excludes day65's own chip row
    assert by_date[day65]["entry"] is False

    assert by_date[day66]["tech"]["confirmed"] is True
    assert by_date[day66]["chip"]["confirmed"] is True  # day65's row is now <= cutoff
    assert by_date[day66]["entry"] is True


def test_simulate_breakout_no_entries_returns_empty_trades_and_none_summary():
    bars = _flat_then_breakout_bars(jump_days=())  # never breaks out
    inst = [{"date": b["date"], "foreign": -1, "trust": -1, "dealer": -1} for b in bars]
    margin = [{"date": b["date"], "margin_balance": 100000} for b in bars]
    rows = bs.replay_breakout(bars, inst, margin)
    sim = bs.simulate_breakout(rows, bars)
    assert sim["trades"] == []
    assert sim["summary"]["n_trades"] == 0
    assert sim["summary"]["total_return"] is None
