# -*- coding: utf-8 -*-
"""Unit tests with hand-computed fixtures (no network)."""
import datetime as dt
import math


import intraday as it


# --------------------------------------------------------------- vwap ----

def test_vwap_bands_hand_computed():
    # vwap = (10*100+11*100+12*100)/300 = 11
    # weighted var = (100*1 + 100*0 + 100*1) / 300 = 2/3 -> sigma = sqrt(2/3)
    r = it.vwap_bands([10, 11, 12], [100, 100, 100])
    sigma = math.sqrt(2 / 3)
    assert abs(r["vwap"] - 11) < 1e-9
    assert abs(r["upper1"] - (11 + sigma)) < 1e-9
    assert abs(r["lower1"] - (11 - sigma)) < 1e-9
    assert abs(r["upper2"] - (11 + 2 * sigma)) < 1e-9
    assert abs(r["lower2"] - (11 - 2 * sigma)) < 1e-9
    assert r["sample_count"] == 3


def test_vwap_bands_empty():
    r = it.vwap_bands([], [])
    assert r == {"vwap": None, "upper1": None, "lower1": None,
                 "upper2": None, "lower2": None, "sample_count": 0}


def test_vwap_bands_single_point_zero_sigma():
    r = it.vwap_bands([50], [10])
    assert r["vwap"] == 50
    assert r["upper1"] == 50 and r["lower1"] == 50
    assert r["sample_count"] == 1


# ------------------------------------------------------- opening range ----

def test_opening_range_breakout_up():
    session_open = dt.datetime(2026, 7, 20, 9, 0)
    ts = [dt.datetime(2026, 7, 20, 9, 0), dt.datetime(2026, 7, 20, 9, 5),
          dt.datetime(2026, 7, 20, 9, 10), dt.datetime(2026, 7, 20, 9, 20)]
    prices = [100, 105, 98, 110]
    r = it.opening_range(prices, ts, session_open, window_minutes=15)
    assert r == {"high": 105, "low": 98, "breakout": "up", "window_minutes": 15}


def test_opening_range_no_breakout():
    session_open = dt.datetime(2026, 7, 20, 9, 0)
    ts = [dt.datetime(2026, 7, 20, 9, 0), dt.datetime(2026, 7, 20, 9, 5),
          dt.datetime(2026, 7, 20, 9, 20)]
    prices = [100, 105, 102]  # last price 102 is inside [100,105] window range
    r = it.opening_range(prices, ts, session_open, window_minutes=15)
    assert r["high"] == 105 and r["low"] == 100 and r["breakout"] is None


def test_opening_range_empty():
    r = it.opening_range([], [], dt.datetime(2026, 7, 20, 9, 0))
    assert r == {"high": None, "low": None, "breakout": None, "window_minutes": 15}


# ------------------------------------------------------ volume profile ----

def test_intraday_vp_delegates_to_volume_profile():
    # intraday_vp() must produce the exact same result as calling
    # analysis.volume_profile() directly with lookback=len(vols) -- proves
    # it delegates rather than reimplementing the binning logic, and that
    # a short series isn't truncated by the default lookback=120.
    from analysis import volume_profile
    highs, lows, vols, price = [110, 105], [100, 100], [1000, 500], 104
    r = it.intraday_vp(highs=highs, lows=lows, vols=vols, price=price)
    expected = volume_profile(highs, lows, vols, price, lookback=len(vols))
    assert r == expected
    assert r["bar_count"] == 2


# ------------------------------------------------------------- CVD ----

def test_realtime_cvd_tick_rule():
    # same values as test_analysis.test_of_from_minutes_tick_rule
    closes = [10.0, 11.0, 11.0, 10.5]
    vols = [5, 100, 200, 50]
    r = it.realtime_cvd(closes, vols, method="tick_rule")
    assert r["is_estimated"] is True
    assert r["cum"] == [0, 100, 300, 250]
    assert r["delta_total"] == 250


def test_realtime_cvd_true_side():
    sides = ["buy", "sell", "buy"]
    vols = [10, 5, 7]
    r = it.realtime_cvd(closes=[0, 0, 0], vols=vols, method="true_side", sides=sides)
    assert r["is_estimated"] is False
    assert r["cum"] == [10, 5, 12]
    assert r["delta_total"] == 12


def test_realtime_cvd_empty():
    r = it.realtime_cvd([], [], method="tick_rule")
    assert r == {"cum": [], "delta_total": 0.0, "is_estimated": True}


# -------------------------------------------------------- depth/book ----

def test_depth_imbalance_bid_heavy():
    r = it.depth_imbalance(bid_prices=[100, 99, 98], bid_vols=[10, 10, 10],
                            ask_prices=[101, 102, 103], ask_vols=[5, 5, 5])
    assert r["bid_total"] == 30 and r["ask_total"] == 15
    assert abs(r["imbalance_ratio"] - (15 / 45)) < 1e-9
    assert r["imbalance_side"] == "bid_heavy"
    assert r["walls"] == []


def test_depth_imbalance_wall_detection():
    r = it.depth_imbalance(bid_prices=[100, 99, 98], bid_vols=[50, 10, 10],
                            ask_prices=[101, 102, 103], ask_vols=[5, 5, 5])
    assert {"side": "bid", "price": 100, "vol": 50} in r["walls"]


def test_depth_imbalance_pulled_orders():
    r = it.depth_imbalance(bid_prices=[100, 99, 98], bid_vols=[50, 10, 2],
                            ask_prices=[101, 102, 103], ask_vols=[5, 5, 5],
                            prev_bid_vols=[50, 10, 10])
    assert {"price": 98, "prev_vol": 10, "vol": 2} in r["pulled_orders"]


def test_depth_imbalance_empty():
    r = it.depth_imbalance([], [], [], [])
    assert r["bid_total"] == 0 and r["ask_total"] == 0
    assert r["imbalance_ratio"] == 0.0
    assert r["imbalance_side"] == "balanced"
    assert r["walls"] == []


# ------------------------------------------------------ relative strength ----

def test_relative_strength_hand_computed():
    r = it.relative_strength([100, 110], [100, 105])
    assert abs(r["target_return_pct"] - 10.0) < 1e-9
    assert abs(r["benchmark_return_pct"] - 5.0) < 1e-9
    assert abs(r["rs_diff"] - 5.0) < 1e-9
    assert abs(r["rs_ratio"] - (1.10 / 1.05)) < 1e-9


def test_relative_strength_insufficient_data():
    r = it.relative_strength([100], [100, 105])
    assert r["target_return_pct"] is None and r["rs_ratio"] is None


# ------------------------------------------------------- volume anomaly ----

def test_volume_anomaly_normal():
    current = [100, 200, 150]  # total 450
    hist = [[80, 150, 120], [90, 160, 130]]  # totals 350, 380 -> avg 365
    r = it.volume_anomaly(current, hist)
    assert r["current_total"] == 450
    assert abs(r["baseline_avg"] - 365) < 1e-9
    assert abs(r["ratio"] - (450 / 365)) < 1e-9
    assert r["is_anomaly"] is False


def test_volume_anomaly_flagged():
    current = {"09:00": 500, "09:05": 500}  # total 1000
    hist = [{"09:00": 200, "09:05": 200}]  # avg 400
    r = it.volume_anomaly(current, hist)
    assert r["ratio"] == 2.5
    assert r["is_anomaly"] is True


def test_volume_anomaly_no_history():
    r = it.volume_anomaly([100, 200], [])
    assert r["current_total"] == 300
    assert r["baseline_avg"] is None
    assert r["is_anomaly"] is False
