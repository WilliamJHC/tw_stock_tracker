# -*- coding: utf-8 -*-
"""Synthetic-data tests for src/ep_strategy.py and scripts/run_ep_backtest.py.
Nothing here reads the real factor cache (pre-registration section 8: no
real-data backtest before the lock)."""
import datetime as dt
import json
import os
import sys

import pytest

import ep_strategy as ep
import pullback_backtest as bt
import pullback_strategy as ps

COST = 2 * 0.001425 * 0.22 + 0.003          # 0.363% round trip, section 2.2
COST_FULL = 2 * 0.001425 + 0.003            # 0.585%, sensitivity


def day(k):
    return (dt.date(2020, 1, 1) + dt.timedelta(days=k)).isoformat()


def bar(k, o, h, l, c, v=1_000_000):
    return {"date": day(k), "o": o, "h": h, "l": l, "c": c, "v": v}


def base_bars(n=25, sig=(105, 108, 104, 107), sig_v=4_000_000):
    """n flat bars at 100 (v 1M -> turnover 100M), then the signal bar at index n.
    Signal: gap 5%, volume 4x, close >= open. Trigger 108.5, stop 103.5, R 5."""
    bars = [bar(k, 100, 101, 99, 100) for k in range(n)]
    bars.append(bar(n, *sig, v=sig_v))
    return bars, n


def sim(extra, mode="ep", params=None, sig=(105, 108, 104, 107), terminal=False):
    bars, i = base_bars(sig=sig)
    for k, (o, h, l, c) in enumerate(extra):
        bars.append(bar(i + 1 + k, o, h, l, c))
    return ep.simulate_trade(bars, i, mode, params, terminal=terminal), bars, i


ENTRY = (107.5, 109, 106, 108)                  # fills at the 108.5 trigger, low stays above the 103.5 stop
FLAT_UP = (108.5, 109, 107, 108)
DRIFT = (105, 106, 104, 104.5)                   # above the stop, under a risen EMA9


# ---------------------------------------------------------- price limits ----

def test_limit_prices_round_in_the_exchange_direction():
    assert ep.limit_up_price(100) == 110.0
    assert ep.limit_up_price(45.4) == 49.9          # 49.94 floors to the 0.05 grid
    assert ep.limit_up_price(107) == 117.5          # 117.7 floors to the 0.5 grid
    assert ep.limit_up_price(9.5) == 10.45
    assert ep.limit_down_price(100) == 90.0
    assert ep.limit_down_price(45.4) == 40.9        # 40.86 rounds UP to the 0.05 grid
    assert ep.limit_down_price(108) == 97.2         # 97.2 is already on the 0.1 grid (price < 100)
    assert ep.limit_down_price(120) == 108.0


# ---------------------------------------------------------- signal rules ----

def raw_of(bars):
    return [{**b, "c_adj": b["c"]} for b in bars]


def scan(bars, **kw):
    return ep.scan_code(raw_of(bars), bars, **kw)


def test_signal_fires_at_exact_boundaries():
    bars, _ = base_bars(sig=(104, 106, 103, 104), sig_v=3_000_000)    # gap exactly 4%, vol exactly 3x, close == open
    s = scan(bars)
    assert len(s) == 1 and s[0]["date"] == bars[-1]["date"]
    assert s[0]["gap_pct"] == pytest.approx(4.0) and s[0]["vol_ratio"] == pytest.approx(3.0)


def test_gap_just_under_four_percent_is_not_a_signal():
    bars, _ = base_bars(sig=(103.9, 106, 103, 104), sig_v=3_000_000)
    assert scan(bars) == []


def test_volume_just_under_three_times_is_not_a_signal():
    bars, _ = base_bars(sig=(104, 106, 103, 104), sig_v=2_999_999)
    assert scan(bars) == []


def test_close_below_open_is_not_a_signal():
    bars, _ = base_bars(sig=(104, 106, 103, 103.9), sig_v=3_000_000)
    assert scan(bars) == []


