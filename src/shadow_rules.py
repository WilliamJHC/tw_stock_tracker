# -*- coding: utf-8 -*-
"""Pure rule functions for the forward shadow portfolio (spec:
docs/shadow_portfolio_spec_2026-11.md; clause numbers such as A5b-9 refer to it).

No I/O, no network, standard library only.  Everything here is a deterministic
function of its arguments so the ledger state machine can be replayed.

Conventions
  - "calendar" is a set (or any iterable) of datetime.date trading days.
    ASSUMPTION: the caller supplies a calendar that covers every month the call
    needs, including the neighbouring month for "previous trading day" lookups.
    A missing month raises ValueError, but a calendar truncated mid-month gives
    a wrong "first trading day" and this module cannot detect that.
  - Money is in TWD (yuan) as float.
  - Strings are compared by Unicode code point (Python str ordering).

Lines marked "ASSUMPTION:" are places where the spec is silent or ambiguous and
one reading had to be chosen; they are listed in the hand-off report.
"""
import math
from bisect import bisect_left, bisect_right
from collections.abc import Mapping
from datetime import date, datetime

# --------------------------------------------------------------------------- #
# Constants fixed by the spec
# --------------------------------------------------------------------------- #

LOCK_CUTOFF = date(2026, 11, 30)   # A5b-9: lock on/before this day -> start 2026-12
FIRST_START_YEAR, FIRST_START_MONTH = 2026, 12
S2_EARLIEST_DAY = 12               # A1: S2 rebalances on the first trading day with day-of-month >= 12
S2_FORCED_DAY = 20                 # A5b-2 / A5c-4: forced rebalance day
REVENUE_DEADLINE_DAY = 10          # same value as scripts/update_fundamentals.py
REVENUE_ATTEMPT_DAY = 11           # A5d-4 (b): attempt on/after the 11th of the month counts as done
REVENUE_COMPLETE_RATIO = 0.90      # A5b-2 / A5d-4

# A5c-6: asymmetric leg costs (fractions of traded value)
BUY_FEE = 0.001425 * 0.22
SELL_FEE = 0.001425 * 0.22 + 0.003
SLIPPAGE = 0.001

# A5b-5: alias merge (variant -> canonical).  The canonical name is the first
# spelling listed in the spec; the spec itself confirms this for "其他電子業".
# ASSUMPTION: the spec does not say which spelling is the canonical one for the
# other seven pairs.  The first-listed spelling is used, because the canonical
# name is the tie-break key and the returned label.
ALIASES = {
    "數位雲端類": "數位雲端",
    "電子通路類": "電子通路業",
    "其他電子類": "其他電子業",
    "居家生活類": "居家生活",
    "綠能環保類": "綠能環保",
    "運動休閒類": "運動休閒",
    "觀光餐旅": "觀光事業",
    "金融保險": "金融業",
}

# A5b-5: labels that are not industries; treated as if absent.
NON_INDUSTRY_LABELS = frozenset({"創新板股票", "創新版股票", "存託憑證", "ETF"})

OTHER = "其他"  # exact-string match only; "其他電子業" is a separate category

# Tolerance (yuan) below which a rebalance difference is treated as "no trade".
# ASSUMPTION: the spec has no tolerance; this only absorbs float noise so that a
# position already at target is not charged a phantom trade.
_EPS = 1e-6


# --------------------------------------------------------------------------- #
# Calendar helpers
# --------------------------------------------------------------------------- #

def _sorted_days(calendar):
    return sorted(set(calendar))


def _add_months(year, month, k):
    idx = year * 12 + (month - 1) + k
    return idx // 12, idx % 12 + 1


def first_trading_day(calendar, year, month):
    """First trading day of the given month."""
    return first_trading_day_on_or_after(calendar, year, month, 1)


def first_trading_day_on_or_after(calendar, year, month, day=S2_EARLIEST_DAY):
    """First trading day of the month whose day-of-month is >= `day` (A1: S2 uses 12).
    Raises ValueError when the calendar has no such day inside that month."""
    days = _sorted_days(calendar)
    i = bisect_left(days, date(year, month, day))
    if i == len(days) or (days[i].year, days[i].month) != (year, month):
        raise ValueError(f"no trading day on/after {year}-{month:02d}-{day:02d} in calendar")
    return days[i]


def next_trading_day(calendar, d):
    """First trading day strictly after `d` (`d` itself need not be a trading day)."""
    days = _sorted_days(calendar)
    i = bisect_right(days, d)
    if i == len(days):
        raise ValueError(f"no trading day after {d} in calendar")
    return days[i]


