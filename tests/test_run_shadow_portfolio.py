# -*- coding: utf-8 -*-
"""Tests for scripts/run_shadow_portfolio.py (the daily driver of the shadow portfolio).

No network: every fetcher is a fake over a hand-made 40-code world (spec A5: nothing here backtests
S2 / S3 on real data; the prices are seeded random walks).  The world spans ~40 trading days from the
start day 2026-12-01 and contains: the start build, a month-start rebalance with the S3 sector cap, an
S2 rebalance on its "revenue refresh complete" day, a day on which one market fails and is fetched
again three days later (re-run with ``supersedes``), an ex-dividend, a suspension and resume, a day
without a 0050 bar, Yahoo-style rewritten 0050 scales, and two skipped jobs (late ranking)."""
import copy
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

    def benchmark_series(self, T):
        k = self.scale(T)
        return {d.isoformat(): k * self.index(d) for d in CAL
                if T - timedelta(days=rsp.BENCH_LOOKBACK_DAYS) <= d <= T and d not in self.bench_missing}

    def is_down(self, d, market):
        return any(fd == d and fm == market and (until is None or self.job_day < until)
                   for fd, fm, until in self.fails)

    def fund_refresh(self, job_day):
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
    adj_fallback = None

    def __init__(self, world):
        self.w = world

    def _check(self):
        assert not self.w.forbid_fetch, "a fetch happened where none is allowed"

    def _close(self, d, market):
        self._check()
        if d not in CAL_SET or self.w.is_down(d, market):
            return None
        return {c: dict(self.w.rows[c][d]) for c in CODES if MARKET[c] == market}

    def twse_close(self, d):
        return self._close(d, "twse")

    def tpex_close(self, d):
        return self._close(d, "tpex")

    def _exright(self, s, e, market):
        self._check()
        return {k: dict(v) for k, v in self.w.events.items() if MARKET[k[1]] == market and s <= k[0] <= e}

    def twse_exright(self, s, e):
        return self._exright(s, e, "twse")

    def tpex_exright(self, s, e):
        return self._exright(s, e, "tpex")

    def calendar(self, year):
        self._check()
        return set(weekdays(date(year, 1, 1), date(year, 12, 31))) if year in (2026, 2027) else None

    def benchmark_series(self, d):
        self._check()
        return self.w.benchmark_series(d) if d in CAL_SET else None


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("real network access in a test")
    monkeypatch.setattr(smd, "_get_json", boom)
    monkeypatch.setattr(smd.requests, "get", boom)


def write_snapshots(tmp_path):
    uni = tmp_path / "universe.json"
    ind = tmp_path / "industries.json"
    uni.write_text(json.dumps(CODES), encoding="utf-8")
    ind.write_text(json.dumps(RAW_INDUSTRIES), encoding="utf-8")
    return str(uni), str(ind)


def make_ledger(tmp_path, name="ledger", start="2026-12-01"):
    uni, ind = write_snapshots(tmp_path)
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
    sc.hist = {}
    for d in JOB_DAYS:
        if d in SKIP_JOBS:
            continue
        assert job(world, ledger, uni, ind, d) == 0
        sc.hist[d] = jsonl_files(ledger)
    sc.store = rsp.Store(ledger)
    sc.done = sc.store.list_dates("snapshots")[-1]
    return sc


def booked_days(done):
    return [d for d in CAL if START <= d <= date.fromisoformat(done)]


def reference(world, store, days, written_at=None):
    """Independent drive of ``step`` over `days`: truth from the world, rankings / s2 from the stored files."""
    cal_iso = [d.isoformat() for d in weekdays(date(2026, 1, 1), date(2027, 12, 31))]
    state = sp.new_state(START, cal_iso, PRIMARY)
    rows_all, last_real = [], None
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
    (the job has stored nothing for 2026-11-30, so the chain starts at 12-01)."""
    w = scenario.world
    cache, _ = w.loader()
    uni = {}
    for code, d in cache.items():
        bars, pend = list(d["bars"]), 1.0
        for day in [x for x in CAL if date(2026, 12, 1) <= x <= date(2026, 12, 31)]:
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
    assert tree_bytes(copy_dir) == before


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


def test_replay_reports_the_first_differing_day_when_a_stored_ranking_is_replaced(scenario, tmp_path, capsys):
    def tamper(src):
        p = os.path.join(src, "rankings", "2026-12-16.json")
        doc = json.load(open(p, encoding="utf-8"))
        doc["candidates"][S2]["ranked"] = list(reversed(doc["candidates"][S2]["ranked"]))
        json.dump(doc, open(p, "w", encoding="utf-8"))
    rc, _, _ = replay(scenario, tmp_path, tamper)
    assert rc == 1
    assert "first difference 2026-12-16 S2_high250_revday" in capsys.readouterr().out


def test_replay_reports_a_changed_stored_price(scenario, tmp_path, capsys):
    def tamper(src):
        p = os.path.join(src, "prices", "2027-01-08.json")
        doc = json.load(open(p, encoding="utf-8"))
        doc["twse"]["1000"]["c"] = doc["twse"]["1000"]["c"] * 2
        json.dump(doc, open(p, "w", encoding="utf-8"))
    rc, _, _ = replay(scenario, tmp_path, tamper)
    assert rc == 1 and "first difference 2027-01-08" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# (e) no look-ahead in the ranking
# --------------------------------------------------------------------------- #

def make_ranker(tmp_path, world, extra_cache_bars=None, poison_prices_from=None):
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
        return cache, {}
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
    data, fund = rsp.default_load_universe(["1111", "2222", "3333", "4444"])
    assert sorted(data) == ["1111", "3333"]
    assert fund == {"1111": {"revenue": "2026-11-01"}, "3333": {}}


def test_a_nearly_empty_cache_is_a_data_error(tmp_path):
    world = World()
    r = rsp.Ranker(rsp.Store(str(tmp_path)), CAL, START, CODES, PRIMARY, loader=lambda: ({}, {}))
    with pytest.raises(rsp.ShadowError):
        r.compute(date(2026, 12, 1), [S0])


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
    raw = rsp.fetch_benchmark_raw(type("F", (), {"benchmark_series": lambda self, d: {"2026-12-01": 1.0, "2026-12-04": 9.0}})(),
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
