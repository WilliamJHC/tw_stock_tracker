# -*- coding: utf-8 -*-
"""Bull/bear/consolidation market-regime classification for TAIEX (see
docs/regime_strategy_research.md and docs/adr/0004-regime-adaptive-strategy.md).
Pure functions over src/data_sources.py's get_index_daily() output -- no I/O,
no FinMind, no DB access, same split as analysis.py/backtest.py/
finlab_factors.py.

Definition mirrors analysis.py's existing per-stock "均線多頭/空頭排列"
idiom (close vs MA20 vs MA60), scaled up to the index level with an explicit
middle "consolidation" state instead of forcing every day into bull or bear:
  bull          close > MA_SHORT > MA_MID, and MA_SHORT has risen more than
                SLOPE_THRESHOLD (as a fraction) over the last SLOPE_LOOKBACK
                bars
  bear          close < MA_SHORT < MA_MID, and MA_SHORT has fallen more than
                SLOPE_THRESHOLD over the last SLOPE_LOOKBACK bars
  consolidation everything else (MAs entangled, or aligned but flat)

None for any bar without enough history yet (same "None during warmup"
convention as analysis.ma_series()) -- never silently counted as
consolidation, since that would conflate "no data yet" with "assessed as
range-bound".

>>> Debouncing is a pure function, not persisted state <<<
effective_regime_series() adds a debounce pass on top of the raw
classification: raw_regime_series() flips on a single noisy day near a
bull/bear boundary, so the "currently in effect" regime only switches once a
NEW raw value has appeared CONFIRM_DAYS days in a row. This is deliberately a
pure function of the whole raw sequence, recomputed from scratch on every
call -- both scripts/run_daily_recommendations.py and src/run_backtest.py
call it fresh over the same TAIEX history and get byte-identical answers, so
there is no shared "today's regime" state to keep in sync across the two
consumers, and nothing to persist to db.py's meta table for this purpose."""
import datetime as dt

import analysis as an

# The three regimes this module ever classifies a bar into (never None's
# own "not enough history" case -- that's a distinct fourth value handled
# explicitly everywhere below, not a member of this tuple). Single source of
# truth for the regime taxonomy: src/factor_eval.py and src/backtest.py both
# import this instead of hardcoding their own copy, so a renamed/added
# regime here can't silently drift out of sync with their bucketing.
REGIMES = ("bull", "bear", "consolidation")

MA_SHORT = 20
MA_MID = 60
SLOPE_LOOKBACK = 10
# MA_SHORT must move at least this fraction (1%) over SLOPE_LOOKBACK bars to
# count as "trending" rather than flat -- see docs/regime_strategy_research.md
# for how this initial value was checked against real TAIEX history.
SLOPE_THRESHOLD = 0.01
CONFIRM_DAYS = 3


def raw_regime_series(closes):
    """[regime_or_None, ...], index-aligned with `closes`. None until both
    MA_MID and a SLOPE_LOOKBACK-old MA_SHORT value exist -- same "None
    during warmup" convention as analysis.ma_series(), which this reuses for
    both moving averages instead of recomputing them by hand."""
    ma_short = an.ma_series(closes, MA_SHORT)
    ma_mid = an.ma_series(closes, MA_MID)
    out = [None] * len(closes)
    for i in range(len(closes)):
        if ma_short[i] is None or ma_mid[i] is None:
            continue
        prev_idx = i - SLOPE_LOOKBACK
        if prev_idx < 0 or not ma_short[prev_idx]:
            continue
        slope = (ma_short[i] - ma_short[prev_idx]) / ma_short[prev_idx]
        close = closes[i]
        if close > ma_short[i] > ma_mid[i] and slope > SLOPE_THRESHOLD:
            out[i] = "bull"
        elif close < ma_short[i] < ma_mid[i] and slope < -SLOPE_THRESHOLD:
            out[i] = "bear"
        else:
            out[i] = "consolidation"
    return out


def classify_regime(bars, idx):
    """bars: get_index_daily()'s {t,o,h,l,c,v,...}. Single-point convenience
    wrapper over raw_regime_series() for a caller that only wants one day's
    classification -- O(idx) per call (recomputes both MA series up to idx).
    regime_series()/raw_regime_series() should be called directly instead
    when classifying every day in a history (Phase B's factor-IC bucketing,
    src/run_backtest.py's regime-aware replay), to avoid paying that cost
    once per day."""
    return raw_regime_series(bars["c"][: idx + 1])[idx]


def regime_series(bars):
    """get_index_daily()'s {t,o,h,l,c,v,...} -> [(date_str, regime_or_None),
    ...], one entry per bar, oldest first. This is the RAW (undebounced)
    series -- see effective_regime_series() for the debounced "currently in
    effect" view. Phase B's regime-bucketed factor-IC/win-rate evaluation
    deliberately buckets against this raw series (finer-grained, cleaner
    statistics) rather than the debounced one: debouncing only matters for
    "what do we act on today", not for measuring how a factor behaved on an
    already-known historical day."""
    raw = raw_regime_series(bars["c"])
    dates = [dt.date.fromtimestamp(t).isoformat() for t in bars["t"]]
    return list(zip(dates, raw))


def effective_regime_series(raw_series, confirm_days=CONFIRM_DAYS):
    """[(date, effective_regime), ...], same length/order as `raw_series` (a
    regime_series() result) -- debounces the raw per-day classification so
    the "currently in effect" regime only switches once a NEW raw value has
    appeared on `confirm_days` consecutive (non-None) days in a row. A
    streak broken by a different raw value (including a reversion to the
    still-effective regime) resets the count -- only `confirm_days` IN A ROW
    confirms a switch.

    None raw entries (warmup) pass through as None and neither extend nor
    break a streak -- the streak only tracks non-None raw values.

    Pure function of the whole input sequence: recomputes from scratch every
    call, no persisted state (see this module's docstring)."""
    out = []
    effective = None
    candidate = None
    streak = 0
    for date, raw in raw_series:
        if raw is None:
            out.append((date, effective))
            continue
        streak = streak + 1 if raw == candidate else 1
        candidate = raw
        if effective is None:
            # Nothing confirmed yet -- seed straight from the first
            # classified day rather than reporting None for confirm_days
            # bars into the available history just to be cautious.
            effective = raw
        elif raw != effective and streak >= confirm_days:
            effective = raw
        out.append((date, effective))
    return out


def current_regime(bars, confirm_days=CONFIRM_DAYS):
    """"bull"/"bear"/"consolidation"/None (not enough history yet) -- the
    debounced regime as of the LAST bar in `bars` (get_index_daily()'s
    output). Convenience wrapper for a caller that only wants "what regime
    are we in right now" (scripts/run_daily_recommendations.py), without
    juggling regime_series()/effective_regime_series() itself."""
    series = effective_regime_series(regime_series(bars), confirm_days)
    return series[-1][1] if series else None
