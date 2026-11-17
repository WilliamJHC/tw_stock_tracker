# -*- coding: utf-8 -*-
"""Ledger state machine for the forward shadow portfolio (spec:
docs/shadow_portfolio_spec_2026-11.md, clause numbers such as A5d-5 refer to it;
file format: docs/shadow_ledger_format_2026-11.md).

``step(state, inputs) -> (new_state, rows)`` is a pure function: no I/O, no network,
no clock, no randomness.  The same ``state`` + ``inputs`` always give a bit-for-bit
identical ``(new_state, rows)`` (key order included).  ``state`` is never mutated.
Rule logic (dates, ranking order, caps, costs, orders, S2 day) lives in
``shadow_rules``; this module only books it.

Lines marked "ASSUMPTION:" are places where the spec is silent or ambiguous and one
reading had to be chosen; they are listed in the hand-off report.

Calling convention
------------------
The caller invokes ``step`` once per trading day, in calendar order, with no gaps
(``ValueError`` otherwise).  A day whose whole-market data is missing is still a
step, with ``prices=None``.  Catch-up after a successful re-fetch (A5d-1) is done by
the caller: re-run ``step`` for the missing day and every later day starting from the
state snapshot taken before the missing day, then pass the produced rows through
``mark_superseded``.  Because the re-run starts from the older snapshot, the repaired
day is not counted as a missing day and a rebalance fills at the nominal day's close.
``rebalance_due`` tells the caller which candidates need a ranking for a given day.

State (all JSON-serialisable; dates are ISO strings)
----------------------------------------------------
{"schema": 1,
 "industries_primary": {code: primary industry}      # locked snapshot (S3 cap)
 "candidates": {name: {"holdings": {code: market value (yuan, 4 dp)},
                       "cash": float,                 # may be slightly negative (A5e-3)
                       "nav": float,                  # == sum(holdings) + cash
                       "rets": [daily return, ...],   # incl. the start-day return
                       "pending_rebalance": None | {"nominal_date": str, "s2_coverage": {...}|None}}},
 "frozen_days": {code: consecutive no-close days}     # shared: a code's price status is market-wide
 "last_close": {code: last valid close of a held code},
 "acc_factor": {code: product of ex-rights factors since last_close},   # A5e-8
 "benchmark": {"start_close": float|None, "last_close": float|None, "rets": [...]},
 "meta": {"schema", "status", "start_date", "start_wait", "start_history", "judgment_date",
          "missing_days", "missing_total", "consecutive_missing", "deferrals",
          "impaired", "impaired_reasons", "void", "last_processed", "pending_retry",
          "s2", "judgment"}}
``meta.status``: ``not_started`` | ``active`` | ``void`` | ``ended`` (after the judgment
day).  ``step`` on a ``void`` / ``ended`` state, or on a date before the start date,
returns the state unchanged and no rows.

Inputs (single trading day T)
-----------------------------
{"date", "calendar" (all trading days, covering T's month and its neighbours),
 "prices": {code: {"c", "status", ...}} | None,       # None = whole-market data missing
 "adj": {code: factor | None},                         # None = adj_missing, absent = 1.0
 "benchmark_close": float | None,                      # 0050 total-return close
 "benchmark_source": optional str (default "yahoo"),
 "rankings": {candidate: {"asof", "ranked": [codes], "detail": [...]}},  # only if due
 "s2_status": {"complete": bool, "ratio": float},      # needed every day for S2
 "written_at": str}                                    # copied into rows only

Rows (each dict has "file" and, where applicable, "candidate")
--------------------------------------------------------------
daily:       date, written_at, nav, ret, cash, holdings, frozen, flags, supersedes
rebalances:  kind ("start"|"rebalance"), nominal_date, signal_asof, fill_date, written_at,
             ranking, targets, frozen, orders, cost, s2_coverage, flags, supersedes
benchmark:   date, adj_close, ret, source, flags, written_at, supersedes
meta:        written_at + the full ``state["meta"]`` (overwrite-style, meta/state.json)
Rows are emitted in the order: rebalances, daily, benchmark, meta.
Daily flags: missing_day, rebalance_deferred, s2_status_missing, rebalance_skipped_all_frozen,
adj_missing:<code>, delisted_exit:<code>, late_fill (added by ``mark_superseded``).
Rebalance-row flags: rebalance_skipped_all_frozen (nothing tradable: no order, cost 0).
``prices={}`` (an empty dict, not None) is a data day on which every held code has no close.
"""
import copy
import math
from datetime import date, datetime