def prev_trading_day(calendar, d):
    """Last trading day strictly before `d` (`d` itself need not be a trading day)."""
    days = _sorted_days(calendar)
    i = bisect_left(days, d)
    if i == 0:
        raise ValueError(f"no trading day before {d} in calendar")
    return days[i - 1]


def trading_days_between(calendar, a, b):
    """Number of trading days d with a < d <= b (a excluded, b included); 0 if b <= a.
    ASSUMPTION: the spec counts delays in trading days without defining the
    endpoints.  With (a, b], "the 3rd trading day after T" is the day d with
    trading_days_between(T, d) == 3, and a delay of one day means b is the day
    right after a."""
    if b <= a:
        return 0
    days = _sorted_days(calendar)
    return bisect_right(days, b) - bisect_right(days, a)


def _first_trading_day_of_next_month(calendar, d):
    y, m = _add_months(d.year, d.month, 1)
    return first_trading_day(calendar, y, m)


def start_date(lock_date, calendar):
    """A5b-9 / A3.  Lock completed on or before 2026-11-30 -> first trading day of
    2026-12.  Otherwise the first trading day of the month after the lock date's
    month (lock on 2026-12-01 -> first trading day of 2027-01)."""
    if lock_date <= LOCK_CUTOFF:
        # ASSUMPTION: any lock date <= 2026-11-30 (even one in an earlier month)
        # starts on the 2026-12 first trading day, as the spec's wording is
        # "lock <= 2026-11-30 -> 2026-12".
        return first_trading_day(calendar, FIRST_START_YEAR, FIRST_START_MONTH)
    return _first_trading_day_of_next_month(calendar, lock_date)


def judgment_date(start, calendar):
    """A3.  Add 12 months to the start date's month; take the first trading day of
    that month; the judgment date is the trading day before it."""
    y, m = _add_months(start.year, start.month, 12)
    return prev_trading_day(calendar, first_trading_day(calendar, y, m))


def restart_date(trigger_date, calendar):
    """A5b-8 / A5d-8.  First trading day of the month after the month of the
    voiding trigger date (also used for a rule-change restart)."""
    return _first_trading_day_of_next_month(calendar, trigger_date)


# --------------------------------------------------------------------------- #
# Ranking (A5b-11)
# --------------------------------------------------------------------------- #

def rank_top(scores, n):
    """Top-n codes by (-score, code): higher score first, ties by code ascending.
    Independent of dict insertion order.
    ASSUMPTION: scores that are None or NaN cannot be ordered and are dropped."""
    if n <= 0:
        return []
    valid = [(code, s) for code, s in scores.items()
             if s is not None and not (isinstance(s, float) and math.isnan(s))]
    valid.sort(key=lambda cs: (-cs[1], cs[0]))
    return [code for code, _ in valid[:n]]


# --------------------------------------------------------------------------- #
# Primary industry (A1, A5b-5, A5c-7)
# --------------------------------------------------------------------------- #

def _merged_categories(raw):
    """Alias-merged, non-industry-label-free, "其他"-free category set of one code."""
    if raw is None:
        return set()
    # ASSUMPTION: a bare string instead of a list is one label (a bare string would
    # otherwise be iterated per character).
    if isinstance(raw, str):
        raw = [raw]
    out = set()
    for label in raw:
        # ASSUMPTION: empty / None labels are ignored.
        if not label:
            continue
        label = ALIASES.get(label, label)
        if label in NON_INDUSTRY_LABELS or label == OTHER:
            continue
        out.add(label)
    return out


def primary_industry(industries, universe):
    """One primary industry per universe code.

    industries: {code: [category, ...]} (a code may carry several categories).
    universe:   set of codes; only these are counted and returned.

    Steps: merge aliases -> drop non-industry labels -> drop "其他" (exact match)
    -> member count of a category = number of universe codes carrying it after
    the merge (union, not a sum over spellings) -> take the category with the
    fewest members, ties by smallest name (Unicode code point).  A code with no
    candidate left (only non-industry labels / "其他" / no entry) gets "其他"."""
    # ASSUMPTION: only universe codes are returned (codes that appear in
    # `industries` but not in `universe` are neither counted nor returned), and a
    # universe code with no entry in `industries` is treated as having no category.
    merged = {code: _merged_categories(industries.get(code)) for code in universe}
    counts = {}
    for cats in merged.values():
        for c in cats:
            counts[c] = counts.get(c, 0) + 1
    result = {}
    for code, cats in merged.items():
        result[code] = min(cats, key=lambda c: (counts[c], c)) if cats else OTHER
    return result


# --------------------------------------------------------------------------- #
# Capped selection (A1 S3, A5d-5, A5c-5)
# --------------------------------------------------------------------------- #

def _industry_of(primary, code):
    # ASSUMPTION: a code missing from the primary-industry map is treated as "其他".
    return primary.get(code, OTHER)


