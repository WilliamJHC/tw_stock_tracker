# -*- coding: utf-8 -*-
"""Tests for src/shadow_rules.py.  Synthetic calendars and data only; no network.
Every group maps to a boundary that the spec (A1/A3/A5b/A5c/A5d) argued about."""
import random
from datetime import date, datetime, timedelta

import pytest

import shadow_rules as sr


def make_calendar(start, end, holidays=()):
    """Weekdays between start and end (inclusive) minus `holidays`."""
    hol = set(holidays)
    out, d = set(), start
    while d <= end:
        if d.weekday() < 5 and d not in hol:
            out.add(d)
        d += timedelta(days=1)
    return out


def span(a, b):
    return [a + timedelta(days=i) for i in range((b - a).days + 1)]


# 2027-01-01 closed; synthetic "lunar new year" closure 2027-02-01..2027-02-14.
CAL = make_calendar(date(2026, 11, 1), date(2028, 2, 29),
                    holidays=[date(2027, 1, 1)] + span(date(2027, 2, 1), date(2027, 2, 14)))


# --------------------------------------------------------------------------- #
# Calendar helpers
# --------------------------------------------------------------------------- #

def test_first_trading_day_simple_and_not_the_first_of_month():
    assert sr.first_trading_day(CAL, 2026, 12) == date(2026, 12, 1)       # Tue
    assert sr.first_trading_day(CAL, 2027, 5) == date(2027, 5, 3)         # 5/1 is Saturday
    assert sr.first_trading_day(CAL, 2027, 1) == date(2027, 1, 4)         # 1/1 closed (Fri), 1/2-3 weekend


def test_first_trading_day_lunar_new_year_month():
    assert sr.first_trading_day(CAL, 2027, 2) == date(2027, 2, 15)


def test_first_trading_day_missing_month_raises():
    with pytest.raises(ValueError):
        sr.first_trading_day(CAL, 2030, 1)


def test_s2_first_day_on_or_after_12_weekday():
    assert sr.first_trading_day_on_or_after(CAL, 2027, 8, 12) == date(2027, 8, 12)   # Thu


def test_s2_first_day_on_or_after_12_saturday():
    assert sr.first_trading_day_on_or_after(CAL, 2026, 12) == date(2026, 12, 14)     # 12/12 Sat -> Mon
    assert sr.first_trading_day_on_or_after(CAL, 2027, 6, 12) == date(2027, 6, 14)


def test_s2_first_day_on_or_after_12_inside_closure():
    assert sr.first_trading_day_on_or_after(CAL, 2027, 2) == date(2027, 2, 15)


def test_next_and_prev_trading_day():
    assert sr.next_trading_day(CAL, date(2026, 12, 4)) == date(2026, 12, 7)          # over a weekend
    assert sr.next_trading_day(CAL, date(2026, 12, 5)) == date(2026, 12, 7)          # from a non-trading day
    assert sr.prev_trading_day(CAL, date(2026, 12, 7)) == date(2026, 12, 4)
    assert sr.prev_trading_day(CAL, date(2027, 1, 4)) == date(2026, 12, 31)          # over the 1/1 closure
    assert sr.prev_trading_day(CAL, date(2027, 2, 15)) == date(2027, 1, 29)          # over the closure
    with pytest.raises(ValueError):
        sr.next_trading_day(CAL, date(2028, 2, 29))
    with pytest.raises(ValueError):
        sr.prev_trading_day(CAL, date(2026, 11, 2))


def test_trading_days_between_excludes_start_includes_end():
    assert sr.trading_days_between(CAL, date(2026, 12, 1), date(2026, 12, 1)) == 0
    assert sr.trading_days_between(CAL, date(2026, 12, 1), date(2026, 12, 2)) == 1
    assert sr.trading_days_between(CAL, date(2026, 12, 4), date(2026, 12, 7)) == 1   # Fri -> Mon
    assert sr.trading_days_between(CAL, date(2026, 12, 1), date(2026, 12, 8)) == 5
    assert sr.trading_days_between(CAL, date(2026, 12, 8), date(2026, 12, 1)) == 0
    # a non-trading start/end day is fine
    assert sr.trading_days_between(CAL, date(2026, 12, 5), date(2026, 12, 6)) == 0


def test_trading_days_between_third_trading_day_after_T():
    # A5d-1: "T 之後第 3 個交易日" is the day d with trading_days_between(T, d) == 3.
    T = date(2026, 12, 17)                                   # Thu
    third = sr.next_trading_day(CAL, sr.next_trading_day(CAL, sr.next_trading_day(CAL, T)))
    assert third == date(2026, 12, 22)                       # Fri 18, Mon 21, Tue 22
    assert sr.trading_days_between(CAL, T, third) == 3
    assert sr.trading_days_between(CAL, T, date(2026, 12, 21)) == 2
    assert sr.trading_days_between(CAL, T, date(2026, 12, 23)) == 4