import shadow_rules as sr
from metrics import sharpe_ratio

# --------------------------------------------------------------------------- #
# Constants fixed by the spec
# --------------------------------------------------------------------------- #

SCHEMA = 1
CANDIDATES = ("S0_baseline", "S1_high250", "S2_high250_revday", "S3_high250_cap")
S2_NAME = "S2_high250_revday"
CAPPED = frozenset({"S3_high250_cap"})
INITIAL_CAPITAL = 1_000_000.0     # A2
N_SLOTS = 20                      # A1 top 20
SECTOR_CAP = 4                    # A1 S3
FREEZE_FROM_DAY = 1               # A5d-5: frozen from the first no-close day (the old 5-day rule is gone)
DELIST_DAYS = 20                  # A5b-6 / A5d-5
VOID_CONSECUTIVE_MISSING = 20     # A5 long-outage rule
MISSING_TOTAL_LIMIT = 10          # A5: cumulative missing days > 10 -> impaired
DEFERRAL_LIMIT = 5                # A5: one rebalance deferred > 5 trading days -> impaired
RETRY_DAYS = 3                    # A5d-1
JUDGMENT_RETRY_DAYS = 10          # A5d-7
START_DEFER_LIMIT = 10            # A5d-8
SKIP_TOLERANCE = 1e-6             # yuan: tradable funds at/below this = nothing to trade
PRICE_OK = ("ok", "exright")      # statuses that carry a valid close (no_close / bad_row do not)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _d(x):
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    return date.fromisoformat(str(x))


def _iso(x):
    return _d(x).isoformat()


def _r4(x):
    """Money rounded to 4 dp (format doc section 1); "+ 0.0" turns -0.0 into 0.0."""
    return round(x, 4) + 0.0


def _ret(nav, prev):
    return round(nav / prev - 1.0, 10) + 0.0


def _cal(inputs):
    return sorted({_d(x) for x in inputs["calendar"]})


def _close(prices, code):
    """Valid close of `code`, or None (no_close / bad_row / absent all mean "no close")."""
    row = prices.get(code)
    if not row or row.get("status") not in PRICE_OK:
        return None
    c = row.get("c")
    if c is None or isinstance(c, bool) or not (c > 0) or (isinstance(c, float) and math.isnan(c)):
        return None
    return float(c)


def _sorted_dict(d):
    return {k: d[k] for k in sorted(d)}


def _nth_after(cal, d, n):
    """ISO date of the n-th trading day after d (A5e-1: trading_days_between == n), or None."""
    later = [x for x in cal if x > d]
    return later[n - 1].isoformat() if len(later) >= n else None


def _held_union(st):
    out = set()
    for cs in st["candidates"].values():
        out.update(cs["holdings"])
    return out


def _frozen_held(st, cs):
    return sorted(c for c in cs["holdings"] if st["frozen_days"].get(c, 0) >= FREEZE_FROM_DAY)


def _impair(meta, reason):
    meta["impaired"] = True
    if reason not in meta["impaired_reasons"]:
        meta["impaired_reasons"].append(reason)


def _set_judgment_date(meta, cal):
    """(Re)compute the judgment date from the start date; None while the calendar is too short
    (A5e-9 / A5d-7: the final calendar may not be published yet)."""
    try:
        meta["judgment_date"] = sr.judgment_date(_d(meta["start_date"]), cal).isoformat()
    except ValueError:
        meta["judgment_date"] = None


def _refresh_judgment_date(meta, cal):
    """A5d-7: recompute the judgment date from the calendar of the current step (the final TWSE
    holiday table replaces the provisional one; this is not a rule change).  A calendar too short
    to answer leaves the previous value untouched."""
    try:
        new = sr.judgment_date(_d(meta["start_date"]), cal).isoformat()
    except ValueError:
        return
    meta["judgment_date"] = new


def _add_retry(meta, cal, d, days=RETRY_DAYS):
    meta["pending_retry"].append({"date": d.isoformat(), "kind": "close",
                                  "until": _nth_after(cal, d, days)})


def _prune_retry(meta, iso):
    meta["pending_retry"] = [p for p in meta["pending_retry"] if p["until"] is None or p["until"] >= iso]


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #

