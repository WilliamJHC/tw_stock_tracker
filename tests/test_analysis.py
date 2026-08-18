# -*- coding: utf-8 -*-
"""Unit tests with hand-computed fixtures (no network)."""


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
    # 2026-07-20 short/mid-term additions are present on every analyze() call
    assert a["atr"] is not None and a["atr"] > 0
    assert isinstance(a["gaps"], list)
    assert a["donchian"] is not None
    assert a["rs_rank"] is None                  # no rs_info passed in -> not available


def test_analyze_short_history():
    daily = {"t": [1, 2, 3], "o": [1, 1, 1], "h": [1, 1, 1],
             "l": [1, 1, 1], "c": [1, 1, 1], "v": [0, 0, 0]}
    assert an.analyze(daily) is None


# ------------------------------------------------- SCORE_WEIGHTS sanity ----

def test_score_weights_sum_to_100():
    assert sum(an.SCORE_WEIGHTS.values()) == 100


# --------------------------------------------------------------- ATR ----

def test_atr_hand_computed():
    highs = [10, 12, 11, 13, 14]
    lows = [8, 9, 9, 10, 11]
    closes = [9, 11, 10, 12, 13]
    # TR1=max(3,3,0)=3 TR2=max(2,0,2)=2 TR3=max(3,3,0)=3 TR4=max(3,2,1)=3
    # first ATR = mean(3,2,3) = 8/3; then Wilder smooth with TR4=3:
    # atr = (8/3 * 2 + 3) / 3 = 25/9
    result = an.atr(highs, lows, closes, period=3)
    assert abs(result - 25 / 9) < 1e-9


def test_atr_insufficient_history():
    assert an.atr([1, 2], [1, 2], [1, 2], period=14) is None


# --------------------------------------------------------- big candle ----

def test_big_candle_signal_bull():
    assert an.big_candle_signal([100], [104], [300], [100], 0) == "bull"


def test_big_candle_signal_bear():
    assert an.big_candle_signal([100], [95], [300], [100], 0) == "bear"


def test_big_candle_signal_none_when_volume_not_big_enough():
    assert an.big_candle_signal([100], [104], [140], [100], 0) is None


def test_big_candle_signal_none_when_body_too_small():
    assert an.big_candle_signal([100], [101], [300], [100], 0) is None


# ---------------------------------------------------------- gap tracker ----

def test_gap_tracker_detects_and_marks_filled():
    opens = [100, 100, 103, 104]
    highs = [100, 100, 105, 104]
    lows = [100, 100, 102, 99]
    closes = [100, 100, 104, 101]
    gaps = an.gap_tracker(opens, highs, lows, closes, lookback=20)
    assert len(gaps) == 1
    g = gaps[0]
    assert g["date_idx"] == 2 and g["direction"] == "up"
    assert abs(g["gap_pct"] - 3.0) < 1e-9
    assert g["filled"] is True   # day3's low (99) trades back below prev close (100)


def test_gap_tracker_unfilled_gap():
    opens = [100, 100, 103]
    highs = [100, 100, 105]
    lows = [100, 100, 102]
    closes = [100, 100, 104]
    gaps = an.gap_tracker(opens, highs, lows, closes, lookback=20)
    assert len(gaps) == 1
    assert gaps[0]["filled"] is False


def test_gap_tracker_below_threshold_not_recorded():
    # 0.5% gap, default threshold 2% -> not recorded
    opens = [100, 100.5]
    highs = [100, 101]
    lows = [100, 100]
    closes = [100, 100.8]
    assert an.gap_tracker(opens, highs, lows, closes) == []


# ----------------------------------------------------------- rs_ranking ----

def test_rs_ranking_hand_computed():
    ranks = an.rs_ranking({"A": 5.0, "B": -2.0, "C": 10.0})
    assert ranks["C"] == {"rank": 1, "percentile": 100.0, "return_pct": 10.0}
    assert ranks["A"] == {"rank": 2, "percentile": 50.0, "return_pct": 5.0}
    assert ranks["B"] == {"rank": 3, "percentile": 0.0, "return_pct": -2.0}


def test_rs_ranking_single_code():
    assert an.rs_ranking({"A": 5.0}) == {"A": {"rank": 1, "percentile": 100.0, "return_pct": 5.0}}


def test_rs_ranking_empty():
    assert an.rs_ranking({}) == {}


# ------------------------------------------------------ bollinger squeeze --

