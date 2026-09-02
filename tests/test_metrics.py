# -*- coding: utf-8 -*-
"""Unit tests for metrics.py. _pct_rank() coverage: see finlab_factors.py's
own _pct_rank() for the sibling implementation this must stay
interchangeable with (docs/https-finlab-finance-blog-python-get-tai-
memoized-lighthouse.md Phase 4). fmt_pct()/exit_reason_breakdown()/
print_exit_reason_table() coverage: these used to be a byte-identical
private copy in each of scripts/report_backtest.py and
scripts/run_breakout_backtest.py."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import finlab_factors as ff
import metrics as m


def test_pct_rank_matches_finlab_factors_pct_rank_convention():
    codes = ["A", "B", "C", "D"]
    raw = {"A": 10.0, "B": 30.0, "C": 20.0, "D": 30.0}  # B/D tied
    ff_ranks = ff._pct_rank(raw)
    m_ranks = dict(zip(codes, m._pct_rank([raw[c] for c in codes])))
    for c in codes:
        assert abs(ff_ranks[c] - m_ranks[c]) < 1e-12


def test_pct_rank_range_is_zero_exclusive_one_inclusive():
    ranks = m._pct_rank([5.0, 1.0, 3.0, 3.0, 9.0])
    assert all(0 < r <= 1 for r in ranks)
    assert max(ranks) == 1.0  # the top value lands exactly at 1.0, not < 1.0


def test_pct_rank_single_value_is_one():
    assert m._pct_rank([42.0]) == [1.0]


def test_pct_rank_empty_is_empty():
    assert m._pct_rank([]) == []


def test_fmt_pct_formats_and_right_aligns():
    assert m.fmt_pct(1.23) == "      1.2%"
    assert m.fmt_pct(-4.5, width=6) == "  -4.5%"


def test_fmt_pct_none_is_a_same_width_dash():
    formatted = m.fmt_pct(None, width=6)
    assert formatted == "     -"
    assert len(formatted) == 6  # == width, so a None column lines up with a real one


def _trade(reason, ret_pct):
    return {"exit_reason": reason, "ret_pct": ret_pct}


def test_exit_reason_breakdown_computes_win_rate_mean_median():
    trades = [_trade("stop_loss", -5.0), _trade("stop_loss", 3.0), _trade("target", 10.0)]
    rows = m.exit_reason_breakdown(trades)
    by_reason = {r["reason"]: r for r in rows}
    assert by_reason["stop_loss"]["n"] == 2
    assert by_reason["stop_loss"]["win_rate"] == 50.0
    assert by_reason["stop_loss"]["mean"] == -1.0
    assert by_reason["stop_loss"]["median"] == -1.0
    assert by_reason["target"]["n"] == 1
    assert by_reason["target"]["win_rate"] == 100.0


def test_exit_reason_breakdown_sorted_by_trade_count_descending():
    trades = [_trade("rare", 1.0), _trade("common", 1.0), _trade("common", 2.0), _trade("common", 3.0)]
    rows = m.exit_reason_breakdown(trades)
    assert [r["reason"] for r in rows] == ["common", "rare"]


def test_print_exit_reason_table_renders_header_and_rows(capsys):
    rows = m.exit_reason_breakdown([_trade("target", 10.0), _trade("target", -2.0)])
    m.print_exit_reason_table(rows)
    out = capsys.readouterr().out
    assert "exit_reason" in out and "median" in out
    assert "target" in out
