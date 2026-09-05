# -*- coding: utf-8 -*-
"""Unit tests for market_regime.py, all on synthetic price series -- no
network, no DB."""
import datetime as dt

import market_regime as mr


def _bars(closes, start="2020-01-01"):
    base = dt.date.fromisoformat(start)
    times = [int(dt.datetime.combine(base + dt.timedelta(days=i), dt.time()).timestamp()) for i in range(len(closes))]
    return {"t": times, "c": closes}


def _rising(n, start=100.0, step=1.0):
    return [start + step * i for i in range(n)]


def _falling(n, start=100.0, step=1.0):
    return [start - step * i for i in range(n)]


def _flat(n, value=100.0):
    return [value] * n


# --------------------------------------------------------- raw_regime_series --

def test_raw_regime_series_none_during_warmup():
    closes = _rising(mr.MA_MID - 1)  # one bar short of MA_MID's own warmup (the binding constraint)
    out = mr.raw_regime_series(closes)
    assert all(r is None for r in out)


def test_raw_regime_series_detects_clear_bull():
    # A long, steady climb: close > MA20 > MA60 and MA20 has clearly risen
    # over the lookback window -- unambiguous bull by this module's rule.
    closes = _rising(mr.MA_MID + mr.SLOPE_LOOKBACK + 30, step=1.0)
    out = mr.raw_regime_series(closes)
    assert out[-1] == "bull"


def test_raw_regime_series_detects_clear_bear():
    closes = _falling(mr.MA_MID + mr.SLOPE_LOOKBACK + 30, start=500.0, step=1.0)
    out = mr.raw_regime_series(closes)
    assert out[-1] == "bear"


def test_raw_regime_series_flat_price_is_consolidation():
    # No trend at all: MA20 == MA60 == every close, slope is exactly 0 --
    # fails both the bull and bear slope thresholds.
    closes = _flat(mr.MA_MID + mr.SLOPE_LOOKBACK + 10)
    out = mr.raw_regime_series(closes)
    assert out[-1] == "consolidation"


def test_classify_regime_matches_raw_regime_series_at_idx():
    closes = _rising(mr.MA_MID + mr.SLOPE_LOOKBACK + 15)
    bars = _bars(closes)
    idx = len(closes) - 1
    assert mr.classify_regime(bars, idx) == mr.raw_regime_series(closes)[idx]


# --------------------------------------------------------- effective_regime_series --

def _dated(regimes, start="2020-01-01"):
    base = dt.date.fromisoformat(start)
    return [((base + dt.timedelta(days=i)).isoformat(), r) for i, r in enumerate(regimes)]


def test_effective_regime_seeds_from_first_classified_day():
    raw = _dated([None, None, "bull", "bull"])
    out = mr.effective_regime_series(raw, confirm_days=3)
    assert [r for _, r in out] == [None, None, "bull", "bull"]


def test_effective_regime_does_not_switch_before_confirm_days():
    # Starts bull, dips to "bear" for only 2 days (< confirm_days=3) then
    # back to bull -- the switch should never be confirmed.
    raw = _dated(["bull"] * 5 + ["bear"] * 2 + ["bull"] * 5)
    out = mr.effective_regime_series(raw, confirm_days=3)
    assert all(r == "bull" for _, r in out)


def test_effective_regime_switches_after_confirm_days_in_a_row():
    raw = _dated(["bull"] * 5 + ["bear"] * 3 + ["bear"] * 2)
    out = mr.effective_regime_series(raw, confirm_days=3)
    regimes = [r for _, r in out]
    # First 5 days stay "bull"; from the 3rd consecutive "bear" day onward
    # (index 7, 0-based) it flips and stays flipped.
    assert regimes[:7] == ["bull"] * 7
    assert regimes[7:] == ["bear"] * 3


def test_effective_regime_broken_streak_resets_confirmation_count():
    # bear, bear, bull, bear, bear -- never 3 "bear" IN A ROW after the
    # initial bull-seeded state, so it should never confirm the switch.
    raw = _dated(["bull"] * 3 + ["bear", "bear", "bull", "bear", "bear"])
    out = mr.effective_regime_series(raw, confirm_days=3)
    assert all(r == "bull" for _, r in out)


def test_effective_regime_none_passes_through_without_breaking_streak():
    # A None (warmup) day in the middle of an otherwise-unbroken 3-day bear
    # streak must not reset the count -- it's missing data, not evidence
    # against the switch.
    raw = _dated(["bull"] * 3 + ["bear", None, "bear", "bear"])
    out = mr.effective_regime_series(raw, confirm_days=3)
    regimes = [r for _, r in out]
    assert regimes[3] == "bull"  # 1st bear day: not yet confirmed
    assert regimes[4] == "bull"  # None: passes through as still-effective regime
    assert regimes[5] == "bull"  # 2nd real bear day: still not 3 in a row
    assert regimes[6] == "bear"  # 3rd real bear day: confirmed


# --------------------------------------------------------- current_regime --

def test_current_regime_reflects_debounced_last_value():
    closes = _rising(mr.MA_MID + mr.SLOPE_LOOKBACK + 30)
    bars = _bars(closes)
    assert mr.current_regime(bars) == "bull"


def test_current_regime_none_when_not_enough_history():
    closes = _rising(mr.MA_MID - 1)  # one bar short of MA_MID's own warmup
    bars = _bars(closes)
    assert mr.current_regime(bars) is None