def test_trading_days_between_single_deferral_over_5_days():
    # A5 / A5b-8: "單次換倉順延超過 5 個交易日" means
    #   trading_days_between(nominal rebalance day, executed day) > 5.
    nominal = date(2026, 12, 14)                             # Mon
    assert sr.trading_days_between(CAL, nominal, date(2026, 12, 21)) == 5   # 5th trading day later: not > 5
    assert not sr.trading_days_between(CAL, nominal, date(2026, 12, 21)) > 5
    assert sr.trading_days_between(CAL, nominal, date(2026, 12, 22)) == 6   # 6th: exceeds the limit
    assert sr.trading_days_between(CAL, nominal, date(2026, 12, 22)) > 5
    # weekends and holidays in between do not count (12/31 Thu -> 2027-01-04 Mon, 1/1 closed)
    assert sr.trading_days_between(CAL, date(2026, 12, 31), date(2027, 1, 4)) == 1


def test_calendar_accepts_list_and_is_order_independent():
    shuffled = list(CAL)
    random.Random(1).shuffle(shuffled)
    assert sr.first_trading_day(shuffled, 2027, 2) == date(2027, 2, 15)


# --------------------------------------------------------------------------- #
# start_date / judgment_date / restart_date
# --------------------------------------------------------------------------- #

def test_judgment_date_from_2026_12_01():
    assert sr.judgment_date(date(2026, 12, 1), CAL) == date(2027, 11, 30)


def test_judgment_date_from_2027_01_04_is_last_trading_day_of_2027_12():
    # 2028-01-01 is a Saturday, first trading day of 2028-01 is 01-03, previous is 2027-12-31.
    assert sr.judgment_date(date(2027, 1, 4), CAL) == date(2027, 12, 31)


def test_judgment_date_last_trading_day_when_year_end_closed():
    cal = CAL - {date(2027, 12, 31)}
    assert sr.judgment_date(date(2027, 1, 4), cal) == date(2027, 12, 30)


def test_judgment_date_is_never_a_first_day_or_s2_day():
    # Judgment date is the trading day before a month's first trading day, so it is
    # neither the first trading day of its month nor on/after the 12th.
    for start in (date(2026, 12, 1), date(2027, 1, 4), date(2027, 2, 15)):
        j = sr.judgment_date(start, CAL)
        assert j != sr.first_trading_day(CAL, j.year, j.month)
        assert j.day >= 12


def _realistic_calendar(seed):
    """Weekdays 2026-11..2029-03 minus ~4% random weekday closures, plus a 13-day
    new-year closure starting in the first 8 days of a random Jan/Feb of each year
    (shape of the real TWSE table: the long closure never reaches past mid-month).
    Edge case outside this shape: if every trading day from the 12th (or 20th) to
    month end were closed except the last one, that last day would be both the
    judgment date and S2's day, so A3's guarantee has this (unrealistic) hole."""
    rng = random.Random(seed)
    hol = set()
    d = date(2026, 11, 1)
    while d <= date(2029, 3, 31):
        if d.weekday() < 5 and rng.random() < 0.04:
            hol.add(d)
        d += timedelta(days=1)
    for y in (2027, 2028, 2029):
        first = date(y, rng.choice([1, 2]), rng.randint(1, 8))
        hol.update(span(first, first + timedelta(days=12)))
    return make_calendar(date(2026, 11, 1), date(2029, 3, 31), holidays=hol)


@pytest.mark.parametrize("cal", [CAL | make_calendar(date(2028, 3, 1), date(2029, 3, 31))] + [_realistic_calendar(k) for k in range(40)])
def test_a3_judgment_date_is_never_any_candidates_rebalance_day(cal):
    """A3 guarantee: the judgment date can never be a rebalance day of S0/S1/S3 (first
    trading day of a month) nor of S2 (first trading day with day >= 12, or the forced 20th)."""
    starts = [sr.first_trading_day(cal, y, m) for (y, m) in
              [(2026, 12)] + [(2027, k) for k in range(1, 12)]]
    for start in starts:
        j = sr.judgment_date(start, cal)
        target_first = sr.first_trading_day(cal, start.year + 1, start.month)
        # independent construction: the trading day immediately before the target month's first one
        assert j in cal and j < target_first
        assert sr.next_trading_day(cal, j) == target_first
        # and it is not a rebalance day of the month it lies in
        assert j != sr.first_trading_day(cal, j.year, j.month)
        assert j != sr.first_trading_day_on_or_after(cal, j.year, j.month, 12)
        assert j != sr.s2_forced_day(cal, j.year, j.month)


def test_judgment_date_over_lunar_new_year_month():
    # start in 2027-02 (first trading day 02-15) -> target month 2028-02.
    # 2028-02-01 is a Tuesday and trading in CAL, so the day before it, Mon 2028-01-31, is the answer.
    assert sr.judgment_date(date(2027, 2, 15), CAL) == date(2028, 1, 31)
    # Same start with 2028-01-31 and 2028-02-01 closed: the first trading day of 2028-02 is
    # Wed 02-02 and the previous trading day is Fri 2028-01-28.
    cal = CAL - {date(2028, 1, 31), date(2028, 2, 1)}
    assert sr.first_trading_day(cal, 2028, 2) == date(2028, 2, 2)
    assert sr.judgment_date(date(2027, 2, 15), cal) == date(2028, 1, 28)


def test_judgment_date_needs_calendar_coverage():
    with pytest.raises(ValueError):
        sr.judgment_date(date(2026, 12, 1), make_calendar(date(2026, 11, 1), date(2027, 6, 30)))