def test_volume_baseline_is_the_20_bars_before_t():
    bars, _ = base_bars(sig=(104, 106, 103, 104), sig_v=3_000_000)
    bars[-2]["v"] = 1_500_000                                       # inside the window: baseline up -> 3x no longer met
    assert scan(bars) == []


def test_liquidity_uses_prior_20_bars_not_the_spike_day():
    # prior turnover 40M < 50M; with the spike day included the average would be ~59M and pass
    bars = [bar(k, 100, 101, 99, 100, v=400_000) for k in range(25)]
    bars.append(bar(25, 104, 106, 103, 105, v=4_000_000))
    assert scan(bars) == []
    bars = [bar(k, 100, 101, 99, 100, v=600_000) for k in range(25)]    # 60M > 50M
    bars.append(bar(25, 104, 106, 103, 105, v=4_000_000))
    assert len(scan(bars)) == 1


def test_liquidity_uses_raw_close_not_adjusted():
    bars, _ = base_bars()
    raw = raw_of(bars)
    for r in raw[:-1]:
        r["c"] = 40.0                               # raw turnover 40M although adjusted bars say 100M
    assert ep.scan_code(raw, bars) == []


def test_scan_signals_only_common_stocks_and_sorted():
    bars, _ = base_bars()
    uni = {"2330": bars, "0050": bars, "00878": bars, "1101": bars}
    raw = {c: raw_of(b) for c, b in uni.items()}
    sig = ep.scan_signals(raw, uni)
    assert [s["code"] for s in sig] == ["1101", "2330"]
    assert ep.is_common_stock("2330") and not ep.is_common_stock("0050") and not ep.is_common_stock("00878")


def test_signals_ignore_later_bars():
    bars, i = base_bars()
    bars += [bar(i + 1 + k, 110, 111, 109, 110) for k in range(10)]
    full = ep.scan_signals({"2330": raw_of(bars)}, {"2330": bars})
    cut = ep.scan_signals({"2330": raw_of(bars[:i + 1])}, {"2330": bars[:i + 1]})
    assert full == cut and len(cut) == 1


def test_revenue_label_uses_latest_usable_row_only():
    series = [{"usable_date": "2019-12-10", "yoy_pct": 35.0}, {"usable_date": "2020-02-10", "yoy_pct": 10.0}]
    assert ep.revenue_label(series, "2019-12-09") == "na"
    assert ep.revenue_label(series, "2019-12-10") == "yes"
    assert ep.revenue_label(series, "2020-01-15") == "yes"
    assert ep.revenue_label(series, "2020-02-10") == "no"
    assert ep.revenue_label(None, "2020-02-10") == "na"
    assert ep.revenue_label([{"usable_date": "2019-01-01", "yoy_pct": 30.0}], "2020-01-01") == "yes"   # >= 30


def test_scan_attaches_revenue_label_without_gating_on_it():
    bars, _ = base_bars()
    s = ep.scan_signals({"2330": raw_of(bars)}, {"2330": bars}, revenue={"2330": [{"usable_date": "2019-01-01", "yoy_pct": -5.0}]})
    assert len(s) == 1 and s[0]["revenue_label"] == "no"


def test_liquid_pool_matches_scan_liquidity_rule():
    bars, i = base_bars()
    thin = [bar(k, 100, 101, 99, 100, v=400_000) for k in range(len(bars))]
    raw = {"1101": raw_of(bars), "2330": raw_of(thin), "0050": raw_of(bars)}
    pool = ep.liquid_pool(raw, [bars[i]["date"], "2030-01-01"])
    assert pool[bars[i]["date"]] == ["1101"] and pool["2030-01-01"] == []


# --------------------------------------------------------------- entries ----

def test_fill_at_trigger_when_intraday_high_crosses_it():
    t, _, _ = sim([ENTRY] + [FLAT_UP] * 40)
    assert t["fill"] == 108.5 and t["entry_kind"] == "trigger" and t["stop"] == 103.5 and t["stop_pct"] == pytest.approx(5 / 108.5 * 100)