def new_state(start_date, calendar, industries_primary):
    """Fresh state before the start day.  `start_date` is the (first) nominal start day;
    `industries_primary` the locked {code: primary industry} map used by the S3 cap."""
    cal = sorted({_d(x) for x in calendar})
    meta = {"schema": SCHEMA, "status": "not_started", "start_date": _iso(start_date),
            "start_wait": [], "start_history": [], "judgment_date": None,
            "missing_days": [], "missing_total": 0, "consecutive_missing": 0,
            "deferrals": [], "impaired": False, "impaired_reasons": [], "void": None,
            "last_processed": None, "pending_retry": [], "s2": {}, "judgment": None}
    _set_judgment_date(meta, cal)
    return {"schema": SCHEMA,
            "industries_primary": dict(sorted(industries_primary.items())),
            "candidates": {c: {"holdings": {}, "cash": INITIAL_CAPITAL, "nav": INITIAL_CAPITAL,
                               "rets": [], "pending_rebalance": None} for c in CANDIDATES},
            "frozen_days": {}, "last_close": {}, "acc_factor": {},
            "benchmark": {"start_close": None, "last_close": None, "rets": []},
            "meta": meta}


def is_judgment_day(state, d):
    j = state["meta"].get("judgment_date")
    return j is not None and _iso(d) == j


def check_invariants(state, tol=1e-6):
    """Invariant 1: sum(holdings) + cash == nav for every candidate.  Raises AssertionError."""
    for c, cs in state["candidates"].items():
        gap = abs(sum(cs["holdings"].values()) + cs["cash"] - cs["nav"])
        assert gap <= tol, f"{c}: holdings + cash != nav (gap {gap})"


# --------------------------------------------------------------------------- #
# Nominal rebalance days -> pending_rebalance
# --------------------------------------------------------------------------- #

def _mark_nominals(st, d, cal, s2_status):
    """Register today as the nominal rebalance day of every candidate it applies to
    (A1 / A2 / A5b-2).  Never overrides a pending (still unexecuted) rebalance.
    ASSUMPTION: a new nominal day arriving while an older rebalance is still pending does
    not create a second one: the single pending rebalance runs once, on the newest rankings."""
    meta = st["meta"]
    iso = d.isoformat()
    first = sr.first_trading_day(cal, d.year, d.month)
    month_key = f"{d.year:04d}-{d.month:02d}"
    for name in CANDIDATES:
        cs = st["candidates"][name]
        if name != S2_NAME:
            if d == first and cs["pending_rebalance"] is None:
                cs["pending_rebalance"] = {"nominal_date": iso, "s2_coverage": None}
            continue
        if month_key in meta["s2"]:
            continue
        status = s2_status or {}
        complete = bool(status.get("complete", False))
        # ASSUMPTION: a day without s2_status counts as "revenue refresh not complete" (the
        # forced day still forces the rebalance), so the caller should supply it every day.
        decided = sr.s2_rebalance_day(
            cal, d.year, d.month,
            lambda x: False if x < d else (complete if x == d else None))
        if decided != d:
            continue
        forced = (d == sr.s2_forced_day(cal, d.year, d.month)) and not complete
        cov = {"ratio": status.get("ratio"), "complete_day": None if forced else iso, "forced": forced}
        meta["s2"][month_key] = {"nominal_date": iso, **cov}
        if cs["pending_rebalance"] is None:
            cs["pending_rebalance"] = {"nominal_date": iso, "s2_coverage": cov}


def rebalance_due(state, d, calendar, s2_status=None):
    """Candidates that need ``inputs["rankings"]`` on day `d` (start day: all four; later: those
    whose nominal day is `d` or whose rebalance is still pending).  Pure helper for the caller."""
    st = copy.deepcopy(state)
    d = _d(d)
    cal = sorted({_d(x) for x in calendar})
    status = st["meta"]["status"]
    if status == "not_started":
        return list(CANDIDATES) if d >= _d(st["meta"]["start_date"]) else []
    if status != "active":
        return []
    _mark_nominals(st, d, cal, s2_status)
    return [c for c in CANDIDATES if st["candidates"][c]["pending_rebalance"] is not None]


# --------------------------------------------------------------------------- #
# Booking one rebalance (also used for the start-day build)
# --------------------------------------------------------------------------- #

