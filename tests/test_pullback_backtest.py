# -*- coding: utf-8 -*-
"""Synthetic-data tests for src/pullback_backtest.py. Nothing here reads the
real factor cache (pre-registration section 8: no real-data backtest before
the lock)."""
import datetime as dt
import random
import sys

import pytest

import pullback_backtest as bt
import pullback_strategy as ps

COST = 0.00585


def bar(date, o, h, l, c, v=1_000_000):
    return {"date": date, "o": o, "h": h, "l": l, "c": c, "v": v}


def day(k):
    return f"2020-{1 + k // 28:02d}-{1 + k % 28:02d}"   # sortable fake dates, 28 per "month"


def base_bars(n=30):
    """n flat bars around 200 (tick 0.5), ATR ~2, then the signal bar."""
    bars = [bar(day(k), 200, 201, 199, 200) for k in range(n)]
    bars.append(bar(day(n), 200, 202, 198, 200))          # signal bar: h 202 -> trigger 202.5, low 198 -> zone stop 197.5
    return bars, n


def run(extra, mode="pullback", params=None):
    bars, i = base_bars()
    for k, (o, h, l, c) in enumerate(extra):
        bars.append(bar(day(i + 1 + k), o, h, l, c))
    return bt.simulate_trade(bars, i, mode, params)


# ------------------------------------------------------------ entries ----

def test_no_fill_when_trigger_not_reached():
    assert run([(201, 202, 200, 201)]) is None


def test_fill_at_trigger_and_gap_fill_at_open():
    t = run([(201, 203, 200, 202)] + [(202, 203, 199, 201)] * 70)
    assert t["fill"] == 202.5
    t = run([(204, 205, 203, 204)] + [(204, 205, 203, 204)] * 70)
    assert t["fill"] == 204             # gap above the trigger: open, never the trigger


def test_stop_is_the_farther_of_zone_and_atr():
    t = run([(201, 203, 200, 202)] + [(202, 203, 199, 201)] * 70)
    assert t["stop"] == 197.5 and t["stop_basis"] == "zone"
    t = run([(201, 203, 200, 202)] + [(202, 203, 199, 201)] * 70, params={"stop_atr_mult": 3.0})
    assert t["stop_basis"] == "atr" and t["stop"] < 197.5


def test_control_enters_at_next_open_with_same_stop_formula():
    t = run([(201, 201.5, 199, 201)] + [(201, 202, 199, 201)] * 70, mode="control")
    assert t["fill"] == 201
    assert t["stop"] == 197.5


# -------------------------------------------------------------- exits ----

def test_stop_exit_and_net_r():
    t = run([(201, 203, 200, 202), (202, 202, 197, 198)])
    assert t["exit_reason"] == "stop"
    assert t["gross_r"] == pytest.approx(-1.0)
    r = 202.5 - 197.5
    assert t["net_r"] == pytest.approx(-1.0 - COST * 202.5 / r)
    assert t["half_sold"] is False


def test_gap_down_through_stop_exits_at_open():
    t = run([(201, 203, 200, 202), (190, 191, 189, 190)])
    assert t["gross_r"] == pytest.approx((190 - 202.5) / 5)


def test_target_ladder_three_r_then_five_r():
    # r = 5 -> t3 = 217.5, t5 = 227.5
    t = run([(201, 203, 200, 202), (203, 218, 202, 217), (217, 230, 216, 229)])
    assert t["exit_reason"] == "target"
    assert t["gross_r"] == pytest.approx(0.5 * 3 + 0.5 * 5)
    assert t["half_sold"] is True


def test_same_bar_three_and_five_r_sells_both_halves():
    t = run([(201, 203, 200, 202), (203, 230, 202, 229)])
    assert t["gross_r"] == pytest.approx(4.0)


def test_stop_beats_target_on_the_same_bar():
    t = run([(201, 203, 200, 202), (203, 220, 197, 210)])
    assert t["exit_reason"] == "stop"
    assert t["gross_r"] == pytest.approx(-1.0)


def test_stop_not_moved_after_half_sold():
    # half out at 3R, later drop to the ORIGINAL stop: 0.5*3 + 0.5*(-1)
    t = run([(201, 203, 200, 202), (203, 218, 202, 217), (210, 211, 197, 198)])
    assert t["gross_r"] == pytest.approx(0.5 * 3 - 0.5 * 1)


def test_ema9_break_after_half_exits_next_open():
    seq = [(201, 203, 200, 202), (203, 218, 202, 217)]           # half sold, close 217 still above EMA9 (~203)
    seq += [(216, 217, 201, 201)]                                  # close 201 < EMA9 (~203.2) -> exit at the next open
    seq += [(204, 205, 203, 204)] * 3
    t = run(seq)
    assert t["exit_reason"] == "ema9"
    assert t["half_sold"] is True
    assert t["gross_r"] == pytest.approx(0.5 * 3 + 0.5 * (204 - 202.5) / 5)


