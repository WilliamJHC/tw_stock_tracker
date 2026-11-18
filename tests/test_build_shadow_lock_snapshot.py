# -*- coding: utf-8 -*-
"""Tests for scripts/build_shadow_lock_snapshot.py (no network: industries come from a saved file)."""
import json
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import build_shadow_lock_snapshot as bsl  # noqa: E402
import run_shadow_portfolio as rsp  # noqa: E402

SNAP = date(2026, 11, 27)
LIVE = [f"{1100 + i}" for i in range(12)]


def _world(tmp_path):
    cache = tmp_path / "factor_run"
    cache.mkdir()
    for c in LIVE:
        (cache / f"{c}.json").write_text(json.dumps({"bars": []}), encoding="utf-8")
    (cache / "2000.json").write_text(json.dumps({"skip": True}), encoding="utf-8")   # skipped: not live
    (cache / "3000.json").write_text("{not json", encoding="utf-8")                   # unreadable
    (cache / "00878.json").write_text(json.dumps({"bars": []}), encoding="utf-8")     # not a 4-digit code
    (cache / "notes.txt").write_text("x", encoding="utf-8")
    inds = {c: ["電子工業", "半導體業"] if i % 2 else ["傳產", "食品工業"] for i, c in enumerate(LIVE)}
    inds["2000"] = ["食品工業"]          # skipped code
    inds["9999"] = ["食品工業"]          # code with no cache file
    ind_file = tmp_path / "industries_raw.json"
    ind_file.write_text(json.dumps(inds, ensure_ascii=False), encoding="utf-8")
    return str(cache), str(ind_file)


def _build(tmp_path, **kw):
    cache, ind_file = _world(tmp_path)
    return bsl.build(cache, str(tmp_path / "shadow"), "2026-11", SNAP, industries_file=ind_file, min_codes=5, **kw)


def test_universe_is_live_four_digit_codes_only(tmp_path):
    cache, _ = _world(tmp_path)
    codes, stats = bsl.live_codes(cache)
    assert codes == LIVE
    assert stats == {"live": 12, "skipped": 1, "bad_json": 1}


def test_files_and_industries_restricted_to_the_universe(tmp_path):
    res = _build(tmp_path)
    uni = json.load(open(res["universe_file"], encoding="utf-8"))
    ind = json.load(open(res["industries_file"], encoding="utf-8"))
    assert uni["codes"] == LIVE and uni["count"] == 12 and uni["snapshot_date"] == "2026-11-27"
    assert set(ind["industries"]) == set(LIVE)                      # no 2000 (skipped), no 9999 (no cache file)
    assert res["stats"]["universe"] == 12 and res["stats"]["primary_other"] == 0


def test_output_loads_with_the_daily_jobs_loader_and_lock_verifies(tmp_path):
    res = _build(tmp_path)
    ledger = str(tmp_path / "ledger")
    path = bsl.write_lock(ledger, date(2026, 12, 1), res, SNAP)
    lock = json.load(open(path, encoding="utf-8"))
    assert lock["start_date"] == "2026-12-01" and lock["universe_file"] == "universe_2026-11.json"
    store = rsp.Store(ledger)
    lock2, universe, primary = rsp.check_lock(store, res["universe_file"], res["industries_file"],
                                              date(2026, 12, 1), dry_run=False)
    assert universe == LIVE and set(primary) == set(LIVE) and lock2["start_date"] == "2026-12-01"


def test_hash_ignores_line_endings_but_not_content(tmp_path):
    res = _build(tmp_path)
    ledger = str(tmp_path / "ledger")
    bsl.write_lock(ledger, date(2026, 12, 1), res, SNAP)
    raw = open(res["universe_file"], "rb").read()
    with open(res["universe_file"], "wb") as f:                      # as if a checkout turned LF into CRLF
        f.write(raw.replace(b"\n", b"\r\n"))
    rsp.check_lock(rsp.Store(ledger), res["universe_file"], res["industries_file"], date(2026, 12, 1), False)
    with open(res["universe_file"], "wb") as f:
        f.write(raw.replace(b"1100", b"1101"))
    with pytest.raises(rsp.ShadowError):
        rsp.check_lock(rsp.Store(ledger), res["universe_file"], res["industries_file"], date(2026, 12, 1), False)


