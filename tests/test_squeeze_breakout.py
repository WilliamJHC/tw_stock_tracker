# -*- coding: utf-8 -*-
"""Synthetic-data tests for src/squeeze_breakout.py and scripts/run_squeeze_backtest.py.
Nothing here reads the real factor cache (pre-registration section 8: no
real-data backtest before the lock). Stop / exit / limit / cost rules are
ep_strategy's and are tested in tests/test_ep_strategy.py; here only the signal,
the strength screen, the control pool and the wiring are tested."""
import datetime as dt
import os
import random

import pytest

import ep_strategy as ep
import pullback_backtest as bt
import pullback_strategy as ps
import squeeze_breakout as sq


def day(k):
    return (dt.date(2020, 1, 1) + dt.timedelta(days=k)).isoformat()


def bar(k, o, h, l, c, v=1_000_000):
    return {"date": day(k), "o": o, "h": h, "l": l, "c": c, "v": v}


def trend_bars(n=80, growth=0.002, amp=0.02):
    """n rising bars (+0.2%/bar), each ranging +/-amp/2 around the close. After
    n >= 50 bars: close > EMA21 > EMA50 and EMA9 within 2% of EMA21."""
    out, c = [], 100.0
    for k in range(n):
        prev = c
        c = prev * (1 + growth)
        out.append(bar(k, prev, c * (1 + amp / 2), c * (1 - amp / 2), c))
    return out


def with_setup(kind, n=80):
    """trend_bars(n) then one setup bar s (index n). kind: "inside" | "squeeze" | "none" | "both"."""
    bars = trend_bars(n)
    last = bars[-1]
    if kind in ("inside", "both"):
        c = last["c"] * 1.001
        bars.append(bar(n, last["c"], last["h"] - 0.01, last["l"] + 0.01, c))     # inside the previous bar
    else:
        c = last["c"] * 1.002
        bars.append(bar(n, last["c"], c * 1.011, c * 0.989, c))                    # normal 2.2% range, not inside
    return bars


def squeeze_tail(bars, ratio=0.5, k=5):
    """Shrink the ranges of the last k bars to `ratio` of the normal amplitude (the earlier
    bars keep 0.02 of the previous close), keeping each bar's close."""
    out = [dict(b) for b in bars]
    for i in range(len(out) - k, len(out)):
        prev_c = out[i - 1]["c"]
        half = prev_c * 0.02 * ratio / 2
        out[i]["h"], out[i]["l"] = out[i]["c"] + half, out[i]["c"] - half
    return out


ALL = set(day(k) for k in range(200))


def scan(bars, strong=ALL, **kw):
    return sq.scan_code(bars, strong, **kw)


def scan_last(bars, strong=ALL, **kw):
    """Signals on the final bar only (earlier bars of a synthetic squeeze can qualify too)."""
    return sq.scan_code(bars, strong, start_date=bars[-1]["date"], **kw)


# ----------------------------------------------------------- pattern rules ----

def test_inside_day_is_a_signal_and_labelled():
    bars = with_setup("inside")
    s = scan(bars)
    assert [x["date"] for x in s] == [bars[-1]["date"]]
    assert s[0]["pattern"] in ("inside", "both")


def test_not_inside_and_not_squeezed_is_no_signal():
    assert scan(with_setup("none")) == []


def test_inside_day_needs_strict_inequalities():
    bars = with_setup("inside")
    bars[-1]["h"] = bars[-2]["h"]                       # high equal to the prior high: not inside
    assert [x for x in scan(bars) if x["pattern"] in ("inside", "both")] == []
    bars = with_setup("inside")
    bars[-1]["l"] = bars[-2]["l"]
    assert [x for x in scan(bars) if x["pattern"] in ("inside", "both")] == []


def test_range_contraction_fires_at_exact_ratio_and_not_above():
    bars = squeeze_tail(with_setup("none"), ratio=0.5)
    s = scan_last(bars)
    assert len(s) == 1 and s[0]["pattern"] == "squeeze" and s[0]["range_ratio"] < 0.7
    r = s[0]["range_ratio"]
    assert len(scan_last(bars, params={"squeeze_ratio_max": r})) == 1            # ratio exactly at the limit: signal
    assert scan_last(bars, params={"squeeze_ratio_max": r - 1e-6}) == []         # just above the limit: none
    assert scan_last(squeeze_tail(with_setup("none"), ratio=0.8)) == []          # default 0.7 limit