def _rebalance(st, name, prices, ranking, nominal_date, fill_date, written_at, kind, s2_cov):
    """Rebalance one candidate at today's close and return the rebalances row.
    cs["nav"] must already be marked to market.  Cost is paid from cash (A5e-3).
    Raises ValueError when the ranking was not computed strictly before the fill date (look-ahead)."""
    cs = st["candidates"][name]
    # ASSUMPTION: a ranking without "asof" cannot be shown to be free of look-ahead, so it is
    # rejected like one whose asof is on/after the fill date (caller bug, never silent).
    asof = ranking.get("asof")
    if asof is None or _d(asof) >= _d(fill_date):
        raise ValueError(f"{name}: ranking asof {asof!r} must be before the fill date {fill_date}")
    frozen_vals = {c: v for c, v in cs["holdings"].items() if st["frozen_days"].get(c, 0) >= FREEZE_FROM_DAY}
    frozen = frozenset(frozen_vals)
    nav_pre = cs["nav"]
    # ASSUMPTION: a ranked code with no valid close today cannot be filled at today's close,
    # so it is skipped and the next ranked code takes its slot (spec silent).
    ranked = [c for c in ranking["ranked"] if _close(prices, c) is not None]
    if name in CAPPED:
        targets = sr.select_with_cap(ranked, st["industries_primary"], cap=SECTOR_CAP,
                                     n=N_SLOTS, frozen=frozen)
    else:
        targets = sr.select_uncapped(ranked, n=N_SLOTS, frozen=frozen)
    investable = nav_pre - sum(frozen_vals.values())
    flags = []
    # ASSUMPTION: when nothing is tradable (no target slot left, or the frozen holdings take all of
    # the nav: with the slightly negative cash of A5e-3 they can exceed it) the rebalance is a
    # no-op, A5c-5's "k = slots -> do not touch", and counts as done.  Also applies when no
    # ranked code has a close today.
    if not targets or investable <= SKIP_TOLERANCE:
        flags.append("rebalance_skipped_all_frozen")
        targets = []
        orders = {"buys": {}, "sells": {}, "cost": 0.0}
        new_holdings = dict(cs["holdings"])
    else:
        orders = sr.rebalance_orders(cs["holdings"], targets, nav_pre, frozen_vals)
        each = _r4(investable / len(targets))
        new_holdings = dict(frozen_vals)
        for code in targets:
            new_holdings[code] = each
            if code not in cs["holdings"]:          # newly bought: price base is today's close
                st["last_close"][code] = _close(prices, code)
                st["acc_factor"][code] = 1.0
    cs["holdings"] = _sorted_dict(new_holdings)
    cost = _r4(orders["cost"])
    cs["cash"] = _r4(nav_pre - sum(cs["holdings"].values()) - cost)
    cs["nav"] = _r4(sum(cs["holdings"].values()) + cs["cash"])
    cs["pending_rebalance"] = None
    return {"file": "rebalances", "candidate": name, "kind": kind,
            "nominal_date": nominal_date, "signal_asof": asof, "fill_date": fill_date,
            "written_at": written_at, "ranking": copy.deepcopy(list(ranking.get("detail") or [])),
            "targets": list(targets), "frozen": sorted(frozen),
            "orders": {"buys": {c: _r4(v) for c, v in orders["buys"].items()},
                       "sells": {c: _r4(v) for c, v in orders["sells"].items()}},
            "cost": cost, "s2_coverage": copy.deepcopy(s2_cov), "flags": flags, "supersedes": None}


def _has_ranking(rankings, name):
    rk = (rankings or {}).get(name)
    return bool(rk and rk.get("ranked"))


# --------------------------------------------------------------------------- #
# Benchmark (0050)
# --------------------------------------------------------------------------- #

def _benchmark_row(st, inp, iso, missing):
    """Update the 0050 series and return its row.  Market-missing day with a 0050 close
    available: the close is used.  ASSUMPTION: spec A5c-10 only covers "0050 and market both
    missing"; when only the market is missing, 0050's own data is still valid, so it is used."""
    b = st["benchmark"]
    close = inp.get("benchmark_close")
    flags = ["missing_day"] if missing else []
    if close is None:
        close = b["last_close"]
        flags.append("carry_forward")
        source = "carry"
    else:
        close = float(close)
        source = inp.get("benchmark_source", "yahoo")
    ret = _ret(close, b["last_close"])
    b["last_close"] = close
    b["rets"].append(ret)
    return {"file": "benchmark", "date": iso, "adj_close": close, "ret": ret, "source": source,
            "flags": flags, "written_at": inp.get("written_at"), "supersedes": None}