def test_fill_at_open_when_gap_is_already_above_trigger():
    t, _, _ = sim([(109, 110, 108.5, 109.5)] + [FLAT_UP] * 40)
    assert t["fill"] == 109 and t["entry_kind"] == "open"


def test_no_fill_when_trigger_not_reached():
    t, _, _ = sim([(107, 108, 106, 107.5)])
    assert t == {"no_fill": "no_trigger"}


def test_trigger_above_limit_up_is_not_filled():
    # signal high 125 is more than 10% above its close 111 -> trigger 125.5 > limit 122
    t, _, _ = sim([(120, 122, 119, 121)], sig=(104, 125, 104, 111))
    assert t == {"no_fill": "trigger_above_limit"}


def test_open_locked_at_limit_up_is_not_filled():
    t, _, _ = sim([(117.5, 117.5, 117.5, 117.5)] + [FLAT_UP] * 40)             # prev close 107 -> limit 117.5, high = low
    assert t == {"no_fill": "locked_limit_up"}
    t, _, _ = sim([(117, 117.5, 117, 117.5)])                                    # within one tick of the limit all day
    assert t == {"no_fill": "locked_limit_up"}


def test_open_at_limit_but_not_locked_is_filled_at_open():
    # same bar as above but it traded lower during the day; a wide max stop lets the 117.5 fill through
    t, _, _ = sim([(117.5, 117.5, 115, 116)] + [FLAT_UP] * 40, params={"max_stop_pct": 50})
    assert t["fill"] == 117.5


def test_control_open_locked_at_limit_up_is_not_filled():
    t, _, _ = sim([(117.5, 117.5, 117.5, 117.5)], mode="control")
    assert t == {"no_fill": "locked_limit_up"}


# ------------------------------------------------------------------ stop ----

def test_stop_is_signal_low_minus_one_tick():
    t, _, _ = sim([ENTRY] + [FLAT_UP] * 40)
    assert t["stop"] == 103.5 and t["stop_widened"] is False


def test_stop_under_three_percent_is_widened_to_three_percent():
    # low 107 -> stop 106.5 -> distance 1.84% -> widened to fill*0.97 = 105.245 -> 105.0 on the tick grid
    t, _, _ = sim([ENTRY] + [FLAT_UP] * 40, sig=(105, 108, 107, 107.5))
    assert t["stop_widened"] is True and t["stop"] == 105.0
    assert t["raw_stop_pct"] == pytest.approx((108.5 - 106.5) / 108.5 * 100)
    assert t["stop_pct"] >= 3.0 - 1e-9


def test_stop_exactly_three_percent_is_left_alone():
    # control fill = open 100; signal low 97.1 -> stop 97.0 -> exactly 3.0%: not widened
    t, _, _ = sim([(100, 101, 99.5, 100.5)] + [(100, 101, 99.5, 100.5)] * 40, mode="control", sig=(100, 101, 97.1, 100.5))
    assert t["stop"] == 97.0 and t["stop_widened"] is False
    # signal low 97.2 -> stop 97.1 -> 2.9% -> widened to 97.0
    t, _, _ = sim([(100, 101, 99.5, 100.5)] + [(100, 101, 99.5, 100.5)] * 40, mode="control", sig=(100, 101, 97.2, 100.5))
    assert t["stop"] == 97.0 and t["stop_widened"] is True


def test_stop_wider_than_eight_percent_is_not_entered():
    t, _, _ = sim([ENTRY], sig=(105, 108, 95, 107))        # stop 94.5 -> 12.9% from 108.5
    assert t == {"no_fill": "stop_too_wide"}


def test_stop_cap_boundary_between_seven_and_nine_percent():
    t, _, _ = sim([ENTRY] + [FLAT_UP] * 40, sig=(105, 108, 100.5, 107))      # stop 100.0 -> 7.8%: entered
    assert t["stop"] == 100.0 and t["stop_pct"] < 8.0
    t, _, _ = sim([ENTRY], sig=(105, 108, 99.5, 107))                         # stop 99.4 -> 8.4%: not entered
    assert t == {"no_fill": "stop_too_wide"}


