# -*- coding: utf-8 -*-
"""Tests for src/shadow_portfolio.py (the ledger state machine).

Synthetic data only: a hand-made 40-code universe, constant or gently drifting prices,
no network.  Nothing here backtests S2/S3 (spec A5): the "year run" only checks that the
books balance and the mechanics fire.  Every step of every test goes through ``Sim.day``,
which asserts the nav identity (sum(holdings) + cash == nav) and that state / inputs /
rows survive a JSON round trip."""
import copy
import json
import math
from datetime import date, timedelta

import pytest

import metrics
import shadow_portfolio as sp
import shadow_rules as sr

# --------------------------------------------------------------------------- #
# Synthetic world
# --------------------------------------------------------------------------- #

CODES = [f"{1000 + i}" for i in range(40)]
PRIMARY = {c: f"IND{i % 5}" for i, c in enumerate(CODES)}          # 8 codes per industry, spread
BLOCKS = {c: "ABCDE"[i // 8] for i, c in enumerate(CODES)}         # A: 1000-1007, B: 1008-1015, ...
S0, S1, S2, S3 = sp.CANDIDATES
START = date(2026, 12, 1)


def make_calendar(start, end, holidays=()):
    hol = set(holidays)
    out, d = [], start
    while d <= end:
        if d.weekday() < 5 and d not in hol:
            out.append(d)
        d += timedelta(days=1)
    return out


CAL = make_calendar(date(2026, 11, 1), date(2028, 2, 29), holidays=[date(2027, 1, 1)])
CAL_ISO = [d.isoformat() for d in CAL]


def trading_days(a, b):
    return [d for d in CAL if a <= d <= b]


def px(overrides=None, base=100.0):
    """Whole-market closes: `base` everywhere; overrides {code: price | None (no_close) |
    "bad" (bad_row) | "absent"}."""
    out = {c: {"c": base, "status": "ok"} for c in CODES}
    for code, v in (overrides or {}).items():
        if v is None:
            out[code] = {"c": None, "status": "no_close"}
        elif v == "bad":
            out[code] = {"c": None, "status": "bad_row"}
        elif isinstance(v, tuple):                      # ("exright", price)
            out[code] = {"c": v[1], "status": v[0]}
        elif v == "absent":
            del out[code]
        else:
            out[code] = {"c": v, "status": "ok"}
    return out


def default_rank(name, d, shift=0):
    ranked = CODES[shift:] + CODES[:shift]
    return {"asof": sr.prev_trading_day(CAL, d).isoformat(), "ranked": ranked,
            "detail": [{"code": c, "score": 1.0 - 0.01 * i} for i, c in enumerate(ranked[:3])]}


class Sim:
    """Drives `step` day by day; asserts invariants and JSON round trips on every step."""

    def __init__(self, state=None, primary=None, start=START, cal_iso=None):
        self.cal_iso = cal_iso or CAL_ISO
        self.state = state if state is not None else sp.new_state(start, CAL, primary or PRIMARY)
        self.rows = []
        self.log = []                      # (date, rows) per step

    def inputs(self, d, prices, adj, bench, s2, rankings, written_at):
        return {"date": d.isoformat(), "calendar": self.cal_iso, "prices": prices, "adj": adj or {},
                "benchmark_close": bench, "rankings": rankings or {}, "s2_status": s2,
                "written_at": written_at or f"{d.isoformat()}T07:00:00"}

    def day(self, d, prices="default", adj=None, bench=100.0, s2="default", rank_fn=None,
            rankings=None, written_at=None):
        if isinstance(prices, str) and prices == "default":
            prices = px()
        if isinstance(s2, str):                          # "default"; pass s2=None for "not supplied"
            s2 = {"complete": False, "ratio": 0.5}
        if rankings is None:
            fn = rank_fn or (lambda name, dd: default_rank(name, dd))
            rankings = {c: fn(c, d) for c in sp.rebalance_due(self.state, d, self.cal_iso, s2)}
        inp = json.loads(json.dumps(self.inputs(d, prices, adj, bench, s2, rankings, written_at)))
        before = json.loads(json.dumps(self.state))
        new_state, rows = sp.step(before, inp)
        assert before == self.state                      # input state not mutated
        sp.check_invariants(new_state)
        new_state = json.loads(json.dumps(new_state))    # must be JSON clean
        rows = json.loads(json.dumps(rows))
        self.state = new_state
        self.rows.extend(rows)
        self.log.append((d, rows))
        return rows

    def run(self, days, **kw):
        out = []
        for d in days:
            out.extend(self.day(d, **kw))
        return out

    # -- accessors -----------------------------------------------------------
    def cand(self, name):
        return self.state["candidates"][name]

    def meta(self):
        return self.state["meta"]

    def rows_of(self, file, name=None, d=None):
        out = [r for r in self.rows if r["file"] == file and (name is None or r.get("candidate") == name)]
        if d is not None:
            iso = d.isoformat()
            out = [r for r in out if r.get("date") == iso or r.get("fill_date") == iso]
        return out


def started(primary=None):
    sim = Sim(primary=primary)
    sim.day(START)
    return sim


def daily(rows, name):
    return next(r for r in rows if r["file"] == "daily" and r["candidate"] == name)


def reb(rows, name):
    return [r for r in rows if r["file"] == "rebalances" and r["candidate"] == name]


START_COST = sr.buy_cost(sp.INITIAL_CAPITAL)


# --------------------------------------------------------------------------- #
# 2. Start day (A3, A5c-1, A5b-10)
# --------------------------------------------------------------------------- #

def test_start_day_builds_all_candidates_and_charges_buy_leg():
    sim = Sim()
    rows = sim.day(START)
    for name in sp.CANDIDATES:
        cs = sim.cand(name)
        assert sorted(cs["holdings"]) == CODES[:20]
        assert all(v == 50000.0 for v in cs["holdings"].values())
        assert cs["cash"] == pytest.approx(-START_COST, abs=1e-3)           # A5e-3: cost sits in cash
        assert cs["nav"] == pytest.approx(sp.INITIAL_CAPITAL - START_COST, abs=1e-3)
        row = daily(rows, name)
        assert row["ret"] == pytest.approx(cs["nav"] / 1e6 - 1, abs=1e-9)    # includes the build cost
        assert row["nav"] == cs["nav"]
        assert cs["rets"] == [row["ret"]]
        r = reb(rows, name)[0]
        assert r["kind"] == "start" and r["nominal_date"] == r["fill_date"] == "2026-12-01"
        assert r["cost"] == pytest.approx(START_COST, abs=1e-3)
        assert r["orders"]["sells"] == {} and len(r["orders"]["buys"]) == 20
    assert sim.meta()["status"] == "active"
    order = [r["file"] for r in rows]
    assert order == ["rebalances"] * 4 + ["daily"] * 4 + ["benchmark", "meta"]             # fixed row order
    assert list(rows[4]) == ["file", "candidate", "date", "written_at", "nav", "ret", "cash",
                             "holdings", "frozen", "flags", "supersedes"]


def test_start_day_benchmark_return_is_zero_and_unpaid():
    sim = Sim()
    rows = sim.day(START, bench=123.45)
    b = next(r for r in rows if r["file"] == "benchmark")
    assert b["ret"] == 0.0 and b["adj_close"] == 123.45 and b["flags"] == []
    assert sim.state["benchmark"]["start_close"] == 123.45


def test_start_day_s3_applies_the_sector_cap():
    sim = Sim(primary=BLOCKS)
    sim.day(START)
    assert sorted(sim.cand(S3)["holdings"]) == [c for i in range(5) for c in CODES[i * 8:i * 8 + 4]]
    assert sorted(sim.cand(S0)["holdings"]) == CODES[:20]       # uncapped candidates ignore industries


def test_before_start_step_is_a_noop():
    sim = Sim()
    before = copy.deepcopy(sim.state)
    rows = sim.day(date(2026, 11, 30))
    assert rows == [] and sim.state == before


def test_start_day_missing_postpones_everything_and_is_not_a_missing_day():
    sim = Sim()
    rows = sim.day(START, prices=None)
    assert [r["file"] for r in rows] == ["meta"]                 # nothing booked, only the meta
    assert sim.meta()["status"] == "not_started" and sim.meta()["missing_total"] == 0
    assert sim.meta()["start_wait"] == ["2026-12-01"]
    assert sim.meta()["pending_retry"][0]["until"] == "2026-12-04"
    rows = sim.day(date(2026, 12, 2))
    r = reb(rows, S0)[0]
    assert r["nominal_date"] == "2026-12-01" and r["fill_date"] == "2026-12-02"
    assert sim.meta()["status"] == "active" and sim.meta()["missing_total"] == 0
    assert sim.meta()["consecutive_missing"] == 0
    assert daily(rows, S1)["ret"] == pytest.approx(-START_COST / 1e6, abs=1e-9)


def test_start_deferred_more_than_10_days_moves_to_next_month():
    sim = Sim()
    for d in trading_days(date(2026, 12, 1), date(2026, 12, 15)):      # delay 0..10: still waiting
        sim.day(d, prices=None)
    assert sim.meta()["start_date"] == "2026-12-01" and not sim.meta()["start_history"]
    sim.day(date(2026, 12, 16), prices=None)                           # delay 11 > 10
    assert sim.meta()["start_date"] == "2027-01-04"
    assert sim.meta()["start_history"][0]["to"] == "2027-01-04"
    assert sim.meta()["judgment_date"] == "2027-12-31"                 # recomputed for the new start
    assert sim.meta()["missing_total"] == 0                            # still not counted as missing
    for d in trading_days(date(2026, 12, 17), date(2026, 12, 31)):
        assert sim.day(d) == []                                        # before the new start: no-op
    assert sim.meta()["status"] == "not_started"
    rows = sim.day(date(2027, 1, 4))
    assert reb(rows, S0)[0]["kind"] == "start" and sim.meta()["status"] == "active"


def test_start_needs_benchmark_and_every_ranking():
    sim = Sim()
    sim.day(START, bench=None)
    assert sim.meta()["status"] == "not_started"
    sim2 = Sim()
    sim2.day(START, rankings={S0: default_rank(S0, START)})
    assert sim2.meta()["status"] == "not_started"


# --------------------------------------------------------------------------- #
# 1. Daily valuation (A5b-1, A5c-1)
# --------------------------------------------------------------------------- #

def test_daily_valuation_scales_holdings_by_close_ratio():
    sim = started()
    nav0 = sim.cand(S0)["nav"]
    rows = sim.day(date(2026, 12, 2), prices=px({"1000": 110.0, "1001": 90.0}))
    r = daily(rows, S0)
    assert r["holdings"]["1000"] == 55000.0 and r["holdings"]["1001"] == 45000.0
    assert r["nav"] == pytest.approx(nav0, abs=1e-6)               # +5000 -5000
    assert r["ret"] == 0.0
    rows = sim.day(date(2026, 12, 3), prices=px({"1000": 121.0, "1001": 90.0}))   # chains off 110 -> 121
    assert daily(rows, S0)["holdings"]["1000"] == pytest.approx(60500.0, abs=1e-3)


def test_exdividend_day_at_reference_price_has_zero_return():
    sim = started()
    nav0 = sim.cand(S0)["nav"]
    rows = sim.day(date(2026, 12, 2), prices=px({"1000": 95.0}), adj={"1000": 0.95})   # ref = 100 * 0.95
    r = daily(rows, S0)
    assert r["holdings"]["1000"] == 50000.0 and r["nav"] == nav0 and r["ret"] == 0.0
    assert r["flags"] == []


def test_adj_missing_uses_raw_price_and_flags_only_held_codes():
    sim = started()
    rows = sim.day(date(2026, 12, 2), prices=px({"1000": 95.0}), adj={"1000": None, "1030": None})
    r = daily(rows, S0)
    assert r["holdings"]["1000"] == 47500.0                      # raw -5%: factor 1.0 used
    assert r["flags"] == ["adj_missing:1000"]                    # 1030 is not held
    nxt = sim.day(date(2026, 12, 3), prices=px({"1000": 95.0}))
    assert daily(nxt, S0)["holdings"]["1000"] == 47500.0 and daily(nxt, S0)["flags"] == []


def test_returns_are_frozen_in_the_rows_after_writing():
    sim = started()
    rows = sim.day(date(2026, 12, 2), prices=px({"1000": 110.0}))
    first = copy.deepcopy(daily(rows, S0))
    sim.day(date(2026, 12, 3), prices=px({"1000": 50.0}))
    assert sim.rows_of("daily", S0, date(2026, 12, 2))[0] == first


# --------------------------------------------------------------------------- #
# 3. Rebalance (A2, A5b-10, A5d-3)
# --------------------------------------------------------------------------- #

JAN4 = date(2027, 1, 4)


def to_jan3(sim):
    sim.run(trading_days(date(2026, 12, 2), date(2026, 12, 31)), s2={"complete": False, "ratio": 0.1})


def shifted(name, d):
    return default_rank(name, d, shift=3)             # drops 1000-1002, adds 1020-1022


def test_rebalance_s0_fills_at_close_and_charges_only_the_differences():
    sim = started()
    sim.run(trading_days(date(2026, 12, 2), date(2026, 12, 31)), rank_fn=shifted)   # S2's Dec rebalance
    nav_pre = sim.cand(S0)["nav"]
    cash_pre = sim.cand(S0)["cash"]
    rows = sim.day(JAN4, rank_fn=shifted)
    r = reb(rows, S0)[0]
    assert (r["nominal_date"], r["fill_date"], r["signal_asof"], r["kind"]) == \
           ("2027-01-04", "2027-01-04", "2026-12-31", "rebalance")
    assert r["targets"] == CODES[3:23]
    each = (nav_pre) / 20
    assert sorted(r["orders"]["sells"]) == sorted(CODES[:20])                     # 3 sold out, 17 trimmed
    assert sorted(r["orders"]["buys"]) == CODES[20:23]
    assert r["orders"]["sells"]["1000"] == 50000.0
    assert r["orders"]["sells"]["1010"] == pytest.approx(50000.0 - each, abs=1e-3)
    assert r["orders"]["buys"]["1020"] == pytest.approx(each, abs=1e-3)
    exp_cost = sr.sell_cost(sum(r["orders"]["sells"].values())) + sr.buy_cost(sum(r["orders"]["buys"].values()))
    assert r["cost"] == pytest.approx(exp_cost, abs=0.01)
    assert r["cost"] < 0.002 * nav_pre                                           # not a full sell + buy
    assert r["ranking"] and r["ranking"][0]["code"] == "1003"
    d = daily(rows, S0)
    assert sorted(d["holdings"]) == CODES[3:23]
    assert all(v == pytest.approx(each, abs=1e-3) for v in d["holdings"].values())
    assert d["cash"] == pytest.approx(-r["cost"], abs=0.01)                       # A5e-3
    assert d["nav"] == pytest.approx(nav_pre - r["cost"], abs=0.01)
    assert d["ret"] == pytest.approx(-r["cost"] / nav_pre, abs=1e-8)              # cost shows in the return
    assert cash_pre < 0                                                          # previous cost was already booked


def test_rebalance_same_ranking_trades_only_price_drift():
    sim = started()
    to_jan3(sim)
    nav_pre = sim.cand(S1)["nav"]
    rows = sim.day(JAN4)
    r = reb(rows, S1)[0]
    assert r["orders"]["buys"] == {} and set(r["orders"]["sells"]) == set(CODES[:20])
    assert r["cost"] == pytest.approx(sr.sell_cost(sum(r["orders"]["sells"].values())), abs=0.01)
    assert r["cost"] < 10.0


def test_rebalance_days_for_each_candidate():
    sim = started()
    to_jan3(sim)
    assert sp.rebalance_due(sim.state, JAN4, CAL_ISO, {"complete": False}) == [S0, S1, S3]   # S2 waits for revenue
    rows = sim.day(JAN4)
    assert {r["candidate"] for r in rows if r["file"] == "rebalances"} == {S0, S1, S3}
    nxt = sim.day(date(2027, 1, 5))
    assert [r for r in nxt if r["file"] == "rebalances"] == []


def test_rebalance_s3_caps_each_primary_industry_at_four():
    sim = Sim(primary=BLOCKS)
    sim.day(START)
    to_jan3(sim)

    def rank(name, d):                                 # best names first, many in the same block
        ranked = CODES[8:] + CODES[:8]                 # B.. E.., then A
        return {"asof": "2026-12-31", "ranked": ranked, "detail": []}

    rows = sim.day(JAN4, rank_fn=rank)
    r = reb(rows, S3)[0]
    assert r["targets"] == [c for i in (1, 2, 3, 4) for c in CODES[i * 8:i * 8 + 4]] + CODES[0:4]
    from collections import Counter
    assert max(Counter(BLOCKS[c] for c in r["targets"]).values()) == 4
    assert sorted(daily(rows, S0)["holdings"]) == sorted(CODES[8:28])          # uncapped S0 keeps the top 20


def test_ranked_code_without_a_close_today_is_skipped():
    sim = started()
    to_jan3(sim)

    def rank(name, d):
        return {"asof": "2026-12-31", "ranked": CODES[20:] + CODES[:20], "detail": []}

    rows = sim.day(JAN4, prices=px({"1020": None}), rank_fn=rank)
    r = reb(rows, S0)[0]
    assert "1020" not in r["targets"] and len(r["targets"]) == 20 and r["targets"][0] == "1021"


# --------------------------------------------------------------------------- #
# 4. S2 rebalance day (A1, A5b-2, A5c-4, A5e-4)
# --------------------------------------------------------------------------- #

def s2_dec(complete_on, ratio=0.93):
    """s2_status function: complete from the dates in `complete_on` (set of dates) onward-true only on those."""
    def fn(d):
        return {"complete": d in complete_on, "ratio": ratio if d in complete_on else 0.7}
    return fn


def run_s2_december(fn, missing=()):
    sim = Sim()
    sim.day(START, s2=fn(START))
    for d in trading_days(date(2026, 12, 2), date(2026, 12, 24)):
        sim.day(d, prices=None if d in missing else "default", s2=fn(d),
                rank_fn=lambda n, dd: default_rank(n, dd, shift=3))
    return sim


def s2_rebalance_rows(sim):
    return [r for r in sim.rows if r["file"] == "rebalances" and r["candidate"] == S2 and r["kind"] == "rebalance"]


def test_s2_rebalances_on_first_complete_day_from_the_12th():
    sim = run_s2_december(s2_dec({date(2026, 12, 14), date(2026, 12, 16)}))
    rows = s2_rebalance_rows(sim)
    assert len(rows) == 1                                           # later "complete" days do nothing
    assert rows[0]["fill_date"] == "2026-12-14" == rows[0]["nominal_date"]    # 12/12 is Saturday -> 12/14
    assert rows[0]["s2_coverage"] == {"ratio": 0.93, "complete_day": "2026-12-14", "forced": False}
    assert sim.meta()["s2"]["2026-12"]["nominal_date"] == "2026-12-14"
    assert sim.meta()["deferrals"] == []
    assert not [r for r in sim.rows if r["file"] == "rebalances" and r["candidate"] in (S0, S1, S3)
                and r["kind"] == "rebalance"]                       # the others wait for 1/4


def test_s2_later_completion_day():
    sim = run_s2_december(s2_dec({date(2026, 12, 16)}))
    assert [r["fill_date"] for r in s2_rebalance_rows(sim)] == ["2026-12-16"]


def test_s2_forced_day_on_a_weekend_moves_to_next_trading_day_and_records_coverage():
    sim = run_s2_december(s2_dec(set()))                            # never complete: 12/20 is Sunday
    rows = s2_rebalance_rows(sim)
    assert [r["fill_date"] for r in rows] == ["2026-12-21"]
    assert rows[0]["s2_coverage"] == {"ratio": 0.7, "complete_day": None, "forced": True}   # A5e-4
    assert sim.meta()["deferrals"] == []                            # waiting for revenue is not a deferral
    assert sim.meta()["impaired"] is False


def test_s2_complete_on_the_forced_day_itself_is_not_flagged_forced():
    sim = run_s2_december(s2_dec({date(2026, 12, 21)}))
    r = s2_rebalance_rows(sim)[0]
    assert r["s2_coverage"]["forced"] is False and r["s2_coverage"]["complete_day"] == "2026-12-21"


def test_s2_nominal_day_missing_defers_and_counts():
    sim = run_s2_december(s2_dec({date(2026, 12, 14)}), missing={date(2026, 12, 14)})
    r = s2_rebalance_rows(sim)[0]
    assert (r["nominal_date"], r["fill_date"]) == ("2026-12-14", "2026-12-15")
    assert sim.meta()["deferrals"] == [{"candidate": S2, "nominal_date": "2026-12-14",
                                        "executed": "2026-12-15", "trading_days": 1}]
    assert sim.meta()["missing_total"] == 1
    assert r["s2_coverage"]["complete_day"] == "2026-12-14"
    flags = {d.isoformat(): daily(rows, S2)["flags"] for d, rows in sim.log if d in (date(2026, 12, 14), date(2026, 12, 15))}
    assert flags["2026-12-14"] == ["missing_day", "rebalance_deferred"]
    assert flags["2026-12-15"] == ["rebalance_deferred"]


def test_s2_start_on_or_after_the_12th_counts_as_the_months_rebalance():
    sim = Sim(start=date(2027, 3, 1))
    sim.state["meta"]["start_date"] = "2027-03-15"
    sim.day(date(2027, 3, 15))
    assert sim.meta()["s2"]["2027-03"]["complete_day"] == "2027-03-15"
    sim.run(trading_days(date(2027, 3, 16), date(2027, 3, 31)), s2={"complete": True, "ratio": 1.0})
    assert s2_rebalance_rows(sim) == []


# --------------------------------------------------------------------------- #
# 5. Freeze, resume, delist (A5b-6, A5c-5, A5d-5, A5e-7/8)
# --------------------------------------------------------------------------- #

D2, D3, D4, D5 = (date(2026, 12, d) for d in (2, 3, 4, 7))


def test_freeze_starts_on_the_first_day_without_a_close():
    for kind in (None, "bad", "absent"):
        sim = started()
        nav0 = sim.cand(S0)["nav"]
        rows = sim.day(D2, prices=px({"1000": kind}))
        r = daily(rows, S0)
        assert r["frozen"] == ["1000"] and sim.state["frozen_days"] == {"1000": 1}
        assert r["holdings"]["1000"] == 50000.0 and r["nav"] == nav0 and r["flags"] == []
        assert "1000" in sim.cand(S0)["holdings"]                   # not sold


def test_freeze_counter_grows_and_clears_on_resume():
    sim = started()
    sim.run([D2, D3, D4], prices=px({"1000": None}))
    assert sim.state["frozen_days"] == {"1000": 3}
    rows = sim.day(D5, prices=px({"1000": 100.0}))
    assert sim.state["frozen_days"] == {} and daily(rows, S0)["frozen"] == []


def test_resume_after_suspension_includes_ex_rights_during_the_suspension():
    sim = started()
    sim.day(D2, prices=px({"1000": None}))
    sim.day(D3, prices=px({"1000": None}), adj={"1000": 0.9})        # ex-dividend while suspended
    sim.day(D4, prices=px({"1000": None}))
    assert sim.state["acc_factor"]["1000"] == pytest.approx(0.9)
    rows = sim.day(D5, prices=px({"1000": 90.0}))                    # resumes at the reference price
    assert daily(rows, S0)["holdings"]["1000"] == pytest.approx(50000.0, abs=1e-3)   # no fake drop
    assert sim.state["acc_factor"]["1000"] == 1.0
    rows = sim.day(date(2026, 12, 8), prices=px({"1000": 99.0}))
    assert daily(rows, S0)["holdings"]["1000"] == pytest.approx(55000.0, abs=1e-3)   # 99 / 90


def test_resume_day_own_factor_is_also_applied():
    sim = started()
    sim.day(D2, prices=px({"1000": None}))
    rows = sim.day(D3, prices=px({"1000": 95.0}), adj={"1000": 0.95})
    assert daily(rows, S0)["holdings"]["1000"] == pytest.approx(50000.0, abs=1e-3)


def test_frozen_holding_is_not_sold_and_takes_a_slot_at_rebalance():
    sim = started()
    to_jan3(sim)
    prices = px({"1000": None})
    rows = sim.day(JAN4, prices=prices)                              # frozen since 1/4, top-ranked
    r = reb(rows, S0)[0]
    assert r["frozen"] == ["1000"] and "1000" not in r["targets"] and len(r["targets"]) == 19
    assert r["targets"] == CODES[1:20]
    d = daily(rows, S0)
    assert d["holdings"]["1000"] == sim.rows_of("daily", S0, date(2026, 12, 31))[0]["holdings"]["1000"]
    assert len(d["holdings"]) == 20 and d["frozen"] == ["1000"]
    assert "1000" not in r["orders"]["sells"] and "1000" not in r["orders"]["buys"]
    # literal: nav before = 1,000,000 - 1,313.5 buy cost; each name gets (nav - frozen) / (20 - 1)
    each = 49930.8684                                                  # = (998686.5 - 50000) / 19
    assert all(d["holdings"][c] == each for c in CODES[1:20]) and d["holdings"]["1000"] == 50000.0
    assert d["cash"] == pytest.approx(-r["cost"], abs=0.02)


def test_frozen_holding_counts_toward_its_industry_cap_in_s3():
    sim = Sim(primary=BLOCKS)
    sim.day(START)                                                  # S3 holds 4 each of A..E
    to_jan3(sim)
    assert [c for c in sim.cand(S3)["holdings"] if BLOCKS[c] == "A"] == CODES[:4]
    rows = sim.day(JAN4, prices=px({"1000": None}))                 # block A member frozen
    r = reb(rows, S3)[0]
    assert r["frozen"] == ["1000"] and len(r["targets"]) == 19
    assert sum(1 for c in r["targets"] if BLOCKS[c] == "A") == 3     # frozen one takes an A slot
    assert [c for c in r["targets"] if BLOCKS[c] == "A"] == CODES[1:4]
    assert "1000" in daily(rows, S3)["holdings"]


def test_delisting_after_20_no_close_days_pays_only_the_sell_leg_and_keeps_cash():
    sim = started()
    nav0, cash0 = sim.cand(S0)["nav"], sim.cand(S0)["cash"]
    days = trading_days(date(2026, 12, 2), date(2026, 12, 29))       # 20 trading days
    assert len(days) == 20
    for d in days[:19]:
        sim.day(d, prices=px({"1000": None}))
    assert "1000" in sim.cand(S0)["holdings"] and sim.state["frozen_days"]["1000"] == 19
    rows = sim.day(days[19], prices=px({"1000": None}))
    sell = sr.sell_cost(50000.0)
    d = daily(rows, S0)
    assert "1000" not in d["holdings"] and d["flags"] == ["delisted_exit:1000"]
    assert d["cash"] == pytest.approx(cash0 + 50000.0 - sell, abs=1e-3)     # exit value minus the sell leg only
    assert d["nav"] == pytest.approx(nav0 - sell, abs=1e-3)
    assert sell == pytest.approx(50000.0 * (0.001425 * 0.22 + 0.003 + 0.001))
    assert sim.state["frozen_days"] == {} and "1000" not in sim.state["last_close"]
    for name in sp.CANDIDATES:                                      # all four held it, all four exit
        assert "1000" not in sim.cand(name)["holdings"]
    # the idle cash is redeployed at the next rebalance
    rows = sim.day(date(2026, 12, 30))
    rows = sim.day(date(2026, 12, 31))
    rows = sim.day(JAN4, prices=px({"1000": None}), rank_fn=lambda n, dd: default_rank(n, dd))
    assert daily(rows, S0)["cash"] == pytest.approx(-reb(rows, S0)[0]["cost"], abs=0.02)
    assert len(daily(rows, S0)["holdings"]) == 20


def test_whole_market_missing_days_pause_the_no_close_count():
    sim = started()
    days = trading_days(date(2026, 12, 2), date(2026, 12, 31))           # 22 trading days
    assert len(days) == 22
    for d in days[:3]:
        sim.day(d, prices=px({"1000": None}))
    assert sim.state["frozen_days"] == {"1000": 3}
    for d in days[3:5]:
        sim.day(d, prices=None)                                          # 2 market-missing days: no count
    assert sim.state["frozen_days"] == {"1000": 3}
    assert sim.meta()["consecutive_missing"] == 2
    for d in days[5:21]:                                                 # 16 more -> 19 counted days
        sim.day(d, prices=px({"1000": None}))
    assert sim.state["frozen_days"] == {"1000": 19}                      # 21 elapsed days, but only 19 counted
    assert "1000" in sim.cand(S0)["holdings"]
    rows = sim.day(days[21], prices=px({"1000": None}))                  # the 20th counted day
    assert daily(rows, S0)["flags"] == ["delisted_exit:1000"]
    assert sim.meta()["consecutive_missing"] == 0                        # reset by the first data day


# --------------------------------------------------------------------------- #
# 6. Missing data days (A5, A5b-8, A5c-3, A5d-1)
# --------------------------------------------------------------------------- #

def test_missing_day_carries_holdings_and_counts():
    sim = started()
    nav0 = sim.cand(S0)["nav"]
    rows = sim.day(D2, prices=None)
    for name in sp.CANDIDATES:
        r = daily(rows, name)
        assert r["nav"] == nav0 and r["ret"] == 0.0 and r["flags"] == ["missing_day"]
    m = sim.meta()
    assert (m["missing_days"], m["missing_total"], m["consecutive_missing"]) == (["2026-12-02"], 1, 1)
    assert m["pending_retry"] == [{"date": "2026-12-02", "kind": "close", "until": "2026-12-07"}]
    rows = sim.day(D3, prices=px({"1000": 110.0}))                   # catches up in one go
    assert daily(rows, S0)["holdings"]["1000"] == 55000.0
    assert sim.meta()["consecutive_missing"] == 0 and sim.meta()["missing_total"] == 1
    sim.run(trading_days(D4, date(2026, 12, 8)))
    assert sim.meta()["pending_retry"] == []                         # expired after the retry window


def test_nominal_rebalance_day_missing_is_deferred_to_next_data_day():
    sim = started()
    to_jan3(sim)
    rows = sim.day(JAN4, prices=None)
    assert reb(rows, S0) == []
    for name in (S0, S1, S3):
        assert daily(rows, name)["flags"] == ["missing_day", "rebalance_deferred"]
    assert daily(rows, S2)["flags"] == ["missing_day"]
    assert sim.cand(S0)["pending_rebalance"]["nominal_date"] == "2027-01-04"
    rows = sim.day(date(2027, 1, 5))
    r = reb(rows, S0)[0]
    assert (r["nominal_date"], r["fill_date"]) == ("2027-01-04", "2027-01-05")
    assert sim.meta()["deferrals"] == [{"candidate": c, "nominal_date": "2027-01-04",
                                        "executed": "2027-01-05", "trading_days": 1} for c in (S0, S1, S3)]
    assert sim.meta()["impaired"] is False and sim.cand(S0)["pending_rebalance"] is None
    assert daily(rows, S0)["flags"] == ["rebalance_deferred"]


def test_missing_ranking_on_a_data_day_defers_without_a_missing_day():
    sim = started()
    to_jan3(sim)
    rows = sim.day(JAN4, rankings={S1: default_rank(S1, JAN4), S3: default_rank(S3, JAN4)})   # S0 has none (A5d-3)
    assert reb(rows, S0) == [] and len(reb(rows, S1)) == 1
    # F4 (user decision 2026-11-18): not counted a second time, only flagged
    assert daily(rows, S0)["flags"] == ["rebalance_deferred", "ranking_missing"]
    assert "ranking_missing" not in daily(rows, S1)["flags"]
    assert sim.meta()["missing_total"] == 0 and sim.cand(S0)["pending_rebalance"]["nominal_date"] == "2027-01-04"
    assert sim.meta()["missing_days"] == [] and sim.meta()["pending_retry"] == []
    assert "ranking" not in sim.cand(S0)["pending_rebalance"]
    rows = sim.day(date(2027, 1, 5))
    assert reb(rows, S0)[0]["fill_date"] == "2027-01-05"
    assert sim.meta()["deferrals"] == [{"candidate": S0, "nominal_date": "2027-01-04",
                                        "executed": "2027-01-05", "trading_days": 1}]


def test_single_deferral_of_5_days_is_fine_but_6_is_impaired():
    sim = started()
    to_jan3(sim)
    sim.run(trading_days(JAN4, date(2027, 1, 8)), prices=None)       # 5 missing days
    sim.day(date(2027, 1, 11))                                       # executes at delay 5
    assert sim.meta()["deferrals"][0]["trading_days"] == 5 and sim.meta()["impaired"] is False

    sim = started()
    to_jan3(sim)
    sim.run(trading_days(JAN4, date(2027, 1, 11)), prices=None)      # 6 missing days
    assert sim.meta()["impaired"] is False
    sim.day(date(2027, 1, 12))
    assert sim.meta()["deferrals"][0]["trading_days"] == 6
    assert sim.meta()["impaired"] is True
    assert "deferral_gt_5:S0_baseline" in sim.meta()["impaired_reasons"]


def test_more_than_10_cumulative_missing_days_is_impaired():
    sim = started()
    miss = [date(2026, 12, d) for d in (2, 4, 8, 10, 14, 16, 18, 22, 24, 28, 30)]    # never consecutive
    assert len(miss) == 11
    for d in trading_days(date(2026, 12, 2), date(2026, 12, 31)):
        sim.day(d, prices=None if d in miss else "default")
        if d == miss[9]:
            assert sim.meta()["missing_total"] == 10 and sim.meta()["impaired"] is False
        if d == miss[10]:
            assert sim.meta()["impaired"] is True
    assert "missing_total_gt_10" in sim.meta()["impaired_reasons"]
    assert sim.meta()["status"] == "active" and sim.meta()["void"] is None


def test_20_consecutive_missing_days_void_the_run():
    sim = started()
    days = trading_days(date(2026, 12, 2), date(2026, 12, 29))
    for d in days[:19]:
        sim.day(d, prices=None)
    assert sim.meta()["void"] is None and sim.meta()["status"] == "active"
    rows = sim.day(days[19], prices=None)
    assert sim.meta()["void"] == {"trigger_date": "2026-12-29", "new_start": "2027-01-04"}
    assert sim.meta()["status"] == "void" and sim.meta()["impaired"] is True
    assert daily(rows, S0)["flags"] == ["missing_day"]               # the trigger day itself is still booked
    before = copy.deepcopy(sim.state)
    assert sim.day(date(2026, 12, 30)) == [] and sim.state == before  # void: no more normal valuation
    with pytest.raises(ValueError):
        sp.final_report(sim.state)


def test_non_consecutive_calls_are_rejected():
    sim = started()
    with pytest.raises(ValueError):
        sim.day(D3)                                                  # 12/02 skipped
    with pytest.raises(ValueError):
        sp.step(sim.state, sim.inputs(date(2026, 12, 12), px(), None, 100.0, None, {}, None))   # a Saturday


# --------------------------------------------------------------------------- #
# 7. Benchmark 0050 (A5c-1, A5c-10)
# --------------------------------------------------------------------------- #

def test_benchmark_chain_and_missing_bar_carry_forward():
    sim = started()
    rows = sim.day(D2, bench=103.0)
    b = next(r for r in rows if r["file"] == "benchmark")
    assert b["ret"] == pytest.approx(0.03) and b["flags"] == []
    rows = sim.day(D3, bench=None)                                   # 0050 bar missing, market fine
    b = next(r for r in rows if r["file"] == "benchmark")
    assert b["adj_close"] == 103.0 and b["ret"] == 0.0 and b["flags"] == ["carry_forward"]
    assert sim.meta()["missing_total"] == 0                          # not a missing-data day
    rows = sim.day(D4, bench=106.09)
    assert next(r for r in rows if r["file"] == "benchmark")["ret"] == pytest.approx(0.03)


def test_benchmark_and_market_both_missing_is_a_missing_day():
    sim = started()
    rows = sim.day(D2, prices=None, bench=None)
    b = next(r for r in rows if r["file"] == "benchmark")
    assert b["flags"] == ["missing_day", "carry_forward"] and b["ret"] == 0.0 and b["adj_close"] == 100.0
    assert sim.meta()["missing_total"] == 1


# --------------------------------------------------------------------------- #
# Determinism and correction series (A5d-1)
# --------------------------------------------------------------------------- #

def test_same_state_and_inputs_give_bit_identical_output():
    sim = started()
    to_jan3(sim)
    state = json.loads(json.dumps(sim.state))
    inp = sim.inputs(JAN4, px({"1005": 97.3}), {"1005": 0.99}, 101.5, {"complete": False, "ratio": 0.2},
                     {c: default_rank(c, JAN4, shift=5) for c in (S0, S1, S3)}, "2027-01-05T07:00:00")
    snap_state, snap_inp = copy.deepcopy(state), copy.deepcopy(inp)
    a = sp.step(state, inp)
    b = sp.step(state, inp)
    assert json.dumps(a) == json.dumps(b)                            # includes key order
    assert state == snap_state and inp == snap_inp                   # arguments not mutated


def test_catch_up_rerun_fills_at_the_nominal_close_and_is_not_a_missing_day():
    base = started()
    to_jan3(base)
    snapshot = copy.deepcopy(base.state)

    first = Sim(state=copy.deepcopy(snapshot))
    first.day(JAN4, prices=None, written_at="2027-01-05T07:00:00", rank_fn=shifted)
    first.day(date(2027, 1, 5), prices=px({"1020": 120.0}), written_at="2027-01-05T07:00:01", rank_fn=shifted)
    assert first.meta()["missing_total"] == 1 and len(first.meta()["deferrals"]) == 3
    assert first.cand(S0)["holdings"]["1020"] == pytest.approx(first.cand(S0)["holdings"]["1021"], abs=1e-3)  # bought at 120

    # the retry at 1/7 succeeds: re-run 1/4 and 1/5 from the snapshot before the missing day
    fixed = Sim(state=copy.deepcopy(snapshot))
    fixed.day(JAN4, prices=px({"1020": 100.0}), written_at="2027-01-07T07:00:00", rank_fn=shifted)
    fixed.day(date(2027, 1, 5), prices=px({"1020": 120.0}), written_at="2027-01-07T07:00:00", rank_fn=shifted)
    assert fixed.meta()["missing_total"] == 0 and fixed.meta()["deferrals"] == []
    r = reb(fixed.rows, S0)[0]
    assert (r["nominal_date"], r["fill_date"]) == ("2027-01-04", "2027-01-04")
    assert fixed.cand(S0)["holdings"]["1020"] == pytest.approx(fixed.cand(S0)["holdings"]["1021"] * 1.2, abs=1e-3)

    corrected = sp.mark_superseded(fixed.rows, sp.supersede_index(first.rows))
    cd = [r for r in corrected if r["file"] == "daily" and r["candidate"] == S0]
    assert [r["supersedes"] for r in cd] == ["2027-01-05T07:00:00", "2027-01-05T07:00:01"]
    # F8: each file points at the original row of the SAME file: the corrected rebalance (nominal
    # and fill 1/4) replaces the provisional one that was filled on 1/5, not the 1/4 daily row.
    cr = [r for r in corrected if r["file"] == "rebalances" and r["candidate"] == S0]
    orig_r = [r for r in first.rows if r["file"] == "rebalances" and r["candidate"] == S0]
    assert len(cr) == len(orig_r) == 1 and orig_r[0]["fill_date"] == "2027-01-05"
    assert cr[0]["supersedes"] == orig_r[0]["written_at"] == "2027-01-05T07:00:01"
    assert cr[0]["supersedes"] != cd[0]["supersedes"]
    cb = [r for r in corrected if r["file"] == "benchmark"]
    assert [r["supersedes"] for r in cb] == ["2027-01-05T07:00:00", "2027-01-05T07:00:01"]
    assert all(r["supersedes"] is not None for r in corrected if r["file"] in ("daily", "rebalances")
               and r["candidate"] in (S0, S1, S3))
    assert all("late_fill" in r["flags"] for r in cd)
    assert all("supersedes" not in r for r in corrected if r["file"] == "meta")
    assert not any("late_fill" in r["flags"] for r in fixed.rows if r["file"] == "daily")   # input untouched

    merged = first.rows + corrected
    eff = sp.effective_rows(merged, "daily", S0)
    assert [r["date"] for r in eff] == ["2027-01-04", "2027-01-05"]
    assert [r["nav"] for r in eff] == [r["nav"] for r in cd]          # the corrected series wins
    assert len(sp.effective_rows(merged, "rebalances", S0)) == 1
    assert sp.effective_rows(merged, "rebalances", S0)[0]["fill_date"] == "2027-01-04"
    assert len(first.rows_of("daily", S0)) == 2                       # the old rows stay (append-only)


# --------------------------------------------------------------------------- #
# Year run: judgment day, Sharpe, cash bound, replay (A3, A4, A5d-7, A5e-3)
# --------------------------------------------------------------------------- #

def drift_prices(d):
    k = CAL.index(d)
    return px({c: round(100.0 * (1 + 0.003 * math.sin(0.37 * k + i)) + 0.05 * k, 4) for i, c in enumerate(CODES)})


def drift_bench(d):
    return 100.0 + 0.04 * CAL.index(d) + math.sin(CAL.index(d))


def s2_year(d):
    if (d.year, d.month) == (2027, 11):
        return {"complete": False, "ratio": 0.5}            # forced on 11/22 (11/20 is a Saturday)
    return {"complete": d.day >= 12 and d.day % 2 == 1 or d.day == 14, "ratio": 0.95}


def year_rank(name, d):
    return default_rank(name, d, shift=(3 * ((d.year - 2026) * 12 + d.month)) % 40)


@pytest.fixture(scope="module")
def year():
    sim = Sim()
    after = {}
    for d in trading_days(START, date(2027, 11, 30)):
        sim.day(d, prices=drift_prices(d), bench=drift_bench(d), s2=s2_year(d), rank_fn=year_rank)
        after[d.isoformat()] = copy.deepcopy(sim.state)
    return sim, after


def test_year_run_rebalance_schedule(year):
    sim, _ = year
    fills = {n: [r["fill_date"] for r in reb(sim.rows, n) if r["kind"] == "rebalance"] for n in sp.CANDIDATES}
    first_days = [sr.first_trading_day(CAL, 2027, m).isoformat() for m in range(1, 12)]
    assert fills[S0] == fills[S1] == fills[S3] == first_days
    s2_days = fills[S2]
    assert len(s2_days) == 12 and s2_days[0] == "2026-12-14"          # December (start month) + Jan..Nov
    assert all(d >= f"{d[:8]}12" for d in s2_days)                    # never before the 12th
    assert s2_days[-1] == "2027-11-22"                                # forced
    assert sim.meta()["deferrals"] == [] and sim.meta()["impaired"] is False
    assert sim.meta()["missing_total"] == 0


def test_year_run_cash_stays_within_one_rebalance_cost(year):
    sim, _ = year
    assert len([r for r in reb(sim.rows, S2) if r["kind"] == "rebalance"]) == 12     # 12 consecutive rebalances
    for name in sp.CANDIDATES:
        costs = [r["cost"] for r in reb(sim.rows, name)]
        for r in reb(sim.rows, name):                                          # cash right after each trade = -its cost
            cash = sim.rows_of("daily", name, date.fromisoformat(r["fill_date"]))[0]["cash"]
            assert cash == pytest.approx(-r["cost"], abs=0.02)
        floor = -2 * max(costs)
        cashes = [r["cash"] for r in sim.rows_of("daily", name)]
        assert min(cashes) >= floor and max(cashes) <= 0.01


def test_year_run_judgment_day_report(year):
    sim, _ = year
    state = sim.state
    assert state["meta"]["judgment_date"] == "2027-11-30" and state["meta"]["status"] == "ended"
    assert sp.is_judgment_day(state, "2027-11-30") and not sp.is_judgment_day(state, date(2027, 11, 29))
    rep = sp.final_report(state)
    assert rep["judgment_date"] == "2027-11-30" and rep["judgment_data_complete"] is True
    assert set(rep) == {"judgment_date", "judgment_data_complete", "impaired", "impaired_reasons",
                        "candidates", "benchmark"}                 # numbers only, no comparison / verdict
    for name in sp.CANDIDATES:
        rows = sim.rows_of("daily", name)
        rets = [r["ret"] for r in rows]
        c = rep["candidates"][name]
        assert c["nav"] == rows[-1]["nav"] and rows[-1]["date"] == "2027-11-30"
        assert c["total_return"] == pytest.approx(rows[-1]["nav"] / 1e6 - 1)
        assert c["sharpe"] == metrics.sharpe_ratio(rets, rf=0.0, periods_per_year=252)
        assert len(rets) == len(trading_days(START, date(2027, 11, 30)))
        assert rets[0] == pytest.approx(rows[0]["nav"] / 1e6 - 1)           # start-day return includes the build cost
    brows = sim.rows_of("benchmark")
    assert brows[0]["ret"] == 0.0
    assert rep["benchmark"]["total_return"] == pytest.approx(brows[-1]["adj_close"] / brows[0]["adj_close"] - 1)
    assert rep["benchmark"]["sharpe"] == metrics.sharpe_ratio([r["ret"] for r in brows], rf=0.0, periods_per_year=252)


def test_after_the_judgment_day_nothing_more_is_booked(year):
    sim, _ = year
    before = copy.deepcopy(sim.state)
    assert sim.day(date(2027, 12, 1), prices=drift_prices(date(2027, 12, 1))) == []
    assert sim.state == before


def test_final_report_before_the_judgment_day_is_refused():
    with pytest.raises(ValueError):
        sp.final_report(started().state)


def test_year_run_replay_is_bit_identical(year):
    sim, after = year
    snap = after["2027-09-30"]
    runs = []
    for _ in range(2):
        again = Sim(state=copy.deepcopy(snap))
        for d in trading_days(date(2027, 10, 1), date(2027, 11, 30)):
            again.day(d, prices=drift_prices(d), bench=drift_bench(d), s2=s2_year(d), rank_fn=year_rank)
        runs.append((json.dumps(again.rows), json.dumps(again.state)))
    assert runs[0] == runs[1]
    tail = [r for r in sim.rows if r["file"] != "meta" and (r.get("date") or r["fill_date"]) >= "2027-10-01"]
    again_rows = [r for r in json.loads(runs[0][0]) if r["file"] != "meta"]
    assert json.dumps(tail) == json.dumps(again_rows)
    assert json.dumps(sim.state) == runs[0][1]


def run_tail(after, days, prices_fn, **kw):
    sim = Sim(state=copy.deepcopy(after["2027-11-19"]))
    for d in days:
        sim.day(d, prices=prices_fn(d), bench=drift_bench(d), s2=s2_year(d), rank_fn=year_rank, **kw)
    return sim


def test_judgment_day_nav_excludes_a_deferred_rebalance_landing_on_it(year):
    _, after = year
    miss = trading_days(date(2027, 11, 22), date(2027, 11, 29))           # S2's forced day 11/22 .. 11/29
    sim = run_tail(after, trading_days(date(2027, 11, 22), date(2027, 11, 30)),
                   lambda d: None if d in miss else drift_prices(d))
    assert sim.meta()["status"] == "ended"
    r = reb(sim.rows, S2)[-1]
    assert (r["nominal_date"], r["fill_date"]) == ("2027-11-22", "2027-11-30")
    last = sim.rows_of("daily", S2)[-1]
    assert last["date"] == "2027-11-30"
    rep = sp.final_report(sim.state)
    assert rep["candidates"][S2]["nav"] == pytest.approx(last["nav"] + r["cost"], abs=0.01)   # pre-rebalance value
    assert rep["candidates"][S2]["nav"] > last["nav"]
    assert rep["candidates"][S2]["total_return"] == pytest.approx(rep["candidates"][S2]["nav"] / 1e6 - 1)
    for name in (S0, S1, S3):                                             # no rebalance that day: plain close nav
        assert rep["candidates"][name]["nav"] == sim.rows_of("daily", name)[-1]["nav"]
    assert rep["impaired"] is True                                        # deferral of 6 trading days
    assert "deferral_gt_5:S2_high250_revday" in rep["impaired_reasons"]


def test_judgment_day_normal_uses_the_close(year):
    sim, after = year
    again = run_tail(after, trading_days(date(2027, 11, 22), date(2027, 11, 30)), drift_prices)
    rep = sp.final_report(again.state)
    for name in sp.CANDIDATES:
        assert rep["candidates"][name]["nav"] == again.rows_of("daily", name)[-1]["nav"]
    assert again.meta()["judgment"]["pre_rebalance"] == {}


def test_judgment_day_missing_then_repaired_by_a_rerun(year):
    _, after = year
    days = trading_days(date(2027, 11, 22), date(2027, 11, 30))
    sim = run_tail(after, days, lambda d: None if d == date(2027, 11, 30) else drift_prices(d))
    m = sim.meta()
    assert m["status"] == "ended" and m["judgment"]["complete"] is False
    assert m["pending_retry"][-1] == {"date": "2027-11-30", "kind": "close",
                                      "until": [d for d in CAL if d > date(2027, 11, 30)][9].isoformat()}   # +10 days
    nav_missing = sim.rows_of("daily", S0)[-1]["nav"]
    assert nav_missing == sim.rows_of("daily", S0)[-2]["nav"]             # old price, return 0
    rep = sp.final_report(sim.state)
    assert rep["judgment_data_complete"] is False and rep["impaired"] is False
    rep = sp.final_report(sim.state, grace_elapsed=True)                  # 10 days passed without data
    assert rep["impaired"] is True and "judgment_data_missing" in rep["impaired_reasons"]
    assert rep["candidates"][S0]["nav"] == nav_missing                    # last available nav

    # data arrives: re-run the judgment day from the snapshot of the day before
    fixed = Sim(state=copy.deepcopy(after["2027-11-29"]))
    fixed.day(date(2027, 11, 30), prices=drift_prices(date(2027, 11, 30)), bench=drift_bench(date(2027, 11, 30)),
              s2=s2_year(date(2027, 11, 30)), rank_fn=year_rank, written_at="2027-12-02T07:00:00")
    rep2 = sp.final_report(fixed.state)
    assert rep2["judgment_data_complete"] is True and rep2["impaired"] is False
    corrected = sp.mark_superseded(fixed.rows, sp.supersede_index(sim.rows))
    merged = sim.rows + corrected
    eff = sp.effective_rows(merged, "daily", S0)
    assert eff[-1]["date"] == "2027-11-30"
    assert eff[-1]["nav"] == rep2["candidates"][S0]["nav"] and eff[-1]["supersedes"] is not None


def test_judgment_date_recomputes_when_calendar_was_too_short():
    short = [d for d in CAL if d <= date(2027, 6, 30)]
    st = sp.new_state(START, short, PRIMARY)
    assert st["meta"]["judgment_date"] is None
    sim = Sim(state=st)
    sim.day(START)
    assert sim.meta()["judgment_date"] == "2027-11-30"                    # the full calendar arrives with the inputs


# --------------------------------------------------------------------------- #
# Review fixes: F1 all-frozen rebalance, F2 provisional calendar, F5 extra mutation guards,
# F6 / F7 ex-rights factors, F9 look-ahead guard, F10 missing s2_status
# --------------------------------------------------------------------------- #

def test_f1_all_holdings_frozen_on_a_rebalance_day_is_a_noop_not_a_crash():
    sim = started()
    to_jan3(sim)
    before = {n: copy.deepcopy(sim.cand(n)) for n in sp.CANDIDATES}
    rows = sim.day(JAN4, prices=px({c: None for c in CODES[:20]}))       # nav < sum(frozen): cash is -1313.5
    for name in (S0, S1, S3):
        r = reb(rows, name)[0]
        assert r["flags"] == ["rebalance_skipped_all_frozen"] and r["cost"] == 0.0
        assert r["orders"] == {"buys": {}, "sells": {}} and r["targets"] == [] and len(r["frozen"]) == 20
        d = daily(rows, name)
        assert d["holdings"] == before[name]["holdings"] and d["cash"] == before[name]["cash"]
        assert d["nav"] == before[name]["nav"] and d["ret"] == 0.0
        assert "rebalance_skipped_all_frozen" in d["flags"] and len(d["frozen"]) == 20
        assert sim.cand(name)["pending_rebalance"] is None             # counts as done
    assert reb(rows, S2) == []


def test_f1_empty_prices_dict_means_every_holding_has_no_close_not_a_missing_day():
    sim = started()
    nav0 = sim.cand(S0)["nav"]
    rows = sim.day(D2, prices={})
    assert sim.meta()["missing_total"] == 0 and sim.meta()["consecutive_missing"] == 0
    assert sim.state["frozen_days"] == {c: 1 for c in CODES[:20]}
    assert daily(rows, S0)["nav"] == nav0 and daily(rows, S0)["flags"] == []
    sim2 = started()
    to_jan3(sim2)
    rows = sim2.day(JAN4, prices={})                                      # rebalance day, nothing has a close
    assert reb(rows, S0)[0]["flags"] == ["rebalance_skipped_all_frozen"]
    assert sim2.meta()["missing_total"] == 0


def test_f1_empty_prices_on_the_start_day_do_not_start():
    sim = Sim()
    sim.day(START, prices={})
    assert sim.meta()["status"] == "not_started"


def test_f2_judgment_date_follows_the_final_calendar(year):
    _, after = year
    final_cal = [d for d in CAL if d != date(2027, 11, 30)]               # 11/30 turns out to be a holiday
    sim = Sim(state=copy.deepcopy(after["2027-11-19"]), cal_iso=[d.isoformat() for d in final_cal])
    assert sim.meta()["judgment_date"] == "2027-11-30"                    # provisional
    sim.day(date(2027, 11, 22), s2=s2_year(date(2027, 11, 22)), rank_fn=year_rank)
    assert sim.meta()["judgment_date"] == "2027-11-29" and sim.meta()["status"] == "active"
    for d in trading_days(date(2027, 11, 23), date(2027, 11, 29)):
        sim.day(d, s2=s2_year(d), rank_fn=year_rank)
    assert sim.meta()["status"] == "ended" and sim.meta()["judgment"]["date"] == "2027-11-29"
    assert sp.final_report(sim.state)["judgment_date"] == "2027-11-29"
    assert sp.is_judgment_day(sim.state, "2027-11-29") and not sp.is_judgment_day(sim.state, "2027-11-30")


def test_f2_short_calendar_keeps_the_previous_judgment_date():
    sim = started()
    short = [d.isoformat() for d in CAL if d <= date(2027, 6, 30)]
    sim.cal_iso = short
    sim.day(D2)
    assert sim.meta()["judgment_date"] == "2027-11-30"


def test_f5b_exright_status_row_has_a_close_and_its_factor_applies():
    sim = started()
    nav0 = sim.cand(S0)["nav"]
    rows = sim.day(D2, prices=px({"1000": ("exright", 95.0)}), adj={"1000": 0.95})
    r = daily(rows, S0)
    assert r["frozen"] == [] and r["holdings"]["1000"] == 50000.0 and r["nav"] == nav0
    assert r["flags"] == [] and sim.state["frozen_days"] == {}


def test_f5c_non_positive_factor_is_adj_missing():
    for bad in (0.0, -0.5):
        sim = started()
        rows = sim.day(D2, prices=px({"1000": 95.0}), adj={"1000": bad})
        r = daily(rows, S0)
        assert r["holdings"]["1000"] == 47500.0 and r["flags"] == ["adj_missing:1000"]


def test_f5d_absent_s2_status_means_not_complete_and_is_flagged_f10():
    sim = Sim()
    sim.day(START, s2=None)
    for d in trading_days(date(2026, 12, 2), date(2026, 12, 24)):
        sim.day(d, s2=None, rank_fn=lambda n, dd: default_rank(n, dd, shift=3))
    rows = s2_rebalance_rows(sim)
    assert [r["fill_date"] for r in rows] == ["2026-12-21"]               # only the forced day
    assert rows[0]["s2_coverage"] == {"ratio": None, "complete_day": None, "forced": True}
    flagged = [d.isoformat() for d, rr in sim.log if "s2_status_missing" in daily(rr, S2)["flags"]]
    assert flagged == [d.isoformat() for d in trading_days(date(2026, 12, 14), date(2026, 12, 21))]
    for name in (S0, S1, S3):                                             # only S2's row carries it
        assert all("s2_status_missing" not in daily(rr, name)["flags"] for _, rr in sim.log)
    # supplied status: no flag
    sim2 = Sim()
    sim2.day(START)
    sim2.run(trading_days(date(2026, 12, 2), date(2026, 12, 18)))
    assert all("s2_status_missing" not in daily(rr, S2)["flags"] for _, rr in sim2.log)


def test_f5e_market_missing_day_still_uses_the_0050_close():
    sim = started()
    rows = sim.day(D2, prices=None, bench=105.0)
    b = next(r for r in rows if r["file"] == "benchmark")
    assert b["adj_close"] == 105.0 and b["ret"] == pytest.approx(0.05)
    assert b["flags"] == ["missing_day"] and b["source"] == "yahoo"


def test_f5f_pending_rebalance_is_not_overridden_by_a_new_nominal_day():
    sim = started()
    to_jan3(sim)
    days = trading_days(JAN4, date(2027, 1, 29))
    for d in days:                                                        # S0 never gets a ranking in January
        due = sp.rebalance_due(sim.state, d, CAL_ISO, {"complete": False, "ratio": 0.5})
        sim.day(d, rankings={c: default_rank(c, d) for c in due if c != S0})
    assert sim.cand(S0)["pending_rebalance"]["nominal_date"] == "2027-01-04"
    feb1 = date(2027, 2, 1)
    assert sr.first_trading_day(CAL, 2027, 2) == feb1
    rows = sim.day(feb1)                                                  # a new nominal day arrives
    assert sim.cand(S0)["pending_rebalance"] is None
    s0_rows = [r for r in sim.rows if r["file"] == "rebalances" and r["candidate"] == S0 and r["kind"] == "rebalance"]
    assert [(r["nominal_date"], r["fill_date"]) for r in s0_rows] == [("2027-01-04", "2027-02-01")]   # one rebalance only
    assert len(reb(rows, S1)) == 1 and reb(rows, S1)[0]["nominal_date"] == "2027-02-01"


def test_f6_ex_rights_factor_on_a_missing_data_day_is_not_lost():
    sim = started()
    nav0 = sim.cand(S0)["nav"]
    rows = sim.day(D2, prices=None, adj={"1000": 0.95})                  # TWT49U answered, the close endpoint did not
    assert daily(rows, S0)["nav"] == nav0 and daily(rows, S0)["flags"] == ["missing_day"]
    assert sim.state["acc_factor"]["1000"] == pytest.approx(0.95) and "1001" in sim.state["acc_factor"]
    assert sim.state["acc_factor"]["1001"] == 1.0
    rows = sim.day(D3, prices=px({"1000": 95.0}))                        # the dividend is already inside 95
    assert daily(rows, S0)["holdings"]["1000"] == pytest.approx(50000.0, abs=1e-3)
    assert daily(rows, S0)["nav"] == nav0 and sim.state["acc_factor"]["1000"] == 1.0
    # without the table the drop would be (wrongly) booked: documents the old behaviour
    sim2 = started()
    sim2.day(D2, prices=None)
    rows = sim2.day(D3, prices=px({"1000": 95.0}))
    assert daily(rows, S0)["holdings"]["1000"] == 47500.0


def test_f6_adj_missing_on_a_missing_day_is_flagged_for_holders_only():
    sim = started()
    rows = sim.day(D2, prices=None, adj={"1000": None, "1030": None})
    assert daily(rows, S0)["flags"] == ["missing_day", "adj_missing:1000"]


def test_f7_exright_status_without_a_factor_is_adj_missing():
    sim = started()
    rows = sim.day(D2, prices=px({"1000": ("exright", 95.0)}), adj={})
    r = daily(rows, S0)
    assert r["holdings"]["1000"] == 47500.0 and r["flags"] == ["adj_missing:1000"]
    rows = sim.day(D3, prices=px({"1000": ("exright", 95.0), "1001": ("exright", 100.0)}), adj={"1001": None})
    assert daily(rows, S0)["flags"] == ["adj_missing:1000", "adj_missing:1001"]
    rows = sim.day(D4, prices=px({"1000": 95.0}))                         # plain "ok" row and no entry: factor 1.0
    assert daily(rows, S0)["flags"] == []


def test_f9_ranking_not_before_the_fill_date_is_rejected():
    sim = started()
    to_jan3(sim)
    for bad in ("2027-01-04", "2027-01-05", None):
        rk = default_rank(S0, JAN4)
        rk["asof"] = bad
        rankings = {c: default_rank(c, JAN4) for c in (S1, S3)}
        rankings[S0] = rk
        before = copy.deepcopy(sim.state)
        with pytest.raises(ValueError):
            sp.step(sim.state, sim.inputs(JAN4, px(), None, 100.0, {"complete": False}, rankings, "w"))
        assert sim.state == before
    good = default_rank(S0, JAN4)
    assert good["asof"] == "2026-12-31"
    # a deferred rebalance needs an asof before the (later) fill date, not the nominal date: only
    # reachable when the nominal day had no ranking at all (F4), else the stored one is used (F3)
    sim.day(JAN4, prices=None, rankings={c: default_rank(c, JAN4) for c in (S1, S3)})
    rk = default_rank(S0, JAN4)
    rk["asof"] = "2027-01-04"                                             # >= nominal but < fill (1/5): allowed
    rankings = {S0: rk}
    sim.day(date(2027, 1, 5), rankings=rankings)
    assert reb(sim.rows, S0)[-1]["signal_asof"] == "2027-01-04"
    bad = default_rank(S0, JAN4)
    bad["asof"] = "2027-01-05"                                            # == fill date: look-ahead, rejected
    sim2 = started()
    to_jan3(sim2)
    sim2.day(JAN4, prices=None, rankings={c: default_rank(c, JAN4) for c in (S1, S3)})
    with pytest.raises(ValueError):
        sp.step(sim2.state, sim2.inputs(date(2027, 1, 5), px(), None, 100.0, {"complete": False}, {S0: bad}, "w"))


def test_f9_start_day_ranking_must_also_precede_the_fill():
    sim = Sim()
    rankings = {c: default_rank(c, START) for c in sp.CANDIDATES}
    rankings[S1]["asof"] = START.isoformat()
    with pytest.raises(ValueError):
        sp.step(sim.state, sim.inputs(START, px(), None, 100.0, None, rankings, "w"))


# --------------------------------------------------------------------------- #
# F3 / F4 (user decision 2026-11-18)
# --------------------------------------------------------------------------- #

def _targets_of(rows, name):
    return set(reb(rows, name)[0]["targets"])


def test_f3_deferred_rebalance_trades_the_nominal_day_ranking():
    """T (1/4) is a missing day; the ranking of 1/4 is stored and traded on 1/5, whatever 1/5 ranks."""
    sim = started()
    to_jan3(sim)
    sim.day(JAN4, prices=None, rank_fn=lambda n, d: default_rank(n, d, shift=0))
    assert sim.cand(S0)["pending_rebalance"]["ranking"]["asof"] == "2026-12-31"
    # the caller no longer needs a ranking for the candidates that already hold one
    assert sp.rebalance_due(sim.state, date(2027, 1, 5), CAL_ISO, {"complete": False, "ratio": 0.5}) == []
    # even if a different ranking is supplied for 1/5 it is not used
    rows = sim.day(date(2027, 1, 5), rankings={c: default_rank(c, date(2027, 1, 5), shift=7) for c in (S0, S1, S3)})
    r = reb(rows, S0)[0]
    assert (r["nominal_date"], r["fill_date"], r["signal_asof"]) == ("2027-01-04", "2027-01-05", "2026-12-31")
    assert _targets_of(rows, S0) == set(CODES[:20])                       # shift=0 ranking, not shift=7
    assert sim.cand(S0)["pending_rebalance"] is None


def test_f3_deferred_and_same_day_rebalance_pick_the_same_stocks():
    """The two paths of the review finding (repaired re-run vs. failed retry) now agree."""
    direct = started()
    to_jan3(direct)
    rows_direct = direct.day(JAN4, rank_fn=lambda n, d: default_rank(n, d, shift=3))
    deferred = started()
    to_jan3(deferred)
    deferred.day(JAN4, prices=None, rank_fn=lambda n, d: default_rank(n, d, shift=3))
    rows_def = deferred.day(date(2027, 1, 5), rankings={c: default_rank(c, date(2027, 1, 5), shift=11) for c in (S0, S1, S3)})
    for name in (S0, S1, S3):
        assert _targets_of(rows_direct, name) == _targets_of(rows_def, name)


def test_f3_stored_ranking_is_json_clean_and_survives_snapshot_round_trip():
    sim = started()
    to_jan3(sim)
    sim.day(JAN4, prices=None)
    snap = json.loads(json.dumps(sim.state))                              # what snapshots/<date>.json holds
    sim2 = Sim(state=snap)
    rows = sim2.day(date(2027, 1, 5), rankings={})
    assert len(reb(rows, S0)) == 1 and len(reb(rows, S1)) == 1 and len(reb(rows, S3)) == 1


def test_f4_no_ranking_on_nominal_day_uses_first_later_ranking_and_is_flagged_each_day():
    sim = started()
    to_jan3(sim)
    only_s1_s3 = {c: default_rank(c, JAN4) for c in (S1, S3)}
    rows = sim.day(JAN4, rankings=only_s1_s3)
    assert "ranking_missing" in daily(rows, S0)["flags"]
    rows = sim.day(date(2027, 1, 5), rankings={})                         # still nothing
    assert "ranking_missing" in daily(rows, S0)["flags"] and reb(rows, S0) == []
    assert sim.meta()["missing_total"] == 0 and sim.meta()["pending_retry"] == []
    rows = sim.day(date(2027, 1, 6), rankings={S0: default_rank(S0, date(2027, 1, 6), shift=2)})
    r = reb(rows, S0)[0]
    assert (r["nominal_date"], r["signal_asof"]) == ("2027-01-04", "2027-01-05")
    assert "ranking_missing" not in daily(rows, S0)["flags"]
    assert sim.meta()["deferrals"][-1]["trading_days"] == 2