def test_range_ratio_definition_is_recent5_over_previous20():
    bars = squeeze_tail(with_setup("none"), ratio=0.5)
    s = len(bars) - 1
    for k, mult in ((s - 25, 7.0), (s - 24, 3.0), (s - 5, 2.0), (s - 4, 0.3)):   # distinct amplitudes at both window edges
        mid = bars[k]["c"]
        half = bars[k - 1]["c"] * 0.02 * mult / 2
        bars[k]["h"], bars[k]["l"] = mid + half, mid - half

    def mean_amp(lo, hi):                                                        # bars lo..hi inclusive
        return sum((bars[k]["h"] - bars[k]["l"]) / bars[k - 1]["c"] for k in range(lo, hi + 1)) / (hi - lo + 1)

    want = mean_amp(s - 4, s) / mean_amp(s - 24, s - 5)                          # bars s-4..s over bars s-24..s-5
    assert scan_last(bars, params={"squeeze_ratio_max": 1e9})[0]["range_ratio"] == pytest.approx(want)


def test_both_label_when_inside_and_squeezed():
    bars = squeeze_tail(with_setup("inside"), ratio=0.4)
    bars[-1]["h"] = bars[-2]["h"] - 0.001
    bars[-1]["l"] = bars[-2]["l"] + 0.001
    s = scan_last(bars)
    assert len(s) == 1 and s[0]["pattern"] == "both"


# ----------------------------------------------------------- trend rules ----

RELAX_GAP = {"ema_gap_max_pct": 100.0}          # isolate the close > EMA21 > EMA50 conditions


def _emas(bars):
    cl = [b["c"] for b in bars]
    return tuple(ps.ema_series(cl, n)[-1] for n in (9, 21, 50))


def _inside_after_wide_bar(bars, close_factor):
    """Widen the last bar, then append an inside setup bar closing at close_factor x that bar's close."""
    bars = [dict(b) for b in bars]
    c = bars[-1]["c"]
    bars[-1]["h"], bars[-1]["l"] = c * 1.15, min(bars[-1]["l"], c * 0.85)
    n = len(bars)
    bars.append(bar(n, c, bars[-1]["h"] - 0.01, bars[-1]["l"] + 0.01, c * close_factor))
    return bars


def test_close_must_be_above_ema21():
    up = trend_bars(80)
    ok = _inside_after_wide_bar(up, 1.0)                                  # positive control: same shape, close above EMA21
    assert len(scan_last(ok, params=RELAX_GAP)) == 1
    weak = _inside_after_wide_bar(up, 0.93)
    _, e21, e50 = _emas(weak)
    assert weak[-1]["h"] < weak[-2]["h"] and weak[-1]["l"] > weak[-2]["l"]    # still an inside day
    assert weak[-1]["c"] < e21 and e21 > e50                                  # only close > EMA21 fails
    assert scan_last(weak, params=RELAX_GAP) == []


def test_ema21_must_be_above_ema50():
    down = _inside_after_wide_bar(trend_bars(80, growth=-0.002), 1.10)       # bounce: close well above EMA21
    _, e21, e50 = _emas(down)
    assert down[-1]["h"] < down[-2]["h"] and down[-1]["l"] > down[-2]["l"]
    assert down[-1]["c"] > e21 and e21 < e50                                  # only EMA21 > EMA50 fails
    assert scan_last(down, params=RELAX_GAP) == []


def test_ema9_ema21_gap_limit_is_two_percent():
    bars = with_setup("inside")
    e9, e21, _ = _emas(bars)
    gap = abs(e9 - e21) / e21 * 100
    assert gap < 2.0
    assert len(scan(bars, params={"ema_gap_max_pct": gap + 0.001})) == 1       # at/above the gap: signal
    assert scan(bars, params={"ema_gap_max_pct": gap - 0.001}) == []           # just under: no signal