def _daily_row(st, name, iso, written_at, ret, flags):
    cs = st["candidates"][name]
    return {"file": "daily", "candidate": name, "date": iso, "written_at": written_at,
            "nav": cs["nav"], "ret": ret, "cash": cs["cash"], "holdings": dict(cs["holdings"]),
            "frozen": _frozen_held(st, cs), "flags": flags, "supersedes": None}


def _meta_row(meta, written_at):
    return {"file": "meta", "written_at": written_at, **copy.deepcopy(meta)}


# --------------------------------------------------------------------------- #
# step
# --------------------------------------------------------------------------- #

def step(state, inputs):
    """Process one trading day.  Returns (new_state, rows); see the module docstring."""
    st = copy.deepcopy(state)
    meta = st["meta"]
    d = _d(inputs["date"])
    if meta["status"] in ("void", "ended"):
        return st, []
    if d < _d(meta["start_date"]):
        return st, []
    cal = _cal(inputs)
    if d not in cal:
        raise ValueError(f"{d} is not in the supplied trading calendar")
    meta_before = copy.deepcopy(meta)
    _prune_retry(meta, d.isoformat())
    _refresh_judgment_date(meta, cal)       # provisional calendar -> final calendar (A5d-7, A5e-9)
    if meta["status"] == "not_started":
        rows = _step_pre_start(st, inputs, d, cal)
    else:
        prev = sr.prev_trading_day(cal, d)
        if meta["last_processed"] != prev.isoformat():
            raise ValueError(f"step must be called on consecutive trading days: last processed "
                             f"{meta['last_processed']}, expected {prev}, got {d}")
        rows = _step_active(st, inputs, d, cal)
    if meta != meta_before:
        rows.append(_meta_row(meta, inputs.get("written_at")))
    return st, rows


def _step_pre_start(st, inp, d, cal):
    """Start-day build (A3, A5c-1, A5d-8) or waiting for it.  Waiting days are not missing days."""
    meta = st["meta"]
    iso = d.isoformat()
    start = _d(meta["start_date"])
    while sr.trading_days_between(cal, start, d) > START_DEFER_LIMIT:
        # ASSUMPTION: "next month's first trading day" is taken relative to the nominal start
        # date being replaced (restart_date(start)); the spec does not say relative to which day.
        new = sr.restart_date(start, cal)
        meta["start_history"].append({"from": start.isoformat(), "to": new.isoformat(),
                                      "detected": iso, "reason": "start_deferred_gt10"})
        start = new
        meta["start_date"] = start.isoformat()
        meta["start_wait"] = []
        _set_judgment_date(meta, cal)
    if d < start:
        return []
    prices = inp.get("prices")
    rankings = inp.get("rankings") or {}
    # ASSUMPTION: the start needs the whole-market closes, a 0050 close (its start base) and a
    # ranking for every candidate; if any is missing the whole start is deferred together
    # (A5c-1 only mentions missing whole-market data) and the day is not counted as missing.
    # (An empty prices dict is treated as not ready: nothing could be bought.)
    ready = (bool(prices) and inp.get("benchmark_close") is not None
             and all(_has_ranking(rankings, c) for c in CANDIDATES))
    if not ready:
        if iso not in meta["start_wait"]:
            meta["start_wait"].append(iso)
        if prices is None:
            _add_retry(meta, cal, d)
        return []
    written_at = inp.get("written_at")
    month_key = f"{d.year:04d}-{d.month:02d}"
    rows = []
    for name in CANDIDATES:
        rows.append(_rebalance(st, name, prices, rankings[name], start.isoformat(), iso,
                               written_at, "start", None))
    # ASSUMPTION: if the start day is itself on/after the month's S2 rebalance day, the start
    # build counts as that month's S2 rebalance (otherwise S2 would rebalance the same month again).
    if d >= sr.first_trading_day_on_or_after(cal, d.year, d.month, sr.S2_EARLIEST_DAY):
        meta["s2"][month_key] = {"nominal_date": iso, "ratio": None, "complete_day": iso, "forced": False}
    for name in CANDIDATES:
        cs = st["candidates"][name]
        ret = _ret(cs["nav"], INITIAL_CAPITAL)
        cs["rets"].append(ret)
    daily = [_daily_row(st, n, iso, written_at, st["candidates"][n]["rets"][-1], []) for n in CANDIDATES]
    b = st["benchmark"]
    close = float(inp["benchmark_close"])
    b["start_close"], b["last_close"], b["rets"] = close, close, [0.0]
    bench = {"file": "benchmark", "date": iso, "adj_close": close, "ret": 0.0,
             "source": inp.get("benchmark_source", "yahoo"), "flags": [],
             "written_at": written_at, "supersedes": None}
    meta["status"] = "active"
    meta["last_processed"] = iso
    st["frozen_days"] = {}
    _cleanup_codes(st)
    return rows + daily + [bench]


