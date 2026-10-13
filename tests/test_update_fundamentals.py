# -*- coding: utf-8 -*-
"""Tests for scripts/update_fundamentals.py -- target-period math, series
merging, per-code refresh and the time-boxed run loop. No network: the fetch
functions, probe and clock are injected."""
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import pytest  # noqa: E402

import run_factor_backtest as rfb  # noqa: E402
import update_fundamentals as uf  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(rfb, "RUN_CACHE_DIR", str(tmp_path))


class NoLimit:
    def __init__(self):
        self.calls = 0

    def before_request(self):
        self.calls += 1


def D(s):
    return dt.date.fromisoformat(s)


def _write(code, obj):
    with open(rfb.code_cache_path(code), "w", encoding="utf-8") as f:
        json.dump(obj, f)


def _read(code):
    with open(rfb.code_cache_path(code), encoding="utf-8") as f:
        return json.load(f)


def _revenue_rows(start_year, start_month, n):
    rows = []
    y, m = start_year, start_month
    for i in range(n):
        rows.append({"date": f"{y:04d}-{m:02d}-01", "revenue": 100.0 + i, "announce_date": None})
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return rows


def _fin_rows(quarter_ends):
    return [{"date": d, "net_income": 10.0, "equity": 100.0, "total_assets": 200.0,
             "operating_cash_flow": 12.0, "revenue": 500.0, "gross_profit": 200.0,
             "operating_income": 80.0} for d in quarter_ends]


# ---- target periods ------------------------------------------------------

@pytest.mark.parametrize("today,expected", [
    ("2026-10-11", "2026-09-01"),   # past the 10th -> last month
    ("2026-10-10", "2026-08-01"),   # deadline day itself is not yet safe
    ("2026-01-05", "2025-11-01"),   # year wrap
    ("2026-02-11", "2026-01-01"),
])
def test_revenue_target(today, expected):
    assert uf.revenue_target(D(today)) == expected


@pytest.mark.parametrize("today,expected", [
    ("2026-05-15", "2025-12-31"),   # Q1 deadline not strictly passed yet
    ("2026-05-16", "2026-03-31"),
    ("2026-10-11", "2026-06-30"),
    ("2026-11-14", "2026-06-30"),
    ("2026-11-15", "2026-09-30"),
    ("2026-03-31", "2025-09-30"),   # Q4 deadline is 3/31 itself
    ("2026-04-01", "2025-12-31"),
])
def test_financials_target(today, expected):
    assert uf.financials_target(D(today)) == expected


# ---- merge_series --------------------------------------------------------

def test_merge_keeps_history_and_replaces_overlap():
    old = [{"usable_date": "2024-01-10", "v": 1}, {"usable_date": "2024-02-10", "v": 2},
           {"usable_date": "2024-03-10", "v": 3}]
    new = [{"usable_date": "2024-03-12", "v": 30}, {"usable_date": "2024-04-10", "v": 4}]
    out = uf.merge_series(old, new)
    # Mar point is the same period (2 days apart, within tolerance) -> replaced, not duplicated.
    assert [p["v"] for p in out] == [1, 2, 30, 4]


def test_merge_empty_new_leaves_old_untouched():
    old = [{"usable_date": "2024-01-10", "v": 1}]
    assert uf.merge_series(old, []) == old
    assert uf.merge_series(None, []) == []


def test_merge_clamps_usable_date_monotonic():
    old = [{"usable_date": "2024-05-20", "v": 1}]
    new = [{"usable_date": "2024-07-01", "v": 2}, {"usable_date": "2024-06-15", "v": 3}]
    out = uf.merge_series(old, new)
    dates = [p["usable_date"] for p in out]
    assert dates == sorted(dates)


# ---- is_due / refresh_code -----------------------------------------------

def test_is_due_rules():
    today = D("2026-10-11")
    live = {"bars": [{"date": "2026-10-09"}]}
    assert uf.is_due(live, "revenue", "2026-09-01", today)
    assert not uf.is_due({"skip": True}, "revenue", "2026-09-01", today)
    assert not uf.is_due({"bars": []}, "revenue", "2026-09-01", today)
    assert not uf.is_due({**live, "fund_refresh": {"revenue": "2026-09-01"}}, "revenue", "2026-09-01", today)
    assert not uf.is_due({**live, "fund_refresh": {"revenue_attempt": "2026-10-11"}}, "revenue", "2026-09-01", today)
    assert uf.is_due({**live, "fund_refresh": {"revenue_attempt": "2026-10-10"}}, "revenue", "2026-09-01", today)


