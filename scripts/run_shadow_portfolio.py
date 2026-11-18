# -*- coding: utf-8 -*-
"""Daily entry point of the forward shadow portfolio (spec: docs/shadow_portfolio_spec_2026-11.md,
clause numbers such as A5d-1 refer to it; file layout: docs/shadow_ledger_format_2026-11.md section 6).

Glue between the three pure modules (shadow_market_data: fetch, shadow_rules: rule functions,
shadow_portfolio: ledger state machine ``step``) and one local directory, the "ledger dir".  It does
all file I/O of the job and NO git operation (checkout / commit / push belong to the workflow).

Job timing (every trading day, 07:00 Taipei; one run does both):
  0. (before the start day, and on the start day before the main run) ``--prefetch`` stores the
             whole-market closes of every closed trading day after the factor cache's end, so the
             start day's ranking (as of the day before the start day) can see them.  Writes only
             prices/ and adj/.
  1. book:   every trading day after the last processed one, up to the last closed trading day
             (yesterday or earlier), strictly in order, one ``step`` per day.
  2. ranking for today D: if ``rebalance_due`` says some candidate rebalances on D, rank with data
             as of D-1 (previous trading day) and store it in ``rankings/<D>.json``; the booking of D
             (next day's 07:00 job) reads that file.  If the file is missing when D is booked (the job
             did not run that day) the ranking is computed on the spot, as of D's previous trading day,
             from the current cache, and the rebalance row is flagged ``late_ranking``.  Nothing at or
             after D is ever used for D's ranking.  No ranking is produced when the whole-market data
             of the as-of day is missing (A5d-3), nor for the start day when the data used is stale.
  Before 1., repairs (A5d-1, A5e-6) are tried: whole-market-missing days inside their retry window and
  data days whose ex-rights tables failed are fetched again; on success the state is restored from the
  snapshot before the first repaired day and every day from there on is re-run from the stored inputs,
  the new rows being marked ``supersedes`` (append-only).  The re-run is protected by
  ``meta/rerun_pending.json`` (written first, removed last; a job that finds it rolls the partial
  writes back and does the re-run again).

Ledger dir (``--ledger-dir``, default <project>/shadow_ledger/)
  ledger/<candidate>/daily.jsonl, ledger/<candidate>/rebalances.jsonl, ledger/benchmark_0050.jsonl
                         JSONL, append only; rows carry no "file"/"candidate" key (the path says it)
  prices/<date>.json     {"date","twse","tpex","benchmark","fetched_at"}.  "twse"/"tpex" are both null on
                         a whole-market-missing day (the file is then the record of the attempt and holds
                         the raw 0050 data).  "benchmark" = {"source","series": {date: adjclose}} raw,
                         a window from (last real 0050 date - 7 days) to the day
  adj/<date>.json        {"date","factors": {code: {"factor","source"}},"tables_ok","fetched_at"}
  rankings/<date>.json   {"date","computed_at","data_through":{"median","max"},"cache_coverage",
                          "bad_cache_files","flags","candidates":{name:{"asof","weights","ranks"(every ranked
                          code),"ranked","detail"(top 200)[,"late_ranking"]}}}
  s2/<date>.json         {"complete","ratio","computed_at"[,"late_s2"]}  (write once)
  snapshots/<date>.json  full ``state`` after processing that day (replay / re-run / resume source)
  meta/state.json        the ``state["meta"]`` of the latest step (+ written_at), overwrite style
  meta/last_run.json     {"run_at","exit_code","processed_days","alerts",...} of the latest real run
  meta/rerun_pending.json  present only while a repair re-run is in progress
  meta/lock.json         lock info (start_date, snapshot hashes, ...), written by the lock procedure
  meta/calendar.json     {"years": {"2026": [dates...]}}  trading calendar actually used (replay needs it)

Exit codes: 0 ok, 1 data / environment error, 2 voided (A5 20 missing days), 3 lock check failed (no
lock.json, snapshot hash mismatch or unusable snapshot, or a real run before the start date), 4 replay
nav series differ from the original.  Every run prints ``ALERT: ...`` lines for things the operator
must look at; the same strings go to meta/last_run.json.

Lines marked "ASSUMPTION:" are places where the spec or the task is silent or ambiguous and one reading
had to be chosen; they are listed in the hand-off report.
"""
import argparse
import bisect
import hashlib
import json
import math
import os
import shutil
import sys
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import finlab_factors as ff  # noqa: E402
import metrics  # noqa: E402
import shadow_market_data as smd  # noqa: E402
import shadow_portfolio as sp  # noqa: E402
import shadow_rules as sr  # noqa: E402

import report_factor_ic as rfi  # noqa: E402

TZ = timezone(timedelta(hours=8))
DEFAULT_LEDGER_DIR = os.path.join(BASE_DIR, "shadow_ledger")
DEFAULT_UNIVERSE_FILE = os.path.join(BASE_DIR, "shadow", "universe_2026-11.json")
DEFAULT_INDUSTRIES_FILE = os.path.join(BASE_DIR, "shadow", "industries_snapshot_2026-11.json")

EXIT_OK, EXIT_ERROR, EXIT_VOID, EXIT_LOCK, EXIT_REPLAY = 0, 1, 2, 3, 4
RANKING_DETAIL_N = 200          # A5c-8: top 200 are stored in the ranking detail
BENCH_LOOKBACK_DAYS = 14        # minimum calendar days of Yahoo history fetched around a day
BENCH_LOOKBACK_MAX = 400        # ... and the maximum, when 0050 has been missing for long
BENCH_PAD_DAYS = 7              # the window starts this many days before the last real 0050 value
BENCH_GAP_ALERT = 3             # more consecutive carried 0050 days than this -> alert
MIN_CACHE_RATIO = 0.5           # fewer cached universe codes than this share -> data error
LOW_COVERAGE = 0.95             # cache coverage below this -> low_cache_coverage flag + alert
STALE_TRADING_DAYS = 1          # ranking data older than as-of by more than this -> stale_data
ADJ_RETRY_DAYS = sp.RETRY_DAYS  # ex-rights tables of a data day are re-fetched inside the same window
SNAPSHOT_MIN_PRIMARY_SHARE = 0.5
RERUN_REL = "meta/rerun_pending.json"
BENCH_REL = "ledger/benchmark_0050.jsonl"

# A1 / task: the locked weights.  Literals on purpose: ff.RANKING_WEIGHTS could be re-adopted later,
# these must not move once the rules are locked.
WEIGHTS = {
    "S0_baseline": {"revenue": 0.4, "roe": 0.3, "momentum": 0.3},      # momentum window = ff.MOMENTUM_DAYS (60)
    "S1_high250": {"revenue": 0.4, "roe": 0.3, "high250": 0.3},
    "S2_high250_revday": {"revenue": 0.4, "roe": 0.3, "high250": 0.3},
    "S3_high250_cap": {"revenue": 0.4, "roe": 0.3, "high250": 0.3},
}


class ShadowError(Exception):
    """A job-level failure with the exit code to report."""

    def __init__(self, message, code=EXIT_ERROR):
        super().__init__(message)
        self.code = code


def log(msg):
    print(msg, flush=True)


class Report:
    """What one run tells the operator: alerts (printed as ``ALERT: ...`` and stored in last_run.json),
    the days booked and the days re-run, and the final state."""

    def __init__(self):
        self.alerts, self.processed, self.reran, self.state = [], [], [], None

    def alert(self, msg):
        if msg not in self.alerts:
            self.alerts.append(msg)
            log("ALERT: " + msg)


# --------------------------------------------------------------------------- #
# Serialisation: fixed key order, no scientific notation, "\n" line ends
# --------------------------------------------------------------------------- #

def _fmt_float(x):
    if not math.isfinite(x):
        raise ValueError(f"non-finite number in ledger data: {x!r}")
    r = repr(x)
    if "e" in r or "E" in r:                 # format doc section 1: no scientific notation
        r = format(Decimal(r), "f")
        if "." not in r:
            r += ".0"
    return r


def dumps(obj, indent=None, _level=0):
    """JSON text of `obj`: dict key order kept (no sort_keys), ensure_ascii=False, floats never in
    scientific notation.  `indent=None` gives one compact line."""
    if obj is None:
        return "null"
    if obj is True:
        return "true"
    if obj is False:
        return "false"
    if isinstance(obj, int):
        return str(int(obj))
    if isinstance(obj, float):
        return _fmt_float(obj)
    if isinstance(obj, str):
        return json.dumps(obj, ensure_ascii=False)
    if isinstance(obj, (list, tuple)):
        if not obj:
            return "[]"
        parts = [dumps(v, indent, _level + 1) for v in obj]
        if indent is None:
            return "[" + ",".join(parts) + "]"
        pad, end = " " * (indent * (_level + 1)), " " * (indent * _level)
        return "[\n" + ",\n".join(pad + p for p in parts) + "\n" + end + "]"
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        parts = [(json.dumps(str(k), ensure_ascii=False), dumps(v, indent, _level + 1)) for k, v in obj.items()]
        if indent is None:
            return "{" + ",".join(f"{k}:{v}" for k, v in parts) + "}"
        pad, end = " " * (indent * (_level + 1)), " " * (indent * _level)
        return "{\n" + ",\n".join(f"{pad}{k}: {v}" for k, v in parts) + "\n" + end + "}"
    raise TypeError(f"cannot serialise {type(obj).__name__}")


