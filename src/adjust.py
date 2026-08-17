# -*- coding: utf-8 -*-
"""Price-adjustment factors: pure functions over already-fetched corporate
action events and bars. No I/O, no FinMind, no DB access -- same split as
backtest.py/finlab_factors.py. scripts/build_adj_factors.py is the offline
batch job that fetches TWSE/Yahoo, resolves events, and writes the results
this module reads (docs/archive/backtest_plan.md Phase 1a/1b).

Design: raw o/h/l/c/v in daily_bars are left untouched (orders, get_quote(),
the K-line chart all want the real historical price the market actually
printed). adj_factor is stored alongside and is defined so that
`close_raw[i] * adj_factor[i]` == the back-adjusted price on today's scale:
the most recent segment (no corporate actions left to adjust for) has
factor == 1.0, so "today's adjusted price" == "today's raw price", and the
whole existing unadjusted code path is unaffected by this module's
existence until a caller explicitly opts in to adj_factor.

>>> Downstream usage rule (docs/archive/backtest_plan.md Phase 1c) <<<
Use the ADJUSTED price (apply_adjustment()'s c_adj/etc.) for returns,
momentum, ATR, and volume_profile -- these compare price *levels* across
time, and a stock split makes unadjusted levels non-comparable.
Use the RAW price (bars' own o/h/l/c) times RAW volume for turnover and
liquidity filtering (finlab_factors.liquidity_ok() is exactly this) --
turnover is real NTD that actually changed hands that day; multiplying by
adj_factor would scale it by a split ratio that has nothing to do with how
much money traded.
"""


def build_adj_factor(bar_dates, actions):
    """bar_dates: ascending list of "YYYY-MM-DD" strings (one per bar).
    actions: iterable of {"date": "YYYY-MM-DD", "ratio": float}, one row per
    corporate action's ex-date -- `ratio` is the ex-date's multiplicative
    price drop (TWSE's ref_price/pre_close, or an equivalent inferred from
    Yahoo's Adj Close/raw Close -- see scripts/build_adj_factors.py). Not
    required to be pre-sorted.

    Returns a list of floats parallel to bar_dates: for a bar on date D,
    the factor is the product of every action's ratio whose date is
    strictly after D. A bar exactly ON an action's ex-date already trades
    at the new (post-action) price level per TWSE convention, so the
    action does not apply to that bar itself -- only to bars strictly
    before it. The most recent bar (after every known action) always gets
    1.0. Usually non-decreasing going forward in time (dividends/splits/
    rights issues all push ratio <= 1), but NOT guaranteed to be: a capital
    reduction (減資) raises the nominal price and has ratio > 1 -- see
    validate_adj_factor()'s docstring."""
    sorted_actions = sorted(actions, key=lambda a: a["date"])
    n = len(bar_dates)
    factors = [1.0] * n
    cum = 1.0
    a_idx = len(sorted_actions) - 1
    for i in range(n - 1, -1, -1):
        while a_idx >= 0 and sorted_actions[a_idx]["date"] > bar_dates[i]:
            cum *= sorted_actions[a_idx]["ratio"]
            a_idx -= 1
        factors[i] = cum
    return factors


def apply_adjustment(bars, factors):
    """bars: ascending list of {date,o,h,l,c,v,...}. factors: build_adj_factor()'s
    output, same length/order. Returns a new list of dicts (bars aren't
    mutated) with o_adj/h_adj/l_adj/c_adj added -- a uniform per-bar scalar
    multiply of the price fields, standard back-adjustment practice. Volume
    is intentionally left alone (see module docstring's downstream rule)."""
    if len(bars) != len(factors):
        raise ValueError("bars and factors must be the same length")
    out = []
    for bar, f in zip(bars, factors):
        adjusted = dict(bar)
        adjusted["adj_factor"] = f
        adjusted["o_adj"] = bar["o"] * f if bar.get("o") is not None else None
        adjusted["h_adj"] = bar["h"] * f if bar.get("h") is not None else None
        adjusted["l_adj"] = bar["l"] * f if bar.get("l") is not None else None
        adjusted["c_adj"] = bar["c"] * f if bar.get("c") is not None else None
        out.append(adjusted)
    return out


def validate_adj_factor(bar_dates, factors, action_dates, tol=1e-9):
    """Invariant checks (docs/archive/backtest_plan.md Phase 1 "不變量測試
    -- 防算錯比不算更糟"). Returns a list of human-readable violation
    strings; empty list == all invariants hold.

    1. The last bar's factor must be 1.0 (nothing left to adjust for).
    2. A factor change between two adjacent bars must be explained by an
       action date falling in (bar_dates[i-1], bar_dates[i]] -- usually
       that's bar_dates[i] itself, but a trading halt spanning the actual
       ex-date (a real, known occurrence around corporate actions -- see
       docs/archive/backtest_plan.md Phase 1's "缺漏 bar...集中在公司行動
       停牌期") can leave a gap with no bar exactly on the action date, so
       the interval (not exact equality) is what's required. Any step NOT
       explained by an action date in that interval means build_adj_factor()
       was fed a phantom action, or a bug introduced a phantom step.

    Deliberately NOT checked: factors are usually non-decreasing going
    forward in time (older bars need scaling UP less, or not at all, to
    reach today's price level) -- but this flips for a genuine capital
    reduction/reverse-consolidation (減資), which raises the nominal share
    price rather than lowering it. 2327 in this project's own watchlist did
    exactly that (confirmed via a matching multi-week trading-halt gap in
    daily_bars around each such date) with a ratio > 1, so monotonicity
    can't be a hard invariant without false-failing on real data -- only
    "did we account for every step with a recorded action" is checked."""
    violations = []
    if not factors:
        return violations
    if abs(factors[-1] - 1.0) > tol:
        violations.append(f"last factor {factors[-1]!r} != 1.0")

    sorted_action_dates = sorted(action_dates)
    for i in range(1, len(factors)):
        if abs(factors[i] - factors[i - 1]) <= tol:
            continue
        explained = any(bar_dates[i - 1] < d <= bar_dates[i] for d in sorted_action_dates)
        if not explained:
            violations.append(
                f"factor stepped at {bar_dates[i]} with no matching corporate action"
            )
    return violations