def select_with_cap(ranked, primary, cap=4, n=20, frozen=frozenset()):
    """Walk `ranked` (best first) and keep a code unless its primary industry is
    already at `cap`; skipped codes are replaced by the next ones down the list.

    Frozen holdings (A5d-5) occupy one of the n slots each and count toward their
    industry's cap before anything else is picked; they are never returned.
    Returns the new non-frozen targets, at most n - len(frozen) of them.
    ASSUMPTION: when `ranked` (or the cap) leaves fewer candidates than slots, the
    shorter list is returned as is; the caller then equal-weights the fewer names
    (see rebalance_orders) instead of leaving cash for the missing slots.
    `ranked` order is taken as given (use rank_top first)."""
    slots = n - len(frozen)
    if slots <= 0:
        return []
    counts = {}
    for code in frozen:
        ind = _industry_of(primary, code)
        counts[ind] = counts.get(ind, 0) + 1
    picked, seen = [], set()
    for code in ranked:
        if code in frozen or code in seen:
            continue
        seen.add(code)
        ind = _industry_of(primary, code)
        if counts.get(ind, 0) >= cap:
            continue
        counts[ind] = counts.get(ind, 0) + 1
        picked.append(code)
        if len(picked) == slots:
            break
    return picked


def select_uncapped(ranked, n=20, frozen=frozenset()):
    """Uncapped candidates (S0-S2) with frozen holdings (A5c-5): k frozen codes
    take k slots, the rest of the target is the first n - k non-frozen codes of
    `ranked`.
    ASSUMPTION: this function is not part of the pinned interface; it is added
    because A5c-5 (frozen holdings take slots) applies to every candidate, not
    only the capped one.  As in select_with_cap, a short `ranked` yields fewer names."""
    slots = n - len(frozen)
    if slots <= 0:
        return []
    picked, seen = [], set()
    for code in ranked:
        if code in frozen or code in seen:
            continue
        seen.add(code)
        picked.append(code)
        if len(picked) == slots:
            break
    return picked


# --------------------------------------------------------------------------- #
# Costs (A5c-6) and rebalance orders (A5b-10)
# --------------------------------------------------------------------------- #

def buy_cost(value):
    """Cost (yuan) of buying `value` yuan: buy fee + one-way slippage."""
    if value < 0:
        raise ValueError("value must be >= 0")
    return value * (BUY_FEE + SLIPPAGE)


def sell_cost(value):
    """Cost (yuan) of selling `value` yuan: fee + securities transaction tax + one-way slippage."""
    if value < 0:
        raise ValueError("value must be >= 0")
    return value * (SELL_FEE + SLIPPAGE)


def trade_cost(buys_value, sells_value):
    """Total cost of one rebalance given summed buy and sell values."""
    return buy_cost(buys_value) + sell_cost(sells_value)


def rebalance_orders(current, target_codes, total_nav, frozen_values={}):
    """Equal-weight rebalance of everything not frozen (A5b-10).

    current:       {code: market value} of held positions (frozen codes may be
                   included; they are ignored).
    target_codes:  codes to hold after the rebalance, equal weight.
    total_nav:     NAV before the rebalance (cash + all holdings incl. frozen);
                   the target value per code is (total_nav - sum(frozen)) / len(targets),
                   with no pre-deduction of trading costs.
    frozen_values: {code: value} of frozen positions: never bought or sold.

    Returns {"buys": {code: value}, "sells": {code: value}, "cost": float}; cost
    is charged only on the actual differences (a position already at target
    costs nothing) and is paid from cash by the caller.
    ASSUMPTION: because targets are sized on pre-cost NAV, a fully invested book
    ends with cash of about -cost after the trades (cash + holdings = NAV - cost
    holds exactly).  This function does not clamp; the ledger decides how to book it.
    ASSUMPTION: if fewer than n targets are given (short ranking, cap exhausted),
    the investable amount is equal-weighted over the fewer names, not reserved as cash.
    ASSUMPTION: invalid input raises ValueError (duplicate targets, a frozen code
    among the targets, frozen value above total_nav); an empty target list sells
    every non-frozen holding and buys nothing."""
    targets = list(target_codes)
    if len(set(targets)) != len(targets):
        raise ValueError("target_codes contains duplicates")
    clash = set(targets) & set(frozen_values)
    if clash:
        raise ValueError(f"frozen codes cannot be targets: {sorted(clash)}")
    investable = total_nav - sum(frozen_values.values())
    if investable < 0:
        raise ValueError("frozen holdings exceed total_nav")

    movable = {c: v for c, v in current.items() if c not in frozen_values and v > 0}
    each = investable / len(targets) if targets else 0.0
    buys, sells = {}, {}
    for code in sorted(set(movable) | set(targets)):
        diff = (each if code in targets else 0.0) - movable.get(code, 0.0)
        if diff > _EPS:
            buys[code] = diff
        elif diff < -_EPS:
            sells[code] = -diff
    return {"buys": buys, "sells": sells,
            "cost": trade_cost(sum(buys.values()), sum(sells.values()))}