def test_refresh_revenue_merges_into_history_and_marks_target():
    today = D("2026-10-11")
    old_series = [{"usable_date": "2020-01-10", "yoy_pct": 1.0}, {"usable_date": "2026-01-10", "yoy_pct": 2.0}]
    _write("2330", {"bars": [{"date": "2026-10-09"}], "revenue_series": old_series, "days": 5300})
    rows = _revenue_rows(2024, 9, 25)  # 2024-09 .. 2026-09
    limiter = NoLimit()
    res = uf.refresh_code("2330", "revenue", "2026-09-01", today, False, limiter,
                          fetch_revenue=lambda code, days, cache: rows)
    assert res["status"] == "updated"
    assert limiter.calls == 1
    cached = _read("2330")
    assert cached["revenue_series"][0] == old_series[0]  # pre-window history survives
    assert len(cached["revenue_series"]) > 2
    assert cached["fund_refresh"]["revenue"] == "2026-09-01"
    assert cached["days"] == 5300 and cached["bars"] == [{"date": "2026-10-09"}]


def test_refresh_revenue_target_not_published_marks_attempt_only():
    today = D("2026-10-11")
    _write("2330", {"bars": [{"date": "x"}], "revenue_series": []})
    rows = _revenue_rows(2024, 8, 25)  # ends 2026-08: target 2026-09 not there yet
    res = uf.refresh_code("2330", "revenue", "2026-09-01", today, False, NoLimit(),
                          fetch_revenue=lambda code, days, cache: rows)
    assert res["status"] == "stale"
    fr = _read("2330")["fund_refresh"]
    assert "revenue" not in fr and fr["revenue_attempt"] == "2026-10-11"


def test_refresh_failure_writes_nothing_so_it_is_retried():
    # A transient 429/timeout returns None just like "no data": it must not
    # consume the once-per-day attempt, or it would wait for the next schedule.
    today = D("2026-10-11")
    old = [{"usable_date": "2026-01-10", "yoy_pct": 2.0}]
    _write("2330", {"bars": [{"date": "x"}], "revenue_series": old})
    res = uf.refresh_code("2330", "revenue", "2026-09-01", today, False, NoLimit(),
                          fetch_revenue=lambda code, days, cache: None)
    assert res["status"] == "failed"
    cached = _read("2330")
    assert cached["revenue_series"] == old
    assert "fund_refresh" not in cached
    assert uf.is_due(cached, "revenue", "2026-09-01", today)


def test_refresh_insufficient_history_is_terminal_for_the_period():
    today = D("2026-10-11")
    _write("6999", {"bars": [{"date": "x"}], "revenue_series": []})
    rows = _revenue_rows(2026, 5, 5)  # 5 months: no YoY point possible
    res = uf.refresh_code("6999", "revenue", "2026-09-01", today, False, NoLimit(),
                          fetch_revenue=lambda code, days, cache: rows)
    assert res["status"] == "insufficient"
    cached = _read("6999")
    assert cached["fund_refresh"]["revenue"] == "2026-09-01"
    assert not uf.is_due(cached, "revenue", "2026-09-01", today)


def test_refresh_financials_missing_cashflow_is_not_marked_done():
    today = D("2026-10-11")
    _write("2330", {"bars": [{"date": "x"}]})
    qends = ["2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31", "2025-03-31", "2025-06-30",
             "2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"]
    rows = _fin_rows(qends)
    rows[-1]["operating_cash_flow"] = None  # cash-flow request failed for the newest quarter
    res = uf.refresh_code("2330", "financials", "2026-06-30", today, False, NoLimit(),
                          fetch_financials=lambda code, days, cache: rows)
    assert res["status"] == "stale"
    assert "financials" not in _read("2330")["fund_refresh"]


