# -*- coding: utf-8 -*-
"""Unit tests for src/pullback_strategy.py (強勢股回調買法). Pure functions,
synthetic bars only -- no network, no DB. The worked numbers in the sizing /
R-target tests are the method's own example (帳戶 10,000、買 20、停損 19.50
-> 100 股、投入 2,000、3R 21.50、5R 22.50)."""
import datetime as dt

import pytest

import pullback_strategy as ps


# ------------------------------------------------------------ fixtures ----

def _bars_from(rows, start=dt.date(2026, 1, 1)):
    out, d = [], start
    for o, h, l, c, v in rows:
        d += dt.timedelta(days=1)
        out.append({"date": d.isoformat(), "o": o, "h": h, "l": l, "c": c, "v": v})
    return out


def _setup_bars():
    """60 flat bars, a 30-bar +2%/day advance, then a 20-bar base with
    higher lows and a range that narrows bar by bar -- the textbook shape."""
    rows, p = [], 50.0
    for _ in range(60):
        rows.append((p, p * 1.01, p * 0.99, p, 3_000_000))
        p *= 1.001
    for _ in range(30):
        o = p
        p *= 1.02
        rows.append((o, p * 1.01, o * 0.99, p, 6_000_000))
    hi = p
    path = [0.97, 0.95, 0.94, 0.96, 0.97, 0.955, 0.95, 0.965, 0.97, 0.96,
            0.958, 0.965, 0.968, 0.963, 0.962, 0.966, 0.967, 0.964, 0.965, 0.966]
    for k, f in enumerate(path):
        c = hi * f
        rng = 0.03 * (1 - k / 25)
        rows.append((c * 1.002, c * (1 + rng / 2), c * (1 - rng / 2), c, 3_000_000))
    return _bars_from(rows)


def _downtrend_bars():
    rows, p = [], 100.0
    for i in range(120):
        p *= 0.995 if i % 5 else 1.004
        rows.append((p, p * 1.01, p * 0.99, p, 3_000_000))
    return _bars_from(rows)


# ------------------------------------------------------------ tick grid ----

@pytest.mark.parametrize("price,tick", [(9.99, 0.01), (10, 0.05), (49.95, 0.05), (50, 0.1),
                                        (100, 0.5), (500, 1.0), (1000, 5.0)])
def test_tw_tick_size(price, tick):
    assert ps.tw_tick_size(price) == tick


def test_round_to_tick_modes():
    assert ps.round_to_tick(20.03, "down") == 20.0
    assert ps.round_to_tick(20.03, "up") == 20.05
    assert ps.round_to_tick(123.3, "nearest") == 123.5
    assert ps.round_to_tick(None) is None


# --------------------------------------------------------- indicators ----

def test_ema_series_seeds_with_sma_then_recurses():
    out = ps.ema_series([1, 2, 3, 4], 3)
    assert out[:2] == [None, None]
    assert out[2] == pytest.approx(2.0)
    assert out[3] == pytest.approx(4 * 0.5 + 2.0 * 0.5)


def test_anchored_vwap_weights_typical_price_by_volume():
    bars = _bars_from([(10, 12, 8, 10, 100), (10, 22, 18, 20, 300)])
    assert ps.anchored_vwap(bars, 0) == pytest.approx((10 * 100 + 20 * 300) / 400)
    assert ps.anchored_vwap(bars, 1) == pytest.approx(20)


def test_unfilled_gaps_drops_filled_ones():
    bars = _bars_from([
        (10, 10, 9, 10, 1), (11, 12, 11, 12, 1),   # gap 10->11, later filled
        (12, 12, 10, 11, 1),                        # low 10 fills it
        (13, 14, 13, 14, 1),                        # gap 12->13, never filled
        (14, 15, 13.5, 15, 1),
    ])
    gaps = ps.unfilled_gaps(bars)
    assert [(g["bottom"], g["top"]) for g in gaps] == [(12, 13)]


# ----------------------------------------------------- step 1: screen ----

