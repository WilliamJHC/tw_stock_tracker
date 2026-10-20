# -*- coding: utf-8 -*-
"""Tests for scripts/report_vs_benchmark.fetch_benchmark_curves()'s cache
staleness handling. No network: data_sources._yf_history_since is stubbed."""
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import pandas as pd  # noqa: E402

import data_sources as ds  # noqa: E402
import report_vs_benchmark as rvb  # noqa: E402

TODAY = dt.date(2026, 10, 20)


def _write_cache(path, last_date):
    curve = [{"date": "2026-09-01", "value": 100.0}, {"date": last_date, "value": 101.0}]
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"start_date": "2021-07-01", "symbol": "0050", "resolved_ticker": "0050.TW",
                   "total_return_curve": curve, "price_curve": curve}, f)


def _stub_fetch(monkeypatch, calls, fail=False):
    def fake(code, start_date, interval):
        calls.append(code)
        if fail:
            raise ds.SourceError("boom")
        df = pd.DataFrame({"Close": [110.0, 111.0], "Adj Close": [110.0, 111.0]},
                          index=pd.to_datetime(["2026-10-19", "2026-10-20"]))
        df.attrs["resolved_ticker"] = "0050.TW"
        return df
    monkeypatch.setattr(ds, "_yf_history_since", fake)


def test_fresh_cache_is_reused(tmp_path, monkeypatch):
    path = str(tmp_path / "b.json")
    _write_cache(path, "2026-10-19")
    calls = []
    _stub_fetch(monkeypatch, calls)
    curve, _, _ = rvb.fetch_benchmark_curves(cache_path=path, today=TODAY)
    assert calls == []
    assert curve[-1]["date"] == "2026-10-19"


def test_stale_cache_is_refetched_and_rewritten(tmp_path, monkeypatch):
    path = str(tmp_path / "b.json")
    _write_cache(path, "2026-09-11")
    calls = []
    _stub_fetch(monkeypatch, calls)
    curve, _, _ = rvb.fetch_benchmark_curves(cache_path=path, today=TODAY)
    assert calls == ["0050"]
    # the stub also returns a bar dated TODAY, which must be dropped as possibly partial
    assert curve[-1]["date"] == "2026-10-19"
    with open(path, encoding="utf-8") as f:
        assert json.load(f)["total_return_curve"][-1]["date"] == "2026-10-19"


def test_stale_cache_falls_back_with_warning_when_refresh_fails(tmp_path, monkeypatch, capsys):
    path = str(tmp_path / "b.json")
    _write_cache(path, "2026-09-11")
    _stub_fetch(monkeypatch, [], fail=True)
    curve, _, _ = rvb.fetch_benchmark_curves(cache_path=path, today=TODAY)
    assert curve[-1]["date"] == "2026-09-11"
    assert "stale" in capsys.readouterr().err
