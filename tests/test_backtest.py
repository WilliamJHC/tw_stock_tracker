# -*- coding: utf-8 -*-
"""Unit tests for backtest.py (Phase 3/4/5 of docs/backtest_plan.md), all on
hand-computed/scripted fixtures -- no network, no DB."""
import datetime as dt
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import analysis as an
import backtest as bt


def _bars(opens, dates=None):
    """[o0, o1, ...] -> [{date, o, h, l, c, v}, ...] with c == o (only entry/exit
    price and stop-loss checks read o/c in these tests; h/l are padded)."""
    dates = dates or [f"d{i}" for i in range(len(opens))]
    return [{"date": d, "o": o, "h": o * 1.01, "l": o * 0.99, "c": o, "v": 1000}
            for d, o in zip(dates, opens)]


# ------------------------------------------------------------ signal_win_rates --

def test_signal_win_rates_hand_computed_monotonic_rise():
    # 100 bars, +1%/day: after the 60-bar MA warmup there's room for several
    # horizon=5 samples where ma5>ma20>ma60 holds on every remaining bar.
    n = 100
    closes = [100.0]
    for _ in range(n - 1):
        closes.append(closes[-1] * 1.01)
    daily = {"c": closes, "v": [1000] * n, "o": closes[:]}

    rows = bt.signal_win_rates(daily, horizons=(5, 10))
    assert len(rows) == len(an.BACKTEST_SIGNALS) * 2
    by_key = {(r["signal"], r["horizon"]): r for r in rows}

    bull_align = by_key[("均線多頭排列", 5)]
    # ma60 valid for i in [59, 99]; backtest_signal() loops i in range(n-horizon)=range(95),
    # so qualifying i in [59, 94] (36 bars), gap=5 sampling keeps i=59,64,...,94 -> 8 samples
    expected_n = len(range(59, 95, 5))
    assert bull_align["n"] == expected_n
    assert bull_align["win_rate"] == 100.0  # monotonic riser -> every forward return is positive
    assert abs(bull_align["avg_ret"] - (1.01 ** 5 - 1) * 100) < 1e-9

    bear_align = by_key[("均線空頭排列", 5)]
    assert bear_align["n"] == 0
    assert bear_align["win_rate"] is None and bear_align["avg_ret"] is None

    # no duplicated logic vs. calling an.backtest_signal() directly
    ctx = an.build_backtest_ctx(daily)
    direct = an.backtest_signal(ctx, an.BACKTEST_SIGNALS["均線多頭排列"], horizon=5, gap=5)
    assert direct["n"] == bull_align["n"]
    assert direct["win_rate"] == bull_align["win_rate"]
    assert direct["avg_ret"] == bull_align["avg_ret"]


# -------------------------------------------------------------- replay_scores --

def test_replay_scores_never_leaks_same_day_chip_data():
    """Protection #2: chip data for bar i's own date must never reach
    an.analyze() -- only data up to the PREVIOUS bar's date."""
    start = dt.date(2024, 1, 1)
    dates = [(start + dt.timedelta(days=i)).isoformat() for i in range(130)]
    bars = []
    price = 100.0
    for d in dates:
        price *= 1.001
        bars.append({"date": d, "o": price, "h": price * 1.01, "l": price * 0.99, "c": price, "v": 1000})
    inst = [{"date": d, "foreign": 10, "trust": 5} for d in dates]

    captured = []
    orig_analyze = an.analyze

    def spy(daily, **kw):
        captured.append(kw.get("inst"))
        return orig_analyze(daily, **kw)

    with patch("backtest.an.analyze", side_effect=spy):
        rows = bt.replay_scores(bars, inst, [], [], [], warmup=120)

    result_dates = [r["date"] for r in rows]
    assert len(captured) == len(result_dates) == 10  # bars 120..129
    for call_inst, row_date in zip(captured, result_dates):
        assert call_inst  # inst data should be present (just not today's)
        assert max(r["date"] for r in call_inst) < row_date