def _close_bars(closes, vol=1_000_000):
    return [{"date": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", "c": c, "v": vol} for i, c in enumerate(closes)]


def test_strength_screen_needs_return_rank_and_liquidity():
    n = 130
    universe = {
        # +100% over 6m, liquid -> qualifies on 6m (and 3m)
        "AAAA": {"bars": _close_bars([100 + i * 100 / (n - 1) for i in range(n)])},
        # +10%: below the 30% bar
        "BBBB": {"bars": _close_bars([100 + i * 10 / (n - 1) for i in range(n)])},
        # +100% but illiquid (turnover far below 50M)
        "CCCC": {"bars": _close_bars([100 + i * 100 / (n - 1) for i in range(n)], vol=10)},
    }
    for k in range(7):  # filler so the rank threshold means something
        universe[f"F{k:03d}"] = {"bars": _close_bars([100] * n)}
    out = ps.strength_screen(universe)
    assert set(out) == {"AAAA"}
    assert "6m" in out["AAAA"]["qualifying"]
    assert out["AAAA"]["strength"] == 1.0


def test_strength_screen_prefers_c_adj():
    n = 130
    bars = _close_bars([100] * n)
    for i, b in enumerate(bars):
        b["c_adj"] = 50 + i * 50 / (n - 1)   # adjusted series doubles; raw is flat
    out = ps.strength_screen({"AAAA": {"bars": bars}}, params={"top_rank_pct": 1.0})
    assert out["AAAA"]["returns"]["6m"] > 90


def test_close_only_prefilter():
    up = [{"date": str(i), "c": 100 + i, "v": 1} for i in range(80)]
    down = [{"date": str(i), "c": 200 - i, "v": 1} for i in range(80)]
    assert ps.close_only_prefilter(up) is True
    assert ps.close_only_prefilter(down) is False


def test_theme_strength_picks_most_crowded_category():
    industries = {"A": ["半導體業", "電子工業"], "B": ["半導體業"], "C": ["航運業"], "D": []}
    out = ps.theme_strength(["A", "B", "C", "D"], industries)
    assert out["A"] == {"theme": "半導體業", "peers": 2}
    assert out["C"] == {"theme": "航運業", "peers": 1}
    assert "D" not in out


# --------------------------------------------------- step 1: the base ----

def test_find_base_none_while_still_making_highs():
    bars = _bars_from([(10 + i, 11 + i, 9 + i, 10.5 + i, 1) for i in range(30)])
    assert ps.find_base(bars) is None


def test_consolidation_checks_pass_on_textbook_base():
    bars = _setup_bars()
    base = ps.find_base(bars)
    closes = [b["c"] for b in bars]
    checks = ps.consolidation_checks(bars, base, ps.ema_series(closes, 9), ps.ema_series(closes, 21))
    assert checks["higher_lows"] and checks["contracting"] and checks["reclaimed_ema"]
    assert not checks["downtrend"]
    assert checks["passed"]


def test_evaluate_setup_rejects_downtrend():
    out = ps.evaluate_setup(_downtrend_bars())
    assert out["passed"] is False
    assert out["reasons"]


def test_evaluate_setup_short_history():
    out = ps.evaluate_setup(_setup_bars()[:30])
    assert out["passed"] is False
    assert "歷史不足" in out["reasons"][0]


def test_evaluate_setup_textbook_case_end_to_end():
    out = ps.evaluate_setup(_setup_bars())
    assert out["passed"] is True
    assert out["status"] == "near_support"
    z = out["zone"]
    assert z["low"] <= z["high"] <= out["close"] * 1.01
    assert z["score"] >= 2
    pl = out["plan"]
    assert pl["stop_ref"] < z["low"] <= pl["entry_ref"]
    assert pl["stop_ok"] is True
    assert [t["r_mult"] for t in pl["r_targets"]["targets"]] == [3, 5]
    # every level is on the TW tick grid
    for price in (z["low"], z["high"], pl["entry_ref"], pl["stop_ref"]):
        assert ps.round_to_tick(price, "nearest") == price


def test_plan_stop_is_farther_of_zone_stop_and_atr_stop():
    zone_only = ps.evaluate_setup(_setup_bars(), {"stop_atr_mult": 0})["plan"]
    default = ps.evaluate_setup(_setup_bars())["plan"]
    wide_atr = ps.evaluate_setup(_setup_bars(), {"stop_atr_mult": 2.0})["plan"]
    assert zone_only["stop_basis"] == "zone" and zone_only["stop_ref"] == zone_only["zone_stop_ref"]
    # textbook zone is wider than 0.5 ATR, so the default keeps the zone stop
    assert default["stop_basis"] == "zone" and default["stop_ref"] == zone_only["stop_ref"]
    # a big multiple makes the ATR stop the farther one
    assert wide_atr["stop_basis"] == "atr"
    assert wide_atr["stop_ref"] < wide_atr["zone_stop_ref"]
    assert wide_atr["stop_ref"] <= ps.round_to_tick(wide_atr["entry_ref"] - 2.0 * wide_atr["atr14"], "down") + 1e-9
    assert ps.round_to_tick(wide_atr["stop_ref"], "nearest") == wide_atr["stop_ref"]


def test_plan_narrow_zone_gets_atr_stop_at_default_multiple():
    """Regression: a narrow confluence zone used to yield a ~0.2% 'stop' (~0.1x ATR).
    cluster_pct=0.5 shrinks the textbook zone to ~0.1% wide, so with the DEFAULT
    stop_atr_mult the ATR stop must be the one chosen."""
    out = ps.evaluate_setup(_setup_bars(), {"cluster_pct": 0.5})
    pl, z = out["plan"], out["zone"]
    assert (z["high"] - z["low"]) / z["high"] * 100 < 0.3
    assert pl["stop_basis"] == "atr"
    assert pl["stop_ref"] < pl["zone_stop_ref"]
    assert pl["stop_ref"] == ps.round_to_tick(pl["entry_ref"] - ps.PULLBACK_PARAMS["stop_atr_mult"] * pl["atr14"], "down")
    assert (pl["entry_ref"] - pl["stop_ref"]) >= 0.5 * pl["atr14"] - 1e-9
    assert pl["r_targets"]["r"] == pytest.approx(pl["entry_ref"] - pl["stop_ref"], abs=1e-6)


@pytest.mark.parametrize("mult", [0, 0.25, 0.5, 1.0, 2.0])
def test_plan_stop_equals_the_farther_of_the_two_candidates(mult):
    pl = ps.evaluate_setup(_setup_bars(), {"stop_atr_mult": mult})["plan"]
    atr_stop = ps.round_to_tick(pl["entry_ref"] - mult * pl["atr14"], "down") if mult else pl["zone_stop_ref"]
    assert pl["stop_ref"] == min(pl["zone_stop_ref"], atr_stop)


def test_plan_wide_atr_stop_flags_stop_ok_false_with_atr_reason():
    out = ps.evaluate_setup(_setup_bars(), {"stop_atr_mult": 2.0})
    assert out["plan"]["stop_ok"] is False
    assert out["passed"] is True                       # advisory only, not a hard failure
    assert any("ATR" in r for r in out["reasons"])


# --------------------------------------------------- step 2: the zone ----

def test_build_zone_prefers_avwap_ema21_prior_high_confluence():
    levels = [
        {"kind": "ema9", "price": 105.0}, {"kind": "prior_low", "price": 104.5},   # higher, 2 kinds
        {"kind": "avwap", "price": 100.0}, {"kind": "ema21", "price": 99.6},
        {"kind": "prior_high", "price": 99.2},                                     # the key confluence
    ]
    z = ps.build_zone(levels)
    assert set(z["kinds"]) == {"avwap", "ema21", "prior_high"}
    assert (z["low"], z["high"]) == (99.2, 100.0)
    assert z["score"] == 4


def test_build_zone_falls_back_to_highest_two_kind_cluster():
    levels = [{"kind": "ema9", "price": 105.0}, {"kind": "prior_low", "price": 104.5},
              {"kind": "ema50", "price": 90.0}]
    z = ps.build_zone(levels)
    assert z["high"] == 105.0


def test_build_zone_empty():
    assert ps.build_zone([]) is None


def test_retest_state_first_touch_after_reclaim():
    zone = {"low": 99, "high": 100}
    rows = [(98, 99, 97, 98, 1), (98, 99.5, 97, 99, 1), (99, 102, 99, 101.5, 1),  # reclaim
            (101.5, 104, 101, 103, 1), (103, 104, 100.1, 100.5, 1)]               # first touch today
    out = ps.retest_state(_bars_from(rows), zone)
    assert out["bars_since_reclaim"] == 2
    assert out["tests_since"] == 0
    assert out["first_retest"] is True


def test_retest_state_second_touch_is_not_first():
    zone = {"low": 99, "high": 100}
    rows = [(98, 99, 97, 98, 1), (99, 102, 99, 101.5, 1),
            (101.5, 102, 100.1, 101, 1),   # touched already
            (101, 103, 101, 102.5, 1), (102.5, 103, 100.2, 100.6, 1)]
    assert ps.retest_state(_bars_from(rows), zone)["first_retest"] is False


@pytest.mark.parametrize("close,low,expected", [
    (101.0, 100.5, "near_support"), (110.0, 108.0, "extended"), (97.0, 96.5, "broken"),
])
def test_zone_status(close, low, expected):
    bars = _bars_from([(close, close + 1, low, close, 1)])
    assert ps.zone_status(bars, {"low": 99, "high": 100}) == expected


# ------------------------------------------- step 3/4: size, R, manage ----

def test_position_size_matches_method_example():
    out = ps.position_size(10_000, 20.0, 19.5, risk_pct=0.5)
    assert out["shares"] == 100
    assert out["notional"] == 2000
    assert out["pct_of_account"] == 20
    assert out["planned_loss"] == 50


def test_position_size_caps_at_account_and_board_lots():
    # tiny stop distance -> risk formula wants more than the account can buy
    out = ps.position_size(100_000, 50.0, 49.95, risk_pct=0.5)
    assert out["shares"] == 2000 and out["pct_of_account"] == 100
    lots = ps.position_size(1_000_000, 20.0, 19.5, risk_pct=0.5, odd_lot=False)
    assert lots["shares"] == 10_000 and lots["lots"] == 10
    assert ps.position_size(10_000, 20, 21) is None


def test_r_targets_match_method_example_and_net_r_is_lower():
    out = ps.r_targets(20.0, 19.5)
    assert out["r"] == pytest.approx(0.5)
    assert [(t["r_mult"], t["price"]) for t in out["targets"]] == [(3, 21.5), (5, 22.5)]
    assert all(t["net_r"] < t["r_mult"] for t in out["targets"])
    assert ps.r_targets(20, 20) is None


def test_management_signal_stages():
    assert "停損" in ps.management_signal(20, 19.5, 19.4, ema9=20)["actions"][0]
    at3 = ps.management_signal(20, 19.5, 21.6, ema9=21.0)
    assert any("3R" in a for a in at3["actions"])
    after = ps.management_signal(20, 19.5, 21.0, ema9=21.2, partials_taken=1)
    assert any("跌破 EMA9" in a for a in after["actions"])
    spike = ps.management_signal(20, 19.5, 26.0, ema9=22.0, atr=0.5, partials_taken=2)
    assert any("遠離" in a for a in spike["actions"])


# ---------------------------------------------- intraday minute trigger ----

def _mb(rows):
    return [{"t": i * 300, "o": o, "h": h, "l": l, "c": c} for i, (o, h, l, c) in enumerate(rows)]


def test_intraday_trigger_waiting_until_price_reaches_zone():
    assert ps.intraday_trigger(_mb([(21, 21.2, 20.8, 21)]), 19.6, 20.0)["state"] == "waiting"
    assert ps.intraday_trigger([], 19.6, 20.0)["state"] == "waiting"


def test_intraday_trigger_dip_then_break_of_previous_high():
    # opens high, dips into 19.6-20.0, base bar high 19.95, next bar takes it out
    bars = _mb([(20.6, 20.7, 20.3, 20.35), (20.35, 20.4, 19.9, 19.95),
                (19.95, 19.95, 19.7, 19.8), (19.8, 20.2, 19.8, 20.15)])
    out = ps.intraday_trigger(bars, 19.6, 20.0)
    assert out["state"] == "triggered"
    assert out["trigger_price"] == pytest.approx(20.0)        # 19.95 + one 0.05 tick
    assert out["stop"] == pytest.approx(19.65)                # min(19.7, 19.8) - tick
    assert out["stop_ok"] is True
    assert out["day_low_stop"] == pytest.approx(19.65)


def test_intraday_trigger_dipped_without_break_yet():
    bars = _mb([(20.6, 20.7, 20.3, 20.35), (20.35, 20.4, 19.9, 19.95), (19.95, 19.95, 19.7, 19.75)])
    out = ps.intraday_trigger(bars, 19.6, 20.0)
    assert out["state"] == "dipped"
    assert out["next_trigger"] == pytest.approx(20.0)


def test_intraday_trigger_broken_when_sliding_through():
    bars = _mb([(20.3, 20.3, 19.9, 19.95), (19.95, 19.95, 19.2, 19.25)])
    assert ps.intraday_trigger(bars, 19.6, 20.0)["state"] == "broken"


def test_ticks_to_bars_buckets_by_minutes():
    bars = ps.ticks_to_bars([10, 11, 9, 10.5, 12], [0, 60, 120, 300, 360], minutes=5)
    assert bars == [{"t": 0, "o": 10, "h": 11, "l": 9, "c": 9},
                    {"t": 300, "o": 10.5, "h": 12, "l": 10.5, "c": 12}]


def test_format_trigger_message_includes_size_only_with_account():
    setup = {"zone": {"low": 19.6, "high": 20.0}}
    trig = {"trigger_price": 20.0, "stop": 19.5, "stop_pct": 2.5, "stop_ok": True}
    no_acct = ps.format_trigger_message("2330", "台積電", setup, trig)
    assert "停損 19.5" in no_acct and "股（" not in no_acct
    with_acct = ps.format_trigger_message("2330", "台積電", setup, trig, account=10_000)
    assert "100 股" in with_acct


def test_setup_sort_key_orders_by_actionability():
    a = {"status": "extended", "zone": {"score": 4}, "plan": {"stop_ok": True}, "strength": 0.99}
    b = {"status": "near_support", "retest": {"first_retest": False}, "zone": {"score": 2},
         "plan": {"stop_ok": True}, "strength": 0.7}
    c = {"status": "near_support", "retest": {"first_retest": True}, "zone": {"score": 2},
         "plan": {"stop_ok": True}, "strength": 0.7}
    assert sorted([a, b, c], key=ps.setup_sort_key) == [c, b, a]
