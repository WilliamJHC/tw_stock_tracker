# -*- coding: utf-8 -*-
"""Unit tests for metrics.py's _pct_rank() -- see finlab_factors.py's
own _pct_rank() for the sibling implementation this must stay
interchangeable with (docs/https-finlab-finance-blog-python-get-tai-
memoized-lighthouse.md Phase 4)."""
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