# --------------------------------------------------------------------------- #
# S2 revenue-completion check and rebalance day (A5d-4, A5c-4, A5b-2)
# --------------------------------------------------------------------------- #

def revenue_target(today):
    """First day ("YYYY-MM-01") of the newest revenue month that should be
    disclosed by `today`: previous month once the 10th has passed, else the month
    before that.  Re-implementation of scripts/update_fundamentals.revenue_target."""
    months_back = 1 if today.day > REVENUE_DEADLINE_DAY else 2
    y, m = _add_months(today.year, today.month, -months_back)
    return f"{y:04d}-{m:02d}-01"


def _attempt_date(value):
    """Date part of a revenue_attempt stamp: datetime, date, "YYYY-MM-DD" or an ISO
    string with a time ("YYYY-MM-DDTHH:MM:SS...", date part taken as written, no
    timezone conversion).  None if it cannot be parsed."""
    if isinstance(value, datetime):       # check first: datetime is a subclass of date
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        # ASSUMPTION: an unparseable attempt stamp does not count as done.
        return None


def revenue_refresh_complete(fund_refresh_by_code, today):
    """A5d-4.  Returns (ratio >= 90%, ratio).

    fund_refresh_by_code: {code: fund_refresh dict} for the universe codes that are
    currently not skipped (an empty dict is allowed and counts as not done).
    The target month is revenue_target(today).
    A code is done when fr["revenue"] == that target (an "insufficient" outcome is
    also stamped, so it counts) or fr["revenue_attempt"] is on/after the 11th of
    today's month.  Failed codes (no stamp) stay in the denominator."""
    target = revenue_target(today)
    total = len(fund_refresh_by_code)
    # ASSUMPTION: an empty universe returns (False, 0.0); the spec defines no such case.
    if total == 0:
        return False, 0.0
    threshold = date(today.year, today.month, REVENUE_ATTEMPT_DAY)
    done = 0
    for fr in fund_refresh_by_code.values():
        fr = fr or {}
        if fr.get("revenue") == target:
            done += 1
            continue
        attempt = fr.get("revenue_attempt")
        if attempt is not None:
            d = _attempt_date(attempt)
            if d is not None and d >= threshold:
                done += 1
    # ASSUMPTION: integer comparison (done*100 >= total*90) so that exactly 90% is
    # complete without float edge cases; the spec only says ">= 90%".
    return done * 100 >= total * round(REVENUE_COMPLETE_RATIO * 100), done / total


def s2_forced_day(calendar, year, month):
    """Forced S2 rebalance day: the 20th, or the next trading day if the 20th is
    not one (A5c-4).  ASSUMPTION: the search is not confined to the month (it
    could only spill over if the rest of the month were all closed)."""
    days = _sorted_days(calendar)
    i = bisect_left(days, date(year, month, S2_FORCED_DAY))
    if i == len(days):
        raise ValueError(f"no trading day on/after {year}-{month:02d}-{S2_FORCED_DAY:02d} in calendar")
    return days[i]


def s2_rebalance_day(calendar, year, month, complete_by_day):
    """Actual S2 rebalance day for the month, or None if not yet decidable.

    From the first trading day with day-of-month >= 12, the first trading day d
    with complete_by_day[d] truthy; the forced day (s2_forced_day) is used when
    none was complete before it, whatever its own value.
    complete_by_day: dict {date: bool} (may be filled partially by a day-by-day
    caller) or a callable date -> bool.  A day that is missing from the dict (or
    for which the callable returns None) is undecided, so the result is None
    as long as no earlier day has already settled it.
    ASSUMPTION (undecided days): a day missing from the dict / None from the
    callable means "not known yet", so no later day may settle the result.
    ASSUMPTION (forced day): on the forced day the function returns it without
    consulting complete_by_day (it is forced whatever the coverage); recording the
    actual coverage on that day (A5b-2) is the ledger layer's separate call to
    revenue_refresh_complete."""
    days = _sorted_days(calendar)
    start = first_trading_day_on_or_after(days, year, month, S2_EARLIEST_DAY)
    forced = s2_forced_day(days, year, month)
    lookup = complete_by_day.get if isinstance(complete_by_day, Mapping) else complete_by_day
    for d in days[bisect_left(days, start):]:
        if d >= forced:
            return forced
        verdict = lookup(d)
        if verdict is None:
            return None
        if verdict:
            return d
    return forced