def test_start_date_lock_on_or_before_cutoff():
    assert sr.start_date(date(2026, 11, 30), CAL) == date(2026, 12, 1)
    assert sr.start_date(date(2026, 11, 17), CAL) == date(2026, 12, 1)
    assert sr.start_date(date(2026, 11, 29), CAL) == date(2026, 12, 1)   # Sunday lock


def test_start_date_lock_on_12_01_defers_to_january():
    assert sr.start_date(date(2026, 12, 1), CAL) == date(2027, 1, 4)


def test_start_date_lock_mid_december():
    assert sr.start_date(date(2026, 12, 15), CAL) == date(2027, 1, 4)
    assert sr.start_date(date(2026, 12, 31), CAL) == date(2027, 1, 4)


def test_start_date_later_locks():
    assert sr.start_date(date(2027, 1, 31), CAL) == date(2027, 2, 15)    # lunar new year month
    assert sr.start_date(date(2027, 4, 30), CAL) == date(2027, 5, 3)


def test_restart_date_is_first_trading_day_of_next_month():
    assert sr.restart_date(date(2027, 3, 17), CAL) == date(2027, 4, 1)
    assert sr.restart_date(date(2027, 5, 31), CAL) == date(2027, 6, 1)
    assert sr.restart_date(date(2026, 12, 31), CAL) == date(2027, 1, 4)
    assert sr.restart_date(date(2027, 1, 20), CAL) == date(2027, 2, 15)
    assert sr.restart_date(date(2027, 12, 20), CAL) == date(2028, 1, 3)  # year rollover
    assert sr.restart_date(date(2027, 4, 30), CAL) == date(2027, 5, 3)   # first day is not the 1st


# --------------------------------------------------------------------------- #
# rank_top
# --------------------------------------------------------------------------- #

def test_rank_top_orders_by_score_then_code():
    scores = {"2330": 0.9, "1101": 0.9, "2317": 0.5, "1102": 0.9, "9999": 0.1}
    assert sr.rank_top(scores, 3) == ["1101", "1102", "2330"]
    assert sr.rank_top(scores, 4) == ["1101", "1102", "2330", "2317"]


def test_rank_top_is_independent_of_input_order():
    items = [(f"{1000 + i}", 0.5 if i % 3 else 0.7) for i in range(40)]
    expected = sr.rank_top(dict(items), 20)
    for seed in range(5):
        shuffled = items[:]
        random.Random(seed).shuffle(shuffled)
        assert sr.rank_top(dict(shuffled), 20) == expected


def test_rank_top_n_larger_than_input_and_nonpositive_n():
    assert sr.rank_top({"a": 1.0, "b": 2.0}, 10) == ["b", "a"]
    assert sr.rank_top({"a": 1.0}, 0) == []
    assert sr.rank_top({}, 5) == []


def test_rank_top_drops_nan_and_none():
    assert sr.rank_top({"a": float("nan"), "b": None, "c": 1.0}, 3) == ["c"]


# --------------------------------------------------------------------------- #
# primary_industry
# --------------------------------------------------------------------------- #

def test_aliases_table_is_the_eight_pairs():
    assert len(sr.ALIASES) == 8
    assert sr.ALIASES["電子通路類"] == "電子通路業"
    assert sr.ALIASES["其他電子類"] == "其他電子業"
    assert sr.ALIASES["觀光餐旅"] == "觀光事業"
    assert sr.ALIASES["金融保險"] == "金融業"
    assert set(sr.NON_INDUSTRY_LABELS) == {"創新板股票", "創新版股票", "存託憑證", "ETF"}


@pytest.mark.parametrize("variant,canonical", sorted(sr.ALIASES.items()))
def test_each_alias_collapses_to_canonical(variant, canonical):
    ind = {"1": [variant], "2": [canonical]}
    assert sr.primary_industry(ind, {"1", "2"}) == {"1": canonical, "2": canonical}


def test_both_alias_spellings_counted_once_with_union_members():
    # a1 carries both spellings, a2 only the "類" spelling.
    #   union count of the merged 電子通路業 = {a1, a2} = 2   -> beats 半導體業 (3 members)
    #   per-spelling sum = 1 (業: a1) + 2 (類: a1, a2) = 3    -> would TIE 半導體業, and
    #   "半導體業" < "電子通路業" by code point, so a sum-based count would pick 半導體業.
    universe = {"a1", "a2", "e1"}
    ind = {
        "a1": ["電子通路業", "電子通路類", "半導體業"],
        "a2": ["電子通路類", "半導體業"],
        "e1": ["半導體業"],
    }
    assert "半導體業" < "電子通路業"
    p = sr.primary_industry(ind, universe)
    assert p["a1"] == "電子通路業"
    assert p["a2"] == "電子通路業"
    assert p["e1"] == "半導體業"


def test_only_innovation_board_label_goes_to_other():
    ind = {"1": ["創新板股票"], "2": ["創新版股票"], "3": ["存託憑證"], "4": ["ETF"],
           "5": ["創新板股票", "ETF"]}
    assert sr.primary_industry(ind, set(ind)) == {c: "其他" for c in ind}


def test_non_industry_label_is_ignored_when_real_industry_present():
    ind = {"1": ["創新板股票", "半導體業"]}
    assert sr.primary_industry(ind, {"1"}) == {"1": "半導體業"}