def test_frozen_parameters_match_plan_section_5():
    p = ep.EP_PARAMS
    assert (p["min_gap_pct"], p["min_volume_mult"], p["min_turnover"], p["avg_window"]) == (4.0, 3.0, 50_000_000, 20)
    assert (p["min_stop_pct"], p["max_stop_pct"], p["stop_buffer_ticks"], p["trigger_ticks"]) == (3.0, 8.0, 1, 1)
    assert (p["r_targets"], p["trail_ema"], p["max_hold_bars"]) == ((3, 5), 9, 30)
    assert p["scale_fractions"] == (1 / 3, 1 / 3) and p["fee_discount"] == 0.22


def test_stop_floor_param_changes_the_floor():
    # raw stop 106.5 is 1.84% below the 108.5 fill
    kw = dict(sig=(105, 108, 107, 107.5))
    t2, _, _ = sim([ENTRY] + [FLAT_UP] * 40, params={"min_stop_pct": 2.0}, **kw)
    t4, _, _ = sim([ENTRY] + [FLAT_UP] * 40, params={"min_stop_pct": 4.0}, **kw)
    assert t2["stop"] == 106.0 and t2["stop_widened"] is True       # 108.5 * 0.98 = 106.33 -> down to the grid
    assert t4["stop"] == 104.0                                      # 108.5 * 0.96 = 104.16 -> down to the grid


# ----------------------------------------------------------------- exits ----

def test_same_bar_stop_wins_on_the_entry_bar_at_the_stop_price():
    t, _, _ = sim([(107.5, 109, 103, 108)])                       # triggers AND trades down through the 103.5 stop
    assert t["exit_reason"] == "stop" and t["hold_bars"] == 0
    assert t["gross_r"] == pytest.approx(-1.0)
    assert t["net_r"] == pytest.approx(-1.0 - COST * 108.5 / 5)


def test_entry_bar_stop_beats_a_big_up_move_too():
    t, _, _ = sim([(107.5, 130, 103, 125)])
    assert t["exit_reason"] == "stop" and t["gross_r"] == pytest.approx(-1.0) and t["scaled_out"] == 0


def test_later_bar_touching_stop_exits_at_stop_price():
    t, _, _ = sim([ENTRY, (106, 107, 103, 104)])
    assert t["exit_reason"] == "stop" and t["gross_r"] == pytest.approx(-1.0) and t["hold_bars"] == 1


def test_gap_through_the_stop_exits_at_the_open():
    t, _, _ = sim([ENTRY, (100, 101, 99, 100)])
    assert t["exit_reason"] == "stop"
    assert t["gross_r"] == pytest.approx((100 - 108.5) / 5)


def test_locked_limit_down_defers_the_exit_to_the_first_tradable_open():
    # entry bar closes 108 -> limit-down 97.2; bar 2 is locked there, bar 3 trades again
    t, _, i = sim([ENTRY, (97.2, 97.2, 97.2, 97.2), (94, 95, 93, 94), (93, 94, 92, 93)])
    assert t["exit_reason"] == "stop" and t["deferred"] is True
    assert t["exit_idx"] == i + 3 and t["gross_r"] == pytest.approx((94 - 108.5) / 5)


def test_gap_down_not_locked_exits_immediately():
    t, _, i = sim([ENTRY, (97.2, 98, 96, 97), (95, 96, 94, 95)])        # open at the limit but traded higher: sellable
    assert t["deferred"] is False and t["exit_idx"] == i + 2
    assert t["gross_r"] == pytest.approx((97.2 - 108.5) / 5)