def test_ema_gap_uses_absolute_value_when_ema9_is_below_ema21():
    bars = trend_bars(80, growth=0.006)
    c = bars[-1]["c"]
    for k in range(12):                                                        # sharp dip drags EMA9 under EMA21
        c *= 0.98
        bars.append(bar(80 + k, c / 0.98, c * 1.01, c * 0.99, c))
    c = _emas(bars)[1] * 1.002                                                 # final bar closes just above EMA21
    bars.append(bar(92, bars[-1]["c"], c * 1.01, c * 0.99, c))
    e9, e21, e50 = _emas(bars)
    assert bars[-1]["c"] > e21 > e50 and (e9 - e21) / e21 * 100 < -2.0         # EMA9 more than 2% BELOW EMA21
    loose = {"squeeze_ratio_max": 1e9}                                         # make the pattern condition always true
    assert len(scan_last(bars, params={**loose, **RELAX_GAP})) == 1
    assert scan_last(bars, params=loose) == []                                 # default 2% limit rejects it


def test_needs_fifty_bars_of_history():
    bars = with_setup("inside", n=45)
    assert scan(bars) == []


# ------------------------------------------------------- strength gating ----

def test_signal_requires_the_strength_screen_on_that_day():
    bars = with_setup("inside")
    assert scan(bars, strong=set()) == []
    assert len(scan(bars, strong={bars[-1]["date"]})) == 1
    assert scan(bars, strong={bars[-2]["date"]}) == []                         # a different day does not count


def test_signals_ignore_later_bars():
    bars = with_setup("inside")
    later = bars + trend_bars(40)[:0] + [bar(81 + k, 1.0, 1000.0, 0.5, 5.0) for k in range(10)]
    assert scan(later)[0]["date"] == scan(bars)[0]["date"]
    assert [x["date"] for x in scan(later) if x["date"] <= bars[-1]["date"]] == [bars[-1]["date"]]


def test_start_end_date_window():
    bars = with_setup("inside")
    d = bars[-1]["date"]
    assert scan(bars, start_date=d) != [] and scan(bars, start_date=day(10_000)) == []
    assert scan(bars, end_date=day(0)) == []


def test_scan_signals_only_common_stocks_and_sorted():
    bars = with_setup("inside")
    d = bars[-1]["date"]
    uni = {"2330": bars, "1101": [dict(b) for b in bars], "0050": [dict(b) for b in bars]}
    strong = {d: ["0050", "1101", "2330"]}
    sig = sq.scan_signals(uni, strong)
    assert [(s["date"], s["code"]) for s in sig] == [(d, "1101"), (d, "2330")]


def _market(seed, codes=40, n=220):
    """Random-walk universe in the factor-cache shape (c, c_adj, v) with different drifts,
    plus a code with too little turnover and a code with a short history."""
    rng = random.Random(seed)
    raw = {}
    for i in range(codes):
        c, bars = 50.0 + i, []
        drift = rng.uniform(-0.002, 0.012)
        for k in range(n if i != codes - 1 else 90):
            c *= 1 + drift + rng.uniform(-0.03, 0.03)
            v = rng.randint(200_000, 3_000_000) if i != 0 else 1000          # code 0: illiquid
            bars.append({"date": day(k), "c": c, "c_adj": c * (0.9 if k < 60 else 1.0), "v": v})
        raw[f"{1000 + i}"] = bars
    return raw


def test_strong_by_day_reproduces_pullback_strength_screen_every_day():
    raw = _market(7)
    uni = {c: [{"date": b["date"], "o": b["c"], "h": b["c"], "l": b["c"], "c": b["c_adj"], "v": b["v"]} for b in bars]
           for c, bars in raw.items()}
    got = sq.strong_by_day(raw, uni)
    dates = sorted({b["date"] for bars in raw.values() for b in bars})
    checked = 0
    for d in dates:
        views = {}
        for code, bars in raw.items():
            ds = [b["date"] for b in bars]
            if d in ds:
                views[code] = {"bars": bt._View(bars, ds.index(d) + 1)}
        want = sorted(ps.strength_screen(views, sq_ps_params()))
        assert got.get(d, []) == want, d
        checked += bool(want)
    assert checked > 20                                                         # the comparison was not vacuous


