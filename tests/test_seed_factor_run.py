# -*- coding: utf-8 -*-
"""Tests for scripts/seed_factor_run.py's slicing/safety logic. No network:
fetch/probe/clock are injected, the cache dir is a tmp_path."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import pytest  # noqa: E402

import run_factor_backtest as rfb  # noqa: E402
import seed_factor_run as sfr  # noqa: E402

DAYS = 1800


@pytest.fixture(autouse=True)
def isolate_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(rfb, "RUN_CACHE_DIR", str(tmp_path))


def _write(code, **extra):
    rfb.save_code_cache(code, {"days": DAYS, **extra})


def _live_fetch(batch):
    for c in batch:
        _write(c, bars=[], revenue_series=[1], roe_series=[1])


def _skip_fetch(batch):
    for c in batch:
        _write(c, skip=True)


class FakeClock:
    """Each call advances time by `step` seconds."""
    def __init__(self, step):
        self.t = 0.0
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t


def test_remaining_codes_counts_missing_corrupt_and_wrong_window():
    _write("1101", bars=[])
    _write("1102", skip=True)  # a cached skip verdict is "done", not remaining
    rfb.save_code_cache("1103", {"days": 999, "bars": []})  # fetched under another window
    with open(rfb.code_cache_path("1104"), "w", encoding="utf-8") as f:
        f.write("{not json")
    assert sfr.remaining_codes(["1101", "1102", "1103", "1104", "1105"], DAYS) == ["1103", "1104", "1105"]


def test_seed_fetches_everything_when_budget_allows():
    codes = [f"{n}" for n in range(1101, 1111)]
    before, after = sfr.seed(codes, DAYS, budget_seconds=1e9, batch_size=3,
                             fetch=_live_fetch, probe=lambda: True)
    assert (before, after) == (10, 0)


def test_seed_stops_at_batch_boundary_when_budget_is_spent_and_resumes():
    codes = [f"{n}" for n in range(1101, 1111)]
    # The first clock() call (t=4) sets deadline=14; the per-batch checks then
    # read t=8 and t=12 (both allowed) and t=16 (refused) -> exactly two batches.
    clock = FakeClock(step=4)
    before, after = sfr.seed(codes, DAYS, budget_seconds=10, batch_size=3,
                             fetch=_live_fetch, probe=lambda: True, clock=clock)
    assert (before, after) == (10, 4)
    # a second run only sees the leftover codes
    before2, after2 = sfr.seed(codes, DAYS, budget_seconds=1e9, batch_size=3,
                               fetch=_live_fetch, probe=lambda: True)
    assert (before2, after2) == (4, 0)


def test_all_skip_batch_with_failing_probe_aborts():
    codes = ["1101", "1102", "1103"]
    with pytest.raises(RuntimeError, match="entirely skip"):
        sfr.seed(codes, DAYS, budget_seconds=1e9, batch_size=3,
                 fetch=_skip_fetch, probe=lambda: False)


def test_all_skip_batch_with_healthy_probe_continues():
    codes = ["1101", "1102", "1103", "1104"]
    calls = []

    def fetch(batch):
        calls.append(batch)
        (_skip_fetch if batch[0] == "1101" else _live_fetch)(batch)

    before, after = sfr.seed(codes, DAYS, budget_seconds=1e9, batch_size=2,
                             fetch=fetch, probe=lambda: True)
    assert (before, after) == (4, 0)
    assert len(calls) == 2
    assert sfr.count_skipped(codes, DAYS) == 2


def test_write_outputs_appends_to_github_output(tmp_path, monkeypatch):
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    sfr.write_outputs(7, 3)
    assert out.read_text(encoding="utf-8") == "remaining=7\nprogress=3\n"


def test_write_outputs_is_noop_without_github_output(monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    sfr.write_outputs(1, 1)  # must not raise