def test_three_r_then_five_r_then_ema9_remainder():
    # r = 5: t3 = 123.5, t5 = 133.5
    seq = [ENTRY, (109, 124, 108, 123), (123, 134, 122, 133), (133, 133, 110, 110), (112, 113, 111, 112), (112, 113, 111, 112)]
    t, bars, i = sim(seq)
    assert t["exit_reason"] == "ema9" and t["scaled_out"] == 2
    assert t["exit_idx"] == i + 5                                       # break on bar i+4 close, exit at the next open
    assert t["gross_r"] == pytest.approx((3 + 5 + (112 - 108.5) / 5) / 3)
    assert t["net_r"] == pytest.approx(t["gross_r"] - COST * 108.5 / 5)


def test_same_bar_three_and_five_r_sells_two_thirds():
    seq = [ENTRY, (109, 134, 108, 133), (133, 133, 105, 105), (112, 113, 111, 112), (112, 113, 111, 112)]
    t, _, _ = sim(seq)
    assert t["scaled_out"] == 2
    assert t["gross_r"] == pytest.approx((3 + 5 + (112 - 108.5) / 5) / 3)


def test_gap_open_above_three_r_sells_at_the_open():
    seq = [ENTRY, (125, 126, 124, 125.5), (125, 126, 100, 101)]          # 1/3 at the 125 open, then the rest stops out at 103.5
    t, _, _ = sim(seq)
    assert t["scaled_out"] == 1 and t["exit_reason"] == "stop"
    assert t["gross_r"] == pytest.approx(((125 - 108.5) / 5 + 2 * (-1)) / 3)


def test_stop_is_not_moved_up_after_the_first_scale_out():
    seq = [ENTRY, (109, 124, 108, 123), (115, 116, 103, 104)]            # sells 1/3 at 3R, then hits the ORIGINAL stop
    t, _, _ = sim(seq)
    assert t["exit_reason"] == "stop" and t["scaled_out"] == 1
    assert t["gross_r"] == pytest.approx((3 + 2 * -1) / 3)


def test_ema9_exit_not_applied_before_the_first_scale_out():
    seq = [ENTRY] + [FLAT_UP] * 5 + [DRIFT] * 40                          # closes sit under EMA9 but above the stop, 3R never hit
    t, _, i = sim(seq)
    assert t["exit_reason"] == "time" and t["scaled_out"] == 0


def test_time_stop_exits_on_the_open_of_the_31st_bar():
    seq = [ENTRY] + [FLAT_UP] * 5 + [DRIFT] * 40
    t, bars, i = sim(seq)
    assert t["hold_bars"] == 30 == ep.EP_PARAMS["max_hold_bars"]
    assert t["exit_idx"] == t["entry_idx"] + 30
    assert t["gross_r"] == pytest.approx((bars[t["exit_idx"]]["o"] - 108.5) / 5)


def test_censored_when_data_ends_and_terminal_when_delisted():
    seq = [ENTRY] + [FLAT_UP] * 4
    t, bars, _ = sim(seq)
    assert t == {"censored": True}
    t, bars, _ = sim(seq, terminal=True)
    assert t["exit_reason"] == "terminal" and t["exit_idx"] == len(bars) - 1
    assert t["gross_r"] == pytest.approx((108 - 108.5) / 5)


def test_cost_rate_main_and_sensitivity():
    assert bt._cost_rate(ep.EP_PARAMS) == pytest.approx(COST) and 0.00362 < COST < 0.00364
    t, _, _ = sim([(107.5, 109, 103, 108)], params={"fee_discount": 1.0})
    assert t["net_r"] == pytest.approx(-1.0 - COST_FULL * 108.5 / 5)


# --------------------------------------------------------------- control ----

def test_control_enters_at_next_open_with_the_same_stop_rules():
    t, _, _ = sim([(107, 108, 106, 107)] + [FLAT_UP] * 40, mode="control")
    assert t["fill"] == 107 and t["stop"] == 103.5 and t["entry_kind"] == "open"
    t, _, _ = sim([(103, 104, 102, 103)], mode="control")
    assert t == {"no_fill": "stop_above_fill"}                              # next open already under the signal-low stop