def test_other_electronics_is_not_other():
    ind = {"1": ["其他電子業"], "2": ["其他電子類"], "3": ["其他", "其他電子業"], "4": ["其他"]}
    p = sr.primary_industry(ind, set(ind))
    assert p["1"] == p["2"] == p["3"] == "其他電子業"
    assert p["4"] == "其他"


def test_other_is_dropped_before_choosing():
    # "其他" would be the smallest category (1 member) but must never win over a real one.
    ind = {"1": ["其他", "電子工業"], "2": ["電子工業"], "3": ["電子工業"]}
    assert sr.primary_industry(ind, set(ind))["1"] == "電子工業"


def test_code_without_classification_goes_to_other():
    ind = {"1": ["電子工業"], "2": [], "3": None}
    p = sr.primary_industry(ind, {"1", "2", "3", "4"})  # "4" has no entry at all
    assert p == {"1": "電子工業", "2": "其他", "3": "其他", "4": "其他"}


def test_tie_goes_to_smallest_code_point():
    ind = {"1": ["甲業", "乙業"], "2": ["甲業", "乙業"]}
    assert "乙業" < "甲業"
    assert sr.primary_industry(ind, {"1", "2"}) == {"1": "乙業", "2": "乙業"}


def test_member_count_uses_universe_only():
    # X has 2 universe members + 10 outside; Y has 3 universe members.  Inside the
    # universe X (2) < Y (3); counting outside codes would reverse it.
    universe = {"1", "2", "3"}
    ind = {"1": ["X", "Y"], "2": ["X", "Y"], "3": ["Y"]}
    ind.update({f"out{i}": ["X"] for i in range(10)})
    p = sr.primary_industry(ind, universe)
    assert p == {"1": "X", "2": "X", "3": "Y"}
    assert set(p) == universe   # codes outside the universe are not returned


def test_spec_a5c7_example_chemical_biotech_vs_biotech():
    # 化學生技醫療 103 members vs 生技醫療業 175 members; a code with both goes to the
    # smaller (化學生技醫療), a code with only 生技醫療業 stays there.
    ind = {}
    for i in range(60):                       # both labels
        ind[f"b{i}"] = ["生技醫療業", "化學生技醫療"]
    for i in range(115):                      # 生技醫療業 only -> 175 total
        ind[f"m{i}"] = ["生技醫療業"]
    for i in range(43):                       # 化學生技醫療 only -> 103 total
        ind[f"c{i}"] = ["化學生技醫療"]
    universe = set(ind)
    p = sr.primary_industry(ind, universe)
    assert sum("化學生技醫療" in v for v in ind.values()) == 103
    assert sum("生技醫療業" in v for v in ind.values()) == 175
    assert all(p[f"b{i}"] == "化學生技醫療" for i in range(60))
    assert all(p[f"m{i}"] == "生技醫療業" for i in range(115))
    assert all(p[f"c{i}"] == "化學生技醫療" for i in range(43))
    # Known limitation (A5c-7): the 4-name cap applies per primary label, so biotech can reach 4 + 4 = 8.
    ranked = [f"b{i}" for i in range(6)] + [f"m{i}" for i in range(6)]
    assert len(sr.select_with_cap(ranked, p, cap=4, n=20)) == 8


def test_str_instead_of_list_is_treated_as_single_label():
    assert sr.primary_industry({"1": "電子工業"}, {"1"}) == {"1": "電子工業"}


# --------------------------------------------------------------------------- #
# select_with_cap / select_uncapped
# --------------------------------------------------------------------------- #

def _prim(**groups):
    return {code: ind for ind, codes in groups.items() for code in codes}


def test_cap_single_industry_concentration_fills_from_below():
    primary = _prim(A=[f"a{i}" for i in range(10)], B=[f"b{i}" for i in range(10)])
    ranked = [f"a{i}" for i in range(10)] + [f"b{i}" for i in range(10)]
    out = sr.select_with_cap(ranked, primary, cap=4, n=6)
    assert out == ["a0", "a1", "a2", "a3", "b0", "b1"]


def test_cap_skips_over_limit_and_keeps_rank_order():
    primary = {"a1": "A", "a2": "A", "b1": "B", "a3": "A", "c1": "C", "b2": "B"}
    ranked = ["a1", "a2", "b1", "a3", "c1", "b2"]
    assert sr.select_with_cap(ranked, primary, cap=2, n=4) == ["a1", "a2", "b1", "c1"]
    assert sr.select_with_cap(ranked, primary, cap=2, n=5) == ["a1", "a2", "b1", "c1", "b2"]


def test_cap_default_values_4_and_20():
    primary = {f"a{i}": "A" for i in range(30)}
    primary.update({f"b{i}": f"B{i}" for i in range(30)})   # 30 distinct industries
    ranked = [f"a{i}" for i in range(30)] + [f"b{i}" for i in range(30)]
    out = sr.select_with_cap(ranked, primary)
    assert len(out) == 20
    assert out[:4] == ["a0", "a1", "a2", "a3"]
    assert out[4:] == [f"b{i}" for i in range(16)]


def test_cap_ranked_too_short_returns_what_it_can():
    primary = {f"a{i}": "A" for i in range(10)}
    ranked = [f"a{i}" for i in range(10)]
    assert sr.select_with_cap(ranked, primary, cap=4, n=20) == ["a0", "a1", "a2", "a3"]
    assert sr.select_with_cap([], primary, cap=4, n=20) == []