def test_partial_cache_is_refused(tmp_path):
    cache, ind_file = _world(tmp_path)
    with pytest.raises(bsl.SnapshotError, match="partial universe"):
        bsl.build(cache, str(tmp_path / "shadow"), "2026-11", SNAP, industries_file=ind_file, min_codes=500)
    assert not (tmp_path / "shadow").exists()


def test_existing_snapshot_and_lock_are_never_replaced_silently(tmp_path):
    res = _build(tmp_path)
    cache, ind_file = str(tmp_path / "factor_run"), str(tmp_path / "industries_raw.json")   # the same world
    with pytest.raises(bsl.SnapshotError, match="already exists"):
        bsl.build(cache, str(tmp_path / "shadow"), "2026-11", SNAP, industries_file=ind_file, min_codes=5)
    bsl.build(cache, str(tmp_path / "shadow"), "2026-11", SNAP, industries_file=ind_file, min_codes=5, force=True)
    bsl.write_lock(str(tmp_path / "ledger"), date(2026, 12, 1), res, SNAP)
    with pytest.raises(bsl.SnapshotError, match="written once"):
        bsl.write_lock(str(tmp_path / "ledger"), date(2026, 12, 1), res, SNAP)


def test_wrong_industry_shape_fails_before_the_lock(tmp_path):
    cache, _ = _world(tmp_path)
    only_other = tmp_path / "other.json"
    only_other.write_text(json.dumps({c: ["其他"] for c in LIVE}, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(rsp.ShadowError):                              # < 50% of codes with a real primary industry
        bsl.build(cache, str(tmp_path / "shadow"), "2026-11", SNAP, industries_file=str(only_other), min_codes=5)


def test_cli_returns_1_on_error_and_0_on_success(tmp_path, capsys):
    cache, ind_file = _world(tmp_path)
    base = ["--cache-dir", cache, "--out-dir", str(tmp_path / "shadow"), "--industries-file", ind_file,
            "--snapshot-date", "2026-11-27"]
    assert bsl.main(base + ["--min-codes", "500"]) == 1
    assert bsl.main(base + ["--min-codes", "5", "--write-lock", str(tmp_path / "ledger"),
                            "--start-date", "2026-12-01"]) == 0
    assert os.path.isfile(tmp_path / "ledger" / "meta" / "lock.json")
    out = capsys.readouterr().out
    assert "universe_sha256" in out and "12 codes" in out
    with pytest.raises(SystemExit):                                    # --write-lock without --start-date
        bsl.main(base + ["--write-lock", str(tmp_path / "l2")])


def test_lock_only_uses_the_existing_files_and_reads_no_cache(tmp_path):
    res = _build(tmp_path)                                           # as if the cloud workflow had built them
    out_dir = os.path.dirname(res["universe_file"])
    ledger = str(tmp_path / "ledger")
    assert bsl.main(["--out-dir", out_dir, "--cache-dir", str(tmp_path / "no_such_cache"), "--lock-only",
                     "--write-lock", ledger, "--start-date", "2026-11-18"]) == 0
    lock = json.load(open(tmp_path / "ledger" / "meta" / "lock.json", encoding="utf-8"))
    assert lock["start_date"] == "2026-11-18" and lock["snapshot_date"] == "2026-11-27"
    assert (lock["universe_sha256"], lock["industries_sha256"]) == (res["sha"]["universe_sha256"], res["sha"]["industries_sha256"])
    rsp.check_lock(rsp.Store(ledger), res["universe_file"], res["industries_file"], date(2026, 11, 18), dry_run=False)


def test_lock_only_refuses_missing_or_broken_snapshots(tmp_path, capsys):
    out_dir = str(tmp_path / "shadow")
    args = ["--out-dir", out_dir, "--lock-only", "--write-lock", str(tmp_path / "ledger"), "--start-date", "2026-11-18"]
    assert bsl.main(args) == 1                                       # nothing there
    res = _build(tmp_path)
    with open(res["industries_file"], "w", encoding="utf-8") as f:
        f.write('{"industries": []}')                                # wrong shape
    assert bsl.main(args) == 1
    assert not os.path.exists(tmp_path / "ledger" / "meta" / "lock.json")
    with pytest.raises(SystemExit):                                  # --lock-only without --write-lock
        bsl.main(["--out-dir", out_dir, "--lock-only"])