def test_revenue_merge_uses_fixed_lag_anchor_not_shifted_announce_date():
    # Cached point for the first refetched month used the fixed lag (2025-10-10).
    # The refetch carries a much later announce_date for that month; cutting by
    # new[0] alone would keep the old point too and duplicate the period.
    today = D("2026-10-11")
    rows = _revenue_rows(2024, 9, 25)
    rows[12]["announce_date"] = "2025-11-20"  # first YoY month (2025-09): restated, late
    _write("2330", {"bars": [{"date": "x"}],
                    "revenue_series": [{"usable_date": "2025-09-10", "yoy_pct": 1.0},
                                       {"usable_date": "2025-10-10", "yoy_pct": 2.0}]})
    uf.refresh_code("2330", "revenue", "2026-09-01", today, False, NoLimit(),
                    fetch_revenue=lambda code, days, cache: rows)
    series = _read("2330")["revenue_series"]
    assert [p["usable_date"] for p in series if p["yoy_pct"] in (1.0, 2.0)] == ["2025-09-10"]
    assert len(series) == 1 + 13  # the one kept old point + 13 fresh YoY points


def test_first_yoy_lag_date():
    assert uf.first_yoy_lag_date(_revenue_rows(2024, 9, 25)) == "2025-10-10"
    assert uf.first_yoy_lag_date(_revenue_rows(2026, 1, 5)) is None


def test_refresh_financials_updates_all_derived_series():
    today = D("2026-10-11")
    _write("2330", {"bars": [{"date": "x"}], "roe_series": [{"usable_date": "2019-01-01", "roe_pct": 9.0}]})
    qends = ["2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31", "2025-03-31", "2025-06-30",
             "2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"]
    limiter = NoLimit()
    res = uf.refresh_code("2330", "financials", "2026-06-30", today, False, limiter,
                          fetch_financials=lambda code, days, cache: _fin_rows(qends))
    assert res["status"] == "updated"
    assert limiter.calls == 3
    cached = _read("2330")
    assert cached["roe_series"][0]["usable_date"] == "2019-01-01"
    assert cached["roe_series"][-1]["usable_date"] == "2026-08-29"  # 2026-06-30 + 60d
    assert cached["fund_refresh"]["financials"] == "2026-06-30"
    assert "accrual_quality_series" in cached and "gross_margin_trend_series" in cached


def test_refresh_dry_run_writes_nothing():
    today = D("2026-10-11")
    _write("2330", {"bars": [{"date": "x"}], "revenue_series": []})
    uf.refresh_code("2330", "revenue", "2026-09-01", today, True, NoLimit(),
                    fetch_revenue=lambda code, days, cache: _revenue_rows(2024, 9, 25))
    assert "fund_refresh" not in _read("2330")


def test_skip_entries_are_never_touched():
    _write("9999", {"skip": True, "days": 5300})
    res = uf.refresh_code("9999", "revenue", "2026-09-01", D("2026-10-11"), False, NoLimit(),
                          fetch_revenue=lambda *a, **k: pytest.fail("must not fetch"))
    assert res["status"] == "skipped"


# ---- build_todo / run ----------------------------------------------------

def test_build_todo_orders_revenue_before_financials_and_excludes_done():
    today = D("2026-10-11")
    _write("1101", {"bars": [{"date": "x"}]})
    _write("1102", {"bars": [{"date": "x"}], "fund_refresh": {"revenue": "2026-09-01"}})
    _write("1103", {"skip": True})
    todo = uf.build_todo(["1101", "1102", "1103"], ["revenue", "financials"], today)
    assert todo == [("revenue", "1101", "2026-09-01"), ("financials", "1101", "2026-06-30"),
                    ("financials", "1102", "2026-06-30")]


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_run_stops_at_budget():
    clock = FakeClock()

    def refresh(code, kind, target, today, dry_run, limiter):
        clock.t += 60
        return {"status": "updated", "detail": ""}

    todo = [("revenue", f"{i:04d}", "t") for i in range(10)]
    counts, attempted, _ = uf.run(todo, D("2026-10-11"), False, 180, NoLimit(), refresh=refresh, clock=clock)
    assert attempted == 3 and counts["updated"] == 3