def sq_ps_params():
    return {"min_turnover": sq.SQUEEZE_PARAMS["min_turnover"], "turnover_window": sq.SQUEEZE_PARAMS["turnover_window"],
            "return_windows": sq.SQUEEZE_PARAMS["return_windows"], "min_return_pct": sq.SQUEEZE_PARAMS["min_return_pct"],
            "top_rank_pct": sq.SQUEEZE_PARAMS["top_rank_pct"]}


def test_screen_values_equal_pullback_params():
    p = ps.PULLBACK_PARAMS
    for k in ("return_windows", "min_return_pct", "top_rank_pct", "min_turnover", "turnover_window"):
        assert sq.SQUEEZE_PARAMS[k] == p[k], k


def test_strong_by_day_ignores_future_bars():
    raw = _market(3)
    uni = {c: [{"date": b["date"], "o": b["c"], "h": b["c"], "l": b["c"], "c": b["c_adj"], "v": b["v"]} for b in bars]
           for c, bars in raw.items()}
    full = sq.strong_by_day(raw, uni)
    cut = {c: bars[:150] for c, bars in raw.items()}
    ucut = {c: bars[:150] for c, bars in uni.items()}
    short = sq.strong_by_day(cut, ucut)
    for d, codes in short.items():
        assert full[d] == codes


def test_strong_by_day_uses_raw_turnover_not_adjusted():
    raw = _market(5, codes=12)
    illiquid = "1000"                                                          # raw close x volume far under 5,000 万
    uni = {c: [{"date": b["date"], "o": 1, "h": 1, "l": 1, "c": b["c_adj"], "v": b["v"]} for b in bars]
           for c, bars in raw.items()}
    base = sq.strong_by_day(raw, uni)
    assert all(illiquid not in codes for codes in base.values())
    for b in raw[illiquid]:                                                    # huge c_adj: returns unchanged, would be "liquid" if c_adj were used
        b["c_adj"] *= 1e6
    assert sq.strong_by_day(raw, uni) == base


def _flat_code(final, n=30, base=100.0, v=1_000_000):
    bars = [{"date": day(k), "c": base, "c_adj": base, "v": v} for k in range(n - 1)]
    bars.append({"date": day(n - 1), "c": final, "c_adj": final, "v": v})
    return bars


def _screen_both(raw, params=None):
    uni = {c: [{"date": b["date"], "o": b["c"], "h": b["c"], "l": b["c"], "c": b["c_adj"], "v": b["v"]} for b in bars]
           for c, bars in raw.items()}
    got = sq.strong_by_day(raw, uni, params)
    d = day(29)
    views = {c: {"bars": bt._View(bars, len(bars))} for c, bars in raw.items()}
    merged = {**sq_ps_params(), **(params or {})}
    return got.get(d, []), sorted(ps.strength_screen(views, merged))


def test_screen_rank_and_return_boundaries_match_pullback():
    # ten liquid codes, only the 1m window has history; returns 0,10,...,90 -> ranks 0.1,...,1.0
    raw = {f"{2000 + i}": _flat_code(100.0 * (1 + i / 10)) for i in range(10)}
    got, want = _screen_both(raw)
    assert got == want == ["2006", "2007", "2008", "2009"]                     # rank 0.7 is included (>=)
    # return must be STRICTLY above the threshold: 60% at min_return_pct=60 is out, 70% is in
    got, want = _screen_both(raw, {"min_return_pct": 60.0})
    assert got == want == ["2007", "2008", "2009"]
    # ties share the average rank: two codes tied at the top both get rank 0.95 and qualify
    tied = {f"{3000 + i}": _flat_code(100.0 + 10 * i) for i in range(8)}
    tied["3008"], tied["3009"] = _flat_code(190.0), _flat_code(190.0)
    got, want = _screen_both(tied)
    assert got == want and {"3008", "3009"} <= set(got)