def test_cap_frozen_takes_slots_and_industry_quota():
    primary = {"f1": "A", "f2": "A", **{f"a{i}": "A" for i in range(5)}, **{f"b{i}": "B" for i in range(5)}}
    ranked = [f"a{i}" for i in range(5)] + [f"b{i}" for i in range(5)]
    # 2 frozen in A: only 2 more A allowed (cap 4), and 6 - 2 = 4 new slots.
    out = sr.select_with_cap(ranked, primary, cap=4, n=6, frozen={"f1", "f2"})
    assert out == ["a0", "a1", "b0", "b1"]


def test_cap_frozen_code_in_ranked_is_not_picked_twice():
    primary = {"f1": "A", "a1": "A", "b1": "B", "b2": "B"}
    out = sr.select_with_cap(["f1", "a1", "b1", "b2"], primary, cap=4, n=3, frozen={"f1"})
    assert out == ["a1", "b1"]            # 1 frozen + 2 new = 3 slots; f1 never returned


def test_cap_frozen_alone_can_exhaust_industry_quota():
    primary = {**{f"f{i}": "A" for i in range(4)}, "a1": "A", "b1": "B"}
    out = sr.select_with_cap(["a1", "b1"], primary, cap=4, n=10, frozen={f"f{i}" for i in range(4)})
    assert out == ["b1"]


def test_cap_n_not_larger_than_frozen_returns_empty():
    primary = {"f1": "A", "a1": "A"}
    assert sr.select_with_cap(["a1"], primary, cap=4, n=1, frozen={"f1"}) == []
    assert sr.select_with_cap(["a1"], primary, cap=4, n=0) == []


def test_cap_applies_to_other_bucket_too():
    primary = {f"o{i}": "其他" for i in range(6)}
    assert sr.select_with_cap([f"o{i}" for i in range(6)], primary, cap=4, n=20) == ["o0", "o1", "o2", "o3"]


def test_cap_unknown_code_counts_as_other():
    out = sr.select_with_cap(["x1", "x2", "x3"], {}, cap=2, n=20)
    assert out == ["x1", "x2"]


def test_cap_with_rank_top_is_deterministic_under_shuffle():
    scores = {f"{2000 + i}": 1.0 for i in range(30)}            # all tied
    primary = {code: ("A" if int(code) % 2 == 0 else "B") for code in scores}
    expected = sr.select_with_cap(sr.rank_top(scores, 30), primary, cap=4, n=8)
    assert expected == ["2000", "2001", "2002", "2003", "2004", "2005", "2006", "2007"]
    for seed in range(5):
        items = list(scores.items())
        random.Random(seed).shuffle(items)
        assert sr.select_with_cap(sr.rank_top(dict(items), 30), primary, cap=4, n=8) == expected


def test_uncapped_with_frozen_takes_n_minus_k_nonfrozen():
    ranked = ["a", "b", "c", "d", "e"]
    assert sr.select_uncapped(ranked, n=4, frozen=frozenset({"b"})) == ["a", "c", "d"]
    assert sr.select_uncapped(ranked, n=4) == ["a", "b", "c", "d"]
    assert sr.select_uncapped(ranked, n=1, frozen=frozenset({"z"})) == []


# --------------------------------------------------------------------------- #
# Costs
# --------------------------------------------------------------------------- #

def test_cost_constants():
    assert sr.BUY_FEE == pytest.approx(0.000314, abs=1e-6)            # 0.0314%
    assert sr.SELL_FEE == pytest.approx(0.003314, abs=1e-6)           # 0.3314%
    assert sr.SLIPPAGE == 0.001


def test_buy_and_sell_cost_values():
    assert sr.buy_cost(1_000_000) == pytest.approx(1_000_000 * (0.001425 * 0.22 + 0.001))
    assert sr.buy_cost(1_000_000) == pytest.approx(1313.5)           # 0.0314% + 0.1%
    assert sr.sell_cost(1_000_000) == pytest.approx(4313.5)          # 0.3314% + 0.1%
    assert sr.buy_cost(0) == 0 and sr.sell_cost(0) == 0
    assert sr.trade_cost(1_000_000, 500_000) == pytest.approx(1313.5 + 2156.75)
    assert sr.trade_cost(0, 0) == 0


def test_negative_value_rejected():
    with pytest.raises(ValueError):
        sr.buy_cost(-1)
    with pytest.raises(ValueError):
        sr.sell_cost(-1)


# --------------------------------------------------------------------------- #
# rebalance_orders
# --------------------------------------------------------------------------- #

def _apply(current, cash, orders, frozen=None):
    """Return (cash_after, holdings_value_after) after applying orders and paying cost from cash."""
    held = dict(current)
    for c, v in orders["sells"].items():
        held[c] = held.get(c, 0) - v
        cash += v
    for c, v in orders["buys"].items():
        held[c] = held.get(c, 0) + v
        cash -= v
    cash -= orders["cost"]
    return cash, held


def test_rebalance_only_charges_differences():
    current = {"a": 300.0, "b": 100.0, "x": 200.0}
    cash = 400.0
    nav = cash + sum(current.values())            # 1000
    out = sr.rebalance_orders(current, ["a", "b", "c", "d"], nav)
    assert out["buys"] == {"b": pytest.approx(150), "c": pytest.approx(250), "d": pytest.approx(250)}
    assert out["sells"] == {"a": pytest.approx(50), "x": pytest.approx(200)}
    assert out["cost"] == pytest.approx(sr.buy_cost(650) + sr.sell_cost(250))