def test_bollinger_squeeze_hand_computed():
    closes = [100, 105, 100, 101, 100.5]
    sq = an.bollinger_squeeze(closes, period=2, std_mult=2, lookback=120, pct_threshold=25)
    assert abs(sq["bandwidth"] - 0.014037) < 1e-3
    assert abs(sq["percentile"] - 25.0) < 1e-9
    assert sq["is_squeeze"] is True


def test_bollinger_squeeze_insufficient_history():
    assert an.bollinger_squeeze([100], period=20) is None


def test_bollinger_squeeze_bounded_scan_matches_full_scan():
    """Bounding the internal loop to a trailing window (Phase 2a's second
    O(n^2) fix, see the function docstring) must not change the result --
    verify against a naive full-history-from-bar-0 reference implementation."""
    import random
    rng = random.Random(17)
    closes = [80.0]
    for _ in range(600):
        closes.append(closes[-1] * (1 + rng.uniform(-0.03, 0.032)))

    def full_scan(closes, period=20, std_mult=2, lookback=120, pct_threshold=20):
        n = len(closes)
        widths = []
        for i in range(period - 1, n):
            seg = closes[i - period + 1:i + 1]
            m = sum(seg) / period
            sd = an.stdev(seg)
            widths.append(None if (not sd or not m) else (2 * std_mult * sd) / m)
        valid = [w for w in widths if w is not None]
        current = widths[-1]
        window = valid[-lookback:]
        percentile = sum(1 for w in window if w <= current) / len(window) * 100
        return {"bandwidth": current, "percentile": percentile, "is_squeeze": percentile <= pct_threshold}

    for n in (25, 50, 140, 300, 601):
        expected = full_scan(closes[:n])
        actual = an.bollinger_squeeze(closes[:n])
        assert actual["bandwidth"] == expected["bandwidth"]
        assert actual["percentile"] == expected["percentile"]
        assert actual["is_squeeze"] == expected["is_squeeze"]


def test_bollinger_squeeze_bounded_scan_matches_full_scan_with_dense_none_gaps():
    """A long flat (zero-stdev) segment produces far more than `period` None
    width readings in a row -- more than the trailing window's slack can
    absorb -- so the bounded scan must widen its window until it finds
    `lookback` valid readings, same as a full scan from bar 0 would."""
    import random
    rng = random.Random(29)
    closes = [80.0]
    for _ in range(200):
        closes.append(closes[-1] * (1 + rng.uniform(-0.03, 0.032)))
    flat_price = closes[-1]
    closes += [flat_price] * 150  # a 150-bar flat run -> ~130 consecutive None widths
    for _ in range(50):
        closes.append(closes[-1] * (1 + rng.uniform(-0.03, 0.032)))

    def full_scan(closes, period=20, std_mult=2, lookback=120, pct_threshold=20):
        n = len(closes)
        widths = []
        for i in range(period - 1, n):
            seg = closes[i - period + 1:i + 1]
            m = sum(seg) / period
            sd = an.stdev(seg)
            widths.append(None if (not sd or not m) else (2 * std_mult * sd) / m)
        valid = [w for w in widths if w is not None]
        current = widths[-1]
        window = valid[-lookback:]
        percentile = sum(1 for w in window if w <= current) / len(window) * 100
        return {"bandwidth": current, "percentile": percentile, "is_squeeze": percentile <= pct_threshold}

    expected = full_scan(closes)
    actual = an.bollinger_squeeze(closes)
    assert actual["bandwidth"] == expected["bandwidth"]
    assert actual["percentile"] == expected["percentile"]
    assert actual["is_squeeze"] == expected["is_squeeze"]


# ------------------------------------------------------- donchian position -

def test_donchian_position_hand_computed():
    highs = [20, 12, 9, 15, 11]
    lows = [1, 6, 4, 7, 6]
    closes = [8, 9, 10, 11, 10]
    d = an.donchian_position(highs, lows, closes, period_short=3, period_long=5)
    assert abs(d["short"] - 54.545454545) < 1e-6   # (10-4)/(15-4)*100
    assert abs(d["long"] - 47.368421053) < 1e-6     # (10-1)/(20-1)*100


def test_donchian_position_insufficient_history_returns_none():
    assert an.donchian_position([1, 2], [1, 2], [1, 2], period_short=5, period_long=10) is None


def test_donchian_position_partial_history_short_only():
    highs = [20, 12, 9]
    lows = [1, 6, 4]
    closes = [8, 9, 10]
    d = an.donchian_position(highs, lows, closes, period_short=3, period_long=10)
    assert d["short"] is not None
    assert d["long"] is None


# ---------------------------------------------------------- ma20 deviation -