def _cleanup_codes(st):
    """Drop per-code state of codes nobody holds any more; keep dict key order deterministic."""
    held = _held_union(st)
    for key in ("last_close", "acc_factor", "frozen_days"):
        st[key] = {c: v for c, v in sorted(st[key].items()) if c in held}


def _step_active(st, inp, d, cal):
    meta = st["meta"]
    iso = d.isoformat()
    written_at = inp.get("written_at")
    prices = inp.get("prices")
    missing = prices is None
    adj = inp.get("adj") or {}
    rankings = inp.get("rankings") or {}
    # F10: S2 still waiting for its month's rebalance, from the 12th on, and no s2_status supplied.
    month_key = f"{d.year:04d}-{d.month:02d}"
    s2_status_missing = (inp.get("s2_status") is None and month_key not in meta["s2"]
                         and d >= sr.first_trading_day_on_or_after(cal, d.year, d.month, sr.S2_EARLIEST_DAY))
    _mark_nominals(st, d, cal, inp.get("s2_status"))
    prev_nav = {c: st["candidates"][c]["nav"] for c in CANDIDATES}
    pre_trade_nav = {}
    pre_flags = {c: [] for c in CANDIDATES}      # adj_missing flags
    delist_flags = {c: [] for c in CANDIDATES}
    rebalance_rows = []

    # A5: a single rebalance deferred by more than 5 trading days -> impaired.
    for name in CANDIDATES:
        p = st["candidates"][name]["pending_rebalance"]
        if p and sr.trading_days_between(cal, _d(p["nominal_date"]), d) > DEFERRAL_LIMIT:
            _impair(meta, f"deferral_gt_{DEFERRAL_LIMIT}:{name}")

    if missing:
        # A5: whole-market missing day: keep holdings, value at old prices (return 0).  The
        # per-code frozen counters are paused (A5c-5): no counter is touched on this branch.
        # The ex-rights table is a separate endpoint and may exist on such a day: its factors are
        # accumulated like those of a suspension (A5e-8) so the next data day shows no fake drop.
        miss_adj = set()
        for code in sorted(_held_union(st)):
            if code in adj:
                if adj[code] is None or not adj[code] > 0:
                    miss_adj.add(code)
                else:
                    st["acc_factor"][code] = st["acc_factor"][code] * float(adj[code])
        for name in CANDIDATES:
            pre_flags[name] = [f"adj_missing:{c}" for c in sorted(miss_adj)
                               if c in st["candidates"][name]["holdings"]]
        meta["missing_days"].append(iso)
        meta["missing_total"] += 1
        meta["consecutive_missing"] += 1
        retry_days = JUDGMENT_RETRY_DAYS if iso == meta["judgment_date"] else RETRY_DAYS
        _add_retry(meta, cal, d, retry_days)
        if meta["missing_total"] > MISSING_TOTAL_LIMIT:
            _impair(meta, f"missing_total_gt_{MISSING_TOTAL_LIMIT}")
        if meta["consecutive_missing"] >= VOID_CONSECUTIVE_MISSING:
            try:
                new_start = sr.restart_date(d, cal).isoformat()
            except ValueError:
                # ASSUMPTION: if the calendar does not reach next month, new_start is None and the
                # caller must compute it (restart_date) once the calendar is available.
                new_start = None
            meta["void"] = {"trigger_date": iso, "new_start": new_start}
            meta["status"] = "void"
    else:
        meta["consecutive_missing"] = 0
        adj_missing = set()
        ratios = {}
        for code in sorted(_held_union(st)):
            if code in adj:
                raw = adj[code]
            else:   # absent = 1.0, except an ex-rights row without any factor (F7): adj_missing
                raw = None if (prices.get(code) or {}).get("status") == "exright" else 1.0
            if raw is None or not raw > 0:
                adj_missing.add(code)           # A5d-6: raw price return, flagged
                factor = 1.0
            else:
                factor = float(raw)
            close = _close(prices, code)
            if close is None:
                # A5d-5: frozen from the first no-close day; A5e-8: ex-rights factors during the
                # suspension accumulate so the resume day does not show a fake drop.
                st["frozen_days"][code] = st["frozen_days"].get(code, 0) + 1
                st["acc_factor"][code] = st["acc_factor"][code] * factor
            else:
                ratios[code] = close / (st["last_close"][code] * st["acc_factor"][code] * factor)
                st["frozen_days"].pop(code, None)
                st["last_close"][code] = close
                st["acc_factor"][code] = 1.0
        for name in CANDIDATES:
            cs = st["candidates"][name]
            cs["holdings"] = {c: (_r4(v * ratios[c]) if c in ratios else v) for c, v in cs["holdings"].items()}
            pre_flags[name] = [f"adj_missing:{c}" for c in sorted(adj_missing) if c in cs["holdings"]]
        # A5b-6 / A5c-6: 20 consecutive no-close days = delisted; exit at the last close and pay
        # only the sell leg (fee + tax + slippage); the cash waits for the next rebalance.
        delisted = sorted(c for c, n in st["frozen_days"].items() if n >= DELIST_DAYS)
        for name in CANDIDATES:
            cs = st["candidates"][name]
            for code in delisted:
                if code in cs["holdings"]:
                    value = cs["holdings"].pop(code)
                    cs["cash"] = _r4(cs["cash"] + value - sr.sell_cost(value))
                    delist_flags[name].append(f"delisted_exit:{code}")
            cs["nav"] = _r4(sum(cs["holdings"].values()) + cs["cash"])
        for code in delisted:
            st["frozen_days"].pop(code, None)
        # Rebalances (A2): nominal day or still pending.
        for name in CANDIDATES:
            cs = st["candidates"][name]
            p = cs["pending_rebalance"]
            if p is None or not _has_ranking(rankings, name):
                continue    # no ranking (T-1 data missing, A5d-3) -> stays pending = deferred
            pre_trade_nav[name] = cs["nav"]
            rebalance_rows.append(_rebalance(st, name, prices, rankings[name], p["nominal_date"],
                                             iso, written_at, "rebalance", p["s2_coverage"]))
            if p["nominal_date"] != iso:
                meta["deferrals"].append({"candidate": name, "nominal_date": p["nominal_date"],
                                          "executed": iso,
                                          "trading_days": sr.trading_days_between(cal, _d(p["nominal_date"]), d)})
        _cleanup_codes(st)

    # Daily rows.
    daily = []
    for name in CANDIDATES:
        cs = st["candidates"][name]
        was_deferred = (cs["pending_rebalance"] is not None
                        or any(r["candidate"] == name and r["nominal_date"] != iso for r in rebalance_rows))
        flags = (["missing_day"] if missing else []) + (["rebalance_deferred"] if was_deferred else [])
        if name == S2_NAME and s2_status_missing:
            flags.append("s2_status_missing")
        flags += [f for r in rebalance_rows if r["candidate"] == name for f in r["flags"]]
        flags += pre_flags[name] + delist_flags[name]
        ret = _ret(cs["nav"], prev_nav[name]) if cs["nav"] != prev_nav[name] else 0.0
        cs["rets"].append(ret)
        daily.append(_daily_row(st, name, iso, written_at, ret, flags))
    bench = _benchmark_row(st, inp, iso, missing)
    meta["last_processed"] = iso

    # A3 / A5d-7: judgment day.  A rebalance that landed on it (only possible through a deferral)
    # is excluded: the judgment nav is the pre-rebalance market value.
    if iso == meta["judgment_date"]:
        pre = {}
        for name, nav_pre in pre_trade_nav.items():
            pre[name] = {"nav": nav_pre, "ret": _ret(nav_pre, prev_nav[name]) if nav_pre != prev_nav[name] else 0.0}
        meta["judgment"] = {"date": iso, "complete": not missing, "pre_rebalance": pre}
        if meta["status"] == "active":
            meta["status"] = "ended"
    return rebalance_rows + daily + [bench]


