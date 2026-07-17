# -*- coding: utf-8 -*-
"""Unit tests for snapshot.py's pure merge_light function."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import snapshot as snap


def _base():
    return {
        "version": 1,
        "mode": "full",
        "full_generated_at": "2026-07-17T08:10:00+08:00",
        "quotes_updated_at": "2026-07-17T08:10:00+08:00",
        "tick_tier": False,
        "config": {"score_bands": [60, 20, -20, -60]},
        "watchlist": [{"code": "2330", "name": "台積電"}, {"code": "2317", "name": "鴻海"}],
        "stocks": {
            "2330": {
                "code": "2330", "name": "台積電",
                "quote": {"price": 900, "change_pct": 0.5, "name": "台積電"},
                "analysis": {"score": 28}, "chart": {"c": [1, 2, 3]},
            },
            "2317": {"error": "抓不到日線資料"},
        },
    }


def test_merge_light_overwrites_quote():
    base = _base()
    quotes = {"2330": {"price": 910, "change_pct": 1.6, "name": "台積電"}}
    out = snap.merge_light(base, quotes, "2026-07-17T09:07:00+08:00")
    assert out["stocks"]["2330"]["quote"]["price"] == 910
    assert out["mode"] == "light"
    assert out["quotes_updated_at"] == "2026-07-17T09:07:00+08:00"


def test_merge_light_keeps_analysis_and_full_generated_at():
    base = _base()
    quotes = {"2330": {"price": 910, "change_pct": 1.6, "name": "台積電"}}
    out = snap.merge_light(base, quotes, "2026-07-17T09:07:00+08:00")
    assert out["stocks"]["2330"]["analysis"] == {"score": 28}
    assert out["stocks"]["2330"]["chart"] == {"c": [1, 2, 3]}
    assert out["full_generated_at"] == base["full_generated_at"]


def test_merge_light_leaves_missing_quote_untouched():
    base = _base()
    original_quote = base["stocks"]["2330"]["quote"]
    out = snap.merge_light(base, {}, "2026-07-17T09:07:00+08:00")
    assert out["stocks"]["2330"]["quote"] == original_quote
    # error entries stay untouched too
    assert out["stocks"]["2317"] == {"error": "抓不到日線資料"}


def test_merge_light_does_not_mutate_base():
    base = _base()
    quotes = {"2330": {"price": 910, "change_pct": 1.6, "name": "台積電"}}
    snap.merge_light(base, quotes, "2026-07-17T09:07:00+08:00")
    assert base["stocks"]["2330"]["quote"]["price"] == 900
    assert base["mode"] == "full"
