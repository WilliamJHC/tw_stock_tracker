# -*- coding: utf-8 -*-
"""Unit tests for run_backtest.py's _adjusted_bars()."""
import pytest

import run_backtest as rb


def _bar(date, c, adj_factor):
    return {"code": "2330", "date": date, "o": c, "h": c, "l": c, "c": c, "v": 1000,
            "adj_factor": adj_factor}


def test_adjusted_bars_defaults_to_1_when_every_bar_is_null():
    """build_adj_factors.py has never run for this code -- uniformly NULL
    is a consistent no-op (adjusted == raw)."""
    bars = [_bar("2026-08-17", 100.0, None), _bar("2026-08-18", 101.0, None)]
    out = rb._adjusted_bars(bars)
    assert [b["c"] for b in out] == [100.0, 101.0]


def test_adjusted_bars_applies_factor_when_every_bar_has_one():
    bars = [_bar("2026-08-17", 100.0, 0.5), _bar("2026-08-18", 200.0, 1.0)]
    out = rb._adjusted_bars(bars)
    assert [b["c"] for b in out] == [50.0, 200.0]


def test_adjusted_bars_raises_when_null_and_computed_factors_are_mixed():
    """--refresh-data appended new dates (NULL adj_factor) after a split
    that scripts/build_adj_factors.py hasn't reprocessed yet -- the old
    bars' factor is now stale, not a legitimate 1.0. Must not silently
    mix, or the seam gets a phantom price jump."""
    bars = [_bar("2026-08-17", 100.0, 1.0), _bar("2026-08-18", 25.0, None)]
    with pytest.raises(ValueError, match="adj_factor 只有部分算過"):
        rb._adjusted_bars(bars)