# --------------------------------------------------------------------------- #
# Judgment-day report (A3, A4, A5c-1, A5d-7)
# --------------------------------------------------------------------------- #

def final_report(state, grace_elapsed=False):
    """Numbers of the judgment day, from the ledger's own accumulated series only: per-candidate
    nav / total return / Sharpe and the 0050 total return / Sharpe.  No pass/fail verdict and no
    comparison outputs.  Raises ValueError if the judgment day has not been processed.
    `grace_elapsed`: the caller states that 10 trading days passed after a judgment day with
    missing data (A5d-7); the report then carries impaired=True (the last available nav is used)."""
    meta = state["meta"]
    j = meta.get("judgment")
    if meta["status"] != "ended" or j is None:
        raise ValueError("judgment day not processed (state is not 'ended')")
    cands = {}
    for name in CANDIDATES:
        cs = state["candidates"][name]
        nav, rets = cs["nav"], list(cs["rets"])
        pre = j["pre_rebalance"].get(name)
        if pre:
            nav, rets[-1] = pre["nav"], pre["ret"]
        cands[name] = {"nav": nav, "total_return": nav / INITIAL_CAPITAL - 1.0,
                       "sharpe": sharpe_ratio(rets, rf=0.0, periods_per_year=252)}
    b = state["benchmark"]
    impaired = bool(meta["impaired"])
    reasons = list(meta["impaired_reasons"])
    if grace_elapsed and not j["complete"]:
        impaired = True
        if "judgment_data_missing" not in reasons:
            reasons.append("judgment_data_missing")
    return {"judgment_date": j["date"], "judgment_data_complete": j["complete"],
            "impaired": impaired, "impaired_reasons": reasons, "candidates": cands,
            "benchmark": {"adj_close": b["last_close"],
                          "total_return": b["last_close"] / b["start_close"] - 1.0,
                          "sharpe": sharpe_ratio(b["rets"], rf=0.0, periods_per_year=252)}}


