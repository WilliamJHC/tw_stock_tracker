# -*- coding: utf-8 -*-
"""Unit tests with hand-computed fixtures (no network)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import analysis as an


def test_ma_series():
    assert an.ma_series([1, 2, 3, 4, 5], 3) == [None, None, 2, 3, 4]


def test_linreg_slope():
    assert abs(an.linreg_slope([1, 3, 5]) - 2) < 1e-12


def test_volume_profile_hand_computed():
    # bar1: 100-110 x 1000 shares -> 100 per 1-wide bin (10 bins)
    # bar2: 100-105 x 500 shares  -> 100 per bin in bins 0-4
    # bins 0-4 = 200, bins 5-9 = 100, total 1500
    vp = an.volume_profile(highs=[110, 105], lows=[100, 100], vols=[1000, 500],
                           price=104, lookback=10, n_bins=10)
    assert abs(vp["poc"] - 100.5) < 1e-9          # first max bin (0) center
    assert abs(vp["val"] - 100.0) < 1e-9
    assert abs(vp["vah"] - 106.0) < 1e-9          # expands 0..5 -> acc 1100 >= 1050
    assert abs(sum(b["v"] for b in vp["bins"]) - 1500) < 1e-6
    assert abs(vp["bins"][0]["v"] - 200) < 1e-9
    assert abs(vp["bins"][9]["v"] - 100) < 1e-9


def test_volume_profile_single_price_bar():
    vp = an.volume_profile(highs=[100, 100], lows=[100, 100], vols=[10, 20],
                           price=100, n_bins=5)
    assert vp is None  # zero price range -> no profile


def test_of_from_minutes_tick_rule():
    base = 1_750_000_000  # some UTC ts; all bars same Taipei date
    m = {"t": [base, base + 300, base + 600, base + 900],
         "c": [10.0, 11.0, 11.0, 10.5],
         "v": [5, 100, 200, 50]}
    days = an.of_from_minutes(m)
    assert len(days) == 1
    # bar0 no sign (skipped), bar1 up +100, bar2 flat carries +200, bar3 down -50
    assert days[0]["delta"] == 100 + 200 - 50
    assert days[0]["volume"] == 350


def test_of_from_ticks():
    rows = [{"volume": 10, "TickType": 2}, {"volume": 4, "TickType": 1},
            {"volume": 3, "TickType": 0}]
    out = an.of_from_ticks({"2026-07-07": rows})
    assert out == [{"date": "2026-07-07", "delta": 6, "volume": 17}]


def test_of_summary_streak_and_ratio():
    days = [{"date": f"d{i}", "delta": d, "volume": 100}
            for i, d in enumerate([50, 50, -30, 40, 20])]
    s = an.of_summary(days)
    assert s["streak"] == 2                      # last two positive days
    assert s["delta5"] == 50 + 50 - 30 + 40 + 20
    assert abs(s["ratio5"] - 130 / 500) < 1e-12
    neg = [{"date": f"d{i}", "delta": -1, "volume": 10} for i in range(3)]
    assert an.of_summary(neg)["streak"] == -3


def test_of_divergence_bearish():
    # price rises >2% but aggressive selling dominates -> bearish divergence
    days = [{"date": f"2026-07-{i:02d}", "delta": -10, "volume": 100} for i in range(1, 11)]
    closes = {f"2026-07-{i:02d}": 100 + i for i in range(1, 11)}
    s = an.of_summary(days, closes)
    assert s["divergence"] == "bearish"


def test_backtest_gap_sampling():
    ctx = {"closes": list(range(100, 130))}      # 30 rising closes
    always = lambda c, i: True
    r = an.backtest_signal(ctx, always, horizon=5, gap=10)
    # samples at i=0, 10, 20 -> n=3, all wins (rising series)
    assert r["n"] == 3 and r["wins"] == 3 and r["win_rate"] == 100


def test_backtest_no_hits():
    ctx = {"closes": list(range(100, 130))}
    never = lambda c, i: False
    assert an.backtest_signal(ctx, never) == {"n": 0}


def test_quadrant():
    closes = [100.0] * 25
    vols = [1000] * 25
    closes[24] = 105          # price up vs closes[21]
    vols[22] = vols[23] = vols[24] = 3000   # recent volume above vol20
    vol20 = an.ma_series(vols, 20)
    assert an.quadrant_at(closes, vols, vol20, 24) == "PU_VU"
    assert an.quadrant_at(closes, vols, vol20, 10) is None  # i < 20


def test_analyze_smoke_and_bounds():
    import random
    rng = random.Random(42)
    n = 200
    closes, highs, lows, vols, times = [], [], [], [], []
    p = 100.0
    for i in range(n):
        p *= 1 + rng.uniform(-0.03, 0.032)
        closes.append(p)
        highs.append(p * 1.01)
        lows.append(p * 0.99)
        vols.append(rng.randint(1000, 5000) * 1000)
        times.append(1_700_000_000 + i * 86400)
    daily = {"t": times, "o": closes[:], "h": highs, "l": lows, "c": closes, "v": vols}
    a = an.analyze(daily)
    assert a is not None
    assert -100 <= a["score"] <= 100
    assert a["vp"] is not None and a["vp"]["val"] < a["vp"]["vah"]
    assert a["of"] is None                       # no minute data -> no OF
    assert a["label"] in ("強多", "偏多", "中性", "偏空", "強空")


def test_analyze_short_history():
    daily = {"t": [1, 2, 3], "o": [1, 1, 1], "h": [1, 1, 1],
             "l": [1, 1, 1], "c": [1, 1, 1], "v": [0, 0, 0]}
    assert an.analyze(daily) is None
