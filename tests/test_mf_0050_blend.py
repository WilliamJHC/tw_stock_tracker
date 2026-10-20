# -*- coding: utf-8 -*-
"""Tests for scripts/run_mf_0050_blend.py and the pure helpers in scripts/run_survivorship_study.py.
Hand-computed fixtures, no network."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import pytest  # noqa: E402

import run_mf_0050_blend as blend  # noqa: E402
import run_survivorship_study as st  # noqa: E402


def _curve(dates, values):
    return [{"date": d, "value": v} for d, v in zip(dates, values)]


DAYS = ["2020-01-30", "2020-01-31", "2020-02-03", "2020-02-04"]


def test_blend_w0_is_pure_benchmark_and_costs_nothing():
    mf = _curve(DAYS, [1, 2, 3, 4])
    bn = _curve(DAYS, [10, 11, 12, 13])
    out = blend.blend_curve(mf, bn, 0.0, "2020-01-01", "2020-12-31")
    assert [round(r["value"], 9) for r in out] == [round(v / 10, 9) for v in (10, 11, 12, 13)]


def test_blend_rebalances_on_first_day_of_month_and_charges_both_legs():
    mf = _curve(DAYS, [1, 1, 2, 2])      # sleeve doubles on 02-03
    bn = _curve(DAYS, [1, 1, 1, 1])
    out = blend.blend_curve(mf, bn, 0.5, "2020-01-01", "2020-12-31")
    # 02-03: vm=1.0, vb=0.5, total 1.5; target mf 0.75; shifted 0.25; cost 2*0.004*0.25 = 0.002
    assert out[2]["value"] == pytest.approx(1.5 - 0.002)
    # after the reset the weights are 50/50 again; 02-04 has no return and no new rebalance
    assert out[3]["value"] == pytest.approx(1.5 - 0.002)


def test_blend_no_rebalance_inside_a_month():
    days = ["2020-02-03", "2020-02-04", "2020-02-05"]
    out = blend.blend_curve(_curve(days, [1, 2, 4]), _curve(days, [1, 1, 1]), 0.5, "2020-01-01", "2020-12-31")
    assert out[-1]["value"] == pytest.approx(0.5 * 4 + 0.5)  # drifted freely, no cost


def test_pick_ratio_prefers_smaller_on_tie_and_highest_sharpe(monkeypatch):
    sharpes = {0.0: 1.0, 0.1: 1.2, 0.2: 1.2, 0.3: 1.1, 0.4: 0.5, 0.5: 0.4}
    monkeypatch.setattr(blend, "blend_curve", lambda mf, bn, w, s, e: w)
    monkeypatch.setattr(blend, "stats", lambda w: {"sharpe": sharpes[w]})
    best, _ = blend.pick_ratio(None, None)
    assert best == 0.1


def test_validate_requires_both_metrics_strictly_higher(monkeypatch):
    monkeypatch.setattr(blend, "stats", lambda curve: curve)
    monkeypatch.setattr(blend, "blend_curve", lambda mf, bn, w, s, e: {"sharpe": 1.3 if w else 1.2, "cagr_pct": 10.0})
    assert blend.validate(None, None, 0.2, "2026-10-16")["pass"] is False  # CAGR equal, not strictly higher
    monkeypatch.setattr(
        blend, "blend_curve",
        lambda mf, bn, w, s, e: {"sharpe": 1.3 if w else 1.2, "cagr_pct": 11.0 if w else 10.0},
    )
    assert blend.validate(None, None, 0.2, "2026-10-16")["pass"] is True


def _mk(c, s):
    return {"cagr_pct": c, "sharpe": s}


def test_gate_needs_cagr_and_sharpe():
    assert st.gate({"strategy": _mk(22, 1.2), "benchmark": _mk(21, 1.1)})["pass"] is True
    assert st.gate({"strategy": _mk(22, 0.9), "benchmark": _mk(21, 1.1)})["pass"] is False
    assert st.gate({"strategy": _mk(20, 1.2), "benchmark": _mk(21, 1.1)})["pass"] is False
    assert st.gate({"strategy": _mk(21, 1.1), "benchmark": _mk(21, 1.1)})["pass"] is True  # >=


def test_prereg_is_locked_reads_the_lock_line(tmp_path):
    p = tmp_path / "p.md"
    p.write_text("鎖定日期：（待填）\n", encoding="utf-8")
    assert st.prereg_is_locked(str(p)) is False
    p.write_text("鎖定日期：2026-10-21　　x\n", encoding="utf-8")
    assert st.prereg_is_locked(str(p)) is True
    assert st.prereg_is_locked(str(tmp_path / "missing.md")) is False


def _u(dates):
    return {"bars": [{"date": d} for d in dates], "bar_dates": list(dates), "revenue_series": [], "roe_series": []}


def test_build_universe_ok_replaces_main_and_dark_only_where_main_is_empty():
    main = {"1111": _u(["2021-09-10"]), "2222": _u(["2020-01-01"])}
    delisted = {
        "1111": {"status": "ok", "bars": [{"date": "2013-01-01"}, {"date": "2021-09-10"}],
                 "revenue_series": [1], "roe_series": [1]},
        "3333": {"status": "dark", "bars": [{"date": "2015-01-01"}], "revenue_series": [], "roe_series": []},
        "2222": {"status": "dark", "bars": [{"date": "2015-01-01"}], "revenue_series": [], "roe_series": []},
        "4444": {"status": "illiquid"},
    }
    assert set(st.build_universe("main", main, delisted)) == {"1111", "2222"}
    assert st.build_universe("main", main, delisted)["1111"]["bar_dates"] == ["2021-09-10"]
    ok = st.build_universe("main+ok", main, delisted)
    assert ok["1111"]["bar_dates"][0] == "2013-01-01" and "3333" not in ok
    dark = st.build_universe("main+ok+dark", main, delisted)
    assert dark["3333"]["dark"] is True and dark["2222"]["bar_dates"] == ["2020-01-01"]


def test_terminal_codes_uses_universe_last_bar():
    u = {"live": _u(["2026-10-16"]), "gone": _u(["2026-09-15"]), "recent": _u(["2026-09-20"])}
    assert st.terminal_codes(u) == {"gone"}