# --------------------------------------------------------------------------- #
# Store: all file I/O of the job (dry-run keeps writes in memory)
# --------------------------------------------------------------------------- #

class Store:
    """One local directory.  JSON files are written via temp file + rename, JSONL files are appended
    and flushed.  With ``dry_run`` nothing touches the disk: writes go to an in-memory overlay that
    later reads in the same process see, so a dry run can still process several days in a row."""

    def __init__(self, root, dry_run=False):
        self.root = root
        self.dry_run = dry_run
        self._json = {}
        self._rows = {}
        self._deleted = set()

    def path(self, rel):
        return os.path.join(self.root, *rel.split("/"))

    def exists(self, rel):
        if rel in self._deleted:
            return False
        return rel in self._json or os.path.isfile(self.path(rel))

    def read_json(self, rel, default=None):
        if rel in self._deleted:
            return default
        if rel in self._json:
            return json.loads(self._json[rel])
        p = self.path(rel)
        if not os.path.isfile(p):
            return default
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError) as e:
            raise ShadowError(f"cannot read {rel}: {e}") from e

    def write_json(self, rel, obj, indent=None):
        text = dumps(obj, indent=indent)
        if self.dry_run:
            self._json[rel] = text
            self._deleted.discard(rel)
            return
        p = self.path(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text + "\n")
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(5):             # Windows: a scanner may hold the target for a moment
            try:
                os.replace(tmp, p)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                _time.sleep(0.2)

    def delete(self, rel):
        if self.dry_run:
            self._json.pop(rel, None)
            if os.path.isfile(self.path(rel)):
                self._deleted.add(rel)
            return
        if os.path.isfile(self.path(rel)):
            os.remove(self.path(rel))

    def read_rows(self, rel):
        out = []
        p = self.path(rel)
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    out = [json.loads(line) for line in f if line.strip()]
            except (OSError, ValueError) as e:
                raise ShadowError(f"cannot read {rel}: {e}") from e
        return out + [json.loads(t) for t in self._rows.get(rel, [])]

    def append_rows(self, rel, rows):
        texts = [dumps(r) for r in rows]
        if not texts:
            return
        if self.dry_run:
            self._rows.setdefault(rel, []).extend(texts)
            return
        p = self.path(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8", newline="\n") as f:
            f.write("".join(t + "\n" for t in texts))
            f.flush()
            os.fsync(f.fileno())

    def jsonl_sizes(self):
        """{rel: byte size} of every ledger JSONL file on disk (the rollback marks of a re-run)."""
        out = {}
        base = self.path("ledger")
        for sub, _, files in os.walk(base):
            for f in files:
                if f.endswith(".jsonl"):
                    p = os.path.join(sub, f)
                    out[os.path.relpath(p, self.root).replace(os.sep, "/")] = os.path.getsize(p)
        return out

    def rollback_jsonl(self, sizes):
        """Cut every ledger JSONL file back to its recorded size (files that did not exist go away).
        Only used on the half-written rows of a crashed re-run."""
        if self.dry_run:
            raise ShadowError("cannot roll back files in a dry run")
        for rel, size in self.jsonl_sizes().items():
            if rel not in sizes:
                os.remove(self.path(rel))
            elif size > sizes[rel]:
                with open(self.path(rel), "r+b") as f:
                    f.truncate(sizes[rel])

    def clean_tmp(self):
        """Remove ``*.tmp`` leftovers of a crashed atomic write."""
        if self.dry_run or not os.path.isdir(self.root):
            return
        for sub, _, files in os.walk(self.root):
            for f in files:
                if f.endswith(".tmp"):
                    try:
                        os.remove(os.path.join(sub, f))
                    except OSError:
                        pass

    def list_dates(self, subdir):
        """Sorted ISO dates of <subdir>/<date>.json (disk and overlay)."""
        names = set()
        d = self.path(subdir)
        if os.path.isdir(d):
            names.update(n[:-5] for n in os.listdir(d) if n.endswith(".json"))
        names.update(r[len(subdir) + 1:-5] for r in self._json if r.startswith(subdir + "/"))
        names -= {r[len(subdir) + 1:-5] for r in self._deleted if r.startswith(subdir + "/")}
        return sorted(names)


def _candidate_rel(name, kind):
    return f"ledger/{name}/{kind}.jsonl"


def load_ledger_rows(store, file, candidate=None):
    """Rows of one ledger file with the "file" / "candidate" keys re-attached (the shape
    ``shadow_portfolio.effective_rows`` / ``supersede_index`` expect)."""
    if file == "benchmark":
        rows = store.read_rows(BENCH_REL)
        return [{"file": "benchmark", **r} for r in rows]
    out = []
    for name in ([candidate] if candidate else sp.CANDIDATES):
        for r in store.read_rows(_candidate_rel(name, file)):
            out.append({"file": file, "candidate": name, **r})
    return out


def write_rows(store, rows):
    """Append step rows to their files.  "meta" rows go to meta/state.json (overwrite style)."""
    by_rel = {}
    for r in rows:
        f = r["file"]
        body = {k: v for k, v in r.items() if k not in ("file", "candidate")}
        if f == "meta":
            store.write_json("meta/state.json", body, indent=1)
            continue
        rel = BENCH_REL if f == "benchmark" else _candidate_rel(r["candidate"], f)
        by_rel.setdefault(rel, []).append(body)
    for rel, bodies in by_rel.items():
        store.append_rows(rel, bodies)


# --------------------------------------------------------------------------- #
# Time
# --------------------------------------------------------------------------- #

def parse_now(text=None):
    """Aware datetime in Taipei time.  A naive string is taken as Taipei time."""
    if text is None:
        return datetime.now(TZ)
    dt = datetime.fromisoformat(text)
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)