def test_replay_scores_daily_slice_never_sees_future_bars():
    """Protection #1: an.analyze() must only ever see bars[:i+1]."""
    start = dt.date(2024, 1, 1)
    dates = [(start + dt.timedelta(days=i)).isoformat() for i in range(130)]
    bars = []
    price = 100.0
    for d in dates:
        price *= 1.001
        bars.append({"date": d, "o": price, "h": price * 1.01, "l": price * 0.99, "c": price, "v": 1000})

    captured = []
    orig_analyze = an.analyze

    def spy(daily, **kw):
        captured.append(daily["c"][-1])
        return orig_analyze(daily, **kw)

    with patch("backtest.an.analyze", side_effect=spy):
        bt.replay_scores(bars, [], [], [], [], warmup=120)

    for i, seen_close in enumerate(captured):
        assert abs(seen_close - bars[120 + i]["c"]) < 1e-9


def test_replay_scores_disables_of_and_per_bar_backtest_recompute():
    """OF can't be reconstructed 2y back (hard limit) and with_backtest=False
    avoids an O(n^2) rescan on every replayed bar."""
    start = dt.date(2024, 1, 1)
    dates = [(start + dt.timedelta(days=i)).isoformat() for i in range(125)]
    bars = [{"date": d, "o": 100 + i, "h": 101 + i, "l": 99 + i, "c": 100 + i, "v": 1000}
            for i, d in enumerate(dates)]

    captured = []
    orig_analyze = an.analyze

    def spy(daily, **kw):
        captured.append((kw.get("minute"), kw.get("tick_days"), kw.get("with_backtest")))
        return orig_analyze(daily, **kw)

    with patch("backtest.an.analyze", side_effect=spy):
        rows = bt.replay_scores(bars, [], [], [], [], warmup=120)

    assert captured  # at least one bar got replayed
    assert all(minute is None and tick_days is None and wb is False for minute, tick_days, wb in captured)
    assert all(r["date"] for r in rows)


# ------------------------- replay_scores Phase 2a/2b (ctx/at + components) --

def _random_bars(n=200, seed=13, p0=60.0):
    import random
    rng = random.Random(seed)
    start = dt.date(2024, 1, 1)
    bars = []
    p = p0
    for i in range(n):
        p *= 1 + rng.uniform(-0.03, 0.032)
        d = (start + dt.timedelta(days=i)).isoformat()
        bars.append({"date": d, "o": p, "h": p * 1.01, "l": p * 0.99, "c": p,
                     "v": rng.randint(1000, 5000) * 1000})
    return bars