def test_ema9_exit_not_applied_before_half_sold():
    seq = [(201, 203, 200, 202)] + [(200, 201, 198, 199)] * 20      # drifts under EMA9, never hits stop (197.5) or 3R
    t = run(seq + [(199, 200, 198, 199)] * 50)
    assert t["exit_reason"] == "time"


def test_time_stop_exits_on_open_of_bar_61():
    t = run([(201, 203, 200, 202)] + [(202, 204, 199, 202)] * 70)
    assert t["exit_reason"] == "time"
    assert t["hold_bars"] == bt.MAX_HOLD_BARS
    assert t["gross_r"] == pytest.approx((202 - 202.5) / 5)


def test_censored_when_data_ends_before_exit():
    assert run([(201, 203, 200, 202), (202, 204, 199, 202)]) == {"censored": True}


# -------------------------------------------------- per-code sequencing ----

def test_one_position_per_code_with_cooldown():
    bars, i = base_bars()
    ext = [(201, 203, 200, 202), (202, 202, 197, 198)] + [(198, 200, 197.8, 199)] * 40
    for k, (o, h, l, c) in enumerate(ext):
        bars.append(bar(day(i + 1 + k), o, h, l, c))
    # signal 1 at i exits (stop) on bar i+2. Signals on the next 6 bars must be blocked or no-fill;
    # make them fillable by giving every bar a high above the previous high.
    universe = {"AAA": bars}
    sigs = [{"date": bars[i]["date"], "code": "AAA"}]
    sigs += [{"date": bars[i + 2 + k]["date"], "code": "AAA"} for k in range(1, 8)]
    trades, stats = bt.run_trades(universe, sigs, "pullback")
    assert len(trades) >= 1
    exit_idx = trades[0]["exit_idx"]
    for later in trades[1:]:
        assert later["entry_idx"] > exit_idx + bt.COOLDOWN_BARS
    assert stats["blocked"] >= 1


def test_excluded_codes_are_dropped():
    bars, i = base_bars()
    bars += [bar(day(i + 1), 201, 203, 200, 202)] + [bar(day(i + 2 + k), 202, 204, 199, 202) for k in range(70)]
    sig = [{"date": bars[i]["date"], "code": "3081"}]
    trades, stats = bt.run_trades({"3081": bars}, sig, "pullback", excluded={"3081"})
    assert trades == [] and stats["signals"] == 0


def test_control_draw_is_seeded_and_excludes():
    bars, i = base_bars()
    bars += [bar(day(i + 1), 201, 203, 200, 202)] + [bar(day(i + 2 + k), 202, 204, 199, 202) for k in range(70)]
    uni = {"AAA": bars, "BBB": bars, "CCC": bars}
    days = {bars[i]["date"]: ["AAA", "BBB", "CCC"]}
    a, _ = bt.run_trades(uni, [], "control", control_days=days)
    b, _ = bt.run_trades(uni, [], "control", control_days=days)
    assert [t["code"] for t in a] == [t["code"] for t in b]
    c, _ = bt.run_trades(uni, [], "control", control_days=days, excluded={a[0]["code"]})
    assert c[0]["code"] != a[0]["code"]


# ------------------------------------------------------------- stats ----

def mk(month, code, net, stop_pct=2.0, basis="zone", sdate=None):
    return {"signal_date": sdate or f"{month}-05", "code": code, "net_r": net, "gross_r": net + 0.1,
            "stop_pct": stop_pct, "stop_basis": basis, "atr_pct": 2.0, "hold_bars": 10}


def test_dedup_keeps_first_per_code_per_month():
    ts = [mk("2020-01", "A", 1, sdate="2020-01-20"), mk("2020-01", "A", -1, sdate="2020-01-03"),
          mk("2020-01", "B", 2), mk("2020-02", "A", 3)]
    out = bt.dedup_monthly(ts)
    assert len(out) == 3
    assert [t["net_r"] for t in out if t["code"] == "A" and t["signal_date"].startswith("2020-01")] == [-1]


def test_cluster_ci_is_deterministic_and_brackets_the_mean():
    rng = random.Random(1)
    ts = [mk(f"20{12 + m // 12}-{m % 12 + 1:02d}", f"C{k}", rng.gauss(0.2, 1.0)) for m in range(96) for k in range(4)]
    a = bt.cluster_bootstrap_ci(ts, n=2000)
    assert a == bt.cluster_bootstrap_ci(ts, n=2000)
    mean = sum(t["net_r"] for t in ts) / len(ts)
    assert a[0] < mean < a[1]