# --------------------------------------------------------------------------- #
# Correction series helpers (A5d-1 / ledger format section 1)
# --------------------------------------------------------------------------- #

def _supersede_key(row):
    """Key of a row inside its own file: rebalances by nominal_date, the others by date."""
    return row["nominal_date"] if row["file"] == "rebalances" else row["date"]


def supersede_index(original_rows):
    """Build the `superseded_written_at` argument of ``mark_superseded`` from the rows being
    replaced: {file: {date or nominal_date: {candidate: written_at}}} (benchmark: written_at str).
    Uses the effective (newest) original row per key."""
    index = {}
    for file in ("daily", "rebalances", "benchmark"):
        for row in effective_rows(original_rows, file):
            slot = index.setdefault(file, {})
            if file == "benchmark":
                slot[_supersede_key(row)] = row["written_at"]
            else:
                slot.setdefault(_supersede_key(row), {})[row["candidate"]] = row["written_at"]
    return index


def mark_superseded(rows, superseded_written_at):
    """Copy of `rows` (a re-run's output) with ``supersedes`` set on daily / rebalances / benchmark
    rows to the written_at of the original row of the SAME file (and candidate) and the same date
    (rebalances: the same nominal_date, whatever the original fill date was), and the ``late_fill``
    flag added to daily rows.  `superseded_written_at` is one string for every row, or the dict
    from ``supersede_index``; a row without an original stays ``supersedes: None``.  Meta rows are
    overwrite-style and left alone."""
    out = []
    for row in rows:
        row = copy.deepcopy(row)
        if row.get("file") in ("daily", "rebalances", "benchmark"):
            if isinstance(superseded_written_at, str):
                w = superseded_written_at
            else:
                w = superseded_written_at.get(row["file"], {}).get(_supersede_key(row))
                if isinstance(w, dict):
                    w = w.get(row["candidate"])
            row["supersedes"] = w
            if row["file"] == "daily" and w is not None and "late_fill" not in row["flags"]:
                row["flags"].append("late_fill")
        out.append(row)
    return out


def effective_rows(rows, file, candidate=None):
    """Read rule of ledger format section 1: per date (rebalances: per nominal_date) keep the row
    with the newest ``written_at`` (later in the list wins ties); returned sorted by that date."""
    best = {}
    for row in rows:
        if row.get("file") != file or (candidate is not None and row.get("candidate") != candidate):
            continue
        key = (row.get("candidate"), row.get("date") or row.get("nominal_date"))
        old = best.get(key)
        if old is None or (row.get("written_at") or "") >= (old.get("written_at") or ""):
            best[key] = row
    return [best[k] for k in sorted(best, key=lambda k: (k[1], k[0] or ""))]
