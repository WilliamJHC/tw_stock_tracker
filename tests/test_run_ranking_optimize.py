# -*- coding: utf-8 -*-
"""Tests for scripts/run_ranking_optimize.py's display helper."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import run_ranking_optimize as rro  # noqa: E402


def test_round_values_rounds_floats_and_passes_other_values_through():
    d = {"a": 107.73912345, "b": None, "c": "ic_suggested", "d": 3}
    assert rro._round_values(d) == {"a": 107.74, "b": None, "c": "ic_suggested", "d": 3}


def test_round_values_does_not_mutate_its_input():
    d = {"a": 1.23456}
    rro._round_values(d)
    assert d == {"a": 1.23456}