def synthetic_set(mean, n_months=100, per_month=4, seed=3, sd=0.5):
    rng = random.Random(seed)
    out = []
    for m in range(n_months):
        y, mo = 2012 + (m + 8) // 12, (m + 8) % 12 + 1
        for k in range(per_month):
            out.append(mk(f"{y}-{mo:02d}", f"C{m}_{k}", rng.gauss(mean, sd)))
    return out


def test_verdict_pass_fail_and_insufficient():
    ctrl = synthetic_set(0.0, n_months=130, seed=4)
    ok = bt.evaluate_criteria(synthetic_set(0.3, n_months=130), ctrl, n=500)
    assert ok["verdict"] == "通過" and all(ok[k] for k in ("c1_sample", "c2_mean", "c3_ci", "c4_vs_control", "c5_halves"))
    bad = bt.evaluate_criteria(synthetic_set(0.02, n_months=130), ctrl, n=500)
    assert bad["verdict"] == "不通過" and not bad["c2_mean"]
    small = bt.evaluate_criteria(synthetic_set(0.3, n_months=20), ctrl, n=500)
    assert small["verdict"] == "樣本不足"


def test_negative_half_fails_even_when_overall_mean_is_high():
    early = [mk(f"2013-{m:02d}", f"E{m}_{k}", -0.2) for m in range(1, 13) for k in range(30)]
    late = [mk(f"2021-{m:02d}", f"L{m}_{k}", 1.5) for m in range(1, 13) for k in range(30)]
    r = bt.evaluate_criteria(early + late, synthetic_set(0.0), n=300)
    assert r["c5_halves"] is False and r["verdict"] != "通過"


def test_stop_groups_boundaries_and_small_group_note():
    ts = [mk("2020-01", f"a{k}", 0.1, stop_pct=1.4) for k in range(40)]
    ts += [mk("2020-02", "b", 0.1, stop_pct=2.5), mk("2020-02", "c", 0.1, stop_pct=3.0), mk("2020-02", "d", 0.1, stop_pct=3.01)]
    g = bt.stop_distance_groups(ts, n=200)
    assert g["<1.5%"]["all"]["n"] == 40 and "ci" in g["<1.5%"]["all"]
    assert g["1.5-2.5%"]["all"]["n"] == 1 and g["1.5-2.5%"]["all"]["note"] == "樣本不足"    # 2.5 belongs to the lower group
    assert g["2.5-3%"]["all"]["n"] == 1                                                    # 3.0 belongs to the lower group
    assert g[">3%"]["all"]["n"] == 1


# ------------------------------------------------------------ data prep ----

def test_adjust_cache_bars_scales_ohl_and_excludes_incomplete_codes():
    raw = [{"date": "2020-01-02", "o": 10, "h": 12, "l": 9, "c": 11, "c_adj": 5.5, "v": 100},
           {"date": "2020-01-03", "o": 11, "h": 13, "l": 10, "c": 12, "c_adj": 12, "v": 100}]
    adj = bt.adjust_cache_bars(raw)
    assert adj[0]["o"] == 5.0 and adj[0]["h"] == 6.0 and adj[0]["c"] == 5.5 and adj[0]["v"] == 100
    assert adj[1]["h"] == 13
    raw[1].pop("o")
    assert bt.adjust_cache_bars(raw) is None
    raw[1]["o"] = 11
    raw[0].pop("c_adj")
    assert bt.adjust_cache_bars(raw) is None
    raw[0]["c_adj"] = 5.5
    raw[0]["l"] = 0  # explicit zero low must exclude the code, not pass as a price
    assert bt.adjust_cache_bars(raw) is None


def test_view_behaves_like_a_prefix_list():
    base = list(range(10))
    v = bt._View(base, 6)
    assert len(v) == 6 and v[-1] == 5 and v[0] == 0 and v[2:5] == [2, 3, 4] and v[-3:] == [3, 4, 5]
    assert list(v) == [0, 1, 2, 3, 4, 5]
    with pytest.raises(IndexError):
        v[6]


def test_params_diff_flags_a_changed_parameter():
    assert bt.params_diff() == []
    changed = {**ps.PULLBACK_PARAMS, "stop_atr_mult": 0.6}
    assert bt.params_diff(changed) == [("stop_atr_mult", 0.5, 0.6)]


# --------------------------------------------- scanning: no look-ahead ----

def synthetic_universe(n_codes=14, n_bars=420, seed=11):
    rng = random.Random(seed)
    raw, adj = {}, {}
    for k in range(n_codes):
        price = 50 + 10 * k
        drift = 0.004 if k % 3 == 0 else 0.0003
        rows = []
        for d in range(n_bars):
            ret = rng.gauss(drift, 0.02)
            if 150 < d < 260 and k % 3 == 0:
                ret = rng.gauss(-0.001, 0.012)               # pause after an advance -> pullback candidates
            o = price
            price *= 1 + ret
            h, l = max(o, price) * 1.01, min(o, price) * 0.99
            rows.append({"date": (dt.date(2015, 1, 1) + dt.timedelta(days=d)).isoformat(),
                         "o": round(o, 2), "h": round(h, 2), "l": round(l, 2), "c": round(price, 2),
                         "c_adj": round(price, 2), "v": 2_000_000})
        raw[f"{1000 + k}"] = rows
        adj[f"{1000 + k}"] = bt.adjust_cache_bars(rows)
    return raw, adj