def test_rebalance_holding_already_at_target_costs_nothing():
    current = {"a": 250.0, "b": 250.0, "c": 250.0, "d": 250.0}
    out = sr.rebalance_orders(current, ["a", "b", "c", "d"], 1000.0)
    assert out == {"buys": {}, "sells": {}, "cost": 0.0}


def test_rebalance_retained_position_not_in_orders_cost_only_on_others():
    current = {"keep": 500.0, "old": 500.0}
    out = sr.rebalance_orders(current, ["keep", "new"], 1000.0)
    assert "keep" not in out["buys"] and "keep" not in out["sells"]
    assert out["sells"] == {"old": pytest.approx(500)}
    assert out["buys"] == {"new": pytest.approx(500)}
    assert out["cost"] == pytest.approx(sr.sell_cost(500) + sr.buy_cost(500))


def test_rebalance_equal_weight_targets_with_initial_build():
    out = sr.rebalance_orders({}, ["a", "b", "c", "d"], 1_000_000.0)
    assert out["sells"] == {}
    assert out["buys"] == {c: pytest.approx(250_000) for c in "abcd"}
    assert out["cost"] == pytest.approx(sr.buy_cost(1_000_000))


def test_rebalance_frozen_not_touched_and_excluded_from_equal_weight_base():
    current = {"fz": 100.0, "a": 600.0, "x": 300.0}      # fz frozen, included in current
    frozen = {"fz": 100.0}
    nav = 1000.0
    out = sr.rebalance_orders(current, ["a", "b", "c"], nav, frozen)
    each = (nav - 100.0) / 3                              # 300
    assert "fz" not in out["buys"] and "fz" not in out["sells"]
    assert out["sells"] == {"a": pytest.approx(600 - each), "x": pytest.approx(300)}
    assert out["buys"] == {"b": pytest.approx(each), "c": pytest.approx(each)}


def test_rebalance_frozen_not_in_current_dict_same_result():
    frozen = {"fz": 100.0}
    a = sr.rebalance_orders({"fz": 100.0, "a": 900.0}, ["a", "b"], 1000.0, frozen)
    b = sr.rebalance_orders({"a": 900.0}, ["a", "b"], 1000.0, frozen)
    assert a == b


def test_rebalance_frozen_code_as_target_rejected():
    with pytest.raises(ValueError):
        sr.rebalance_orders({"fz": 100.0}, ["fz", "a"], 1000.0, {"fz": 100.0})


def test_rebalance_balance_identity_cash_plus_holdings_equals_nav_minus_cost():
    current = {"a": 300.0, "b": 100.0, "x": 200.0, "fz": 50.0}
    frozen = {"fz": 50.0}
    cash = 350.0
    nav = cash + sum(current.values())
    out = sr.rebalance_orders(current, ["a", "b", "c", "d"], nav, frozen)
    cash_after, held = _apply(current, cash, out)
    holdings_after = sum(v for v in held.values())
    assert cash_after + holdings_after == pytest.approx(nav - out["cost"])
    each = (nav - 50.0) / 4
    for c in "abcd":
        assert held[c] == pytest.approx(each)
    assert held["x"] == pytest.approx(0)
    assert held["fz"] == pytest.approx(50.0)


def test_rebalance_fully_invested_book_ends_with_cash_of_minus_cost():
    # Documented behaviour: targets are sized on pre-cost NAV.
    current = {"a": 600.0, "b": 400.0}
    out = sr.rebalance_orders(current, ["c", "d"], 1000.0)
    cash_after, held = _apply(current, 0.0, out)
    assert cash_after == pytest.approx(-out["cost"])
    assert sum(held.values()) == pytest.approx(1000.0)


def test_rebalance_empty_target_sells_everything_movable():
    out = sr.rebalance_orders({"a": 100.0, "fz": 50.0}, [], 200.0, {"fz": 50.0})
    assert out["buys"] == {}
    assert out["sells"] == {"a": pytest.approx(100)}
    assert out["cost"] == pytest.approx(sr.sell_cost(100))


def test_rebalance_input_validation():
    with pytest.raises(ValueError):
        sr.rebalance_orders({}, ["a", "a"], 100.0)
    with pytest.raises(ValueError):
        sr.rebalance_orders({}, ["a"], 10.0, {"fz": 20.0})


def test_rebalance_orders_deterministic_key_order():
    out = sr.rebalance_orders({}, ["c", "a", "b"], 300.0)
    assert list(out["buys"]) == ["a", "b", "c"]


# --------------------------------------------------------------------------- #
# revenue_target / revenue_refresh_complete
# --------------------------------------------------------------------------- #

def test_revenue_target_boundary_at_10th_and_11th():
    assert sr.revenue_target(date(2026, 11, 10)) == "2026-09-01"     # still two months back
    assert sr.revenue_target(date(2026, 11, 11)) == "2026-10-01"     # previous month
    assert sr.revenue_target(date(2026, 11, 1)) == "2026-09-01"
    assert sr.revenue_target(date(2026, 11, 30)) == "2026-10-01"