def test_run_aborts_when_failures_streak_and_probe_fails():
    refresh = lambda *a: {"status": "failed", "detail": "boom"}  # noqa: E731
    todo = [("revenue", f"{i:04d}", "t") for i in range(20)]
    with pytest.raises(RuntimeError):
        uf.run(todo, D("2026-10-11"), False, 1e9, NoLimit(), refresh=refresh, probe=lambda: False)


def test_run_continues_when_probe_ok_after_failure_streak():
    refresh = lambda *a: {"status": "failed", "detail": "no data"}  # noqa: E731
    todo = [("revenue", f"{i:04d}", "t") for i in range(25)]
    counts, attempted, failures = uf.run(todo, D("2026-10-11"), False, 1e9, NoLimit(),
                                         refresh=refresh, probe=lambda: True)
    assert attempted == 25 and counts["failed"] == 25 and len(failures) == 25


# ---- FinMind quota (HTTP 402) handling ----

QUOTA_MSG = "FinMind TaiwanStockMonthRevenue: HTTP 402 / Requests reach the upper limit."


def test_fetch_guarded_turns_swallowed_402_into_quota_exceeded(monkeypatch):
    # get_monthly_revenue() swallows the SourceError and returns None -- the
    # wrapper must still notice, and must restore ds._finmind afterwards.
    original = uf.ds._finmind

    def quota(*a, **kw):
        raise uf.ds.SourceError(QUOTA_MSG)

    monkeypatch.setattr(uf.ds, "_finmind", quota)
    with pytest.raises(uf.QuotaExceeded):
        uf._fetch_revenue("2330", 760)
    assert uf.ds._finmind is quota
    monkeypatch.setattr(uf.ds, "_finmind", original)


def test_fetch_guarded_catches_cashflow_only_quota(monkeypatch):
    # get_financials() swallows a cash-flow-only failure and degrades silently.
    def finmind(dataset, *a, **kw):
        if dataset == "TaiwanStockCashFlowsStatement":
            raise uf.ds.SourceError("FinMind TaiwanStockCashFlowsStatement: HTTP 402 / quota")
        return []

    monkeypatch.setattr(uf.ds, "_finmind", finmind)
    with pytest.raises(uf.QuotaExceeded):
        uf._fetch_financials("2330", 900)


def test_fetch_guarded_ignores_non_quota_errors(monkeypatch):
    def finmind(*a, **kw):
        raise uf.ds.SourceError("FinMind x: HTTP 500 / boom")

    monkeypatch.setattr(uf.ds, "_finmind", finmind)
    assert uf._fetch_revenue("2330", 760) is None


def test_refresh_quota_writes_nothing_and_stays_due():
    today = D("2026-10-11")
    old = [{"usable_date": "2026-01-10", "yoy_pct": 2.0}]
    _write("2330", {"bars": [{"date": "x"}], "revenue_series": old})

    def fetch(code, days, cache):
        raise uf.QuotaExceeded(QUOTA_MSG)

    res = uf.refresh_code("2330", "revenue", "2026-09-01", today, False, NoLimit(), fetch_revenue=fetch)
    assert res["status"] == "quota"
    cached = _read("2330")
    assert cached["revenue_series"] == old and "fund_refresh" not in cached
    assert uf.is_due(cached, "revenue", "2026-09-01", today)


def test_run_waits_for_quota_then_retries_the_same_item():
    clock, sleeps, calls = FakeClock(), [], []

    def sleep(s):
        sleeps.append(s)
        clock.t += s

    def refresh(code, kind, target, today, dry_run, limiter):
        calls.append(code)
        if code == "0001" and calls.count("0001") == 1:
            return {"status": "quota", "detail": QUOTA_MSG}
        return {"status": "updated", "detail": ""}

    todo = [("revenue", "0001", "t"), ("revenue", "0002", "t")]
    counts, attempted, failures = uf.run(todo, D("2026-10-11"), False, 1e9, NoLimit(), refresh=refresh,
                                         probe=lambda: True, clock=clock, sleep=sleep)
    assert calls == ["0001", "0001", "0002"]
    assert sleeps == [uf.QUOTA_WAIT_SECONDS]
    assert counts["quota_waits"] == 1 and counts["updated"] == 2 and counts["failed"] == 0
    assert attempted == 2 and failures == []