def _utf8_stdio():
    """Windows consoles (cp950) cannot print every character of the alerts."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


# --------------------------------------------------------------------------- #
# Fetchers (injectable; tests pass fakes, nothing here is called in --replay)
# --------------------------------------------------------------------------- #

def fetch_yahoo_0050_series(day, lookback_days=BENCH_LOOKBACK_DAYS):
    """{ISO date: adjclose} of 0050.TW for `lookback_days` calendar days up to and including `day`,
    or None.  Yahoo rewrites adjclose backwards at every ex-dividend date, so only ratios inside ONE
    fetch are meaningful (see ``benchmark_close_for``)."""
    try:
        p1 = datetime.combine(day - timedelta(days=lookback_days), time(0, 0), tzinfo=TZ)
        p2 = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=TZ)
        url = (smd.YAHOO_CHART_URL.format(symbol="0050.TW")
               + "?period1=%d&period2=%d&interval=1d&includeAdjustedClose=true"
               % (int(p1.timestamp()), int(p2.timestamp())))
        j = smd._get_json(url, f"Yahoo 0050 {day}")
        if j is None:
            return None
        result = j["chart"]["result"][0]
        stamps = result["timestamp"]
        adj = result["indicators"]["adjclose"][0]["adjclose"]
        if len(stamps) != len(adj):
            return None
        out = {}
        for ts, a in zip(stamps, adj):
            if isinstance(a, (int, float)) and not isinstance(a, bool) and math.isfinite(a) and a > 0:
                out[datetime.fromtimestamp(ts, TZ).date().isoformat()] = float(a)
        return out
    except Exception as e:  # noqa: BLE001 - a fetch problem must degrade to "missing", never raise
        smd.logger.warning("Yahoo 0050 %s failed: %r", day, e)
        return None


class Fetchers:
    """Default network fetchers.  Methods call the shadow_market_data functions at call time."""
    adj_fallback = None            # None = shadow_market_data's default (Yahoo ratio, TPEx cash ex-dividend only)

    def twse_close(self, d):
        return smd.fetch_twse_close(d)

    def tpex_close(self, d):
        return smd.fetch_tpex_close(d)

    def twse_exright(self, s, e):
        return smd.fetch_twse_exright(s, e)

    def tpex_exright(self, s, e):
        return smd.fetch_tpex_exright(s, e)

    def calendar(self, year):
        return smd.fetch_trading_calendar(year)

    def benchmark_series(self, d, lookback_days=BENCH_LOOKBACK_DAYS):
        return fetch_yahoo_0050_series(d, lookback_days)


# --------------------------------------------------------------------------- #
# Lock and locked snapshots
# --------------------------------------------------------------------------- #

def sha256_file(path):
    """SHA-256 of the file with CRLF turned into LF: the same snapshot checked out with different
    line-ending settings (core.autocrlf) must hash the same.  (.gitattributes also marks shadow/**
    as -text; both are used on purpose.)  The lock procedure must hash with this function."""
    with open(path, "rb") as f:
        return hashlib.sha256(f.read().replace(b"\r\n", b"\n")).hexdigest()


def _unwrap(data, keys):
    if isinstance(data, dict):
        for k in keys:
            if k in data and isinstance(data[k], (list, dict)):
                return data[k]
    return data


def load_snapshots(universe_file, industries_file):
    """(sorted universe codes, {code: primary industry}) from the locked snapshot files.
    ASSUMPTION: the lock-snapshot builder (stage 6) is not written yet, so these shapes are accepted:
    universe = list of codes, or {"codes": [...]}; industries = {code: [categories]} (raw
    ``get_industries()``; the primary industry is derived here with ``shadow_rules.primary_industry``),
    optionally wrapped in {"industries": ...}, or {code: "primary industry"} / {"primary": {...}} when
    the primary map was stored already.  Any other shape, or a result where fewer than half of the
    codes get a primary industry other than "其他", is a lock failure (exit 3): such a wrapper would
    otherwise silently turn every code into "其他" and defeat the S3 sector cap."""
    try:
        with open(universe_file, encoding="utf-8") as f:
            uni = _unwrap(json.load(f), ("codes", "universe"))
        with open(industries_file, encoding="utf-8") as f:
            ind = _unwrap(json.load(f), ("primary", "industries"))
    except (OSError, ValueError) as e:
        raise ShadowError(f"cannot read the locked snapshot files: {e}", EXIT_LOCK) from e
    if isinstance(uni, dict):
        uni = list(uni)
    if not isinstance(uni, list) or not uni:
        raise ShadowError("universe snapshot is not a non-empty list of codes", EXIT_LOCK)
    universe = sorted({str(c) for c in uni})
    if not isinstance(ind, dict) or not ind:
        raise ShadowError("industries snapshot is not a mapping", EXIT_LOCK)
    if not all(isinstance(v, (str, list)) or v is None for v in ind.values()):
        raise ShadowError("industries snapshot: values must be category lists or primary-industry strings "
                          "(is the data wrapped in another object?)", EXIT_LOCK)
    if all(isinstance(v, str) for v in ind.values()):
        primary = {c: ind.get(c, sr.OTHER) for c in universe}
    else:
        primary = sr.primary_industry(ind, set(universe))
    share = sum(1 for v in primary.values() if v != sr.OTHER) / len(primary)
    if share < SNAPSHOT_MIN_PRIMARY_SHARE:
        raise ShadowError(f"industries snapshot: only {share:.0%} of the universe has a primary industry other "
                          f"than 其他 (< {SNAPSHOT_MIN_PRIMARY_SHARE:.0%}); wrong shape or wrong file", EXIT_LOCK)
    return universe, dict(sorted(primary.items()))


def check_lock(store, universe_file, industries_file, today, dry_run, allow_before_start=False):
    """Read meta/lock.json and verify the snapshot hashes (both files).  Returns (lock, universe, primary)."""
    lock = store.read_json("meta/lock.json")
    if not isinstance(lock, dict) or not lock.get("start_date"):
        raise ShadowError("meta/lock.json missing or without start_date", EXIT_LOCK)
    for label, path, key in (("universe", universe_file, "universe_sha256"),
                             ("industries", industries_file, "industries_sha256")):
        if not os.path.isfile(path):
            raise ShadowError(f"locked {label} snapshot not found: {path}", EXIT_LOCK)
        want = lock.get(key) or ""
        if not want:
            # ASSUMPTION: an empty hash means "not locked yet"; tolerated only in a dry run.
            if not dry_run:
                raise ShadowError(f"lock.json has no {key}", EXIT_LOCK)
            log(f"warning: lock.json has no {key} (dry run only)")
        elif sha256_file(path) != want:
            raise ShadowError(f"{label} snapshot hash differs from lock.json ({path})", EXIT_LOCK)
    start = date.fromisoformat(lock["start_date"])
    if today < start and not dry_run and not allow_before_start:
        raise ShadowError(f"today {today} is before the start date {start}: only --dry-run is allowed", EXIT_LOCK)
    universe, primary = load_snapshots(universe_file, industries_file)
    return lock, universe, primary


# --------------------------------------------------------------------------- #
# Calendar
# --------------------------------------------------------------------------- #

def load_calendar(store, fetchers, lock_start, today, allow_fetch=True, processed_through=None,
                  report=None, persist=True):
    """Sorted list of trading days from the lock start year to next year.  Years fetched fresh are
    stored in meta/calendar.json (the replay uses the stored copy).  A year that cannot be fetched
    falls back to the stored one; a year known to neither is simply absent (``step`` copes).
    A freshly fetched year that differs from the stored one on days already processed (<=
    `processed_through`) is NOT taken over (alert): the stored version is what the ledger was booked
    with.  A difference only in the future updates the stored copy."""
    doc = store.read_json("meta/calendar.json") or {"years": {}}
    years = doc.get("years") or {}
    changed = False
    first, last = lock_start.year, today.year + 1
    if allow_fetch:
        for y in range(first, last + 1):
            if y >= today.year or str(y) not in years:
                got = fetchers.calendar(y)
                if got is None:
                    continue
                new = sorted(d.isoformat() for d in got)
                old = years.get(str(y))
                if old is None:
                    years[str(y)], changed = new, True
                elif old != new:
                    past = sorted(x for x in set(old) ^ set(new) if processed_through and x <= processed_through)
                    if past:
                        msg = (f"calendar {y}: the fetched TWSE table differs from the stored one on processed "
                               f"days {past[:5]}; the stored version is kept")
                        if report:
                            report.alert(msg)
                        else:
                            log("ALERT: " + msg)
                    else:
                        years[str(y)], changed = new, True
    if changed and persist:
        store.write_json("meta/calendar.json", {"years": years}, indent=1)
    cal = sorted({date.fromisoformat(x) for y in range(first, last + 1) for x in years.get(str(y), [])})
    if not cal:
        raise ShadowError("no trading calendar available (fetch failed and none stored)")
    return cal


# --------------------------------------------------------------------------- #
# Day inputs: prices / adj / benchmark
# --------------------------------------------------------------------------- #

def _prices_complete(pf):
    return bool(pf) and pf.get("twse") is not None and pf.get("tpex") is not None


def merge_markets(twse, tpex):
    """{code: entry} of both markets.  ASSUMPTION: a code present in both markets cannot be trusted
    and becomes ``bad_row`` ("no close"); it should not happen."""
    out = dict(twse)
    for code, e in tpex.items():
        if code in out:
            out[code] = smd._bad_entry()
        else:
            out[code] = e
    return out


def build_adj(fetchers, day, twse, tpex, now_iso):
    """adj/<day>.json content: factors of the codes the day's quotes flag as ex-rights plus every code
    in the day's ex-rights tables.  `twse` / `tpex` are the day's closes or None (market missing)."""
    tw_ex = fetchers.twse_exright(day, day)
    tp_ex = fetchers.tpex_exright(day, day)
    tables = {"twse": tw_ex, "tpex": tp_ex, "twse_close": twse, "tpex_close": tpex}
    codes = set()
    for q in (twse, tpex):
        if q:
            codes.update(c for c, e in q.items() if e.get("status") == "exright")
    for t in (tw_ex, tp_ex):
        if t:
            codes.update(code for (d, code) in t if d == day)
    factors = {}
    for code in sorted(codes):
        f, src = smd.adj_factor(day, code, tables, fallback=fetchers.adj_fallback)
        factors[code] = {"factor": f, "source": src}
    return {"date": day.isoformat(), "factors": factors,
            "tables_ok": {"twse": tw_ex is not None, "tpex": tp_ex is not None}, "fetched_at": now_iso}


def fetch_benchmark_raw(fetchers, day, lookback_days=BENCH_LOOKBACK_DAYS):
    """Raw 0050 adjclose series up to `day` (nothing after it), or None."""
    series = fetchers.benchmark_series(day, lookback_days)
    if series is None:
        return None
    keep = {d: float(v) for d, v in sorted(series.items()) if d <= day.isoformat()}
    return {"source": "yahoo", "series": keep}


def last_real_benchmark_date(store, before):
    """Date of the newest effective benchmark row before `before` that holds a real value."""
    rows = sp.effective_rows(load_ledger_rows(store, "benchmark"), "benchmark")
    for r in reversed(rows):
        if r["date"] < before.isoformat() and "carry_forward" not in (r.get("flags") or []):
            return r["date"]
    return None


def bench_lookback(store, day):
    """Calendar days of Yahoo history to fetch for `day`: enough to reach 7 days before the last real
    0050 value of the ledger (so the ratio against it can be taken however long 0050 was missing),
    at least 14 and at most 400."""
    last = last_real_benchmark_date(store, day)
    if last is None:
        return BENCH_LOOKBACK_DAYS
    need = (day - date.fromisoformat(last)).days + BENCH_PAD_DAYS
    return min(BENCH_LOOKBACK_MAX, max(BENCH_LOOKBACK_DAYS, need))


def benchmark_close_for(raw, day, status, last_close, last_real_date):
    """The ``benchmark_close`` handed to ``step``.

    Yahoo's adjclose is rewritten backwards at every ex-dividend date, so adjclose values fetched on
    different days are on different scales and must not be strung together.  Inside ONE fetch the
    ratio of two days is scale-free, so the ledger's own 0050 series is chained with it:
    ``last_close x adjclose(day) / adjclose(last real observation)`` (normally the previous trading
    day).  On the start day (state not started) the raw adjclose of the day itself is the origin.
    ASSUMPTION: the reference is the last date the ledger booked a REAL 0050 value (not a carried
    one); it equals the previous trading day unless 0050 data was missing, in which case it keeps
    the skipped days' move instead of losing it.  The fetch window reaches back to that date
    (``bench_lookback``).  None when anything needed is missing."""
    if not raw:
        return None
    series = raw.get("series") or {}
    a_day = series.get(day.isoformat())
    if a_day is None or not a_day > 0:
        return None
    if status == "not_started":
        return float(a_day)
    if last_close is None or last_real_date is None:
        return None
    a_ref = series.get(last_real_date)
    if a_ref is None or not a_ref > 0:
        return None
    return last_close * (a_day / a_ref)


# --------------------------------------------------------------------------- #
# Ranking (A5b-1, A5d-3, A5c-8, A5d-2)
# --------------------------------------------------------------------------- #

def default_load_universe(universe):
    """(universe_data, fund_refresh_by_code, stats) of the locked universe from cache/factor_run.
    universe_data has the shape of ``report_factor_ic.load_cached_universe`` (non-skipped codes only,
    built by the same ``universe_item_from_cache_entry``); fund_refresh_by_code holds the cache's
    ``fund_refresh`` of every non-skipped universe code (A5d-4 denominator); stats = {"files":
    universe codes with a readable cache file (skipped ones included), "bad_json": unreadable files}.
    Each universe code's file is parsed once, and files of codes outside the universe are not read.
    ASSUMPTION: a universe code without a cache file is not in the denominator."""
    data, fund, files, bad = {}, {}, 0, 0
    for code in universe:
        p = os.path.join(rfi.RUN_CACHE_DIR, code + ".json")
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                entry = json.load(f)
        except (OSError, ValueError):
            bad += 1
            continue
        files += 1
        if entry.get("skip"):
            continue
        data[code] = rfi.universe_item_from_cache_entry(entry)
        fund[code] = entry.get("fund_refresh") or {}
    return data, fund, {"files": files, "bad_json": bad}


def extend_bars(universe_data, day_inputs, asof):
    """Return a copy of `universe_data` whose bars run to `asof` (A5b-1).

    Cache bars are kept as they are (the weekly update has priority).  For every later trading day
    <= asof, every code that has a valid close that day gets a bar from that day's stored prices, with

        c_adj_t = c_adj_{t-1} x c_t / (c_{t-1} x f_t)

    chained onto the code's OWN last bar (f_t from adj/<day>.json; missing or None = 1.0).
    A day without a close for the code adds no bar; ASSUMPTION: its factor is kept and multiplied into
    the next bar's (like the suspension rule A5e-8), so an ex-dividend inside a gap is not lost.
    `day_inputs` = {iso day: (prices {code: row} | None, adj {code: factor | None})} (days after `asof`
    are ignored).  Input dicts are not mutated."""
    days = sorted(d for d in day_inputs if d <= asof)
    out = {}
    for code, entry in universe_data.items():
        bars = entry["bars"]
        if not bars:
            out[code] = entry
            continue
        new_bars = list(bars)
        last = bars[-1]
        pending = 1.0
        for d in days:
            if d <= last["date"]:
                continue
            prices, adj = day_inputs[d]
            f = (adj or {}).get(code)
            f = 1.0 if f is None else float(f)
            close = sp._close(prices or {}, code)           # the ledger's own rule for "has a close"
            if close is None:
                pending *= f
                continue
            row = prices[code]
            prev_adj = last.get("c_adj")
            if prev_adj is None:
                prev_adj = last["c"]
            bar = {"date": d,
                   "o": row.get("o") if row.get("o") is not None else close,
                   "h": row.get("h") if row.get("h") is not None else close,
                   "l": row.get("l") if row.get("l") is not None else close,
                   "c": close, "v": row.get("v") or 0,
                   "c_adj": prev_adj * close / (last["c"] * pending * f)}
            pending = 1.0
            new_bars.append(bar)
            last = bar
        if len(new_bars) == len(bars):
            out[code] = entry
        else:
            out[code] = {**entry, "bars": new_bars, "bar_dates": [b["date"] for b in new_bars]}
    return out


def build_detail(ranked, scores, ranks, weights, primary):
    """The top-200 detail rows of a ranking: code, score, per-factor percentiles, primary industry."""
    return [{"code": c, "score": scores[c], "ranks": {k: ranks[c][k] for k in weights},
             "industry": primary.get(c, sr.OTHER)} for c in ranked[:RANKING_DETAIL_N]]


def rederive(entry, primary):
    """A5d-2: rebuild ``ranked`` (and the detail) of a stored ranking from its stored per-factor
    percentiles and weights with the current composite / sort code.  Returns (entry, changed) where
    `changed` says the stored ``ranked`` differed.  Entries without ranks/weights are returned as is."""
    ranks, weights = entry.get("ranks"), entry.get("weights")
    if not ranks or not weights:
        return entry, False
    scores = ff.composite_scores({}, entry["asof"], weights=weights, ranks=ranks)
    ranked = sr.rank_top(scores, len(scores))
    return ({**entry, "ranked": ranked, "detail": build_detail(ranked, scores, ranks, weights, primary)},
            ranked != entry.get("ranked"))


class Ranker:
    """Computes the S0-S3 rankings and the S2 revenue-refresh status from the factor cache plus the
    stored daily prices.  `loader()` -> (universe_data, fund_refresh_by_code[, stats]), called once."""

    def __init__(self, inputs, cal, lock_start, universe, primary, loader=None):
        self.inputs = inputs                      # Store holding prices/ and adj/
        self.cal = cal
        self.lock_start = lock_start
        self.universe = universe
        self.primary = primary
        self.loader = loader or (lambda: default_load_universe(universe))
        self.report = None
        self.last_meta = {}
        self._loaded = None

    def _alert(self, msg):
        if self.report is not None:
            self.report.alert(msg)
        else:
            log("ALERT: " + msg)

    def loaded(self):
        if self._loaded is None:
            got = self.loader()
            data, fund = got[0], got[1]
            stats = got[2] if len(got) > 2 else {"files": len(data), "bad_json": 0}
            wanted = set(self.universe)
            data = {c: d for c, d in data.items() if c in wanted}
            if len(data) < MIN_CACHE_RATIO * len(self.universe):
                raise ShadowError(f"factor cache holds {len(data)} of {len(self.universe)} universe codes "
                                  f"(< {MIN_CACHE_RATIO:.0%}): cannot rank")
            self._loaded = (data, fund, stats)
        return self._loaded[0], self._loaded[1]

    def fund(self):
        return self.loaded()[1]

    def s2_status(self, day):
        """A5d-4: {"complete", "ratio"} from the cache's fund_refresh stamps, as of `day`."""
        complete, ratio = sr.revenue_refresh_complete(self.fund(), day)
        return {"complete": bool(complete), "ratio": ratio}

    def cache_end_median(self):
        """Median (over the universe codes) of the last bar date of the factor cache."""
        data, _ = self.loaded()
        ends = sorted(d["bars"][-1]["date"] for d in data.values() if d["bars"])
        return ends[len(ends) // 2] if ends else None

    def stored_days(self, asof):
        """{iso day: (prices | None, adj)} for the stored days after the oldest cache bar end, up to asof."""
        data, _ = self.loaded()
        ends = [d["bars"][-1]["date"] for d in data.values() if d["bars"]]
        floor = min(ends) if ends else asof
        out = {}
        for d in self.cal:
            iso = d.isoformat()
            if not (floor < iso <= asof):
                continue
            pf = self.inputs.read_json(f"prices/{iso}.json")
            af = self.inputs.read_json(f"adj/{iso}.json")
            if pf is None and af is None:
                continue
            prices = merge_markets(pf["twse"], pf["tpex"]) if _prices_complete(pf) else None
            adj = {c: v["factor"] for c, v in ((af or {}).get("factors") or {}).items()}
            out[iso] = (prices, adj)
        return out

    def extended_universe(self, asof):
        """The cached universe with each code's bars extended from the stored days up to `asof`."""
        data, _ = self.loaded()
        return extend_bars(data, self.stored_days(asof), asof)

    def coverage(self):
        self.loaded()
        stats = self._loaded[2]
        return stats["files"] / len(self.universe), stats.get("bad_json", 0)

    def compute(self, day, names, late=False, strict=False):
        """Rankings of `names` for rebalance day `day`, using data as of the previous trading day only.
        Returns {name: {"asof","weights","ranks","ranked","detail"[,"late_ranking"]}}; empty when the as-of
        day's whole-market data is missing (A5d-3: treated as missing data on the rebalance day), and,
        with `strict` (the start build), also when the data actually used is stale.  The file-level
        facts (data_through, cache coverage, flags) are left in ``self.last_meta``."""
        asof_d = sr.prev_trading_day(self.cal, day)
        asof = asof_d.isoformat()
        if not _prices_complete(self.inputs.read_json(f"prices/{asof}.json")):
            self._alert(f"ranking for {day} not produced: whole-market data of {asof} is missing"
                        + (" (run --prefetch)" if asof_d < self.lock_start else ""))
            return {}
        universe = self.extended_universe(asof)
        through = sorted(d["bar_dates"][i] for d in universe.values() if d["bar_dates"]
                         for i in [bisect.bisect_right(d["bar_dates"], asof) - 1] if i >= 0)
        flags = []
        if not through:
            self._alert(f"ranking for {day} not produced: no bars at all up to {asof}")
            return {}
        median, latest = through[len(through) // 2], through[-1]
        if sr.trading_days_between(self.cal, date.fromisoformat(median), asof_d) > STALE_TRADING_DAYS:
            flags.append("stale_data")
            self._alert(f"stale_data: the ranking for {day} (as of {asof}) uses data through {median} (median)")
            if strict:
                return {}
        cov, bad = self.coverage()
        if cov < LOW_COVERAGE:
            flags.append("low_cache_coverage")
            self._alert(f"low_cache_coverage: only {cov:.1%} of the universe has a readable cache file "
                        f"({bad} unreadable)")
        self.last_meta = {"data_through": {"median": median, "max": latest}, "cache_coverage": cov,
                          "bad_cache_files": bad, "flags": flags}
        out = {}
        ranks_memo, scores_memo = {}, {}
        for name in names:
            weights = WEIGHTS[name]
            extra = ff.used_extra_factors([weights])
            ekey = tuple(sorted(extra))
            if ekey not in ranks_memo:
                ranks_memo[ekey] = ff.factor_ranks(universe, asof, extra_factors=extra)
            ranks = ranks_memo[ekey]
            wkey = (ekey, tuple(sorted(weights.items())))
            if wkey not in scores_memo:
                scores_memo[wkey] = ff.composite_scores(universe, asof, weights=weights, ranks=ranks,
                                                        extra_factors=extra)
            scores = scores_memo[wkey]
            ranked = sr.rank_top(scores, len(scores))
            if not ranked:
                log(f"ranking {name} for {day}: empty (no code passed the filters)")
                continue
            out[name] = {"asof": asof, "weights": dict(weights),
                         "ranks": {c: {k: ranks[c][k] for k in weights} for c in ranked},
                         "ranked": ranked, "detail": build_detail(ranked, scores, ranks, weights, self.primary)}
            if late:
                out[name]["late_ranking"] = True
        return out


# --------------------------------------------------------------------------- #
# Context and per-day processing
# --------------------------------------------------------------------------- #

class Ctx:
    """Everything one processing pass needs.  `store` receives the ledger output, `inputs` is where
    prices / adj / rankings / s2 are read (the same store in a normal run, the original ledger dir in
    a replay).  `live` = may fetch and may compute rankings (False in a replay); `rederive` = rebuild
    every stored ranking from its stored percentiles (replay)."""

    def __init__(self, store, inputs, fetchers, now, cal, lock_start, ranker, live=True, primary=None,
                 report=None, rederive=False):
        self.store, self.inputs, self.fetchers = store, inputs, fetchers
        self.now, self.now_iso = now, now.isoformat(timespec="seconds")
        self.cal = cal
        self.cal_iso = [d.isoformat() for d in cal]
        self.lock_start = lock_start
        self.ranker = ranker
        self.live = live
        self.primary = primary or {}
        self.report = report or Report()
        self.rederive = rederive
        self.rederived = []
        if ranker is not None:
            ranker.report = self.report


def fetch_day_prices(ctx, day):
    """Fetch both markets and 0050; returns (prices-file dict, twse, tpex) (twse / tpex may be None)."""
    tw = ctx.fetchers.twse_close(day)
    tp = ctx.fetchers.tpex_close(day)
    ok = tw is not None and tp is not None
    # ASSUMPTION: half a market is worse than none (task decision): either market missing = whole day missing.
    pf = {"date": day.isoformat(), "twse": tw if ok else None, "tpex": tp if ok else None,
          "benchmark": fetch_benchmark_raw(ctx.fetchers, day, bench_lookback(ctx.store, day)),
          "fetched_at": ctx.now_iso}
    return pf, tw, tp


def ensure_day_files(ctx, day):
    """(prices file | None, adj file | None) for `day`; in a live run missing files are fetched and
    stored.  A stored file is never fetched again (re-runs, retries and replays use the stored one)."""
    iso = day.isoformat()
    pf = ctx.inputs.read_json(f"prices/{iso}.json")
    af = ctx.inputs.read_json(f"adj/{iso}.json")
    if ctx.live and (pf is None or af is None):
        if pf is None:
            pf, tw, tp = fetch_day_prices(ctx, day)
            ctx.store.write_json(f"prices/{iso}.json", pf)
        else:
            tw, tp = pf.get("twse"), pf.get("tpex")
        if af is None:
            # A5d / F6: the ex-rights tables are fetched even when the whole-market close is missing.
            af = build_adj(ctx.fetchers, day, tw, tp, ctx.now_iso)
            ctx.store.write_json(f"adj/{iso}.json", af)
    return pf, af


def fetch_repair(ctx, day, need_prices):
    """Fetch what the repair of `day` needs.  Returns (prices file | None, adj file, needs_rerun) or None
    when the fetch did not succeed.  need_prices: the whole-market close is missing (A5d-1); otherwise
    only the ex-rights tables of a data day are fetched again (A5e-6) and a re-run is needed only when
    the factors changed."""
    iso = day.isoformat()
    old_pf = ctx.inputs.read_json(f"prices/{iso}.json") or {}
    old_af = ctx.inputs.read_json(f"adj/{iso}.json") or {}
    if need_prices:
        pf, tw, tp = fetch_day_prices(ctx, day)
        if pf["twse"] is None or pf["tpex"] is None:
            return None
        if old_pf.get("benchmark"):
            pf["benchmark"] = old_pf["benchmark"]
        return pf, build_adj(ctx.fetchers, day, tw, tp, ctx.now_iso), True
    af = build_adj(ctx.fetchers, day, old_pf.get("twse"), old_pf.get("tpex"), ctx.now_iso)
    if not all(af["tables_ok"].values()):
        return None
    return None, af, af["factors"] != old_af.get("factors")


def write_repaired(ctx, day, pf, af):
    """Store a repair: adj first, prices last (a day counts as repaired once its prices are complete, so
    a crash in between leaves a day that is simply retried)."""
    iso = day.isoformat()
    ctx.store.write_json(f"adj/{iso}.json", af)
    if pf is not None:
        ctx.store.write_json(f"prices/{iso}.json", pf)


def merge_rankings(ctx, day, new, meta=None):
    """Add `new` candidates to rankings/<day>.json; an existing candidate entry is never replaced."""
    rel = f"rankings/{day.isoformat()}.json"
    doc = ctx.store.read_json(rel)
    if doc is None:
        doc = {"date": day.isoformat(), "computed_at": ctx.now_iso, **(meta or {}), "candidates": {}}
    added = {n: v for n, v in new.items() if n not in doc["candidates"]}
    if added:
        doc["candidates"].update(added)
        ctx.store.write_json(rel, doc)
    return doc


def _late_s2(ctx, state, day, month_key):
    """A5b-2 asks the ledger to record the coverage on the forced day even if the job did not run:
    build the missing s2 doc from the cache with the previous trading day as `today`; "complete" stays
    False (that is what a missing file means), so the decision is unchanged."""
    cal = ctx.cal
    try:
        forced = sr.s2_forced_day(cal, day.year, day.month)
    except ValueError:
        return None
    if day != forced or month_key in state["meta"]["s2"] or state["meta"]["status"] != "active":
        return None
    ratio = sr.revenue_refresh_complete(ctx.ranker.fund(), sr.prev_trading_day(cal, day))[1]
    doc = {"complete": False, "ratio": ratio, "computed_at": ctx.now_iso, "late_s2": True}
    ctx.store.write_json(f"s2/{day.isoformat()}.json", doc)
    ctx.report.alert(f"late_s2: no s2 file for the S2 forced day {day}; coverage {ratio:.1%} computed late")
    return doc


def day_inputs(ctx, state, day):
    """The ``step`` inputs of one trading day, plus the candidates whose ranking was computed late.
    Shared by the normal run, the re-run after a repair and the replay (only the data source differs)."""
    iso = day.isoformat()
    pf, af = ensure_day_files(ctx, day)
    prices = merge_markets(pf["twse"], pf["tpex"]) if _prices_complete(pf) else None
    adj = {c: v["factor"] for c, v in ((af or {}).get("factors") or {}).items()}
    meta = state["meta"]
    bench = benchmark_close_for((pf or {}).get("benchmark"), day, meta["status"], state["benchmark"]["last_close"],
                                last_real_benchmark_date(ctx.store, day))
    s2 = ctx.inputs.read_json(f"s2/{iso}.json")
    if s2 is None and ctx.live:
        # ASSUMPTION: a missing s2 file (the job did not run that day) means "revenue refresh not
        # complete": the coverage of that day cannot be reconstructed without the cache of that day
        # (a later cache would carry later stamps), and the forced day forces the rebalance anyway.
        s2 = _late_s2(ctx, state, day, f"{day.year:04d}-{day.month:02d}")
    s2_status = {"complete": bool(s2["complete"]), "ratio": s2["ratio"]} if s2 else None
    rk_doc = ctx.inputs.read_json(f"rankings/{iso}.json") or {}
    stored = dict(rk_doc.get("candidates") or {})
    if ctx.live:
        due = sp.rebalance_due(state, day, ctx.cal_iso, s2_status)
        missing = [c for c in due if not (stored.get(c) or {}).get("ranked")]
        if missing:
            # The job did not produce today's ranking at 07:00 (or the as-of data was missing then):
            # compute it now, as of the previous trading day only, and mark it.
            new = ctx.ranker.compute(day, missing, late=True, strict=meta["status"] == "not_started")
            if new:
                merge_rankings(ctx, day, new, ctx.ranker.last_meta)
                stored.update(new)
    rankings = {}
    for n, v in stored.items():
        if not v.get("ranked"):
            continue
        if ctx.rederive:
            v, changed = rederive(v, ctx.primary)
            if changed:
                ctx.rederived.append((iso, n))
        rankings[n] = {"asof": v["asof"], "ranked": v["ranked"], "detail": v.get("detail") or []}
    late = sorted(n for n, v in stored.items() if v.get("late_ranking"))
    inputs = {"date": iso, "calendar": ctx.cal_iso, "prices": prices, "adj": adj, "benchmark_close": bench,
              "benchmark_source": ((pf or {}).get("benchmark") or {}).get("source", "yahoo"),
              "rankings": rankings, "s2_status": s2_status, "written_at": ctx.now_iso}
    return inputs, late


def collect_alerts(ctx, rows):
    """Turn the flags of freshly booked rows into operator alerts."""
    for r in rows:
        flags = r.get("flags") or []
        if r["file"] == "daily":
            for f in flags:
                if f == "missing_day":
                    ctx.report.alert(f"missing_day: no whole-market data for {r['date']}")
                elif f.startswith("adj_missing:") or f.startswith("delisted_exit:") or f == "s2_status_missing":
                    ctx.report.alert(f"{f} on {r['date']} ({r['candidate']})")
        elif r["file"] == "rebalances":
            for f in flags:
                if f in ("late_ranking", "rebalance_skipped_all_frozen"):
                    ctx.report.alert(f"{f}: {r['candidate']} nominal {r['nominal_date']}")
        elif r["file"] == "benchmark" and "carry_forward" in flags:
            brs = sp.effective_rows(load_ledger_rows(ctx.store, "benchmark"), "benchmark")
            n = 0
            for b in reversed(brs):
                if "carry_forward" not in (b.get("flags") or []):
                    break
                n += 1
            if n > BENCH_GAP_ALERT:
                ctx.report.alert(f"0050 carry_forward for {n} consecutive days (no Yahoo value)")
            else:
                ctx.report.alert(f"0050 carry_forward on {r['date']}")


def record_day(ctx, state, day, supersede=None):
    """Book one trading day: ``step`` it, append the rows, store the snapshot.  Returns (state, rows).
    `supersede` = ``supersede_index`` of the rows this re-run replaces (None for a first booking)."""
    inputs, late = day_inputs(ctx, state, day)
    new_state, rows = sp.step(state, inputs)
    for r in rows:
        if r["file"] == "rebalances" and r["candidate"] in late and "late_ranking" not in r["flags"]:
            r["flags"].append("late_ranking")
    if supersede is not None:
        rows = sp.mark_superseded(rows, supersede)
    write_rows(ctx.store, rows)
    ctx.store.write_json(f"snapshots/{day.isoformat()}.json", new_state)
    collect_alerts(ctx, rows)
    return new_state, rows


# --------------------------------------------------------------------------- #
# State, resume, repairs
# --------------------------------------------------------------------------- #

def initial_state(lock, cal, primary):
    return sp.new_state(date.fromisoformat(lock["start_date"]), cal, primary)


def load_state(store, lock, cal, primary):
    """(state, last processed ISO day | None).  Resumes from the newest snapshot."""
    days = store.list_dates("snapshots")
    if not days:
        return initial_state(lock, cal, primary), None
    return store.read_json(f"snapshots/{days[-1]}.json"), days[-1]


def check_consistency(store, done, state=None):
    """Every ledger row must belong to a processed (snapshotted) day: rows beyond the newest
    snapshot mean a job died half-way through a day.  A day the state still lists as missing /
    pending retry although its stored prices are complete means a repair was cut off half-way."""
    files = [("daily", n) for n in sp.CANDIDATES] + [("benchmark", None)]
    for file, name in files:
        rows = store.read_rows(BENCH_REL if name is None else _candidate_rel(name, file))
        last = max((r["date"] for r in rows), default=None)
        if last is not None and (done is None or last > done):
            raise ShadowError(f"ledger inconsistent: {file}{'/' + name if name else ''} has rows up to {last} "
                              f"but the newest snapshot is {done}; a previous job died mid-day")
    for name in sp.CANDIDATES:
        rows = store.read_rows(_candidate_rel(name, "rebalances"))
        last = max((r["fill_date"] for r in rows), default=None)
        if last is not None and (done is None or last > done):
            raise ShadowError(f"ledger inconsistent: rebalances/{name} has rows up to {last} "
                              f"but the newest snapshot is {done}")
    if state is not None:
        meta = state["meta"]
        listed = set(meta["missing_days"]) | {p["date"] for p in meta["pending_retry"]}
        for iso in sorted(listed):
            if _prices_complete(store.read_json(f"prices/{iso}.json")):
                raise ShadowError(f"ledger inconsistent: the state still lists {iso} as missing but its prices "
                                  f"are complete (a repair was cut off); the ledger has forked")


def processing_days(cal, lock_start, done, last_closed):
    return [d for d in cal if d >= lock_start and d <= last_closed and (done is None or d.isoformat() > done)]


def state_before(ctx, lock, primary, day):
    """State after the trading day before `day` (restored from its snapshot; the initial state when
    that day lies before the lock start)."""
    prev = sr.prev_trading_day(ctx.cal, day)
    if prev < ctx.lock_start:
        return initial_state(lock, ctx.cal, primary)
    snap = ctx.store.read_json(f"snapshots/{prev.isoformat()}.json")
    if snap is None:
        raise ShadowError(f"snapshot of {prev} missing: cannot re-run from {day}")
    return snap


def do_rerun(ctx, lock, primary, first, through):
    """Re-run every trading day from `first` through `through` (ISO) from the state before `first`,
    from the stored inputs; the new rows supersede the old ones."""
    originals = [r for r in load_ledger_rows(ctx.store, "daily") + load_ledger_rows(ctx.store, "benchmark")
                 if r["date"] >= first.isoformat()]
    originals += [r for r in load_ledger_rows(ctx.store, "rebalances") if r["fill_date"] >= first.isoformat()]
    index = sp.supersede_index(originals)
    state = state_before(ctx, lock, primary, first)
    for d in (x for x in ctx.cal if first <= x and x.isoformat() <= through):
        state, _ = record_day(ctx, state, d, supersede=index)
        ctx.report.reran.append(d.isoformat())
    return state


def resume_rerun(ctx, lock, primary):
    """A job found ``meta/rerun_pending.json``: a repair was cut off.  Roll the ledger files back to
    their sizes before the re-run began, make sure the repaired days' data is complete (fetch again
    what a crash left out) and do the re-run again."""
    marker = ctx.store.read_json(RERUN_REL)
    ctx.report.alert(f"resuming an interrupted repair re-run from {marker['from']}")
    ctx.store.rollback_jsonl(marker["sizes"])
    days = []
    for iso in marker["days"]:
        d = date.fromisoformat(iso)
        if _prices_complete(ctx.inputs.read_json(f"prices/{iso}.json")):
            days.append(d)
            continue
        res = fetch_repair(ctx, d, True)
        if res:
            write_repaired(ctx, d, res[0], res[1])
            days.append(d)
    if days:
        do_rerun(ctx, lock, primary, min(days), marker["through"])
    ctx.store.delete(RERUN_REL)


def repair_candidates(ctx, state, today, done):
    """{iso day: need_prices}: whole-market-missing days inside their retry window (need prices) and
    processed data days whose ex-rights tables failed, inside the same window (adj only)."""
    out = {}
    iso_today = today.isoformat()
    for p in state["meta"]["pending_retry"]:
        if p.get("kind") == "close" and (p.get("until") is None or iso_today <= p["until"]):
            if not _prices_complete(ctx.inputs.read_json(f"prices/{p['date']}.json")):
                out[p["date"]] = True
    if done is not None:
        for d in ctx.cal:
            iso = d.isoformat()
            if iso in out or not (ctx.lock_start <= d and iso <= done):
                continue
            if not 0 < sr.trading_days_between(ctx.cal, d, today) <= ADJ_RETRY_DAYS:
                continue
            af = ctx.inputs.read_json(f"adj/{iso}.json")
            if (af and not all((af.get("tables_ok") or {"x": True}).values())
                    and _prices_complete(ctx.inputs.read_json(f"prices/{iso}.json"))):
                out[iso] = False
    return out


def run_repairs(ctx, state, lock, primary, today, done):
    """A5d-1 / A5e-6.  Returns the (possibly re-run) state."""
    fetched = {}
    for iso, need in sorted(repair_candidates(ctx, state, today, done).items()):
        res = fetch_repair(ctx, date.fromisoformat(iso), need)
        if res:
            fetched[iso] = res
            log(f"repair: {'whole-market data' if need else 'ex-rights tables'} of {iso} fetched")
    if not fetched or done is None:
        return state
    rerun = sorted(i for i, (_, _, rr) in fetched.items() if rr)
    for iso, (pf, af, rr) in fetched.items():
        if not rr:
            write_repaired(ctx, date.fromisoformat(iso), pf, af)       # nothing in the ledger depends on it
    if not rerun:
        return state
    first = date.fromisoformat(rerun[0])
    ctx.store.write_json(RERUN_REL, {"from": rerun[0], "through": done, "days": rerun,
                                     "sizes": ctx.store.jsonl_sizes(), "created_at": ctx.now_iso})
    for iso in rerun:
        write_repaired(ctx, date.fromisoformat(iso), fetched[iso][0], fetched[iso][1])
    state = do_rerun(ctx, lock, primary, first, done)
    ctx.store.delete(RERUN_REL)
    return state


# --------------------------------------------------------------------------- #
# The job
# --------------------------------------------------------------------------- #

def describe(state, report):
    meta = state["meta"]
    log(f"status={meta['status']} last_processed={meta['last_processed']} "
        f"missing_total={meta['missing_total']} pending_retry={len(meta['pending_retry'])}")
    if meta["impaired"]:
        report.alert("impaired (口徑受損): " + ", ".join(meta["impaired_reasons"]))
    if meta["pending_retry"]:
        report.alert("pending_retry: " + ", ".join(p["date"] for p in meta["pending_retry"]))
    if meta["status"] == "ended":
        log(f"判定日 {meta['judgment_date']} 已記帳，影子組合結束（不再記帳）")


def run_daily(store, now, fetchers, universe_file, industries_file, dry_run=False, loader=None, report=None):
    """One 07:00 job.  Returns the exit code."""
    report = report or Report()
    today = now.date()
    lock, universe, primary = check_lock(store, universe_file, industries_file, today, dry_run)
    if not dry_run:
        store.clean_tmp()
    lock_start = date.fromisoformat(lock["start_date"])
    snaps = store.list_dates("snapshots")
    cal = load_calendar(store, fetchers, lock_start, today, processed_through=snaps[-1] if snaps else None,
                        report=report)
    ranker = Ranker(store, cal, lock_start, universe, primary, loader=loader)
    ctx = Ctx(store, store, fetchers, now, cal, lock_start, ranker, live=True, primary=primary, report=report)
    if store.read_json(RERUN_REL) is not None:
        if dry_run:
            raise ShadowError("meta/rerun_pending.json exists: a real run must finish the interrupted repair first")
        resume_rerun(ctx, lock, primary)
    state, done = load_state(store, lock, cal, primary)
    report.state = state
    check_consistency(store, done, state)
    if state["meta"]["status"] == "void":
        return _void_exit(state, report)

    # Repairs also run once the portfolio has "ended": step() ends it on the judgment day even when
    # that day's whole-market data is missing, and A5d-7 gives that day a 10-day retry window.  The
    # re-run restarts from the snapshot before the missing day, so it re-ends the state by itself.
    state = run_repairs(ctx, state, lock, primary, today, done)

    # (1) book every closed, not yet processed trading day
    closed = [d for d in cal if d < today]
    last_closed = closed[-1] if closed else None
    if last_closed is not None and state["meta"]["status"] in ("active", "not_started"):
        snaps = store.list_dates("snapshots")
        done = snaps[-1] if snaps else None
        for d in processing_days(cal, lock_start, done, last_closed):
            if state["meta"]["status"] not in ("active", "not_started"):
                break
            state, _ = record_day(ctx, state, d)
            report.processed.append(d.isoformat())
            log(f"booked {d}")
    report.state = state

    # (2) today's ranking (as of the previous trading day)
    status = state["meta"]["status"]
    if status in ("active", "not_started") and today in set(cal):
        iso = today.isoformat()
        s2 = store.read_json(f"s2/{iso}.json")
        if s2 is None:
            st = ranker.s2_status(today)
            s2 = {"complete": st["complete"], "ratio": st["ratio"], "computed_at": ctx.now_iso}
            store.write_json(f"s2/{iso}.json", s2)
        due = sp.rebalance_due(state, today, ctx.cal_iso, {"complete": s2["complete"], "ratio": s2["ratio"]})
        stored = (store.read_json(f"rankings/{iso}.json") or {}).get("candidates") or {}
        need = [c for c in due if not (stored.get(c) or {}).get("ranked")]
        if need:
            new = ranker.compute(today, need, strict=status == "not_started")
            if new:
                merge_rankings(ctx, today, new, ranker.last_meta)
                log(f"ranking for {today}: {', '.join(sorted(new))}")

    describe(state, report)
    if state["meta"]["status"] == "void":
        return _void_exit(state, report)
    return EXIT_OK


def _void_exit(state, report):
    v = state["meta"]["void"] or {}
    report.alert(f"void (作廢): 連續缺資料觸發日 {v.get('trigger_date')}，新起始日 {v.get('new_start')}；不再記帳")
    return EXIT_VOID


def run_prefetch(store, now, fetchers, universe_file, industries_file, loader=None):
    """Store the whole-market closes (and ex-rights factors) of every closed trading day after the factor
    cache's median last bar, so the first ranking can see data up to its as-of day.  Allowed before the
    start date.  Writes ONLY prices/ and adj/ (a day is written only when both markets arrived; days
    that already have a file are left alone).  No ledger, snapshot, meta or calendar file is touched."""
    today = now.date()
    lock, universe, primary = check_lock(store, universe_file, industries_file, today, False,
                                         allow_before_start=True)
    lock_start = date.fromisoformat(lock["start_date"])
    cal = load_calendar(store, fetchers, lock_start, today, persist=False)
    ranker = Ranker(store, cal, lock_start, universe, primary, loader=loader)
    ctx = Ctx(store, store, fetchers, now, cal, lock_start, ranker, live=True, primary=primary)
    end = ranker.cache_end_median()
    fetched = 0
    for d in cal:
        iso = d.isoformat()
        if end is None or not (end < iso and d < today):
            continue
        if store.exists(f"prices/{iso}.json"):
            continue
        pf, tw, tp = fetch_day_prices(ctx, d)
        if not _prices_complete(pf):
            log(f"prefetch: whole-market data of {iso} not available")
            continue
        write_repaired(ctx, d, pf, build_adj(fetchers, d, tw, tp, ctx.now_iso))
        fetched += 1
        log(f"prefetch: stored {iso}")
    log(f"prefetch done: {fetched} day(s) stored (cache median end {end})")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Replay (A5c-8, A5d-2)
# --------------------------------------------------------------------------- #

def nav_series(store):
    """{series name: {date: value}} of the effective daily nav of each candidate and the 0050 close."""
    out = {}
    for name in sp.CANDIDATES:
        rows = sp.effective_rows(load_ledger_rows(store, "daily", name), "daily", name)
        out[name] = {r["date"]: r["nav"] for r in rows}
    out["benchmark_0050"] = {r["date"]: r["adj_close"]
                             for r in sp.effective_rows(load_ledger_rows(store, "benchmark"), "benchmark")}
    return out


def first_difference(old, new):
    """First (date, series, old value, new value) where the two nav series differ, or None."""
    diffs = []
    for series in old:
        a, b = old[series], new.get(series, {})
        for d in sorted(set(a) | set(b)):
            if a.get(d) != b.get(d):
                diffs.append((d, series, a.get(d), b.get(d)))
    return min(diffs, key=lambda x: (x[0], x[1])) if diffs else None


def run_replay(src, out_dir, now, universe_file, industries_file, dry_run=False):
    """Recompute the whole ledger from the lock start with the current code, from the stored inputs
    only (prices / adj / rankings / s2 / raw 0050 / calendar).  Every stored ranking is rebuilt from its
    stored per-factor percentiles and weights (A5d-2: a bug in the weighting / sorting is repaired by a
    replay); where the rebuilt order differs from the stored one the rebuilt one is used and the day is
    listed.  Writes to `out_dir` (must not hold anything) and compares the nav series with the original.
    Returns the exit code (4 when the nav series differ)."""
    if os.path.isdir(out_dir) and os.listdir(out_dir):
        raise ShadowError(f"--out {out_dir} exists and is not empty: refusing to overwrite")
    lock, universe, primary = check_lock(src, universe_file, industries_file, now.date(), dry_run)
    lock_start = date.fromisoformat(lock["start_date"])
    cal = load_calendar(src, None, lock_start, now.date(), allow_fetch=False)
    done_days = src.list_dates("snapshots")
    if not done_days:
        raise ShadowError("nothing to replay: the ledger has no snapshots")
    last = done_days[-1]
    out = Store(out_dir, dry_run=dry_run)
    ctx = Ctx(out, src, None, now, cal, lock_start, None, live=False, primary=primary, rederive=True)
    state = initial_state(lock, cal, primary)
    for d in processing_days(cal, lock_start, None, date.fromisoformat(last)):
        state, _ = record_day(ctx, state, d)
    if not dry_run:
        for sub in ("prices", "adj", "rankings", "s2"):
            if os.path.isdir(src.path(sub)):
                shutil.copytree(src.path(sub), out.path(sub), dirs_exist_ok=True)
        for rel in ("meta/lock.json", "meta/calendar.json"):
            if os.path.isfile(src.path(rel)):
                os.makedirs(os.path.dirname(out.path(rel)), exist_ok=True)
                shutil.copyfile(src.path(rel), out.path(rel))
    if ctx.rederived:
        log("replay: the rankings rebuilt from the stored percentiles differ from the stored ones on: "
            + ", ".join(f"{d} {n}" for d, n in ctx.rederived) + " (the rebuilt ones were used)")
    diff = first_difference(nav_series(src), nav_series(out))
    if diff is None:
        log(f"replay: nav series identical day by day (through {last})")
        return EXIT_OK
    log(f"replay: nav series differ; first difference {diff[0]} {diff[1]}: original {diff[2]} vs replay {diff[3]}")
    return EXIT_REPLAY


# --------------------------------------------------------------------------- #
# Weekly summary text (sent by the workflow, not here)
# --------------------------------------------------------------------------- #

def _curve(rets, start=1_000_000.0):
    eq = [start]
    for r in rets:
        eq.append(eq[-1] * (1.0 + r))
    return eq


def build_summary(state):
    """Plain-text weekly summary from the state alone: cumulative return, Sharpe, MDD per candidate
    and for 0050.  Header carries the "not a signal" notice."""
    meta = state["meta"]
    lines = ["影子組合・未驗證・不是買賣訊號"]
    if meta["status"] == "not_started" or not state["benchmark"]["rets"]:
        lines.append("尚未起算（起始日 " + str(meta["start_date"]) + "）")
        return "\n".join(lines)
    lines.append(f"截至 {meta['last_processed']}（狀態 {meta['status']}"
                 + ("、口徑受損" if meta["impaired"] else "") + "）")

    def fmt(x, spec):
        return "n/a" if x is None else format(x, spec)

    for name in sp.CANDIDATES:
        cs = state["candidates"][name]
        lines.append(f"{name}: 累計 {fmt((cs['nav'] / sp.INITIAL_CAPITAL - 1) * 100, '+.2f')}%  "
                     f"Sharpe {fmt(metrics.sharpe_ratio(cs['rets']), '.2f')}  "
                     f"MDD {metrics.max_drawdown_pct(_curve(cs['rets'])):.2f}%")
    b = state["benchmark"]
    lines.append(f"0050: 累計 {fmt((b['last_close'] / b['start_close'] - 1) * 100, '+.2f')}%  "
                 f"Sharpe {fmt(metrics.sharpe_ratio(b['rets']), '.2f')}  "
                 f"MDD {metrics.max_drawdown_pct(_curve(b['rets'])):.2f}%")
    return "\n".join(lines)


def run_summary(store):
    days = store.list_dates("snapshots")
    if not days:
        print("影子組合・未驗證・不是買賣訊號\n尚無記帳資料")
        return EXIT_OK
    print(build_summary(store.read_json(f"snapshots/{days[-1]}.json")))
    return EXIT_OK


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def write_last_run(store, now, rc, report):
    """meta/last_run.json (overwrite style): what the workflow needs to decide on alerts."""
    if store.dry_run or not os.path.isdir(store.root):
        return
    meta = (report.state or {}).get("meta") or {}
    payload = {"run_at": now.isoformat(timespec="seconds"), "exit_code": rc,
               "processed_days": report.processed, "reran_days": report.reran, "alerts": report.alerts,
               "missing_total": meta.get("missing_total"), "impaired": meta.get("impaired"),
               "void": meta.get("void"), "pending_retry": meta.get("pending_retry")}
    try:
        store.write_json("meta/last_run.json", payload, indent=1)
    except OSError as e:
        log(f"warning: could not write meta/last_run.json: {e}")


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ledger-dir", default=DEFAULT_LEDGER_DIR, help="local ledger directory (default shadow_ledger/)")
    ap.add_argument("--dry-run", action="store_true", help="compute but write no file")
    ap.add_argument("--now", default=None, help="Taipei-time ISO timestamp to use instead of the clock")
    ap.add_argument("--universe-file", default=DEFAULT_UNIVERSE_FILE)
    ap.add_argument("--industries-file", default=DEFAULT_INDUSTRIES_FILE)
    ap.add_argument("--replay", action="store_true", help="recompute from the stored inputs into --out")
    ap.add_argument("--out", default=None, help="output directory of --replay (must be new or empty)")
    ap.add_argument("--summary", action="store_true", help="print the weekly summary text only")
    ap.add_argument("--prefetch", action="store_true",
                    help="store whole-market closes since the factor cache's end (prices/ and adj/ only; "
                         "allowed before the start date)")
    return ap


def main(argv=None, fetchers=None, loader=None):
    _utf8_stdio()
    args = build_parser().parse_args(argv)
    report = Report()
    store, now, daily = None, None, False
    try:
        now = parse_now(args.now)
        store = Store(args.ledger_dir, dry_run=args.dry_run)
        if args.summary:
            return run_summary(store)
        if args.replay:
            if not args.out:
                raise ShadowError("--replay needs --out <new directory>")
            return run_replay(store, args.out, now, args.universe_file, args.industries_file, args.dry_run)
        if args.prefetch:
            return run_prefetch(store, now, fetchers or Fetchers(), args.universe_file, args.industries_file,
                                loader=loader)
        daily = True
        rc = run_daily(store, now, fetchers or Fetchers(), args.universe_file, args.industries_file,
                       dry_run=args.dry_run, loader=loader, report=report)
    except ShadowError as e:
        log(f"ERROR: {e}")
        report.alert(f"error (exit {e.code}): {e}")
        rc = e.code
    except ValueError as e:        # e.g. ``step`` refusing inputs (look-ahead ranking, calendar gap)
        log(f"ERROR: {e}")
        report.alert(f"error (exit {EXIT_ERROR}): {e}")
        rc = EXIT_ERROR
    if daily and rc != EXIT_LOCK:           # a lock failure is reported by the exit code alone
        write_last_run(store, now, rc, report)
    return rc


if __name__ == "__main__":
    sys.exit(main())