def test_revenue_target_year_rollover():
    assert sr.revenue_target(date(2027, 1, 10)) == "2026-11-01"
    assert sr.revenue_target(date(2027, 1, 11)) == "2026-12-01"
    assert sr.revenue_target(date(2027, 2, 5)) == "2026-12-01"
    assert sr.revenue_target(date(2027, 2, 12)) == "2027-01-01"


def test_revenue_target_matches_update_fundamentals_logic():
    # Same rule as scripts/update_fundamentals.revenue_target, reproduced inline.
    for d in span(date(2026, 1, 1), date(2027, 12, 31)):
        months_back = 1 if d.day > 10 else 2
        y, m = d.year, d.month - months_back
        while m < 1:
            y, m = y - 1, m + 12
        assert sr.revenue_target(d) == f"{y:04d}-{m:02d}-01"


TODAY = date(2026, 12, 14)
TARGET = sr.revenue_target(TODAY)     # "2026-11-01"


def _frs(done_stamp=0, done_attempt=0, not_done=0, empty=0):
    d = {}
    for i in range(done_stamp):
        d[f"s{i}"] = {"revenue": TARGET}
    for i in range(done_attempt):
        d[f"t{i}"] = {"revenue": "2026-10-01", "revenue_attempt": "2026-12-11"}
    for i in range(not_done):
        d[f"n{i}"] = {"revenue": "2026-10-01", "revenue_attempt": "2026-12-10"}
    for i in range(empty):
        d[f"e{i}"] = {}
    return d


def test_complete_by_stamp():
    ok, ratio = sr.revenue_refresh_complete({"1": {"revenue": TARGET}}, TODAY)
    assert ok and ratio == 1.0


def test_complete_by_attempt_on_or_after_11th():
    for stamp in ("2026-12-11", "2026-12-12", "2026-12-14"):
        ok, ratio = sr.revenue_refresh_complete({"1": {"revenue": "2026-10-01", "revenue_attempt": stamp}},
                                                TODAY)
        assert ok and ratio == 1.0


def test_attempt_before_11th_does_not_count():
    ok, ratio = sr.revenue_refresh_complete({"1": {"revenue": "2026-10-01", "revenue_attempt": "2026-12-10"}},
                                            TODAY)
    assert not ok and ratio == 0.0


def test_attempt_from_previous_month_does_not_count():
    ok, ratio = sr.revenue_refresh_complete({"1": {"revenue_attempt": "2026-11-20"}}, TODAY)
    assert not ok and ratio == 0.0


def test_empty_fund_refresh_does_not_count():
    ok, ratio = sr.revenue_refresh_complete({"1": {}, "2": None}, TODAY)
    assert not ok and ratio == 0.0


def test_exactly_90_percent_is_complete():
    frs = _frs(done_stamp=5, done_attempt=4, not_done=1)       # 9 / 10
    ok, ratio = sr.revenue_refresh_complete(frs, TODAY)
    assert ok and ratio == pytest.approx(0.9)


def test_just_below_90_percent_is_not_complete():
    frs = _frs(done_stamp=89, not_done=11)                       # 89 / 100
    ok, ratio = sr.revenue_refresh_complete(frs, TODAY)
    assert not ok and ratio == pytest.approx(0.89)
    frs = _frs(done_stamp=8, empty=2)                            # 8 / 10
    assert sr.revenue_refresh_complete(frs, TODAY)[0] is False


def test_insufficient_stamp_counts_as_complete():
    # update_fundamentals stamps fr["revenue"] = target for "insufficient" codes too.
    frs = {f"{i}": {"revenue": TARGET, "revenue_attempt": "2026-12-14"} for i in range(9)}
    frs["x"] = {"revenue_attempt": "2026-12-01"}                 # failed/stale: not done
    ok, ratio = sr.revenue_refresh_complete(frs, TODAY)
    assert ok and ratio == pytest.approx(0.9)


def test_attempt_threshold_follows_todays_month():
    today = date(2027, 1, 14)
    frs = {"1": {"revenue_attempt": "2026-12-20"}, "2": {"revenue_attempt": "2027-01-11"}}
    ok, ratio = sr.revenue_refresh_complete(frs, today)
    assert ratio == 0.5 and not ok


def test_empty_universe_and_bad_attempt_stamp():
    assert sr.revenue_refresh_complete({}, TODAY) == (False, 0.0)
    assert sr.revenue_refresh_complete({"1": {"revenue_attempt": date(2026, 12, 12)}}, TODAY)[0] is True


def test_target_is_derived_from_today():
    # Stamped with the previous cycle's target: not complete once the 11th passes.
    stale = {"1": {"revenue": "2026-10-01"}}
    assert sr.revenue_refresh_complete(stale, date(2026, 12, 14)) == (False, 0.0)    # target is 2026-11-01
    assert sr.revenue_refresh_complete(stale, date(2026, 11, 14)) == (True, 1.0)     # target is 2026-10-01
    # Before the 11th the target is two months back.
    assert sr.revenue_refresh_complete({"1": {"revenue": "2026-11-01"}}, date(2026, 12, 10)) == (False, 0.0)
    assert sr.revenue_refresh_complete({"1": {"revenue": "2026-10-01"}}, date(2026, 12, 10)) == (True, 1.0)