def universe_with_event(codes=("1101", "2330", "2454")):
    bars, i = base_bars()
    bars += [bar(i + 1, *ENTRY)] + [bar(i + 2 + k, *FLAT_UP) for k in range(40)]
    return {c: [dict(b) for b in bars] for c in codes}, bars[i]["date"]


def test_control_draw_is_seeded_and_reproducible():
    uni, d = universe_with_event()
    pool = {d: ["1101", "2330", "2454"]}
    a, _ = ep.run_trades(uni, [], "control", control_days=pool)
    b, _ = ep.run_trades(uni, [], "control", control_days=pool)
    assert len(a) == 1 and [t["code"] for t in a] == [t["code"] for t in b]
    picks = {ep.run_trades(uni, [], "control", control_days=pool, rng_seed=s)[0][0]["code"] for s in range(30)}
    assert picks == {"1101", "2330", "2454"}                                # the draw really depends on the seed
    seen = {ep.run_trades(uni, [], "control", control_days={d: list(reversed(pool[d]))})[0][0]["code"]}
    assert seen == {a[0]["code"]}                                           # pool order does not matter


def test_control_uses_fixed_module_seed():
    assert ep.SEED == bt.SEED == 20261014


# ---------------------------------------------------- per-code sequencing ----

def test_one_position_per_code_with_cooldown_and_stats():
    bars, i = base_bars()
    ext = [ENTRY, (106, 107, 103, 104)] + [(104, 105, 103.6, 104.5)] * 40     # first trade stops out on bar i+2
    for k, row in enumerate(ext):
        bars.append(bar(i + 1 + k, *row))
    sigs = [{"date": bars[i]["date"], "code": "1101"}] + [{"date": bars[i + 2 + k]["date"], "code": "1101"} for k in range(1, 8)]
    trades, stats = ep.run_trades({"1101": bars}, sigs, "ep")
    assert len(trades) >= 1 and stats["blocked"] >= 1
    for later in trades[1:]:
        assert later["entry_idx"] > trades[0]["exit_idx"] + ep.COOLDOWN_BARS
    assert stats["signals"] == len(sigs) and sum(stats["no_fill"].values()) + len(trades) + stats["blocked"] + stats["censored"] == len(sigs)


def test_revenue_label_is_carried_to_trades_and_grouped():
    uni, d = universe_with_event(("1101",))
    trades, _ = ep.run_trades(uni, [{"date": d, "code": "1101", "revenue_label": "yes"}], "ep")
    assert trades[0]["revenue_label"] == "yes"
    ts = [{"signal_date": f"2020-{m:02d}-05", "code": f"C{k}", "net_r": 0.1, "gross_r": 0.2, "hold_bars": 5,
           "revenue_label": "yes"} for m in range(1, 13) for k in range(3)]
    g = ep.catalyst_groups(ts, n=200)
    assert g["yes"]["n"] == 36 and "ci" in g["yes"] and g["no"]["note"] == "樣本不足" and g["na"]["n"] == 0


def test_terminal_codes_rule_and_hold_distribution():
    uni = {"A": [{"date": "2020-06-30"}], "B": [{"date": "2020-06-01"}], "C": [{"date": "2020-05-31"}]}
    assert ep.terminal_codes(uni) == set()                                  # cutoff 2020-05-31: only strictly earlier last bars count
    uni["D"] = [{"date": "2020-05-30"}]
    assert ep.terminal_codes(uni) == {"D"}
    d = ep.hold_distribution([{"hold_bars": h, "exit_reason": "stop"} for h in (0, 0, 3, 5, 6, 12, 25, 30)])
    assert d["hold_bars"] == {"0": 2, "1-5": 2, "6-10": 1, "11-20": 1, "21-30": 2} and d["exit_reason"] == {"stop": 8}


# ----------------------------------------------- reuse of the pullback stats ----

def mk(month, code, net, sdate=None):
    return {"signal_date": sdate or f"{month}-05", "code": code, "net_r": net, "gross_r": net + 0.1, "hold_bars": 5}


