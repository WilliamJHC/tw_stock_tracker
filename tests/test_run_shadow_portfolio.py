# -*- coding: utf-8 -*-
"""Tests for scripts/run_shadow_portfolio.py (the daily driver of the shadow portfolio).

No network: every fetcher is a fake over a hand-made 40-code world (spec A5: nothing here backtests
S2 / S3 on real data; the prices are seeded random walks).  The world spans ~40 trading days from the
start day 2026-12-01 and contains: the start build, a month-start rebalance with the S3 sector cap, an
S2 rebalance on its "revenue refresh complete" day, a day on which one market fails and is fetched
again three days later (re-run with ``supersedes``), an ex-dividend, a suspension and resume, a day
without a 0050 bar, Yahoo-style rewritten 0050 scales, and two skipped jobs (late ranking)."""
import copy
import hashlib
import json
import math
import os
import random
import shutil
import sys
from datetime import date, timedelta

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import finlab_factors as ff  # noqa: E402
import report_factor_ic as rfi  # noqa: E402
import run_shadow_portfolio as rsp  # noqa: E402
import shadow_market_data as smd  # noqa: E402
import shadow_portfolio as sp  # noqa: E402
import shadow_rules as sr  # noqa: E402

S0, S1, S2, S3 = sp.CANDIDATES
START = date(2026, 12, 1)
CACHE_END = date(2026, 11, 27)
HOLIDAYS = {date(2027, 1, 1)}
CODES = [f"{1000 + i}" for i in range(40)]
BLOCK = {c: "ABCDE"[i // 8] for i, c in enumerate(CODES)}
MARKET = {c: ("twse" if i % 2 == 0 else "tpex") for i, c in enumerate(CODES)}
RAW_INDUSTRIES = {c: ["IND_" + BLOCK[c]] for c in CODES}
PRIMARY = sr.primary_industry(RAW_INDUSTRIES, set(CODES))

DOWN_DAY = date(2026, 12, 9)          # TPEx fails (TWSE is fine): the whole day counts as missing
DOWN_UNTIL_JOB = date(2026, 12, 14)   # ... until the job of this day, which re-fetches it (3rd trading day after)
BENCH_MISSING = date(2026, 12, 15)    # Yahoo has no 0050 bar this day
EXDIV = {"1005": (date(2026, 12, 29), 0.95), "1004": (date(2026, 12, 30), 0.97)}
SUSPEND = {"1002": [date(2026, 12, 22), date(2026, 12, 23), date(2026, 12, 24)]}
SKIP_JOBS = {date(2027, 1, 4), date(2027, 1, 5)}
LAST_JOB = date(2027, 1, 26)


def weekdays(a, b, holidays=HOLIDAYS):
    out, d = [], a
    while d <= b:
        if d.weekday() < 5 and d not in holidays:
            out.append(d)
        d += timedelta(days=1)
    return out


CAL = weekdays(date(2026, 1, 1), date(2027, 12, 31))
CAL_SET = set(CAL)
HIST_DAYS = weekdays(date(2025, 8, 1), CACHE_END)
SIM_DAYS = weekdays(date(2026, 11, 30), date(2027, 3, 31))
JOB_DAYS = weekdays(START, LAST_JOB)


class World:
    """Seeded synthetic market: cache history, daily closes, ex-rights events, 0050 index."""

    def __init__(self):
        self.job_day = None
        self.fails = [(DOWN_DAY, "tpex", DOWN_UNTIL_JOB)]      # (day, market, until job day or None = for ever)
        self.bench_missing = {BENCH_MISSING}
        self.exright_fails = []                                    # (day, market, until job day)
        self.fund_never_complete = False
        self.forbid_fetch = False
        self.rows, self.events, self.cache = {}, {}, {}
        for i, code in enumerate(CODES):
            rng = random.Random(1000 + i)
            p = 100.0 + i
            bars = []
            for d in HIST_DAYS:
                p = round(p * (1 + rng.gauss(0, 0.01)), 2)
                bars.append({"date": d.isoformat(), "o": p, "h": p, "l": p, "c": p, "v": 500_000 + 1000 * i, "c_adj": p})
            self.cache[code] = {
                "bars": bars, "bar_dates": [b["date"] for b in bars],
                "revenue_series": [{"usable_date": "2025-12-10", "yoy_pct": 100 - 1.3 * i}],
                "roe_series": [{"usable_date": "2025-12-10", "roe_pct": 40 - 0.9 * i}]}
            rows = {}
            for d in SIM_DAYS:
                g = rng.gauss(0, 0.01)
                if d in SUSPEND.get(code, []):
                    rows[d] = {"o": None, "h": None, "l": None, "c": None, "v": None,
                               "status": "no_close", "exright_kind": None}
                    continue
                kind, status = None, "ok"
                if code in EXDIV and d == EXDIV[code][0]:
                    pre, p = p, round(p * EXDIV[code][1], 2)            # closes exactly at the reference price
                    self.events[(d, code)] = {"pre_close": pre, "ref_price": p}
                    status, kind = "exright", ("cash" if MARKET[code] == "tpex" else "unknown")
                else:
                    p = round(p * (1 + g), 2)
                rows[d] = {"o": round(p * 0.998, 2), "h": round(p * 1.003, 2), "l": round(p * 0.995, 2),
                           "c": p, "v": 500_000 + 1000 * i, "status": status, "exright_kind": kind}
            self.rows[code] = rows

    # -- truth ---------------------------------------------------------------
    def complete_prices(self, d):
        return {c: dict(self.rows[c][d]) for c in CODES}

    def adj_map(self, d):
        return {c: e["ref_price"] / e["pre_close"] for (dd, c), e in self.events.items() if dd == d}

    def index(self, d):
        n = CAL.index(d)
        return 100.0 * (1 + 0.002 * n + 0.01 * math.sin(n))

    def scale(self, fetch_day):
        return 1.0 + 0.01 * (fetch_day.toordinal() % 7)           # Yahoo rewrites old values: scale differs per fetch

    def benchmark_series(self, T, lookback=rsp.BENCH_LOOKBACK_DAYS):
        k = self.scale(T)
        return {d.isoformat(): k * self.index(d) for d in CAL
                if T - timedelta(days=lookback) <= d <= T and d not in self.bench_missing}

    def is_down(self, d, market):
        return any(fd == d and fm == market and (until is None or self.job_day < until)
                   for fd, fm, until in self.fails)

    def exright_down(self, d, market):
        return any(fd == d and fm == market and self.job_day < until for fd, fm, until in self.exright_fails)

    def fund_refresh(self, job_day):
        if self.fund_never_complete:                                # 75 % done: never reaches the 90 % bar
                return {c: ({"revenue": "2026-11-01"} if i < 30 else {"revenue": "2000-01-01"})
                        for i, c in enumerate(CODES)}
        if job_day.month == 12:
            ok, rev = job_day >= date(2026, 12, 16), "2026-11-01"
        elif job_day.month == 1:
            ok, rev = job_day >= date(2027, 1, 13), "2026-12-01"
        else:
            ok, rev = False, "2027-01-01"
        fr = ({"revenue": rev} if ok
              else {"revenue": "2000-01-01", "revenue_attempt": f"{job_day.year}-{job_day.month:02d}-05"})
        return {c: dict(fr) for c in CODES}

    def loader(self):
        return self.cache, self.fund_refresh(self.job_day)


class FakeFetchers:
    adj_fallback = staticmethod(lambda d, c: None)               # no Yahoo: a missing TPEx table = adj_missing

    def __init__(self, world):
        self.w = world
        self.calls = []

    def _check(self):
        assert not self.w.forbid_fetch, "a fetch happened where none is allowed"

    def _close(self, d, market):
        self._check()
        self.calls.append((market + "_close", d))
        if d not in CAL_SET or self.w.is_down(d, market):
            return None
        return {c: dict(self.w.rows[c][d]) for c in CODES if MARKET[c] == market}

    def twse_close(self, d):
        return self._close(d, "twse")

    def tpex_close(self, d):
        return self._close(d, "tpex")

    def _exright(self, s, e, market):
        self._check()
        if self.w.exright_down(s, market):
            return None
        return {k: dict(v) for k, v in self.w.events.items() if MARKET[k[1]] == market and s <= k[0] <= e}

    def twse_exright(self, s, e):
        return self._exright(s, e, "twse")

    def tpex_exright(self, s, e):
        return self._exright(s, e, "tpex")

    def calendar(self, year):
        self._check()
        return set(weekdays(date(year, 1, 1), date(year, 12, 31))) if year in (2026, 2027) else None

    def benchmark_series(self, d, lookback_days=rsp.BENCH_LOOKBACK_DAYS):
        self._check()
        return self.w.benchmark_series(d, lookback_days) if d in CAL_SET else None


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("real network access in a test")
    monkeypatch.setattr(smd, "_get_json", boom)
    monkeypatch.setattr(smd.requests, "get", boom)


def write_snapshots(tmp_path, ind_obj=None):
    uni = tmp_path / "universe.json"
    ind = tmp_path / "industries.json"
    uni.write_text(json.dumps(CODES), encoding="utf-8")
    ind.write_text(json.dumps(RAW_INDUSTRIES if ind_obj is None else ind_obj), encoding="utf-8")
    return str(uni), str(ind)


def make_ledger(tmp_path, name="ledger", start="2026-12-01", ind_obj=None):
    uni, ind = write_snapshots(tmp_path, ind_obj)
    ledger = tmp_path / name
    (ledger / "meta").mkdir(parents=True)
    lock = {"lock_commit": "", "lock_tag": "shadow-lock-v1", "lock_date": "2026-11-30", "start_date": start,
            "judgment_date": "2027-11-30", "universe_sha256": rsp.sha256_file(uni),
            "industries_sha256": rsp.sha256_file(ind), "calendar_source": "test"}
    (ledger / "meta" / "lock.json").write_text(json.dumps(lock), encoding="utf-8")
    return str(ledger), uni, ind


def now_str(d):
    return f"{d.isoformat()}T07:00:00+08:00"


def job(world, ledger, uni, ind, d, extra=(), fetchers=None):
    world.job_day = d
    return rsp.main(["--ledger-dir", ledger, "--now", now_str(d), "--universe-file", uni,
                     "--industries-file", ind, *extra],
                    fetchers=fetchers or FakeFetchers(world), loader=world.loader)


def jsonl_files(ledger):
    out = {}
    for sub, _, files in os.walk(ledger):
        for f in files:
            if f.endswith(".jsonl"):
                p = os.path.join(sub, f)
                with open(p, "rb") as fh:
                    out[os.path.relpath(p, ledger).replace(os.sep, "/")] = fh.read()
    return out


def tree_bytes(ledger):
    out = {}
    for sub, _, files in os.walk(ledger):
        for f in files:
            p = os.path.join(sub, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, ledger).replace(os.sep, "/")] = fh.read()
    return out


def input_hashes(ledger):
    """sha1 of every prices / adj / rankings / s2 file (inputs must not change once written)."""
    out = {}
    for sub in ("prices", "adj", "rankings", "s2"):
        base = os.path.join(ledger, sub)
        for f in sorted(os.listdir(base)) if os.path.isdir(base) else []:
            with open(os.path.join(base, f), "rb") as fh:
                out[f"{sub}/{f}"] = hashlib.sha1(fh.read()).hexdigest()
    return out


class Scenario:
    pass


@pytest.fixture(scope="module")
def scenario(tmp_path_factory):
    """The full ~40-day run: one job per trading day except SKIP_JOBS, capturing the jsonl bytes after each."""
    tmp = tmp_path_factory.mktemp("scn")
    world = World()
    ledger, uni, ind = make_ledger(tmp)
    sc = Scenario()
    sc.world, sc.ledger, sc.uni, sc.ind, sc.tmp = world, ledger, uni, ind, tmp
    sc.hist, sc.filehash, sc.last_runs = {}, {}, {}
    for d in JOB_DAYS:
        if d in SKIP_JOBS:
            continue
        if d == START:                                  # the workflow runs --prefetch before the main run
            assert job(world, ledger, uni, ind, d, extra=["--prefetch"]) == 0
        assert job(world, ledger, uni, ind, d) == 0
        sc.hist[d] = jsonl_files(ledger)
        sc.filehash[d] = input_hashes(ledger)
        sc.last_runs[d] = json.load(open(os.path.join(ledger, "meta", "last_run.json"), encoding="utf-8"))
    sc.store = rsp.Store(ledger)
    sc.done = sc.store.list_dates("snapshots")[-1]
    return sc


def booked_days(done):
    return [d for d in CAL if START <= d <= date.fromisoformat(done)]


def reference(world, store, days, written_at=None):
    """Independent drive of ``step`` over `days`: truth from the world, rankings / s2 from the stored files."""
    cal_iso = [d.isoformat() for d in weekdays(date(2026, 1, 1), date(2027, 12, 31))]
    state = sp.new_state(START, cal_iso, PRIMARY)
    rows_all, last_real, states = [], None, {}
    for T in days:
        iso = T.isoformat()
        ser = world.benchmark_series(T)
        if state["meta"]["status"] == "not_started":
            bench = ser[iso]
        elif iso not in ser:
            bench = None
        else:
            bench = state["benchmark"]["last_close"] * (ser[iso] / ser[last_real])
        if bench is not None:
            last_real = iso
        s2 = store.read_json(f"s2/{iso}.json")
        rk = (store.read_json(f"rankings/{iso}.json") or {}).get("candidates") or {}
        inputs = {"date": iso, "calendar": cal_iso, "prices": world.complete_prices(T), "adj": world.adj_map(T),
                  "benchmark_close": bench,
                  "rankings": {n: {"asof": v["asof"], "ranked": v["ranked"], "detail": v["detail"]}
                               for n, v in rk.items()},
                  "s2_status": {"complete": s2["complete"], "ratio": s2["ratio"]} if s2 else None,
                  "written_at": (written_at or (lambda t: "ref"))(T)}
        state, rows = sp.step(state, inputs)
        rows_all.extend(rows)
        states[iso] = state
    reference.states = states
    return state, rows_all


def norm(row):
    r = {k: v for k, v in row.items() if k not in ("written_at", "supersedes")}
    if "flags" in r:
        r["flags"] = [f for f in r["flags"] if f not in ("late_fill", "late_ranking")]
    return r


def effective_map(rows):
    out = {}
    for file in ("daily", "rebalances", "benchmark"):
        for r in sp.effective_rows(rows, file):
            out[(file, r.get("candidate"), r.get("date") or r.get("nominal_date"))] = norm(r)
    return out


def ledger_rows(store):
    return (rsp.load_ledger_rows(store, "daily") + rsp.load_ledger_rows(store, "rebalances")
            + rsp.load_ledger_rows(store, "benchmark"))


def lines(b):
    return [json.loads(x) for x in b.decode("utf-8").splitlines() if x.strip()]


# --------------------------------------------------------------------------- #
# (a) ledger == plain step() over the same inputs
# --------------------------------------------------------------------------- #

def test_scenario_books_every_trading_day_through_the_last_closed_one(scenario):
    assert scenario.done == "2027-01-25"
    assert len(booked_days(scenario.done)) == 39
    assert rsp.load_state(scenario.store, {"start_date": "2026-12-01"}, CAL, PRIMARY)[0]["meta"]["status"] == "active"


def test_effective_ledger_equals_reference_step_run_bit_for_bit(scenario):
    ref_state, ref_rows = reference(scenario.world, scenario.store, booked_days(scenario.done))
    got = effective_map(ledger_rows(scenario.store))
    want = effective_map(ref_rows)
    assert got.keys() == want.keys()
    for k in want:
        assert got[k] == want[k], k
    snap = scenario.store.read_json(f"snapshots/{scenario.done}.json")
    assert json.loads(rsp.dumps(snap)) == json.loads(rsp.dumps(ref_state))        # the whole state, incl. meta


def test_clean_prefix_of_the_ledger_is_byte_identical_to_reference_rows(scenario):
    """Before any incident (jobs 12-01 .. 12-08) every ledger line equals the reference row, written_at included."""
    store = scenario.store
    days = [d for d in CAL if START <= d <= date(2026, 12, 7)]
    nxt = {d: CAL[CAL.index(d) + 1] for d in days}
    _, rows = reference(scenario.world, store, days, written_at=lambda t: now_str(nxt[t]))
    mem = rsp.Store(os.path.join(str(scenario.tmp), "mem"), dry_run=True)
    rsp.write_rows(mem, [r for r in rows if r["file"] != "meta"])
    hist = scenario.hist[date(2026, 12, 8)]
    assert set(mem._rows) == set(hist)
    for rel, texts in mem._rows.items():
        assert hist[rel].decode("utf-8").split("\n")[:-1] == texts, rel


def test_incident_flags_show_up_in_the_ledger(scenario):
    s = scenario.store
    daily = {r["date"]: r for r in sp.effective_rows(rsp.load_ledger_rows(s, "daily", S0), "daily", S0)}
    # suspension 12-22..12-24 freezes 1002 (if held), resume 12-25 clears it
    held_frozen = [d for d, r in daily.items() if "1002" in r["frozen"]]
    assert held_frozen == ["2026-12-22", "2026-12-23", "2026-12-24"]
    # ex-dividend closing exactly at the reference price leaves the position untouched
    assert "1005" in daily["2026-12-28"]["holdings"]
    assert daily["2026-12-29"]["holdings"]["1005"] == daily["2026-12-28"]["holdings"]["1005"]
    ev = scenario.world.events[(date(2026, 12, 29), "1005")]
    adj = s.read_json("adj/2026-12-29.json")
    assert adj["factors"]["1005"] == {"factor": ev["ref_price"] / ev["pre_close"], "source": "tpex"}
    assert adj["tables_ok"] == {"twse": True, "tpex": True}
    # benchmark: the day without a Yahoo bar carries forward
    bench = {r["date"]: r for r in sp.effective_rows(rsp.load_ledger_rows(s, "benchmark"), "benchmark")}
    assert "carry_forward" in bench["2026-12-15"]["flags"] and "carry_forward" not in bench["2026-12-16"]["flags"]


def test_s2_rebalances_on_its_complete_days_and_s3_applies_the_cap(scenario):
    s = scenario.store
    reb = {n: [r for r in rsp.load_ledger_rows(s, "rebalances", n)] for n in sp.CANDIDATES}
    for n in (S0, S1, S3):
        assert [r["nominal_date"] for r in reb[n] if r["supersedes"] is None] == ["2026-12-01", "2027-01-04"]
    assert [r["nominal_date"] for r in reb[S2] if r["supersedes"] is None] == ["2026-12-01", "2026-12-16", "2027-01-13"]
    jan = next(r for r in reb[S3] if r["nominal_date"] == "2027-01-04")
    per_block = {}
    for c in jan["targets"]:
        per_block[BLOCK[c]] = per_block.get(BLOCK[c], 0) + 1
    assert max(per_block.values()) <= 4 and len(jan["targets"]) == 20
    s1 = next(r for r in reb[S1] if r["nominal_date"] == "2027-01-04")
    assert sum(1 for c in s1["targets"] if BLOCK[c] == "A") > 4              # uncapped S1 holds more of block A


def test_skipped_jobs_give_a_late_ranking_and_flagged_rows(scenario):
    s = scenario.store
    jan4 = s.read_json("rankings/2027-01-04.json")
    assert set(jan4["candidates"]) == {S0, S1, S3} and all(v.get("late_ranking") for v in jan4["candidates"].values())
    assert all(v["asof"] == "2026-12-31" for v in jan4["candidates"].values())
    for n in (S0, S1, S3):
        r = next(x for x in rsp.load_ledger_rows(s, "rebalances", n) if x["nominal_date"] == "2027-01-04")
        assert "late_ranking" in r["flags"]
    # a ranking made on time carries no flag
    dec16 = s.read_json("rankings/2026-12-16.json")
    assert not any(v.get("late_ranking") for v in dec16["candidates"].values())
    assert s.read_json("s2/2027-01-04.json") is None and s.read_json("s2/2027-01-13.json")["complete"] is True


def test_stored_ranking_is_the_independent_ff_result(scenario):
    """The late ranking of 2027-01-04 (as of 2026-12-31) equals ff called directly on bars the test extends by hand
    (the cache ends 2026-11-27; 11-30 comes from the prefetch)."""
    w = scenario.world
    cache, _ = w.loader()
    uni = {}
    for code, d in cache.items():
        bars, pend = list(d["bars"]), 1.0
        for day in [x for x in CAL if date(2026, 11, 30) <= x <= date(2026, 12, 31)]:
            row = w.rows[code][day]
            f = w.adj_map(day).get(code, 1.0)
            if row["status"] == "no_close":
                pend *= f
                continue
            last = bars[-1]
            bars.append({"date": day.isoformat(), "o": row["o"], "h": row["h"], "l": row["l"], "c": row["c"],
                         "v": row["v"], "c_adj": last["c_adj"] * row["c"] / (last["c"] * pend * f)})
            pend = 1.0
        uni[code] = {**d, "bars": bars, "bar_dates": [b["date"] for b in bars]}
    stored = scenario.store.read_json("rankings/2027-01-04.json")["candidates"]
    for name, weights in (("S0_baseline", {"revenue": .4, "roe": .3, "momentum": .3}),
                          ("S1_high250", {"revenue": .4, "roe": .3, "high250": .3})):
        extra = ff.used_extra_factors([weights])
        ranks = ff.factor_ranks(uni, "2026-12-31", extra_factors=extra)
        scores = ff.composite_scores(uni, "2026-12-31", weights=weights, ranks=ranks, extra_factors=extra)
        assert stored[name]["ranked"] == sr.rank_top(scores, len(scores))
        assert stored[name]["detail"][0]["score"] == scores[stored[name]["ranked"][0]]
        assert len(stored[name]["detail"]) == min(200, len(scores))
    assert stored[S0]["ranked"] != stored[S1]["ranked"]                     # momentum vs high250 really differ


# --------------------------------------------------------------------------- #
# (c) idempotent, append only
# --------------------------------------------------------------------------- #

def test_every_job_only_appends_to_the_jsonl_files(scenario):
    prev = {}
    for d in sorted(scenario.hist):
        cur = scenario.hist[d]
        for rel, b in prev.items():
            assert cur[rel].startswith(b), (d, rel)
        prev = cur


def test_running_the_last_job_again_changes_nothing(scenario, tmp_path):
    copy_dir = str(tmp_path / "copy")
    shutil.copytree(scenario.ledger, copy_dir)
    before = tree_bytes(copy_dir)
    assert job(scenario.world, copy_dir, scenario.uni, scenario.ind, LAST_JOB) == 0
    after = tree_bytes(copy_dir)
    assert json.loads(after.pop("meta/last_run.json"))["processed_days"] == []        # nothing new was booked
    before.pop("meta/last_run.json")
    assert after == before


# --------------------------------------------------------------------------- #
# (d) retry re-run
# --------------------------------------------------------------------------- #

def test_retry_rerun_rows_all_supersede_their_originals(scenario):
    before, after = scenario.hist[date(2026, 12, 11)], scenario.hist[date(2026, 12, 14)]
    day_rel = [rel for rel in after if rel.endswith("daily.jsonl") or rel.endswith("benchmark_0050.jsonl")]
    assert day_rel
    for rel in day_rel:
        old_rows = lines(before[rel])
        new_rows = lines(after[rel][len(before[rel]):])
        by_date = {r["date"]: r for r in new_rows}
        assert set(by_date) == {"2026-12-09", "2026-12-10", "2026-12-11"}
        for dd in ("2026-12-09", "2026-12-10"):
            orig = next(r for r in old_rows if r["date"] == dd)
            assert by_date[dd]["supersedes"] == orig["written_at"]          # same file, same date, the original row
        assert by_date["2026-12-11"]["supersedes"] is None                  # a first booking, not a repair
    daily_new = lines(after["ledger/S0_baseline/daily.jsonl"][len(before["ledger/S0_baseline/daily.jsonl"]):])
    assert all("late_fill" in r["flags"] for r in daily_new if r["date"] != "2026-12-11")
    orig9 = next(r for r in lines(before["ledger/S0_baseline/daily.jsonl"]) if r["date"] == "2026-12-09")
    assert "missing_day" in orig9["flags"]
    eff9 = next(r for r in sp.effective_rows(rsp.load_ledger_rows(scenario.store, "daily", S0), "daily", S0)
                if r["date"] == "2026-12-09")
    assert "missing_day" not in eff9["flags"] and eff9["written_at"] == now_str(date(2026, 12, 14))


def test_a_market_missing_alone_is_a_whole_missing_day(scenario):
    orig = next(r for r in lines(scenario.hist[date(2026, 12, 10)]["ledger/S0_baseline/daily.jsonl"])
                if r["date"] == "2026-12-09")
    assert "missing_day" in orig["flags"] and orig["ret"] == 0.0
    pf = scenario.store.read_json("prices/2026-12-09.json")
    assert pf["twse"] is not None and pf["tpex"] is not None                 # repaired by the 12-14 job


# --------------------------------------------------------------------------- #
# (b) replay
# --------------------------------------------------------------------------- #

def replay(scenario, tmp_path, tamper=None):
    src = str(tmp_path / "src")
    shutil.copytree(scenario.ledger, src)
    if tamper:
        tamper(src)
    out = str(tmp_path / "out")
    scenario.world.forbid_fetch = True
    try:
        rc = rsp.main(["--ledger-dir", src, "--replay", "--out", out, "--now", now_str(LAST_JOB),
                       "--universe-file", scenario.uni, "--industries-file", scenario.ind])
    finally:
        scenario.world.forbid_fetch = False
    return rc, src, out


def test_replay_reproduces_every_nav_without_any_fetch(scenario, tmp_path, capsys):
    rc, src, out = replay(scenario, tmp_path)
    assert rc == 0
    assert "identical" in capsys.readouterr().out
    a, b = rsp.nav_series(rsp.Store(src)), rsp.nav_series(rsp.Store(out))
    assert a == b and len(a[S0]) == 39 and rsp.first_difference(a, b) is None
    assert os.path.isfile(os.path.join(out, "meta", "lock.json")) and os.path.isdir(os.path.join(out, "prices"))
    ref_state, _ = reference(scenario.world, rsp.Store(src), booked_days(scenario.done))
    assert json.loads(rsp.dumps(rsp.Store(out).read_json(f"snapshots/{scenario.done}.json"))) == \
        json.loads(rsp.dumps(ref_state))


def test_replay_refuses_a_non_empty_out_dir(scenario, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "x").write_text("keep", encoding="utf-8")
    rc = rsp.main(["--ledger-dir", scenario.ledger, "--replay", "--out", str(out), "--now", now_str(LAST_JOB),
                   "--universe-file", scenario.uni, "--industries-file", scenario.ind])
    assert rc == 1 and (out / "x").read_text(encoding="utf-8") == "keep"


def _edit_json(path, fn):
    doc = json.load(open(path, encoding="utf-8"))
    fn(doc)
    json.dump(doc, open(path, "w", encoding="utf-8"))


def test_replay_rebuilds_ranked_from_the_stored_percentiles_and_lists_the_differing_day(scenario, tmp_path, capsys):
    """A5d-2: a stored `ranked` that disagrees with the stored percentiles is replaced by the rebuilt order."""
    def tamper(src):
        _edit_json(os.path.join(src, "rankings", "2026-12-16.json"),
                   lambda d: d["candidates"][S2].update(ranked=list(reversed(d["candidates"][S2]["ranked"]))))
    rc, src, out = replay(scenario, tmp_path, tamper)
    text = capsys.readouterr().out
    assert rc == 0                                                        # the rebuilt ranking is the original one
    assert "differ from the stored ones on: 2026-12-16 S2_high250_revday" in text and "identical" in text
    assert rsp.nav_series(rsp.Store(src)) == rsp.nav_series(rsp.Store(out))


def test_replay_follows_a_changed_stored_percentile(scenario, tmp_path, capsys):
    def tamper(src):
        def f(d):
            e = d["candidates"][S2]
            for code in e["ranked"][:25]:                                 # push the top 25 to the bottom
                e["ranks"][code] = {k: 0.0 for k in e["ranks"][code]}
        _edit_json(os.path.join(src, "rankings", "2026-12-16.json"), f)
    rc, _, out = replay(scenario, tmp_path, tamper)
    text = capsys.readouterr().out
    assert rc == rsp.EXIT_REPLAY == 4
    assert "first difference 2026-12-16 S2_high250_revday" in text
    new = rsp.Store(out)
    assert next(r for r in rsp.load_ledger_rows(new, "rebalances", S2) if r["nominal_date"] == "2026-12-16")["targets"]         != next(r for r in rsp.load_ledger_rows(rsp.Store(scenario.ledger), "rebalances", S2)
                if r["nominal_date"] == "2026-12-16" and r["supersedes"] is None)["targets"]


def test_replay_reports_a_changed_stored_price(scenario, tmp_path, capsys):
    def tamper(src):
        _edit_json(os.path.join(src, "prices", "2027-01-08.json"),
                   lambda d: d["twse"]["1000"].update(c=d["twse"]["1000"]["c"] * 2))
    rc, _, _ = replay(scenario, tmp_path, tamper)
    assert rc == 4 and "first difference 2027-01-08" in capsys.readouterr().out


def test_replay_compares_the_0050_series_too(scenario, tmp_path, capsys):
    """Only the raw 0050 value of one day is changed: every candidate nav is identical, the 0050 series is not."""
    def tamper(src):
        _edit_json(os.path.join(src, "prices", "2026-12-22.json"),
                   lambda d: d["benchmark"]["series"].update({"2026-12-22": d["benchmark"]["series"]["2026-12-22"] * 1.1}))
    rc, src, out = replay(scenario, tmp_path, tamper)
    a, b = rsp.nav_series(rsp.Store(src)), rsp.nav_series(rsp.Store(out))
    assert all(a[n] == b[n] for n in sp.CANDIDATES) and a["benchmark_0050"] != b["benchmark_0050"]
    assert rc == 4 and "first difference 2026-12-22 benchmark_0050" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# (e) no look-ahead in the ranking
# --------------------------------------------------------------------------- #

def make_ranker(tmp_path, world, extra_cache_bars=None, poison_prices_from=None, stats=None):
    store = rsp.Store(str(tmp_path / "r"))
    for d in [x for x in CAL if START <= x <= date(2026, 12, 8)]:
        rows = world.complete_prices(d)
        if poison_prices_from and d >= poison_prices_from:
            for i, c in enumerate(CODES):
                rows[c]["c"] = rows[c]["o"] = rows[c]["h"] = rows[c]["l"] = 1_000_000.0 * (1 + (39 - i))
        tw = {c: e for c, e in rows.items() if MARKET[c] == "twse"}
        tp = {c: e for c, e in rows.items() if MARKET[c] == "tpex"}
        store.write_json(f"prices/{d.isoformat()}.json",
                         {"date": d.isoformat(), "twse": tw, "tpex": tp, "benchmark": None, "fetched_at": "x"})
        store.write_json(f"adj/{d.isoformat()}.json", {"date": d.isoformat(), "factors": {}, "fetched_at": "x"})

    def loader():
        cache = copy.deepcopy(world.cache)
        if extra_cache_bars:
            for code, d in cache.items():
                for bar in extra_cache_bars(code):
                    d["bars"].append(bar)
                d["bar_dates"] = [b["date"] for b in d["bars"]]
        return (cache, {}, stats) if stats else (cache, {})
    return rsp.Ranker(store, CAL, START, CODES, PRIMARY, loader=loader)


def _real_bars(world, code, days):
    out = []
    for d in days:
        r = world.rows[code][d]
        out.append({"date": d.isoformat(), "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": r["v"], "c_adj": r["c"]})
    return out


def test_ranking_for_day_d_ignores_stored_prices_from_d_onward(tmp_path):
    """Extension path: the cache ends 2026-11-27, days 12-01.. come from prices/; 12-03 and later are poisoned."""
    world = World()
    names = [S0, S1, S2, S3]
    day = date(2026, 12, 3)                                                  # as of 2026-12-02
    clean = make_ranker(tmp_path / "a", world).compute(day, names)
    assert clean[S0]["asof"] == "2026-12-02"
    poisoned = make_ranker(tmp_path / "b", world, poison_prices_from=date(2026, 12, 3)).compute(day, names)
    assert poisoned == clean


def test_ranking_for_day_d_ignores_cache_bars_from_d_onward(tmp_path):
    """Cache path: a weekly refresh that already holds bars for 12-03 / 12-04 (poisoned) must not leak into D = 12-03."""
    world = World()
    names = [S0, S1, S2, S3]
    day = date(2026, 12, 3)
    real = [date(2026, 12, 1), date(2026, 12, 2)]
    clean = make_ranker(tmp_path / "a", world, lambda c: _real_bars(world, c, real)).compute(day, names)
    poison = lambda c: _real_bars(world, c, real) + [
        {"date": d, "o": 9e9, "h": 9e9, "l": 9e9, "c": 9e9, "v": 9e9, "c_adj": 9e9} for d in ("2026-12-03", "2026-12-04")]
    poisoned = make_ranker(tmp_path / "b", world, poison, poison_prices_from=date(2026, 12, 3)).compute(day, names)
    assert poisoned == clean
    assert clean[S0]["asof"] == "2026-12-02"


def test_ranking_does_depend_on_the_day_before(tmp_path):
    """Sensitivity of the test above: poisoning the as-of day itself must change the ranking."""
    world = World()
    names = [S0, S1]
    day = date(2026, 12, 3)
    clean = make_ranker(tmp_path / "a", world).compute(day, names)
    poisoned = make_ranker(tmp_path / "b", world, poison_prices_from=date(2026, 12, 2)).compute(day, names)
    assert poisoned != clean


def test_ranking_is_skipped_when_the_asof_days_market_data_is_missing(tmp_path):
    world = World()
    r = make_ranker(tmp_path, world)
    r.inputs.write_json("prices/2026-12-02.json", {"date": "2026-12-02", "twse": None, "tpex": None,
                                                   "benchmark": None, "fetched_at": "x"})
    assert r.compute(date(2026, 12, 3), [S0]) == {}


# --------------------------------------------------------------------------- #
# (g) bars extension and the c_adj chain
# --------------------------------------------------------------------------- #

def _bar(d, c, c_adj=None, v=1000):
    return {"date": d, "o": c, "h": c, "l": c, "c": c, "v": v, "c_adj": c_adj}


def _row(c, status="ok"):
    return {"o": c, "h": c, "l": c, "c": c, "v": 10, "status": status}


def test_c_adj_chain_is_unchanged_when_the_close_equals_the_reference_price():
    ud = {"2330": {"bars": [_bar("2026-11-27", 100.0, 90.0)], "bar_dates": ["2026-11-27"], "revenue_series": []}}
    days = {"2026-11-30": ({"2330": _row(95.0, "exright")}, {"2330": 0.95})}
    out = rsp.extend_bars(ud, days, "2026-11-30")["2330"]
    assert [b["date"] for b in out["bars"]] == ["2026-11-27", "2026-11-30"] and out["bar_dates"] == ["2026-11-27", "2026-11-30"]
    assert out["bars"][1]["c_adj"] == pytest.approx(90.0, rel=1e-12) and out["bars"][1]["c"] == 95.0
    assert len(ud["2330"]["bars"]) == 1                                       # the input is not mutated


def test_c_adj_chain_follows_the_return_and_handles_gaps_and_missing_factors():
    ud = {"A": {"bars": [_bar("2026-11-27", 100.0, 50.0)], "bar_dates": ["2026-11-27"], "revenue_series": []}}
    days = {"2026-11-30": ({"A": _row(110.0)}, {}),                          # no factor entry = 1.0
            "2026-12-01": ({"A": _row(None, "no_close")}, {"A": 0.5}),        # suspension day: factor kept
            "2026-12-02": ({}, {"A": None}),                                 # absent code, None factor = 1.0
            "2026-12-03": ({"A": _row(55.0)}, {"A": 1.0}),
            "2026-12-04": ({"A": _row(1e9)}, {})}                             # after as-of: ignored
    bars = rsp.extend_bars(ud, days, "2026-12-03")["A"]["bars"]
    assert [b["date"] for b in bars] == ["2026-11-27", "2026-11-30", "2026-12-03"]
    assert bars[1]["c_adj"] == pytest.approx(50.0 * 110.0 / 100.0)
    assert bars[2]["c_adj"] == pytest.approx(bars[1]["c_adj"] * 55.0 / (110.0 * 0.5))      # 0.5 accumulated over the gap


def test_c_adj_chain_without_a_cache_c_adj_and_bars_not_after_the_cache_end():
    ud = {"A": {"bars": [_bar("2026-11-27", 100.0, None)], "bar_dates": ["2026-11-27"], "revenue_series": []}}
    days = {"2026-11-26": ({"A": _row(1.0)}, {}), "2026-11-30": ({"A": _row(103.0)}, {})}
    bars = rsp.extend_bars(ud, days, "2026-11-30")["A"]["bars"]
    assert len(bars) == 2 and bars[1]["c_adj"] == pytest.approx(103.0)         # falls back to the raw close


# --------------------------------------------------------------------------- #
# (h) 0050 chain
# --------------------------------------------------------------------------- #

def test_benchmark_chain_survives_a_rewritten_yahoo_scale():
    start = rsp.benchmark_close_for({"series": {"2026-11-30": 100.0, "2026-12-01": 101.0}},
                                    date(2026, 12, 1), "not_started", None, None)
    assert start == 101.0
    # next day Yahoo has rewritten history (everything x0.5 after an ex-dividend): only the ratio counts
    nxt = rsp.benchmark_close_for({"series": {"2026-12-01": 50.5, "2026-12-02": 51.01}},
                                  date(2026, 12, 2), "active", 101.0, "2026-12-01")
    assert nxt == pytest.approx(101.0 * 51.01 / 50.5)
    # a day without a real value in between: the move of the skipped day is kept
    skip = rsp.benchmark_close_for({"series": {"2026-12-01": 40.4, "2026-12-02": 40.804, "2026-12-03": 41.2}},
                                   date(2026, 12, 3), "active", 101.0, "2026-12-01")      # 12-02 was carried
    assert skip == pytest.approx(101.0 * 41.2 / 40.4)
    assert rsp.benchmark_close_for({"series": {"2026-12-02": 5.0}}, date(2026, 12, 3), "active", 1.0, "2026-12-02") is None
    assert rsp.benchmark_close_for(None, date(2026, 12, 3), "active", 1.0, "2026-12-02") is None


def test_ledger_0050_series_follows_the_true_index_despite_per_fetch_scales(scenario):
    w = scenario.world
    rows = {r["date"]: r for r in sp.effective_rows(rsp.load_ledger_rows(scenario.store, "benchmark"), "benchmark")}
    origin = rows["2026-12-01"]["adj_close"]
    assert origin == pytest.approx(w.scale(START) * w.index(START))
    for d in booked_days(scenario.done)[1:]:
        want = origin * w.index(d) / w.index(START)
        if d == BENCH_MISSING:                                                  # no Yahoo bar: carried, then caught up
            assert rows[d.isoformat()]["adj_close"] == rows["2026-12-14"]["adj_close"]
            continue
        assert rows[d.isoformat()]["adj_close"] == pytest.approx(want, rel=1e-9), d
    assert len({round(w.scale(d), 6) for d in booked_days(scenario.done)}) > 3      # the scales really differed


# --------------------------------------------------------------------------- #
# (f) lock checks, exit codes, dry run
# --------------------------------------------------------------------------- #

def test_a_real_run_before_the_start_date_is_refused_but_a_dry_run_works_and_writes_nothing(tmp_path, capsys):
    world = World()
    ledger, uni, ind = make_ledger(tmp_path)
    before = tree_bytes(ledger)
    assert job(world, ledger, uni, ind, date(2026, 11, 25)) == 3
    assert "before the start date" in capsys.readouterr().out
    assert tree_bytes(ledger) == before
    assert job(world, ledger, uni, ind, date(2026, 11, 25), extra=["--dry-run"]) == 0
    assert tree_bytes(ledger) == before


def test_dry_run_after_the_start_computes_but_leaves_the_directory_untouched(tmp_path):
    world = World()
    ledger, uni, ind = make_ledger(tmp_path)
    before = tree_bytes(ledger)
    for d in (date(2026, 12, 1), date(2026, 12, 2), date(2026, 12, 3)):
        assert job(world, ledger, uni, ind, d, extra=["--dry-run"]) == 0
    assert tree_bytes(ledger) == before


def test_snapshot_hash_mismatch_is_refused_in_every_mode(tmp_path, capsys):
    world = World()
    ledger, uni, ind = make_ledger(tmp_path)
    with open(uni, "w", encoding="utf-8") as f:
        json.dump(CODES[:-1], f)                                              # the locked universe was edited
    for extra in ([], ["--dry-run"]):
        assert job(world, ledger, uni, ind, date(2026, 12, 1), extra=extra) == 3
    assert "hash differs" in capsys.readouterr().out
    assert not os.path.isdir(os.path.join(ledger, "snapshots"))


def test_missing_lock_file_is_a_lock_failure(tmp_path):
    world = World()
    uni, ind = write_snapshots(tmp_path)
    (tmp_path / "empty").mkdir()
    assert job(world, str(tmp_path / "empty"), uni, ind, date(2026, 12, 1)) == 3


def test_an_empty_hash_in_lock_json_is_tolerated_only_in_a_dry_run(tmp_path):
    world = World()
    ledger, uni, ind = make_ledger(tmp_path)
    p = os.path.join(ledger, "meta", "lock.json")
    lock = json.load(open(p, encoding="utf-8"))
    lock["universe_sha256"] = ""
    json.dump(lock, open(p, "w", encoding="utf-8"))
    assert job(world, ledger, uni, ind, date(2026, 12, 1)) == 3
    assert job(world, ledger, uni, ind, date(2026, 12, 1), extra=["--dry-run"]) == 0


def test_twenty_missing_days_void_the_ledger_and_the_job_exits_2(tmp_path, capsys):
    world = World()
    world.fails = [(d, "twse", None) for d in CAL if date(2026, 12, 2) <= d]
    ledger, uni, ind = make_ledger(tmp_path)
    codes, last = [], None
    for d in weekdays(START, date(2026, 12, 31)):
        if d == START:
            assert job(world, ledger, uni, ind, d, extra=["--prefetch"]) == 0
        codes.append((d, job(world, ledger, uni, ind, d)))
    first_void = next(d for d, rc in codes if rc == 2)
    assert first_void == date(2026, 12, 30)                                   # books the 20th missing day (12-29)
    assert all(rc == 0 for d, rc in codes if d < first_void) and codes[-1][1] == 2
    out = capsys.readouterr().out
    assert "作廢" in out
    state = rsp.Store(ledger).read_json("snapshots/2026-12-29.json")
    assert state["meta"]["status"] == "void" and state["meta"]["void"]["trigger_date"] == "2026-12-29"
    assert rsp.Store(ledger).list_dates("snapshots")[-1] == "2026-12-29"      # nothing booked after the void


def test_a_job_that_died_mid_day_is_detected(scenario, tmp_path):
    copy_dir = str(tmp_path / "c")
    shutil.copytree(scenario.ledger, copy_dir)
    os.remove(os.path.join(copy_dir, "snapshots", "2027-01-25.json"))
    assert job(scenario.world, copy_dir, scenario.uni, scenario.ind, LAST_JOB) == 1


def test_summary_text_carries_the_notice_and_all_series(scenario, capsys):
    assert rsp.main(["--ledger-dir", scenario.ledger, "--summary"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "影子組合・未驗證・不是買賣訊號"
    for n in sp.CANDIDATES:
        assert n in out
    assert "0050" in out and "Sharpe" in out and "MDD" in out


# --------------------------------------------------------------------------- #
# Small units: serialisation, loader, calendar
# --------------------------------------------------------------------------- #

def test_dumps_keeps_key_order_never_uses_scientific_notation_and_keeps_chinese():
    obj = {"b": 1e-05, "a": [1.5, 2e20, -0.0, 1e-07], "n": None, "t": True, "名": "半導體業"}
    text = rsp.dumps(obj)
    assert text == '{"b":0.00001,"a":[1.5,200000000000000000000.0,-0.0,0.0000001],"n":null,"t":true,"名":"半導體業"}'
    back = json.loads(text)
    assert back["b"] == 1e-05 and back["a"][1] == 2e20 and list(back) == ["b", "a", "n", "t", "名"]
    with pytest.raises(ValueError):
        rsp.dumps({"x": float("nan")})
    assert json.loads(rsp.dumps(obj, indent=1)) == back


def test_default_loader_reads_the_cache_dir_skips_skip_codes_and_collects_fund_refresh(tmp_path, monkeypatch):
    cache = tmp_path / "factor_run"
    cache.mkdir()
    base = {"bars": [_bar("2026-11-27", 10.0, 10.0)], "revenue_series": [], "roe_series": []}
    (cache / "1111.json").write_text(json.dumps({**base, "fund_refresh": {"revenue": "2026-11-01"}}), encoding="utf-8")
    (cache / "2222.json").write_text(json.dumps({**base, "skip": True, "fund_refresh": {"revenue": "x"}}), encoding="utf-8")
    (cache / "3333.json").write_text(json.dumps(base), encoding="utf-8")
    (cache / "9999.json").write_text(json.dumps(base), encoding="utf-8")        # cached but not in the locked universe
    monkeypatch.setattr(rfi, "RUN_CACHE_DIR", str(cache))
    (cache / "5555.json").write_text("{not json", encoding="utf-8")
    data, fund, stats = rsp.default_load_universe(["1111", "2222", "3333", "4444", "5555"])
    assert sorted(data) == ["1111", "3333"]
    assert fund == {"1111": {"revenue": "2026-11-01"}, "3333": {}}
    assert stats == {"files": 3, "bad_json": 1}                              # skip file counts as present, bad json not


def test_a_nearly_empty_cache_is_a_data_error(tmp_path):
    world = World()
    r = rsp.Ranker(rsp.Store(str(tmp_path)), CAL, START, CODES, PRIMARY, loader=lambda: ({}, {}))
    with pytest.raises(rsp.ShadowError):
        r.loaded()


def test_calendar_is_stored_and_a_failed_fetch_falls_back_to_the_stored_copy(tmp_path):
    world = World()
    store = rsp.Store(str(tmp_path))
    cal = rsp.load_calendar(store, FakeFetchers(world), START, date(2026, 12, 1))
    assert date(2027, 1, 1) not in cal and date(2027, 1, 4) in cal
    assert set(store.read_json("meta/calendar.json")["years"]) == {"2026", "2027"}

    class Down(FakeFetchers):
        def calendar(self, year):
            return None
    assert rsp.load_calendar(store, Down(world), START, date(2026, 12, 2)) == cal
    with pytest.raises(rsp.ShadowError):
        rsp.load_calendar(rsp.Store(str(tmp_path / "other")), Down(world), START, date(2026, 12, 2))


def test_weights_are_the_locked_ones():
    assert rsp.WEIGHTS[S0] == {"revenue": 0.4, "roe": 0.3, "momentum": 0.3}
    for n in (S1, S2, S3):
        assert rsp.WEIGHTS[n] == {"revenue": 0.4, "roe": 0.3, "high250": 0.3}
        assert set(ff.used_extra_factors([rsp.WEIGHTS[n]])) == {"high250"}
    assert ff.MOMENTUM_DAYS == 60 and set(ff.RANKING_WEIGHTS) == set(rsp.WEIGHTS[S0])


def test_yahoo_0050_series_parser_reads_adjclose_by_taipei_date(monkeypatch):
    t = lambda y, m, d: int(__import__("datetime").datetime(y, m, d, 1, 0, tzinfo=__import__("datetime").timezone.utc).timestamp())
    payload = {"chart": {"result": [{"timestamp": [t(2026, 12, 1), t(2026, 12, 2), t(2026, 12, 3)],
                                     "indicators": {"adjclose": [{"adjclose": [100.5, None, 101.25]}]}}]}}
    monkeypatch.setattr(smd, "_get_json", lambda url, label: payload)
    assert rsp.fetch_yahoo_0050_series(date(2026, 12, 3)) == {"2026-12-01": 100.5, "2026-12-03": 101.25}
    monkeypatch.setattr(smd, "_get_json", lambda url, label: {"chart": {"result": None}})
    assert rsp.fetch_yahoo_0050_series(date(2026, 12, 3)) is None
    monkeypatch.setattr(smd, "_get_json", lambda url, label: None)
    assert rsp.fetch_yahoo_0050_series(date(2026, 12, 3)) is None
    raw = rsp.fetch_benchmark_raw(type("F", (), {"benchmark_series": lambda self, d, n: {"2026-12-01": 1.0, "2026-12-04": 9.0}})(),
                                  date(2026, 12, 3))
    assert raw == {"source": "yahoo", "series": {"2026-12-01": 1.0}}           # nothing after the day is kept


def test_build_adj_marks_an_exright_day_missing_from_the_table_as_adj_missing():
    world = World()
    fetch = FakeFetchers(world)
    tw = {"1000": {"o": 10, "h": 10, "l": 10, "c": 10, "v": 1, "status": "exright", "exright_kind": "unknown"},
          "1002": {"o": 10, "h": 10, "l": 10, "c": 10, "v": 1, "status": "ok", "exright_kind": None}}
    adj = rsp.build_adj(fetch, date(2026, 12, 2), tw, {}, "now")
    assert adj["factors"] == {"1000": {"factor": None, "source": "adj_missing"}}      # no safe fallback for TWSE X
    adj = rsp.build_adj(fetch, date(2026, 12, 29), None, None, "now")                  # market missing: tables only
    assert list(adj["factors"]) == ["1005"] and adj["factors"]["1005"]["source"] == "tpex"
    assert rsp.build_adj(fetch, date(2026, 12, 30), None, None, "now")["factors"]["1004"]["source"] == "twse"


# =========================================================================== #
# Review round 2
# =========================================================================== #

def run_jobs(world, ledger, uni, ind, first, last, skip=(), prefetch=True):
    """One job per weekday trading day in [first, last] (a --prefetch before the very first one)."""
    runs = {}
    for d in weekdays(first, last):
        if d in skip:
            continue
        if prefetch and d == first:
            assert job(world, ledger, uni, ind, d, extra=["--prefetch"]) == 0
        assert job(world, ledger, uni, ind, d) == 0
        runs[d] = json.load(open(os.path.join(ledger, "meta", "last_run.json"), encoding="utf-8"))
    return runs


def eff(ledger):
    return effective_map(ledger_rows(rsp.Store(ledger)))


def final_state(ledger):
    st = rsp.Store(ledger)
    return json.loads(rsp.dumps(st.read_json(f"snapshots/{st.list_dates('snapshots')[-1]}.json")))


# -- major 1: 0050 gap ------------------------------------------------------- #

def test_0050_chain_survives_a_30_day_yahoo_gap_and_alerts(tmp_path):
    world = World()
    world.fails = []
    missing = set(weekdays(date(2026, 12, 3), date(2027, 1, 4)))                 # 33 calendar days without a bar
    world.bench_missing = missing
    ledger, uni, ind = make_ledger(tmp_path)
    runs = run_jobs(world, ledger, uni, ind, START, date(2027, 1, 8))
    rows = {r["date"]: r for r in sp.effective_rows(rsp.load_ledger_rows(rsp.Store(ledger), "benchmark"), "benchmark")}
    origin = rows["2026-12-01"]["adj_close"]
    for d in weekdays(START, date(2027, 1, 7)):
        iso = d.isoformat()
        if d in missing:
            assert "carry_forward" in rows[iso]["flags"] and rows[iso]["adj_close"] == rows["2026-12-02"]["adj_close"]
        else:
            assert rows[iso]["adj_close"] == pytest.approx(origin * world.index(d) / world.index(START), rel=1e-9), d
    # total return equals the one computed straight from the complete index
    last = rsp.Store(ledger).read_json("snapshots/2027-01-07.json")["benchmark"]
    assert last["last_close"] / last["start_close"] - 1 == pytest.approx(
        world.index(date(2027, 1, 7)) / world.index(START) - 1, rel=1e-9)
    series = rsp.Store(ledger).read_json("prices/2027-01-05.json")["benchmark"]["series"]
    assert "2026-12-02" in series and min(series) <= "2026-11-25"                # window reached back past the gap
    texts = [a for r in runs.values() for a in r["alerts"]]
    assert any(a.startswith("0050 carry_forward for 4 consecutive days") for a in texts)
    assert any("0050 carry_forward on 2026-12-03" in a for a in texts)


def test_bench_lookback_grows_with_the_gap_and_is_capped(tmp_path):
    st = rsp.Store(str(tmp_path))
    assert rsp.bench_lookback(st, date(2026, 12, 2)) == 14
    st.append_rows(rsp.BENCH_REL, [{"date": "2026-12-01", "adj_close": 1.0, "flags": [], "written_at": "a"}])
    assert rsp.bench_lookback(st, date(2026, 12, 2)) == 14
    assert rsp.bench_lookback(st, date(2027, 1, 5)) == 35 + 7
    assert rsp.bench_lookback(st, date(2028, 6, 1)) == rsp.BENCH_LOOKBACK_MAX


# -- major 2: prefetch and the start ranking --------------------------------- #

def test_no_start_ranking_without_the_asof_days_data_then_prefetch_provides_it(tmp_path, capsys):
    world = World()
    ledger, uni, ind = make_ledger(tmp_path)
    assert job(world, ledger, uni, ind, START) == 0
    assert not os.path.exists(os.path.join(ledger, "rankings", "2026-12-01.json"))   # cache ends 11-27, nothing for 11-30
    out = capsys.readouterr().out
    assert "ALERT: ranking for 2026-12-01 not produced: whole-market data of 2026-11-30 is missing (run --prefetch)" in out
    alerts = json.load(open(os.path.join(ledger, "meta", "last_run.json"), encoding="utf-8"))["alerts"]
    assert any("not produced" in a for a in alerts)

    (tmp_path / "b").mkdir()
    ledger2, uni2, ind2 = make_ledger(tmp_path / "b")
    before = tree_bytes(ledger2)
    assert job(world, ledger2, uni2, ind2, START, extra=["--prefetch"]) == 0
    added = set(tree_bytes(ledger2)) - set(before)
    assert added == {"prices/2026-11-30.json", "adj/2026-11-30.json"}               # nothing else is written
    assert job(world, ledger2, uni2, ind2, START) == 0
    doc = rsp.Store(ledger2).read_json("rankings/2026-12-01.json")
    assert doc["data_through"] == {"median": "2026-11-30", "max": "2026-11-30"} and doc["flags"] == []
    assert all(v["asof"] == "2026-11-30" for v in doc["candidates"].values()) and len(doc["candidates"]) == 4


def test_a_start_without_a_ranking_slides_to_the_next_day_with_data(tmp_path):
    world = World()
    ledger, uni, ind = make_ledger(tmp_path)
    run_jobs(world, ledger, uni, ind, START, date(2026, 12, 4), prefetch=False)
    st = rsp.Store(ledger)
    first_build = next(r for r in rsp.load_ledger_rows(st, "rebalances", S0) if r["kind"] == "start")
    assert first_build["fill_date"] == "2026-12-02" and first_build["signal_asof"] == "2026-12-01"


def test_prefetch_skips_existing_days_failed_days_and_needs_no_ledger(tmp_path):
    world = World()
    ledger, uni, ind = make_ledger(tmp_path)
    world.fails = [(date(2026, 11, 30), "twse", None)]
    assert job(world, ledger, uni, ind, START, extra=["--prefetch"]) == 0
    assert set(tree_bytes(ledger)) == {"meta/lock.json"}                          # the failed day wrote nothing
    world.fails = []
    f1 = FakeFetchers(world)
    assert job(world, ledger, uni, ind, START, extra=["--prefetch"], fetchers=f1) == 0
    assert ("twse_close", date(2026, 11, 30)) in f1.calls
    f2 = FakeFetchers(world)
    assert job(world, ledger, uni, ind, START, extra=["--prefetch"], fetchers=f2) == 0
    assert f2.calls == []                                                          # already stored: nothing fetched
    assert job(world, ledger, uni, ind, date(2026, 11, 20), extra=["--prefetch"]) == 0        # before start: allowed


def test_ranking_with_stale_data_is_flagged_and_refused_for_the_start(tmp_path):
    world = World()
    r = make_ranker(tmp_path, world)
    for d in (date(2026, 12, 1), date(2026, 12, 2)):                  # 30 of 40 codes without a close on both days
        pf = r.inputs.read_json(f"prices/{d.isoformat()}.json")
        for code in CODES[:30]:
            pf[MARKET[code]][code] = {"o": None, "h": None, "l": None, "c": None, "v": None, "status": "no_close"}
        r.inputs.write_json(f"prices/{d.isoformat()}.json", pf)
    day = date(2026, 12, 3)
    assert r.compute(day, [S0], strict=True) == {}
    out = r.compute(day, [S0])
    assert out and r.last_meta["flags"] == ["stale_data"]
    assert r.last_meta["data_through"]["median"] == "2026-11-27" and r.last_meta["data_through"]["max"] == "2026-12-02"


def test_rankings_files_record_data_through_and_cache_coverage(scenario):
    doc = scenario.store.read_json("rankings/2026-12-16.json")
    assert doc["data_through"] == {"median": "2026-12-15", "max": "2026-12-15"}
    assert doc["cache_coverage"] == 1.0 and doc["bad_cache_files"] == 0 and doc["flags"] == []


def test_low_cache_coverage_is_flagged(tmp_path):
    world = World()
    r = make_ranker(tmp_path, world, stats={"files": 30, "bad_json": 2})
    rep = rsp.Report()
    r.report = rep
    assert r.compute(date(2026, 12, 3), [S0])
    assert r.last_meta["cache_coverage"] == 0.75 and r.last_meta["bad_cache_files"] == 2
    assert r.last_meta["flags"] == ["low_cache_coverage"]
    assert any(a.startswith("low_cache_coverage") for a in rep.alerts)


# -- major 3: a cut-off repair re-run ---------------------------------------- #

class Crash(Exception):
    pass


def arm_crash(monkeypatch, kind, rel, nth=1):
    orig = getattr(rsp.Store, kind)
    seen = {"n": 0}

    def wrapper(self, r, *a, **k):
        if r == rel:
            seen["n"] += 1
            if seen["n"] == nth:
                raise Crash(f"{kind} {rel}")
        return orig(self, r, *a, **k)
    monkeypatch.setattr(rsp.Store, kind, wrapper)
    return orig


CRASH_POINTS = [
    ("write_json", "adj/2026-12-09.json", 1),                         # marker written, nothing else yet
    ("write_json", "prices/2026-12-09.json", 1),                      # adj written, prices not
    ("write_json", "snapshots/2026-12-10.json", 1),                   # re-run: day 1 done, day 2 cut off
    ("append_rows", "ledger/S2_high250_revday/daily.jsonl", 2),       # re-run day 2: S0 / S1 rows in, S2 not
]


@pytest.mark.parametrize("kind,rel,nth", CRASH_POINTS)
def test_a_cut_off_repair_is_completed_by_the_next_job_without_forking(tmp_path, monkeypatch, kind, rel, nth):
    clean_w, crash_w = World(), World()
    (tmp_path / "clean").mkdir()
    (tmp_path / "crash").mkdir()
    cl, cu, ci = make_ledger(tmp_path / "clean")
    kl, ku, ki = make_ledger(tmp_path / "crash")
    run_jobs(clean_w, cl, cu, ci, START, date(2026, 12, 11))
    run_jobs(crash_w, kl, ku, ki, START, date(2026, 12, 11))
    orig = arm_crash(monkeypatch, kind, rel, nth)
    with pytest.raises(Crash):
        job(crash_w, kl, ku, ki, date(2026, 12, 14))
    monkeypatch.setattr(rsp.Store, kind, orig)
    assert os.path.exists(os.path.join(kl, "meta", "rerun_pending.json"))            # the cut-off is on record
    run_jobs(clean_w, cl, cu, ci, date(2026, 12, 14), date(2026, 12, 17), prefetch=False)
    run_jobs(crash_w, kl, ku, ki, date(2026, 12, 14), date(2026, 12, 17), prefetch=False)
    assert not os.path.exists(os.path.join(kl, "meta", "rerun_pending.json"))
    assert tree_bytes(kl) == tree_bytes(cl)                                           # bit for bit the undisturbed path
    assert "2026-12-09" not in final_state(kl)["meta"]["missing_days"]


def test_the_state_listing_a_day_whose_prices_are_complete_is_detected_as_a_fork(tmp_path):
    world = World()
    ledger, uni, ind = make_ledger(tmp_path)
    run_jobs(world, ledger, uni, ind, START, date(2026, 12, 11))
    rsp.Store(ledger).write_json("prices/2026-12-09.json", {"date": "2026-12-09", "benchmark": None, "fetched_at": "x",
                                                            "twse": {}, "tpex": {}})
    assert job(world, ledger, uni, ind, date(2026, 12, 12)) == 1


def test_a_dry_run_refuses_to_continue_over_a_pending_rerun(tmp_path):
    world = World()
    ledger, uni, ind = make_ledger(tmp_path)
    run_jobs(world, ledger, uni, ind, START, date(2026, 12, 2))
    rsp.Store(ledger).write_json("meta/rerun_pending.json", {"from": "2026-12-01", "through": "2026-12-01",
                                                              "days": ["2026-12-01"], "sizes": {}})
    assert job(world, ledger, uni, ind, date(2026, 12, 3), extra=["--dry-run"]) == 1


# -- major 5: line endings --------------------------------------------------- #

def test_lock_hash_ignores_line_endings_and_gitattributes_marks_the_snapshots(tmp_path):
    a, b, c = tmp_path / "a.json", tmp_path / "b.json", tmp_path / "c.json"
    a.write_bytes(b'{"x": 1,\n "y": [1, 2]}\n')
    b.write_bytes(b'{"x": 1,\r\n "y": [1, 2]}\r\n')
    c.write_bytes(b'{"x": 2,\n "y": [1, 2]}\n')
    assert rsp.sha256_file(str(a)) == rsp.sha256_file(str(b)) != rsp.sha256_file(str(c))
    attrs = open(os.path.join(os.path.dirname(HERE), ".gitattributes"), encoding="utf-8").read().split()
    assert "shadow/**" in attrs and "shadow_ledger/**" in attrs and attrs.count("-text") >= 2


def test_a_crlf_copy_of_the_locked_universe_still_passes_the_lock_check(tmp_path):
    LF, CRLF = bytes([10]), bytes([13, 10])
    world = World()
    ledger, uni, ind = make_ledger(tmp_path)
    open(uni, 'wb').write(json.dumps(CODES, indent=1).encode('utf-8') + LF)          # locked with LF endings
    lp = os.path.join(ledger, 'meta', 'lock.json')
    lock = json.load(open(lp, encoding='utf-8'))
    lock['universe_sha256'] = rsp.sha256_file(uni)
    json.dump(lock, open(lp, 'w', encoding='utf-8'))
    assert job(world, ledger, uni, ind, START, extra=['--dry-run']) == 0
    converted = open(uni, 'rb').read().replace(LF, CRLF)                             # checked out with autocrlf
    open(uni, 'wb').write(converted)
    assert CRLF in open(uni, 'rb').read()
    assert job(world, ledger, uni, ind, START, extra=['--dry-run']) == 0
    open(uni, 'wb').write(json.dumps(CODES[:-1], indent=1).encode('utf-8') + CRLF)   # a real edit is still caught
    assert job(world, ledger, uni, ind, START, extra=['--dry-run']) == 3


# -- minor a: ex-rights tables retried -------------------------------------- #

def test_a_failed_exright_table_is_fetched_again_and_the_return_corrected(tmp_path):
    fail_w, ok_w = World(), World()
    fail_w.fails = ok_w.fails = []
    fail_w.exright_fails = [(date(2026, 12, 29), "tpex", date(2026, 12, 31))]
    (tmp_path / "f").mkdir()
    (tmp_path / "o").mkdir()
    fl, fu, fi = make_ledger(tmp_path / "f")
    ol, ou, oi = make_ledger(tmp_path / "o")
    runs = run_jobs(fail_w, fl, fu, fi, START, date(2027, 1, 5))
    run_jobs(ok_w, ol, ou, oi, START, date(2027, 1, 5))
    orig = [r for r in lines(open(os.path.join(fl, "ledger", "S0_baseline", "daily.jsonl"), "rb").read())
            if r["date"] == "2026-12-29"]
    assert len(orig) == 2 and "adj_missing:1005" in orig[0]["flags"] and "adj_missing:1005" not in orig[1]["flags"]
    assert orig[0]["nav"] < orig[1]["nav"]                                          # the fake 5 % drop is gone
    assert orig[1]["supersedes"] == orig[0]["written_at"] and "late_fill" in orig[1]["flags"]
    assert eff(fl) == eff(ol) and final_state(fl) == final_state(ol)
    assert rsp.Store(fl).read_json("adj/2026-12-29.json")["tables_ok"] == {"twse": True, "tpex": True}
    assert any("adj_missing:1005" in a for r in runs.values() for a in r["alerts"])
    assert runs[date(2026, 12, 31)]["reran_days"] == ["2026-12-29"]


def test_an_exright_table_that_stays_down_is_not_retried_after_the_window(tmp_path):
    world = World()
    world.fails = []
    world.exright_fails = [(date(2026, 12, 29), "tpex", date(2027, 1, 6))]           # back only after the window
    ledger, uni, ind = make_ledger(tmp_path)
    run_jobs(world, ledger, uni, ind, START, date(2027, 1, 7))
    assert rsp.Store(ledger).read_json("adj/2026-12-29.json")["tables_ok"]["tpex"] is False


# -- minor b: last_run.json and alerts --------------------------------------- #

def test_last_run_json_lists_processed_days_reran_days_and_alerts(scenario):
    lr = scenario.last_runs
    last = lr[LAST_JOB]
    assert list(last) == ["run_at", "exit_code", "processed_days", "reran_days", "alerts", "missing_total",
                          "impaired", "void", "pending_retry"]
    assert last["exit_code"] == 0 and last["processed_days"] == ["2027-01-25"] and last["void"] is None
    assert "missing_day: no whole-market data for 2026-12-09" in lr[date(2026, 12, 10)]["alerts"]
    assert "pending_retry: 2026-12-09" in lr[date(2026, 12, 11)]["alerts"]
    assert lr[date(2026, 12, 14)]["reran_days"] == ["2026-12-09", "2026-12-10"] and lr[date(2026, 12, 14)]["pending_retry"] == []
    assert "0050 carry_forward on 2026-12-15" in lr[date(2026, 12, 16)]["alerts"]
    jan6 = lr[date(2027, 1, 6)]
    assert jan6["processed_days"] == ["2026-12-31", "2027-01-04", "2027-01-05"]
    assert any(a.startswith("late_ranking: S0_baseline nominal 2027-01-04") for a in jan6["alerts"])


def test_a_real_run_that_fails_for_data_reasons_still_writes_last_run(tmp_path):
    world = World()
    ledger, uni, ind = make_ledger(tmp_path)
    assert job(world, ledger, uni, ind, START) == 0
    rc = rsp.main(["--ledger-dir", ledger, "--now", now_str(date(2026, 12, 2)), "--universe-file", uni,
                   "--industries-file", ind], fetchers=FakeFetchers(world), loader=lambda: ({}, {}))
    assert rc == 1
    lr = json.load(open(os.path.join(ledger, "meta", "last_run.json"), encoding="utf-8"))
    assert lr["exit_code"] == 1 and any(a.startswith("error (exit 1)") for a in lr["alerts"])


# -- minor c: snapshot sanity ------------------------------------------------ #

@pytest.mark.parametrize("ind_obj", [
    {"data": RAW_INDUSTRIES},                                        # wrapped in another object
    {c: ["其他"] for c in CODES},                                     # nothing but 其他
    [["1000", "x"]],                                                 # not a mapping
    {**{c: ["其他"] for c in CODES[:25]}, **{c: ["IND_X"] for c in CODES[25:]}},   # < 50 % real industries
    {**{c: ["IND_A"] for c in CODES[:30]}, **{c: {"name": "x"} for c in CODES[30:]}},   # a value of the wrong type
])
def test_unusable_industry_snapshots_are_a_lock_failure(tmp_path, ind_obj, capsys):
    world = World()
    ledger, uni, ind = make_ledger(tmp_path, ind_obj=ind_obj)
    assert job(world, ledger, uni, ind, START) == 3
    assert "industries snapshot" in capsys.readouterr().out


def test_a_changed_industries_file_alone_fails_the_hash_check(tmp_path, capsys):
    world = World()
    ledger, uni, ind = make_ledger(tmp_path)
    obj = json.load(open(ind, encoding="utf-8"))
    obj["1000"] = ["IND_Z"]
    json.dump(obj, open(ind, "w", encoding="utf-8"))
    assert job(world, ledger, uni, ind, START) == 3
    assert "industries snapshot hash differs" in capsys.readouterr().out


# -- minor d: calendar ------------------------------------------------------- #

def test_a_fetched_calendar_never_rewrites_processed_days_but_updates_the_future(tmp_path):
    world = World()
    store = rsp.Store(str(tmp_path))
    cal1 = rsp.load_calendar(store, FakeFetchers(world), START, date(2026, 12, 1))

    class Changed(FakeFetchers):
        def calendar(self, year):
            days = super().calendar(year)
            if year == 2026:
                days.discard(date(2026, 12, 15))                 # a processed day suddenly a holiday
            if year == 2027:
                days.discard(date(2027, 1, 20))                  # a future day: legitimate update
            return days
    rep = rsp.Report()
    cal2 = rsp.load_calendar(store, Changed(world), START, date(2026, 12, 20), processed_through="2026-12-18", report=rep)
    assert date(2026, 12, 15) in cal2 and date(2027, 1, 20) not in cal2
    assert len(rep.alerts) == 1 and rep.alerts[0].startswith("calendar 2026") and "2026-12-15" in rep.alerts[0]
    assert "2026-12-15" in store.read_json("meta/calendar.json")["years"]["2026"]
    assert "2027-01-20" not in store.read_json("meta/calendar.json")["years"]["2027"]
    assert cal1 != cal2


# -- minor f: late s2 -------------------------------------------------------- #

def test_s2_forced_day_without_an_s2_file_records_the_coverage_late(tmp_path):
    world = World()
    world.fails = []
    world.fund_never_complete = True
    ledger, uni, ind = make_ledger(tmp_path)
    runs = run_jobs(world, ledger, uni, ind, START, date(2026, 12, 22), skip={date(2026, 12, 21)})
    st = rsp.Store(ledger)
    doc = st.read_json("s2/2026-12-21.json")
    assert doc["late_s2"] is True and doc["complete"] is False and doc["ratio"] == 0.75
    row = next(r for r in rsp.load_ledger_rows(st, "rebalances", S2) if r["nominal_date"] == "2026-12-21")
    assert row["s2_coverage"] == {"ratio": 0.75, "complete_day": None, "forced": True}
    assert "late_ranking" in row["flags"]
    assert any(a.startswith("late_s2") for a in runs[date(2026, 12, 22)]["alerts"])
    assert st.read_json("s2/2026-12-18.json").get("late_s2") is None                 # ordinary days are untouched


# -- minor g, h -------------------------------------------------------------- #

def test_main_switches_stdout_to_utf8(monkeypatch, tmp_path):
    calls = []

    class Out:
        def reconfigure(self, **kw):
            calls.append(kw)

        def write(self, text):
            return len(text)

        def flush(self):
            pass
    monkeypatch.setattr(sys, "stdout", Out())
    monkeypatch.setattr(sys, "stderr", Out())
    assert rsp.main(["--ledger-dir", str(tmp_path), "--summary"]) == 0
    assert calls == [{"encoding": "utf-8", "errors": "replace"}] * 2


def test_leftover_tmp_files_are_removed_when_a_job_starts(tmp_path):
    world = World()
    ledger, uni, ind = make_ledger(tmp_path)
    run_jobs(world, ledger, uni, ind, START, date(2026, 12, 2))
    for rel in ("meta/state.json.tmp", "prices/2026-12-01.json.tmp", "ledger/S0_baseline/x.tmp"):
        open(os.path.join(ledger, *rel.split("/")), "w").write("partial")
    assert job(world, ledger, uni, ind, date(2026, 12, 3)) == 0
    assert not [f for f in tree_bytes(ledger) if f.endswith(".tmp")]


# -- minor i: gaps the first review found ----------------------------------- #

def test_every_snapshot_equals_the_reference_state_of_its_day(scenario):
    reference(scenario.world, scenario.store, booked_days(scenario.done))
    assert len(reference.states) == 39
    for iso, ref in reference.states.items():
        assert json.loads(rsp.dumps(scenario.store.read_json(f"snapshots/{iso}.json"))) == json.loads(rsp.dumps(ref)), iso


def test_inputs_are_never_modified_once_written_except_by_the_repair(scenario):
    prev, allowed = {}, {date(2026, 12, 14): {"prices/2026-12-09.json", "adj/2026-12-09.json"}}
    changes = {}
    for d in sorted(scenario.filehash):
        cur = scenario.filehash[d]
        changes[d] = {rel for rel, h in prev.items() if cur.get(rel) != h}
        assert changes[d] <= allowed.get(d, set()), (d, changes[d])
        prev = cur
    assert changes[date(2026, 12, 14)] == allowed[date(2026, 12, 14)]                  # and the repair did change them


def test_the_retry_window_closes(tmp_path):
    world = World()
    world.fails = [(DOWN_DAY, "tpex", date(2026, 12, 15))]            # back one day after the window (12-14) ended
    ledger, uni, ind = make_ledger(tmp_path)
    run_jobs(world, ledger, uni, ind, START, date(2026, 12, 17))
    st = rsp.Store(ledger)
    assert st.read_json("prices/2026-12-09.json")["twse"] is None
    assert "2026-12-09" in st.read_json("snapshots/2026-12-16.json")["meta"]["missing_days"]
    assert not any("late_fill" in r.get("flags", []) for r in rsp.load_ledger_rows(st, "daily"))


def test_s2_status_is_computed_for_today_and_the_file_is_written_once(tmp_path, monkeypatch):
    world = World()
    seen = []
    orig = rsp.Ranker.s2_status
    monkeypatch.setattr(rsp.Ranker, "s2_status", lambda self, day: (seen.append(day), orig(self, day))[1])
    ledger, uni, ind = make_ledger(tmp_path)
    run_jobs(world, ledger, uni, ind, START, START)
    assert seen == [START]
    world.fails = []
    run_jobs(world, ledger, uni, ind, date(2026, 12, 2), date(2026, 12, 16), prefetch=False)
    st = rsp.Store(ledger)
    assert st.read_json("s2/2026-12-16.json")["complete"] is True
    before = tree_bytes(ledger)["s2/2026-12-16.json"]
    stale = {c: {"revenue": "2000-01-01"} for c in CODES}
    rc = rsp.main(["--ledger-dir", ledger, "--now", now_str(date(2026, 12, 16)), "--universe-file", uni,
                   "--industries-file", ind], fetchers=FakeFetchers(world), loader=lambda: (world.cache, stale))
    assert rc == 0 and tree_bytes(ledger)["s2/2026-12-16.json"] == before


def test_bars_extension_starts_from_each_codes_own_last_bar(tmp_path):
    world = World()
    r = make_ranker(tmp_path, world)
    full = r.loader

    def loader():
        cache, fund = full()
        for i, code in enumerate(CODES):
            if i % 2 == 0:                                            # these caches end a day earlier (11-26)
                d = cache[code]
                d["bars"] = d["bars"][:-1]
                d["bar_dates"] = d["bar_dates"][:-1]
        return cache, fund
    r.loader = loader
    row = {"o": 50.0, "h": 50.0, "l": 50.0, "c": 50.0, "v": 5, "status": "ok"}
    r.inputs.write_json("prices/2026-11-27.json", {
        "date": "2026-11-27", "benchmark": None, "fetched_at": "x",
        "twse": {c: dict(row) for c in CODES if MARKET[c] == "twse"},
        "tpex": {c: dict(row) for c in CODES if MARKET[c] == "tpex"}})
    ud = r.extended_universe("2026-12-02")
    assert ud["1000"]["bar_dates"][-4:] == ["2026-11-26", "2026-11-27", "2026-12-01", "2026-12-02"]
    assert ud["1001"]["bar_dates"][-3:] == ["2026-11-27", "2026-12-01", "2026-12-02"]
    assert ud["1000"]["bars"][-3]["c"] == 50.0                         # the 11-27 bar of an early-ending cache comes from prices/


def _big_cache(n):
    rng = random.Random(5)
    out = {}
    for i in range(n):
        p, bars = 100.0 + i % 10, []
        for d in HIST_DAYS[-300:]:
            p = round(p * (1 + rng.gauss(0, 0.01)), 2)
            bars.append({"date": d.isoformat(), "o": p, "h": p, "l": p, "c": p, "v": 500_000, "c_adj": p})
        out[f"{3000 + i}"] = {"bars": bars, "bar_dates": [b["date"] for b in bars],
                              "revenue_series": [{"usable_date": "2025-12-10", "yoy_pct": rng.random()}],
                              "roe_series": [{"usable_date": "2025-12-10", "roe_pct": rng.random()}]}
    return out


def test_a_universe_of_more_than_200_codes_stores_every_rank_but_a_200_row_detail(tmp_path):
    cache = _big_cache(230)
    store = rsp.Store(str(tmp_path))
    store.write_json("prices/2026-11-27.json", {"date": "2026-11-27", "twse": {}, "tpex": {}, "benchmark": None,
                                                "fetched_at": "x"})
    r = rsp.Ranker(store, CAL, START, sorted(cache), {}, loader=lambda: (cache, {}))
    out = r.compute(date(2026, 11, 30), [S0, S1])
    for name in (S0, S1):
        e = out[name]
        assert len(e["ranked"]) == 230 and len(e["ranks"]) == 230 and len(e["detail"]) == rsp.RANKING_DETAIL_N == 200
        assert [d["code"] for d in e["detail"]] == e["ranked"][:200]
        assert list(e["ranks"][e["ranked"][0]]) == list(e["weights"])
    ent, changed = rsp.rederive(out[S1], {})                            # deeper ranks make the rebuild possible
    assert ent["ranked"] == out[S1]["ranked"] and not changed


def test_a_failed_json_write_leaves_the_old_file_intact(tmp_path, monkeypatch):
    store = rsp.Store(str(tmp_path))
    store.write_json("a/x.json", {"v": 1})

    def boom(fd):
        raise OSError("disk full")
    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError):
        store.write_json("a/x.json", {"v": 2, "pad": "x" * 10000})
    assert json.load(open(os.path.join(str(tmp_path), "a", "x.json"), encoding="utf-8")) == {"v": 1}


def test_a_prefetched_day_is_booked_from_the_stored_file_not_fetched_again(tmp_path):
    world = World()
    ledger, uni, ind = make_ledger(tmp_path)
    run_jobs(world, ledger, uni, ind, START, START)
    assert job(world, ledger, uni, ind, date(2026, 12, 2), extra=["--prefetch"]) == 0          # stores 12-01
    assert rsp.Store(ledger).exists("prices/2026-12-01.json")
    fetchers = FakeFetchers(world)
    assert job(world, ledger, uni, ind, date(2026, 12, 2), fetchers=fetchers) == 0
    assert ("twse_close", date(2026, 12, 1)) not in fetchers.calls and ("tpex_close", date(2026, 12, 1)) not in fetchers.calls
    assert rsp.Store(ledger).list_dates("snapshots") == ["2026-12-01"]


def test_rederive_reproduces_every_stored_ranking_of_the_scenario(scenario):
    for iso in scenario.store.list_dates("rankings"):
        for name, entry in scenario.store.read_json(f"rankings/{iso}.json")["candidates"].items():
            new, changed = rsp.rederive(entry, PRIMARY)
            assert not changed and new["detail"] == entry["detail"], (iso, name)
