# -*- coding: utf-8 -*-
"""Unit tests for snapshot.py's pure merge_light function."""
import json
import sys

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
        "recommendations": {
            "as_of": "2026-08-20",
            "recommendations": [
                {"code": "2454", "name": "聯發科", "composite_score": 0.8,
                 "factor_ranks": {"revenue": 0.8, "roe": 0.7, "momentum": 0.6},
                 "quote": {"price": 800, "change_pct": 0.2, "name": "聯發科"},
                 "target_price": {"level": 810.0, "basis": "VAH", "as_of": "2026-08-20"},
                 "tracked": False},
            ],
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


# ---------------------------- merge_light: recommendation quotes (Phase 2) --
# docs/dev_plan_recommendations.md Phase 2: "run_light() 也要對這 5 檔（若存在）
# 刷新報價，不能只刷追蹤清單" -- recommendation cards get the same live-quote
# refresh treatment as watchlist stocks in light mode.

def test_merge_light_refreshes_recommendation_quote():
    base = _base()
    quotes = {"2454": {"price": 820, "change_pct": 2.5, "name": "聯發科"}}
    out = snap.merge_light(base, quotes, "2026-07-17T09:07:00+08:00")
    assert out["recommendations"]["recommendations"][0]["quote"]["price"] == 820


def test_merge_light_leaves_recommendation_without_quote_untouched():
    base = _base()
    original_quote = base["recommendations"]["recommendations"][0]["quote"]
    out = snap.merge_light(base, {}, "2026-07-17T09:07:00+08:00")
    assert out["recommendations"]["recommendations"][0]["quote"] == original_quote


def test_merge_light_keeps_recommendation_analysis_fields_untouched():
    base = _base()
    quotes = {"2454": {"price": 820, "change_pct": 2.5, "name": "聯發科"}}
    out = snap.merge_light(base, quotes, "2026-07-17T09:07:00+08:00")
    r = out["recommendations"]["recommendations"][0]
    assert r["target_price"] == {"level": 810.0, "basis": "VAH", "as_of": "2026-08-20"}
    assert r["composite_score"] == 0.8


def test_merge_light_missing_recommendations_key_does_not_crash():
    """A base predating Phase 2 (no "recommendations" key at all) must not
    crash merge_light() -- graceful degradation, same stance as an empty
    meta.daily_recommendations."""
    base = _base()
    del base["recommendations"]
    out = snap.merge_light(base, {"2330": {"price": 910, "name": "台積電"}}, "2026-07-17T09:07:00+08:00")
    assert out["stocks"]["2330"]["quote"]["price"] == 910
    assert "recommendations" not in out


# ---------------------------------------------------------------- load_base --
# light mode runs every 20 min off the previous snapshot; load_base must never
# crash the scheduled job when that base is absent or corrupt. It returns None
# and main() then falls back to a full run -- never deploying a data.json built
# on a missing base. test_main_falls_back_to_full_when_base_missing locks that
# second half so the two can't drift apart.

def test_load_base_reads_valid_file(tmp_path):
    p = tmp_path / "data.json"
    p.write_text(json.dumps(_base()), encoding="utf-8")
    out = snap.load_base(str(p), None)
    assert out is not None
    assert out["mode"] == "full"


def test_load_base_missing_file_returns_none(tmp_path):
    out = snap.load_base(str(tmp_path / "nope.json"), None)
    assert out is None


def test_load_base_corrupt_file_returns_none(tmp_path):
    p = tmp_path / "data.json"
    p.write_text("{not valid json", encoding="utf-8")
    out = snap.load_base(str(p), None)
    assert out is None


def test_main_falls_back_to_full_when_base_missing(monkeypatch, tmp_path):
    """--mode light with an unusable base must run full, not skip: skipping
    would leave the 20-min cadence deploying nothing after a bad deploy."""
    monkeypatch.setattr(sys, "argv", [
        "snapshot.py", "--mode", "light",
        "--base-file", str(tmp_path / "nope.json"),
        "--out", str(tmp_path / "dist"),
    ])
    calls = []
    monkeypatch.setattr(snap, "run_full", lambda out_dir: calls.append("full") or (5, 0, 5))
    monkeypatch.setattr(snap, "run_light", lambda out_dir, base: calls.append("light") or (0, 0, 0))
    snap.main()
    assert calls == ["full"]