def test_pullback_statistics_are_reused_with_the_plan_gates():
    assert (bt.MIN_TRADES, bt.MIN_MEAN_NET_R, bt.SPLIT_DATE) == (300, 0.10, "2020-01-01")
    ts = [mk("2020-01", "A", 1, sdate="2020-01-20"), mk("2020-01", "A", -1, sdate="2020-01-03"), mk("2020-02", "A", 3)]
    assert [t["net_r"] for t in bt.dedup_monthly(ts)] == [-1, 3]            # one per code per MONTH, the first by date
    crit = bt.evaluate_criteria(ts, ts, n=100)
    assert crit["verdict"] == "樣本不足" and crit["c1_sample"] is False


# ------------------------------------------------------------ prereg doc ----

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREREG = os.path.join(REPO, "docs", "ep_backtest_preregistration_2026-10.md")


def test_every_ep_param_is_named_in_the_preregistration():
    text = open(PREREG, encoding="utf-8").read()
    missing = [k for k in ep.EP_PARAMS if f"`{k}`" not in text]
    assert missing == []
    for n in range(1, 9):
        assert f"## {n}. " in text


# ---------------------------------------------------------- lock guard (CLI) ----

def _load_cli():
    import importlib.util
    path = os.path.join(REPO, "scripts", "run_ep_backtest.py")
    spec = importlib.util.spec_from_file_location("run_ep_backtest", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LOCK_BLANK = """## 8. 鎖定

- [ ] V2 池資料量測已貼在 §2.1：______
- [ ] 使用者已確認 §2、§4：______
- [ ] 鎖定時 `src/ep_strategy.py` 所在 commit：______
"""
LOCK_DONE = """## 8. 鎖定

- [x] V2 池資料量測已貼在 §2.1：1860 檔
- [x] 使用者已確認 §2、§4：已確認
- [x] 鎖定時 `src/ep_strategy.py` 所在 commit：abc1234
鎖定日期：2026-10-22
"""


def test_cli_refuses_unlocked_and_parses_locked(tmp_path):
    cli = _load_cli()
    blank = tmp_path / "blank.md"
    blank.write_text(LOCK_BLANK, encoding="utf-8")
    with pytest.raises(SystemExit):
        cli.read_lock(str(blank))
    half = tmp_path / "half.md"
    half.write_text(LOCK_DONE.replace("- [x] 使用者", "- [ ] 使用者"), encoding="utf-8")
    with pytest.raises(SystemExit):
        cli.read_lock(str(half))
    done = tmp_path / "done.md"
    done.write_text(LOCK_DONE, encoding="utf-8")
    assert cli.read_lock(str(done)) == "abc1234"
    nosec = tmp_path / "nosec.md"
    nosec.write_text("# x\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        cli.read_lock(str(nosec))


def test_real_preregistration_is_still_unlocked():
    """The checked-in draft must stay unlocked until the user fills section 8."""
    cli = _load_cli()
    sec = open(cli.DEFAULT_PREREG, encoding="utf-8").read().split("## 8. 鎖定")[-1]
    if "______" not in sec:
        pytest.skip("pre-registration has been locked")
    assert "- [x]" not in sec
    with pytest.raises(SystemExit):
        cli.read_lock(cli.DEFAULT_PREREG)


def test_cli_main_refuses_the_real_unlocked_preregistration(monkeypatch):
    cli = _load_cli()
    if "______" not in open(cli.DEFAULT_PREREG, encoding="utf-8").read().split("## 8. 鎖定")[-1]:
        pytest.skip("pre-registration has been locked")
    monkeypatch.setattr(sys, "argv", ["x"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert "尚未鎖定" in str(e.value)


# ------------------------------------------------ CLI end to end (synthetic) ----

def synth_rows(code_k, n=120, events=(30, 70), v=1_000_000):
    """Flat ~100 with EP events at the given bar indexes. Event bar: gap 5%, 4x volume, close above
    open. The bar after it opens at the event close and trades 1.9% higher (crosses the trigger,
    stop distance ~4.6%). Real ISO dates, shifted per code."""
    rows, price = [], 100.0
    for k in range(n):
        if k in events:
            o, h, l, c, vol = price * 1.05, price * 1.08, price * 1.04, price * 1.07, v * 4
        elif k - 1 in events:
            o, h, l, c, vol = price, price * 1.019, price * 0.99, price * 1.01, v
        else:
            o, h, l, c, vol = price, price * 1.01, price * 0.99, price, v
        rows.append({"date": day(k + code_k), "o": round(o, 2), "h": round(h, 2), "l": round(l, 2),
                     "c": round(c, 2), "c_adj": round(c, 2), "v": vol})
        price = c
    return rows


def write_cache(tmp_path):
    main = tmp_path / "factor_run"
    delisted = tmp_path / "factor_run_delisted"
    main.mkdir()
    delisted.mkdir()
    rev = [{"usable_date": "2019-01-01", "yoy_pct": 40.0}]
    for k, code in enumerate(("1101", "1102", "2330", "2454", "3008", "0050")):
        (main / f"{code}.json").write_text(json.dumps({"bars": synth_rows(k), "revenue_series": rev, "roe_series": []}), encoding="utf-8")
    (main / "9999.json").write_text(json.dumps({"skip": True}), encoding="utf-8")
    rows = synth_rows(2, n=60)                       # ends ~60 days before the main cache: a delisted code
    (delisted / "1234.json").write_text(json.dumps({"code": "1234", "status": "ok", "bars": rows, "revenue_series": rev, "roe_series": []}), encoding="utf-8")
    (delisted / "_manifest.json").write_text(json.dumps({"candidates": ["1234"], "in_main_cache": []}), encoding="utf-8")
    return main, delisted


def test_cli_end_to_end_on_synthetic_cache(tmp_path, monkeypatch, capsys):
    cli = _load_cli()
    main, delisted = write_cache(tmp_path)
    prereg = tmp_path / "prereg.md"
    prereg.write_text(LOCK_DONE, encoding="utf-8")
    out = tmp_path / "result.json"
    monkeypatch.setattr(cli.rfi, "RUN_CACHE_DIR", str(main))
    monkeypatch.setattr(cli.rss, "DELISTED_DIR", str(delisted))
    monkeypatch.setattr(cli, "check_locked_strategy", lambda commit: None)
    monkeypatch.setattr(sys, "argv", ["x", "--prereg", str(prereg), "--bootstrap-n", "100", "--out", str(out)])
    cli.main()
    text = capsys.readouterr().out
    for needle in ("主要設定", "結論：樣本不足", "敏感度", "催化劑", "持有期", "V2", "關卡 6", "不可直接"):
        assert needle in text, needle
    res = json.loads(out.read_text(encoding="utf-8"))
    assert res["criteria"]["verdict"] == "樣本不足"
    u = res["universe"]
    assert (u["main"], u["delisted_ok"], u["merged"], u["not_common"], u["tradable"]) == (6, 1, 7, 1, 6)   # 0050 dropped, skip file ignored
    codes = {t["code"] for t in res["trades"]}
    assert codes and "0050" not in codes
    assert all(t["revenue_label"] == "yes" for t in res["trades"])
    assert res["control"] and res["stats"]["terminal"] >= 1                       # the delisted code's open trade closes at its last bar
    assert set(res["sensitivity"]) == {"cost_0.585%", "stop_floor_2%", "stop_floor_4%"}
    assert res["sensitivity"]["cost_0.585%"]["mean_net_r"] < res["criteria"]["mean_net_r"]   # same trades, higher cost


def test_cli_check_locked_strategy_names_the_dependencies():
    cli = _load_cli()
    assert set(cli.LOCKED_FILES) == {"src/ep_strategy.py", "src/pullback_backtest.py", "src/pullback_strategy.py"}
    for f in cli.LOCKED_FILES:
        assert os.path.exists(os.path.join(REPO, f))
