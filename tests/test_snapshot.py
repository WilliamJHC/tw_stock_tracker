# -*- coding: utf-8 -*-
"""Unit tests for snapshot.py's pure merge_light function."""
import json
import os
import sys

import pipeline as pl
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
# docs/archive/dev_plan_recommendations.md Phase 2: "run_light() 也要對這 5 檔（若存在）
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


# ------------------------------------------------------------------ run_full --
# run_full() must batch tracked AND recommended codes into a SINGLE
# build_watchlist_payloads() call so 5-day RS ranking (an.rs_ranking()) is
# computed across one consistent universe, instead of the tracked watchlist
# and the recommendation-only codes getting two separately-scoped (and
# differently-ranked) batches (altitude review finding on the
# prefetched_stocks fix, see pipeline.build_recommendations()'s docstring).

def test_run_full_batches_tracked_and_recommended_codes_in_one_call(monkeypatch, tmp_path):
    monkeypatch.setattr(pl, "load_watchlist", lambda: [{"code": "2330", "name": "台積電"}])
    monkeypatch.setattr(pl, "get_recommended_codes", lambda: ["2330", "2454"])

    batch_calls = []

    def fake_build_watchlist_payloads(codes):
        batch_calls.append(sorted(codes))
        return {c: {"code": c, "quote": {"price": 1.0}, "analysis": {}} for c in codes}
    monkeypatch.setattr(pl, "build_watchlist_payloads", fake_build_watchlist_payloads)

    reco_calls = []

    def fake_build_recommendations(prefetched_stocks=None, tracked_codes=None):
        reco_calls.append((set(prefetched_stocks), set(tracked_codes)))
        return {"recommendations": [], "as_of": None}
    monkeypatch.setattr(pl, "build_recommendations", fake_build_recommendations)

    pullback_calls = []

    def fake_build_pullback_watchlist(prefetched_quotes=None, tracked_codes=None):
        pullback_calls.append((set(prefetched_quotes), set(tracked_codes)))
        return {"setups": [], "as_of": None}
    monkeypatch.setattr(pl, "build_pullback_watchlist", fake_build_pullback_watchlist)

    monkeypatch.setattr(snap.ds, "tick_tier_available", lambda: False)
    monkeypatch.setattr(snap.ds, "data_tier", lambda: "free")

    out_dir = tmp_path / "dist"
    ok, fail, total = snap.run_full(str(out_dir))
    # 強勢回調 cards reuse every quote the batched fetch already has
    assert pullback_calls == [({"2330", "2454"}, {"2330"})]

    assert batch_calls == [["2330", "2454"]]  # one batched call, union of tracked+recommended
    assert reco_calls == [({"2330", "2454"}, {"2330"})]
    assert (ok, fail, total) == (1, 0, 1)  # ok/fail/total stay scoped to the tracked watchlist

    data = json.loads((out_dir / "data.json").read_text(encoding="utf-8"))
    assert set(data["stocks"].keys()) == {"2330"}  # payload["stocks"] stays watchlist-scoped


# ------------------------------- merge_light: 強勢回調 cards (pullback) --

def test_merge_light_refreshes_pullback_quote_only():
    base = _base()
    base["pullback"] = {"as_of": "2026-10-12", "setups": [
        {"code": "3017", "zone": {"low": 600, "high": 610}, "quote": {"price": 650}}]}
    out = snap.merge_light(base, {"3017": {"price": 612}}, "2026-10-13T09:07:00+08:00")
    s = out["pullback"]["setups"][0]
    assert s["quote"]["price"] == 612
    assert s["zone"] == {"low": 600, "high": 610}


def test_run_light_includes_pullback_codes_in_quote_refresh(monkeypatch, tmp_path):
    base = _base()
    base["pullback"] = {"as_of": "2026-10-12", "setups": [{"code": "3017", "quote": None}]}
    asked = []

    def fake_quote(code):
        asked.append(code)
        return {"price": 1.0}
    monkeypatch.setattr(snap.ds, "get_quote", fake_quote)
    monkeypatch.setattr(snap, "assemble_site", lambda out_dir: os.makedirs(out_dir, exist_ok=True))
    snap.run_light(str(tmp_path / "dist"), base)
    assert "3017" in asked