def test_windowed_history_matches_full_history():
    raw, adj = synthetic_universe(n_bars=900)
    for code, bars in adj.items():
        for e in (600, 750, 899):
            full = ps.evaluate_setup(bars[:e])
            win = ps.evaluate_setup(bars[e - bt.HISTORY_WINDOW:e])
            assert full.get("passed") == win.get("passed")
            assert full.get("status") == win.get("status")
            assert (full.get("zone") or {}).get("low") == (win.get("zone") or {}).get("low")
            assert (full.get("zone") or {}).get("high") == (win.get("zone") or {}).get("high")


def test_signals_on_or_before_T_ignore_all_later_bars():
    raw, adj = synthetic_universe()
    full_sigs, full_strong = bt.scan_signals(raw, adj)
    cut = 330
    T = list(adj.values())[0][cut]["date"]
    raw_cut = {c: v[:cut + 1] for c, v in raw.items()}
    adj_cut = {c: v[:cut + 1] for c, v in adj.items()}
    cut_sigs, cut_strong = bt.scan_signals(raw_cut, adj_cut)
    assert [s for s in full_sigs if s["date"] <= T] == cut_sigs
    assert {d: c for d, c in full_strong.items() if d <= T} == cut_strong
    assert len(full_strong) > 0


# ---------------------------------------------------- lock guard (CLI) ----

def _load_cli():
    import importlib.util
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "run_pullback_backtest.py")
    spec = importlib.util.spec_from_file_location("run_pullback_backtest", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LOCK_BLANK = """## 8. 鎖定

- [ ] 回補已完成：______
- [ ] §3 的 4 檔排除清單已補齊：3081、2344、______、______
- [ ] 鎖定時 `src/pullback_strategy.py` 所在 commit：______
"""
LOCK_DONE = """## 8. 鎖定

- [x] 回補已完成，ohl=2100/2100
- [x] §3 的 4 檔排除清單已補齊：3081、2344、1234、5678
- [x] 鎖定時 `src/pullback_strategy.py` 所在 commit：abc1234
鎖定日期：2026-10-20
"""


def test_cli_refuses_unlocked_and_parses_locked(tmp_path):
    cli = _load_cli()
    blank = tmp_path / "blank.md"
    blank.write_text(LOCK_BLANK, encoding="utf-8")
    with pytest.raises(SystemExit):
        cli.read_lock(str(blank))
    done = tmp_path / "done.md"
    done.write_text(LOCK_DONE, encoding="utf-8")
    commit, excluded = cli.read_lock(str(done))
    assert commit == "abc1234" and excluded == ["3081", "2344", "1234", "5678"]


def test_real_preregistration_is_still_unlocked():
    """The checked-in document must stay unlocked until the user fills section 8."""
    import os
    cli = _load_cli()
    if "______" not in open(cli.DEFAULT_PREREG, encoding="utf-8").read().split("## 8. 鎖定")[-1]:
        pytest.skip("pre-registration has been locked")
    with pytest.raises(SystemExit):
        cli.read_lock(cli.DEFAULT_PREREG)


def test_cli_end_to_end_on_synthetic_cache(tmp_path, monkeypatch, capsys):
    """Whole pipeline once on a fake cache and a fake 'locked' document."""
    import json
    cli = _load_cli()
    raw, _ = synthetic_universe()
    cache = tmp_path / "factor_run"
    cache.mkdir()
    for code, rows in raw.items():
        (cache / f"{code}.json").write_text(json.dumps({"bars": rows}), encoding="utf-8")
    prereg = tmp_path / "prereg.md"
    prereg.write_text(LOCK_DONE, encoding="utf-8")
    out = tmp_path / "result.json"
    monkeypatch.setattr(cli, "CACHE_DIR", str(cache))
    monkeypatch.setattr(cli, "BENCH_PATH", str(tmp_path / "none.json"))
    monkeypatch.setattr(cli, "check_locked_strategy", lambda commit: None)
    monkeypatch.setattr(sys, "argv", ["x", "--prereg", str(prereg), "--bootstrap-n", "100", "--out", str(out)])
    cli.main()
    text = capsys.readouterr().out
    assert "主要設定" in text and "附錄 A" in text and "結論：樣本不足" in text
    assert json.loads(out.read_text(encoding="utf-8"))["criteria"]["verdict"] == "樣本不足"