@pytest.mark.parametrize("stamp", [
    datetime(2026, 12, 11, 7, 5, 3),
    date(2026, 12, 11),
    "2026-12-11",
    "2026-12-11T07:05:03",
    "2026-12-11T07:05:03+08:00",
    "2026-12-11 07:05:03",
])
def test_attempt_stamp_formats_on_the_11th_count(stamp):
    assert sr.revenue_refresh_complete({"1": {"revenue_attempt": stamp}}, TODAY) == (True, 1.0)


@pytest.mark.parametrize("stamp", [
    datetime(2026, 12, 10, 23, 59, 59),
    date(2026, 12, 10),
    "2026-12-10",
    "2026-12-10T23:59:59",
])
def test_attempt_stamp_formats_on_the_10th_do_not_count(stamp):
    assert sr.revenue_refresh_complete({"1": {"revenue_attempt": stamp}}, TODAY) == (False, 0.0)


@pytest.mark.parametrize("stamp", ["garbage", "", "12/11/2026", 12345, None])
def test_unparseable_attempt_stamp_is_not_done(stamp):
    assert sr.revenue_refresh_complete({"1": {"revenue_attempt": stamp}}, TODAY) == (False, 0.0)


# --------------------------------------------------------------------------- #
# S2 forced day and actual rebalance day
# --------------------------------------------------------------------------- #

def test_s2_forced_day_20th_is_trading_day():
    assert sr.s2_forced_day(CAL, 2027, 10) == date(2027, 10, 20)       # Wed


def test_s2_forced_day_20th_on_weekend_rolls_forward():
    assert sr.s2_forced_day(CAL, 2026, 12) == date(2026, 12, 21)       # 12/20 Sunday
    assert sr.s2_forced_day(CAL, 2027, 2) == date(2027, 2, 22)         # 2/20 Saturday
    assert sr.s2_forced_day(CAL, 2027, 3) == date(2027, 3, 22)         # 3/20 Saturday


def test_s2_forced_day_20th_weekday_holiday_rolls_forward():
    cal = CAL - {date(2027, 9, 20)}                                    # Monday closed
    assert sr.s2_forced_day(cal, 2027, 9) == date(2027, 9, 21)


def test_s2_day_complete_on_first_eligible_day():
    out = sr.s2_rebalance_day(CAL, 2027, 8, lambda d: True)
    assert out == date(2027, 8, 12)


def test_s2_day_12th_on_weekend_starts_monday():
    assert sr.s2_rebalance_day(CAL, 2026, 12, lambda d: True) == date(2026, 12, 14)
    # never before the 12th even though the check is True on earlier days
    assert sr.s2_rebalance_day(CAL, 2027, 6, {date(2027, 6, 11): True, date(2027, 6, 14): True}) == date(2027, 6, 14)


def test_s2_day_waits_until_complete():
    done_from = date(2026, 12, 16)
    assert sr.s2_rebalance_day(CAL, 2026, 12, lambda d: d >= done_from) == done_from


def test_s2_day_forced_when_never_complete():
    assert sr.s2_rebalance_day(CAL, 2027, 10, lambda d: False) == date(2027, 10, 20)


def test_s2_day_forced_on_weekend_20th_goes_to_next_trading_day():
    assert sr.s2_rebalance_day(CAL, 2026, 12, lambda d: False) == date(2026, 12, 21)
    assert sr.s2_rebalance_day(CAL, 2027, 3, lambda d: False) == date(2027, 3, 22)


def test_s2_day_lunar_new_year_month():
    assert sr.s2_rebalance_day(CAL, 2027, 2, lambda d: True) == date(2027, 2, 15)
    assert sr.s2_rebalance_day(CAL, 2027, 2, lambda d: False) == date(2027, 2, 22)


def test_s2_day_forced_does_not_call_check_on_forced_day():
    calls = []

    def check(d):
        calls.append(d)
        return False

    assert sr.s2_rebalance_day(CAL, 2027, 10, check) == date(2027, 10, 20)
    assert date(2027, 10, 20) not in calls and calls[0] == date(2027, 10, 12)


def test_s2_day_partial_dict_returns_none_until_decided():
    # 12/14 known False, 12/15 not yet known -> undecided
    assert sr.s2_rebalance_day(CAL, 2026, 12, {date(2026, 12, 14): False}) is None
    assert sr.s2_rebalance_day(CAL, 2026, 12, {}) is None
    # decided as soon as an eligible day is True
    assert sr.s2_rebalance_day(CAL, 2026, 12, {date(2026, 12, 14): False, date(2026, 12, 15): True}) == date(2026, 12, 15)
    # an unknown day *after* the True day does not matter
    assert sr.s2_rebalance_day(CAL, 2026, 12, {date(2026, 12, 14): True}) == date(2026, 12, 14)


def test_s2_day_partial_dict_gap_before_true_is_undecided():
    # 12/14 unknown, 12/15 True: cannot rule out 12/14 -> None
    assert sr.s2_rebalance_day(CAL, 2026, 12, {date(2026, 12, 15): True}) is None


def test_s2_day_all_false_dict_reaches_forced_day():
    days = [d for d in sorted(CAL) if date(2026, 12, 14) <= d < date(2026, 12, 21)]
    assert sr.s2_rebalance_day(CAL, 2026, 12, {d: False for d in days}) == date(2026, 12, 21)


def test_s2_day_callable_none_is_undecided():
    assert sr.s2_rebalance_day(CAL, 2026, 12, lambda d: None) is None
