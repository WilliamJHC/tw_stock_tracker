# -*- coding: utf-8 -*-
"""Unit tests for run_backtest.py's _adjusted_bars() and chip-row merge/split."""
import pytest

import run_backtest as rb


def test_split_chip_rows_round_trips_dealer_net():
    """_chip_rows()' by_date merge and _split_chip_rows()' inverse must
    carry the dealer bucket through, same as foreign/trust."""
    chip_rows = [{
        "date": "2026-01-05", "foreign_net": 100, "trust_net": -50, "dealer_net": 25,
        "margin_balance": None, "margin_delta": None, "short_balance": None, "short_delta": None,
        "foreign_ratio": None, "foreign_shares": None,
        "sbl_balance": None, "sbl_sales": None, "sbl_returns": None,
    }]
    inst, margin, shareholding, sbl = rb._split_chip_rows(chip_rows)
    assert inst == [{"date": "2026-01-05", "foreign": 100, "trust": -50, "dealer": 25}]


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