# ------------------------------------------------------ wiring with EP code ----

def test_signal_bar_convention_matches_ep_trade_rules():
    """Entry at setup-bar high + 1 tick on the next bar, stop at setup-bar low - 1 tick
    widened to 3% (ep_strategy.simulate_trade is reused unchanged)."""
    bars = with_setup("inside")
    s = len(bars) - 1
    hi, lo = bars[s]["h"], bars[s]["l"]
    trig = ps.round_to_tick(hi + ps.tw_tick_size(hi), "up")
    nxt_o = trig - 0.01
    bars.append(bar(s + 1, nxt_o, trig + 1, nxt_o - 0.1, trig + 0.5))
    bars += [bar(s + 2 + k, trig + 0.5, trig + 1, trig, trig + 0.6) for k in range(40)]
    t = ep.simulate_trade(bars, s, "ep")
    assert t["fill"] == pytest.approx(trig) and t["entry_kind"] == "trigger"
    assert t["signal_date"] == bars[s]["date"]
    assert t["stop_pct"] >= 3.0 - 1e-6


def test_control_pool_is_the_strong_set_on_signal_days_only():
    strong = {"2020-01-02": ["1101", "2330"], "2020-01-03": ["1101"], "2020-01-06": []}
    pool = sq.control_pool(strong, ["2020-01-03", "2020-01-02", "2020-01-02", "2020-01-09", "2020-01-06"])
    assert pool == {"2020-01-02": ["1101", "2330"], "2020-01-03": ["1101"]}


def test_pattern_groups_split_and_small_groups_report_no_ci():
    signals = [{"code": "1101", "date": "2020-01-02", "pattern": "inside"},
               {"code": "2330", "date": "2020-01-03", "pattern": "squeeze"}]
    trades = [{"code": "1101", "signal_date": "2020-01-02", "net_r": 1.0},
              {"code": "2330", "signal_date": "2020-01-03", "net_r": -1.0}]
    g = sq.pattern_groups(trades, signals, n=50)
    assert g["inside"]["n"] == 1 and g["inside"]["note"] == "樣本不足" and g["inside"]["mean_net_r"] == 1.0
    assert g["squeeze"]["n"] == 1 and g["both"]["n"] == 0


# --------------------------------------------------- pre-registration guard ----

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREREG = os.path.join(REPO, "docs", "squeeze_breakout_preregistration_2026-10.md")


def test_every_param_is_named_in_the_preregistration():
    text = open(PREREG, encoding="utf-8").read()
    missing = [k for k in list(sq.SQUEEZE_PARAMS) + list(ep.EP_PARAMS) if f"`{k}`" not in text]
    assert missing == []
    for n in range(1, 9):
        assert f"## {n}. " in text


def _load_cli():
    import importlib.util
    import sys
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    path = os.path.join(REPO, "scripts", "run_squeeze_backtest.py")
    spec = importlib.util.spec_from_file_location("run_squeeze_backtest", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_locked_files_cover_every_module_that_decides_the_result():
    cli = _load_cli()
    for f in ("src/squeeze_breakout.py", "src/ep_strategy.py", "src/pullback_backtest.py",
              "src/pullback_strategy.py", "src/finlab_factors.py", "scripts/run_ep_backtest.py", "scripts/run_squeeze_backtest.py"):
        assert f in cli.LOCKED_FILES
        assert os.path.exists(os.path.join(REPO, f))


def test_cli_refuses_an_unlocked_preregistration(tmp_path):
    cli = _load_cli()
    blank = tmp_path / "blank.md"
    blank.write_text("## 8. 鎖定\n\n- [ ] 量測：______\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        cli.epr.read_lock(str(blank))


def test_cli_main_refuses_the_real_preregistration_until_locked(monkeypatch):
    cli = _load_cli()
    sec = open(cli.DEFAULT_PREREG, encoding="utf-8").read().split("## 8. 鎖定")[-1]
    if "______" not in sec:
        pytest.skip("pre-registration has been locked")
    monkeypatch.setattr("sys.argv", ["run_squeeze_backtest.py"])
    with pytest.raises(SystemExit):
        cli.main()