def _replay_scores_old_per_bar_rebuild(bars, inst, margin, shareholding, sbl, warmup=120):
    """Pre-Phase-2a replay_scores(): rebuilds build_backtest_ctx() from an
    ever-growing prefix on every bar instead of once up front. Kept here
    only as a golden-master reference for the O(n^2)->O(n) refactor -- not
    the production code path."""
    dates = [b["date"] for b in bars]
    times = [bt._epoch(d) for d in dates]
    opens = [b["o"] for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    closes = [b["c"] for b in bars]
    vols = [b["v"] for b in bars]
    rows = []
    for i in range(warmup, len(bars)):
        date = dates[i]
        daily_slice = {"t": times[:i + 1], "o": opens[:i + 1], "h": highs[:i + 1],
                       "l": lows[:i + 1], "c": closes[:i + 1], "v": vols[:i + 1]}
        cutoff = dates[i - 1]
        result = an.analyze(
            daily_slice, minute=None, tick_days=None,
            inst=[r for r in inst if r["date"] <= cutoff],
            margin=[r for r in margin if r["date"] <= cutoff],
            shareholding=[r for r in shareholding if r["date"] <= cutoff],
            sbl=[r for r in sbl if r["date"] <= cutoff],
            rs_info=None, with_backtest=False)
        if result is None:
            continue
        rows.append({"date": date, "tech_score": result["score"], "components": result["components"]})
    return rows


def test_replay_scores_ctx_built_once_matches_per_bar_rebuild():
    """docs/https-finlab-finance-blog-python-get-tai-memoized-lighthouse.md
    Phase 2a golden master: replay_scores()'s ctx-built-once optimization
    must produce bit-identical tech_score/components to the old per-bar
    rebuild."""
    bars = _random_bars()
    new_rows = bt.replay_scores(bars, [], [], [], [], warmup=120)
    old_rows = _replay_scores_old_per_bar_rebuild(bars, [], [], [], [], warmup=120)
    assert len(new_rows) == len(old_rows) > 0
    for new, old in zip(new_rows, old_rows):
        assert new["date"] == old["date"]
        assert new["tech_score"] == old["tech_score"]
        for key, weight in an.SCORE_WEIGHTS.items():
            assert new[f"c_{key}"] == old["components"][key]


def test_replay_scores_components_weighted_sum_equals_tech_score():
    """docs/.../lighthouse.md Phase 2b identity test."""
    bars = _random_bars(seed=21)
    rows = bt.replay_scores(bars, [], [], [], [], warmup=120)
    assert rows
    for row in rows:
        weighted = sum(row[f"c_{key}"] * weight for key, weight in an.SCORE_WEIGHTS.items())
        weighted += row["order_flow_override"]
        assert row["tech_score"] == round(max(-100.0, min(100.0, weighted)))


def test_replay_scores_persists_order_flow_override():
    """replay_scores() always calls an.analyze() with minute=None/
    tick_days=None (see its own docstring), so the divergence override
    never fires here -- every row's order_flow_override is 0.0, not
    missing/None. Regression test: db.upsert_score_history() used to have
    no column to receive this at all, so the c_* dot product silently
    dropped it whenever a caller DID have real intraday data."""
    bars = _random_bars(seed=21)
    rows = bt.replay_scores(bars, [], [], [], [], warmup=120)
    assert rows
    assert all(row["order_flow_override"] == 0.0 for row in rows)


def test_compute_rs_by_date_hand_computed():
    dates = [f"d{i}" for i in range(10)]
    bars_a = [{"date": d, "c": 100.0} for d in dates[:6]] + [{"date": d, "c": c} for d, c in zip(dates[6:], [110, 120, 130, 140])]
    bars_b = [{"date": d, "c": 100 - i} for i, d in enumerate(dates)]  # 100,99,...,91
    out = bt.compute_rs_by_date({"A": bars_a, "B": bars_b})

    a6, b6 = out["A"]["d6"], out["B"]["d6"]
    assert a6["rank"] == 1 and b6["rank"] == 2
    assert abs(a6["return_pct"] - 10.0) < 1e-9          # (110-100)/100*100
    assert abs(b6["return_pct"] - (94 - 99) / 99 * 100) < 1e-9
    assert a6["percentile"] == 100.0 and b6["percentile"] == 0.0
    for d in dates[:5]:  # idx<5 -> no 5-day return yet, no rank entry
        assert d not in out["A"] and d not in out["B"]


# ------------------------------------------------------------------ simulate --

def test_simulate_action_sell_exit_and_fee_deduction():
    """Hand-scripted action sequence: entry on 準備進場 while flat, exit on
    減碼警示. Verifies fill happens at bars[i+1]['o'], never at the signal
    bar's own close (protection #3), and that fee_pct is deducted.

    Entry signal at d1 (idx=1) fills at bars[2]'s open (d2). Exit signal at
    d3 (idx=3) fills at bars[4]'s open (d4) -- the one-bar fill lag applies
    to every signal uniformly, so it shows up here too, not just on entry.
    """
    bars = _bars([100, 101, 102, 103, 104, 105])
    score_rows = [
        {"date": "d0", "action": "無變化", "composite": 0},
        {"date": "d1", "action": "準備進場", "composite": 45},
        {"date": "d2", "action": "今日關注", "composite": 45},
        {"date": "d3", "action": "減碼警示", "composite": -50},
    ]
    out = bt.simulate(score_rows, bars)
    assert len(out["trades"]) == 1
    t = out["trades"][0]
    assert t["entry_date"] == "d2" and t["entry_price"] == bars[2]["o"]
    assert t["exit_date"] == "d4" and t["exit_price"] == bars[4]["o"]
    fee = bt.STRATEGY_PARAMS["fee_pct"]
    expected_ret = (bars[4]["o"] / bars[2]["o"] - 1) * 100 - fee
    assert abs(t["ret_pct"] - expected_ret) < 1e-9
    assert t["exit_reason"] == "action_sell"


def test_simulate_composite_exit_distinct_from_action_sell():
    bars = _bars([100, 101, 102, 103, 104, 105])
    score_rows = [
        {"date": "d0", "action": "無變化", "composite": 0},
        {"date": "d1", "action": "準備進場", "composite": 45},
        {"date": "d2", "action": "今日關注", "composite": -15},  # action != 減碼警示, but composite<=-10
        {"date": "d3", "action": "無變化", "composite": 0},
    ]
    # exit_composite explicit here (not STRATEGY_PARAMS' Phase 3 default of
    # -40) so this test stays independent of that default's own value.
    out = bt.simulate(score_rows, bars, params={"exit_composite": -10})
    t = out["trades"][0]
    assert t["exit_reason"] == "composite_exit"
    assert t["entry_price"] == bars[2]["o"] and t["exit_price"] == bars[3]["o"]


def test_simulate_stop_loss_exit():
    opens = [100.0] * 21 + [100.0, 60.0] + [60.0] * 18
    closes = [100.0] * 21 + [100.0] + [55.0] * 19  # crash right after entry fill
    dates = [f"d{i:03d}" for i in range(len(opens))]
    bars = [{"date": d, "o": o, "h": max(o, c) * 1.01, "l": min(o, c) * 0.99, "c": c, "v": 1000}
            for d, o, c in zip(dates, opens, closes)]
    score_rows = [{"date": dates[i], "action": ("準備進場" if i == 20 else "無變化"),
                   "composite": (45 if i == 20 else 0)} for i in range(len(opens) - 1)]
    out = bt.simulate(score_rows, bars, params={"atr_mult": 2.0})
    assert out["trades"], "expected the price crash to trip the ATR stop-loss"
    assert out["trades"][0]["exit_reason"] == "stop_loss"


def test_simulate_max_hold_days_exit():
    # signal fires when hold_days (idx - entry_idx) >= max_hold_days; execution
    # is one bar later (fill lag applies uniformly to every exit path), so the
    # *stored* hold_days on the trade is max_hold_days + 1, not max_hold_days.
    n = 70
    opens = [100.0 + i * 0.01 for i in range(n)]
    dates = [f"d{i:03d}" for i in range(n)]
    bars = _bars(opens, dates)
    score_rows = [{"date": dates[0], "action": "準備進場", "composite": 45}]
    score_rows += [{"date": dates[i], "action": "無變化", "composite": 0} for i in range(1, n - 1)]
    out = bt.simulate(score_rows, bars, params={"max_hold_days": 60})
    t = out["trades"][0]
    assert t["exit_reason"] == "max_hold"
    assert t["hold_days"] == 61


def test_simulate_end_of_data_force_closes_open_position():
    bars = _bars([100 + i for i in range(10)])
    score_rows = [{"date": f"d{i}", "action": ("準備進場" if i == 1 else "無變化"),
                   "composite": (45 if i == 1 else 0)} for i in range(9)]
    out = bt.simulate(score_rows, bars)
    t = out["trades"][-1]
    assert t["exit_reason"] == "end_of_data"
    assert t["exit_price"] == bars[-1]["c"]


def test_simulate_with_precomputed_bar_index_matches_default():
    """build_bar_index() lets a caller (e.g. optimize.py's walk-forward grid
    search) reuse one (bar_idx_by_date, highs, lows, closes) tuple across
    many simulate() calls against the same bars -- must produce identical
    results to the default (rebuild internally) path."""
    bars = _bars([100 + i for i in range(10)])
    score_rows = [{"date": f"d{i}", "action": ("準備進場" if i == 1 else "無變化"),
                   "composite": (45 if i == 1 else 0)} for i in range(9)]
    baseline = bt.simulate(score_rows, bars)
    with_index = bt.simulate(score_rows, bars, bar_index=bt.build_bar_index(bars))
    assert with_index == baseline


def test_simulate_summary_win_rate_avg_ret_total_return_max_drawdown():
    """One losing trade then one winning trade -- independently recomputed
    expectations for every summary field, including a real (negative)
    max_drawdown on a known equity curve."""
    opens = [100, 101, 102, 103, 90, 80, 70, 90, 100, 110, 120, 130]
    dates = [f"d{i}" for i in range(len(opens))]
    bars = _bars(opens, dates)
    score_rows = [
        {"date": "d0", "action": "無變化", "composite": 0},
        {"date": "d1", "action": "準備進場", "composite": 50},
        {"date": "d2", "action": "今日關注", "composite": 45},
        {"date": "d3", "action": "減碼警示", "composite": -50},   # closes trade1 at a loss
        {"date": "d4", "action": "準備進場", "composite": 45},
        {"date": "d5", "action": "無變化", "composite": 20},
        {"date": "d6", "action": "減碼警示", "composite": -60},   # closes trade2 at a gain
        {"date": "d7", "action": "無變化", "composite": 0},
    ]
    out = bt.simulate(score_rows, bars)
    trades, summary = out["trades"], out["summary"]
    assert len(trades) == 2

    fee = bt.STRATEGY_PARAMS["fee_pct"]
    ret1 = (90 / 102 - 1) * 100 - fee    # entry d2@102 -> exit d4@90 (loss)
    ret2 = (90 / 80 - 1) * 100 - fee     # entry d5@80 -> exit d7@90 (gain)
    assert abs(trades[0]["ret_pct"] - ret1) < 1e-9
    assert abs(trades[1]["ret_pct"] - ret2) < 1e-9

    assert summary["n_trades"] == 2
    assert abs(summary["win_rate"] - 50.0) < 1e-9
    assert abs(summary["avg_ret"] - (ret1 + ret2) / 2) < 1e-9
    equity = (1 + ret1 / 100) * (1 + ret2 / 100)
    assert abs(summary["total_return"] - (equity - 1) * 100) < 1e-9
    # equity dips after trade1 and never recovers above the starting peak (1.0)
    equity_after_t1 = 1 + ret1 / 100
    assert abs(summary["max_drawdown"] - (equity_after_t1 - 1.0) * 100) < 1e-9
    assert summary["start_date"] == "d0" and summary["end_date"] == "d7"
    expected_bh = (90 / 100 - 1) * 100 - fee
    assert abs(summary["buy_hold_return"] - expected_bh) < 1e-9


def test_simulate_no_trades_returns_none_summary_fields():
    bars = _bars([100, 101, 102, 103])
    score_rows = [{"date": "d0", "action": "無變化", "composite": 0},
                  {"date": "d1", "action": "無變化", "composite": 0}]
    out = bt.simulate(score_rows, bars)
    assert out["trades"] == []
    s = out["summary"]
    assert s["n_trades"] == 0
    assert s["win_rate"] is None and s["avg_ret"] is None
    assert s["total_return"] is None and s["max_drawdown"] is None
    assert s["start_date"] == "d0" and s["end_date"] == "d1"  # dates still reported


# --------------------------------------------------------------- confidence --

def test_compute_confidence_weighted_average_horizon_filter_and_vp_exclusion():
    signals = [
        {"text": "均線多頭排列", "bias": 1, "bt": "均線多頭排列"},
        {"text": "爆量", "bias": 0, "bt": "爆量(>2倍均量)"},
        {"text": "現價高於 VA", "bias": 1, "bt": None},  # VP signal -- no backtest evidence, must be excluded
    ]
    signal_stats = [
        {"signal": "均線多頭排列", "horizon": 20, "n": 40, "win_rate": 70.0, "avg_ret": 5.0},
        {"signal": "均線多頭排列", "horizon": 5, "n": 40, "win_rate": 99.0, "avg_ret": 99.0},  # wrong horizon
        {"signal": "爆量(>2倍均量)", "horizon": 20, "n": 10, "win_rate": 40.0, "avg_ret": -2.0},
        {"signal": "跌破MA60", "horizon": 20, "n": 100, "win_rate": 10.0, "avg_ret": -20.0},  # not triggered today
    ]
    out = bt.compute_confidence(signals, signal_stats)
    exp_n = 40 + 10
    assert out["sample_n"] == exp_n
    assert abs(out["confidence"] - (40 * 70.0 + 10 * 40.0) / exp_n) < 1e-9
    assert abs(out["expected_ret"] - (40 * 5.0 + 10 * (-2.0)) / exp_n) < 1e-9
    assert out["note"] is None
    assert {e["signal"] for e in out["evidence"]} == {"均線多頭排列", "爆量(>2倍均量)"}


def test_compute_confidence_min_samples_guard():
    signals = [{"text": "x", "bias": 1, "bt": "突破站上MA60"}]
    signal_stats = [{"signal": "突破站上MA60", "horizon": 20, "n": 3, "win_rate": 100.0, "avg_ret": 10.0}]
    out = bt.compute_confidence(signals, signal_stats)
    assert out["confidence"] is None
    assert out["sample_n"] == 0
    assert out["note"] == "樣本不足"


def test_compute_confidence_empty_signal_stats_graceful():
    signals = [{"text": "x", "bias": 1, "bt": "均線多頭排列"}]
    out = bt.compute_confidence(signals, [])
    assert out["confidence"] is None and out["note"] == "樣本不足" and out["evidence"] == []