def test_run_keeps_probing_until_quota_recovers():
    clock, probes = FakeClock(), []

    def sleep(s):
        clock.t += s

    def probe():
        probes.append(clock.t)
        return len(probes) >= 3   # third probe succeeds

    state = {"n": 0}

    def refresh(*a):
        state["n"] += 1
        return {"status": "quota" if state["n"] == 1 else "updated", "detail": ""}

    counts, attempted, _ = uf.run([("revenue", "0001", "t")], D("2026-10-11"), False, 1e9, NoLimit(),
                                  refresh=refresh, probe=probe, clock=clock, sleep=sleep)
    assert len(probes) == 3 and counts["updated"] == 1 and attempted == 1


def test_run_stops_quietly_when_budget_ends_while_waiting_for_quota():
    clock = FakeClock()

    def sleep(s):
        clock.t += s

    refresh = lambda *a: {"status": "quota", "detail": QUOTA_MSG}  # noqa: E731
    todo = [("revenue", f"{i:04d}", "t") for i in range(5)]
    counts, attempted, failures = uf.run(todo, D("2026-10-11"), False, 1500, NoLimit(), refresh=refresh,
                                         probe=lambda: False, clock=clock, sleep=sleep)
    assert attempted == 0 and failures == [] and counts["failed"] == 0 and counts["quota_waits"] == 1


def test_run_abort_carries_partial_results():
    results = iter([{"status": "updated", "detail": ""}] * 3 + [{"status": "failed", "detail": "boom"}] * 50)
    refresh = lambda *a: next(results)  # noqa: E731
    todo = [("revenue", f"{i:04d}", "t") for i in range(20)]
    with pytest.raises(uf.RunAborted) as exc:
        uf.run(todo, D("2026-10-11"), False, 1e9, NoLimit(), refresh=refresh, probe=lambda: False)
    assert exc.value.counts["updated"] == 3 and exc.value.counts["failed"] == 10
    assert exc.value.attempted == 13 and len(exc.value.failures) == 10


def test_summary_message_mentions_quota_waits_and_abort():
    counts = {"updated": 5, "stale": 1, "insufficient": 0, "failed": 2, "skipped": 0, "quota_waits": 3}
    msg = uf.summary_message(counts, 8, 100, ["revenue 1: x"], aborted="probe failed")
    assert "3 次" in msg and "中止：probe failed" in msg and "尚餘 100 項" in msg and "revenue 1: x" in msg
    assert "額度" not in uf.summary_message({**counts, "quota_waits": 0}, 8, 100, [])


def test_main_exits_zero_and_flags_abort_so_progress_can_be_saved(monkeypatch, tmp_path):
    out = tmp_path / "gh_output"
    sent = []
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setattr(sys, "argv", ["update_fundamentals.py", "--kind", "revenue", "--limit", "5"])
    monkeypatch.setattr(uf.uub, "_cached_codes", lambda: ["1101"])
    monkeypatch.setattr(uf, "build_todo", lambda codes, kinds, today: [("revenue", "1101", "t")])
    monkeypatch.setattr(uf, "probe_status", lambda: "ok")
    monkeypatch.setattr(uf.alerts, "send_telegram", lambda msg: sent.append(msg))

    def boom(*a, **kw):
        raise uf.RunAborted("bad token", {"updated": 4, "stale": 0, "insufficient": 0, "failed": 10,
                                          "skipped": 0, "quota_waits": 0}, 14, ["revenue 1: x"])

    monkeypatch.setattr(uf, "run", boom)
    uf.main()   # must not raise SystemExit
    text = out.read_text(encoding="utf-8")
    assert "aborted=true" in text and "progress=0" in text
    assert sent and "中止：bad token" in sent[0]


def test_main_dead_token_at_start_still_exits_nonzero(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["update_fundamentals.py", "--kind", "revenue", "--limit", "5"])
    monkeypatch.setattr(uf.uub, "_cached_codes", lambda: ["1101"])
    monkeypatch.setattr(uf, "build_todo", lambda codes, kinds, today: [("revenue", "1101", "t")])
    monkeypatch.setattr(uf, "probe_status", lambda: "error")
    with pytest.raises(SystemExit) as exc:
        uf.main()
    assert exc.value.code == 1
