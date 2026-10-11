# -*- coding: utf-8 -*-
"""build_high250_candidates() guard: scripts/ is not on conftest's sys.path,
so add it here to import the script module."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import run_strategy_search as rss  # noqa: E402

LIVE = {"revenue": 0.4, "roe": 0.3, "momentum": 0.3}


def test_build_high250_candidates_swaps_and_splits_momentum():
    c = rss.build_high250_candidates(LIVE)
    assert c["baseline"] == LIVE
    assert c["high250"] == {"revenue": 0.4, "roe": 0.3, "high250": 0.3}
    assert c["mom_high250_half"] == {"revenue": 0.4, "roe": 0.3, "momentum": 0.15, "high250": 0.15}


def test_build_high250_candidates_refuses_when_high250_already_live():
    with pytest.raises(ValueError, match="already include high250"):
        rss.build_high250_candidates({"revenue": 0.4, "roe": 0.3, "momentum": 0.0, "high250": 0.3})