def test_ma20_deviation():
    assert an.ma20_deviation(110, 100) == 10.0
    assert an.ma20_deviation(95, 100) == -5.0
    assert an.ma20_deviation(100, None) is None
    assert an.ma20_deviation(100, 0) is None


# ------------------------------------------------------ analyze() + rs_info -

def test_analyze_with_rs_info_included_in_output_and_score():
    import random
    rng = random.Random(7)
    n = 200
    closes, highs, lows, vols, times = [], [], [], [], []
    p = 100.0
    for i in range(n):
        p *= 1 + rng.uniform(-0.02, 0.021)
        closes.append(p)
        highs.append(p * 1.01)
        lows.append(p * 0.99)
        vols.append(rng.randint(1000, 5000) * 1000)
        times.append(1_700_000_000 + i * 86400)
    daily = {"t": times, "o": closes[:], "h": highs, "l": lows, "c": closes, "v": vols}
    rs_info = {"rank": 1, "percentile": 95.0, "return_pct": 8.0}
    a = an.analyze(daily, rs_info=rs_info)
    assert a["rs_rank"] == rs_info
    # a leading (percentile>=80) rs_info must produce a bullish rs signal
    assert any("相對強弱排名領先" in s["text"] for s in a["signals"])


# ---------------------------------- analyze() ctx/at (Phase 2a golden master) -

def _random_daily(n=200, seed=7, p0=80.0):
    import random
    rng = random.Random(seed)
    closes, highs, lows, vols, times = [], [], [], [], []
    p = p0
    for i in range(n):
        p *= 1 + rng.uniform(-0.03, 0.032)
        closes.append(p)
        highs.append(p * 1.01)
        lows.append(p * 0.99)
        vols.append(rng.randint(1000, 5000) * 1000)
        times.append(1_700_000_000 + i * 86400)
    return {"t": times, "o": closes[:], "h": highs, "l": lows, "c": closes, "v": vols}


def test_analyze_ctx_at_bit_identical_to_full_rebuild():
    """docs/https-finlab-finance-blog-python-get-tai-memoized-lighthouse.md
    Phase 2a: build_backtest_ctx() built once over full history + at=i must
    give bit-identical analyze() output to the old per-bar rebuild (ctx
    rebuilt fresh from a [:i+1] prefix each time)."""
    full_daily = _random_daily()
    ctx = an.build_backtest_ctx(full_daily)

    for i in (10, 50, 119, 120, 199):
        sliced = {k: full_daily[k][:i + 1] for k in full_daily}
        old = an.analyze(sliced, with_backtest=False)          # old path: rebuilds ctx from the prefix
        new = an.analyze(sliced, with_backtest=False, ctx=ctx, at=i)  # new path: precomputed ctx, indexed at i
        if old is None:
            assert new is None
            continue
        assert new is not None
        assert new["score"] == old["score"]
        assert new["label"] == old["label"]
        assert new["components"] == old["components"]
        assert new["signals"] == old["signals"]
        assert new["ma5"][i] == old["ma5"][i]
        assert new["ma20"][i] == old["ma20"][i]
        assert new["ma60"][i] == old["ma60"][i]
        assert new["quadrant_last"] == old["quadrant_last"]


# ------------------------------ analyze() components (Phase 2b identity) -

def test_components_weighted_sum_equals_score():
    daily = _random_daily(seed=3)
    a = an.analyze(daily)
    weighted = sum(a["components"][k] * an.SCORE_WEIGHTS[k] for k in an.SCORE_WEIGHTS)
    assert a["score"] == round(max(-100.0, min(100.0, weighted)))


def test_components_weighted_sum_equals_score_with_of_divergence():
    # exercise the orderFlow component's folded-in divergence bonus (the one
    # component whose raw value isn't a plain pre-weight ratio/bias).
    m_closes = [100.0 - i * 0.1 for i in range(10)]
    m = {"t": [1_750_000_000 + i * 300 for i in range(10)],
         "c": m_closes, "h": [c * 1.001 for c in m_closes], "l": [c * 0.999 for c in m_closes],
         "v": [100] * 10}
    daily = _random_daily(seed=11)
    a = an.analyze(daily, minute=m)
    assert a["of"] is not None
    weighted = sum(a["components"][k] * an.SCORE_WEIGHTS[k] for k in an.SCORE_WEIGHTS)
    assert a["score"] == round(max(-100.0, min(100.0, weighted)))


def test_components_keys_match_score_weights():
    daily = _random_daily(seed=5)
    a = an.analyze(daily)
    assert set(a["components"]) == set(an.SCORE_WEIGHTS)
