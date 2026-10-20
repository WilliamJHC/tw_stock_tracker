# -*- coding: utf-8 -*-
"""Multi-factor composite ranking, reproducing finlab.finance's AI-agent
guide "ranking method" (排名法多因子) on FinMind data instead of a FinLab
subscription -- see src/finlab_strategy.py for the original FinLab-backed
version this was ported from.

Pure functions over already-fetched per-code series -- no I/O, no FinMind,
no DB access, same split as backtest.py/analysis.py.

Point-in-time discipline: FinMind doesn't expose actual disclosure dates,
only period-end dates, so a fixed lag stands in for "when this number
became public." Taiwan's regulatory deadlines are ~10 days after month-end
for monthly revenue and up to ~45-60 days after quarter-end for quarterly
financials; REVENUE_LAG_DAYS/FINANCIALS_LAG_DAYS use the conservative end
of that range rather than a per-filing lookup.

>>> momentum_pct() prefers ADJUSTED close, falls back to raw <<<
adjust.py's downstream rule (docs/archive/backtest_plan.md Phase 1c) says
momentum must run on split/dividend-adjusted price levels, not raw ones.
Phase 6a (docs/https-finlab-finance-blog-python-get-tai-memoized-lighthouse.md)
closed this gap for this module's (potentially 2000+ code) universe:
scripts/run_factor_backtest.py's fetch_universe_data() now does its own
lightweight per-code Yahoo-ratio adjustment (fetch_yahoo_adj_close() +
compute_yahoo_ratio_series() + detect_steps(), reused from
scripts/build_adj_factors.py, plus this project's own adjust.build_adj_factor())
and stamps a "c_adj" key onto each bar dict -- no TWSE table, no DB write,
just an in-memory factor applied straight onto the bars this module already
receives. momentum_pct() below reads bars[i]["c_adj"] when present, its raw
"c" otherwise. liquidity_ok() is NOT affected either way -- it wants raw
close*volume by design (adjust.py's own rule), not a bug. A caller that
builds `bars` without ever computing adj_factor (e.g. a hand-built fixture,
or a future caller that skips the Yahoo augmentation step) still gets the
old raw-close behavior -- the phantom-return risk near a split only remains
where "c_adj" was never populated.
"""
import bisect
import datetime as dt

REVENUE_LAG_DAYS = 10
FINANCIALS_LAG_DAYS = 60
LIQUIDITY_MIN_TURNOVER = 10_000_000  # NTD, 20-bar avg trading value floor
LIQUIDITY_WINDOW = 20
MOMENTUM_DAYS = 60
RANKING_WEIGHTS = {"revenue": 0.4, "roe": 0.3, "momentum": 0.3}
RANKING_TOP_N = 20


def _shift_date(date_str, days):
    d = dt.datetime.strptime(date_str, "%Y-%m-%d") + dt.timedelta(days=days)
    return d.strftime("%Y-%m-%d")


def _month_end_lag_date(month_start_str, lag_days):
    """month_start_str ("YYYY-MM-01") -> that month's end + lag_days, i.e.
    the 1st of the following month plus (lag_days - 1). Taiwan's monthly
    revenue disclosure deadline is measured from month-END, not month-start
    -- get_monthly_revenue() stores the revenue month's 1st, so shifting
    from that directly (as an earlier version of this module did) understated
    the real-world lag by up to a month."""
    d = dt.datetime.strptime(month_start_str, "%Y-%m-%d")
    next_month_first = d.replace(year=d.year + 1, month=1) if d.month == 12 else d.replace(month=d.month + 1)
    return (next_month_first + dt.timedelta(days=lag_days - 1)).strftime("%Y-%m-%d")


def _month_end(month_start_str):
    """"YYYY-MM-01" -> that same month's last calendar day, as a datetime."""
    d = dt.datetime.strptime(month_start_str, "%Y-%m-%d")
    next_month_first = d.replace(year=d.year + 1, month=1) if d.month == 12 else d.replace(month=d.month + 1)
    return next_month_first - dt.timedelta(days=1)


def _sane_announce_date(announce_date, period_start_date):
    """announce_date usable as-is only if it parses and isn't before that
    revenue PERIOD'S OWN END (a real disclosure can't happen before the
    month being reported on has even finished -- checking only >= period
    START would let through an impossible mid-month date). Guards against a
    malformed or stale value slipping past get_monthly_revenue()'s own
    filtering. Returns announce_date or None."""
    if not announce_date:
        return None
    try:
        d = dt.datetime.strptime(announce_date, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    if d <= _month_end(period_start_date):
        return None
    return announce_date


def revenue_yoy_series(revenue_rows):
    """[{date, revenue, announce_date}] (ascending, monthly) -> [{usable_date,
    yoy_pct}]. usable_date = announce_date (get_monthly_revenue()'s real
    FinMind-reported disclosure date) when present and sane, else the old
    revenue month-end + REVENUE_LAG_DAYS fallback -- see
    get_monthly_revenue()'s docstring: only recent months carry a real
    announce_date, so most of any historical backtest still runs on the
    fixed-lag assumption exactly as before. Matches each row against the
    entry exactly 12 calendar months earlier by date (not by position), so a
    gap in the input -- get_monthly_revenue() doesn't backfill missing
    months -- skips that point instead of silently comparing non-adjacent
    months. Needs >=13 months of input to produce a single point (one year
    of comparison + the point itself); shorter input yields [].

    usable_date is clamped to be non-decreasing across the output (a running
    max, in input/period order) -- factor_ranks()/composite_scores() reach
    this series through _as_of(), which bisects assuming ascending
    usable_date. A restated period's surviving row (get_monthly_revenue()
    keeps whichever row FinMind lists LAST for that period, not necessarily
    the on-time one) can carry an announce_date from well after that
    period's original disclosure -- late enough, in the worst case, to land
    after a LATER period's own on-time announce_date, which would otherwise
    break that ordering. The clamp is a one-directional pushback (never
    pulls a date earlier), so it can only make an usable_date more
    conservative, never introduce lookahead."""
    if not revenue_rows or len(revenue_rows) < 13:
        return []
    by_date = {r["date"]: r["revenue"] for r in revenue_rows}
    out = []
    last_usable_dt = None
    for r in revenue_rows:
        cur_dt = dt.datetime.strptime(r["date"], "%Y-%m-%d")
        prev_key = cur_dt.replace(year=cur_dt.year - 1).strftime("%Y-%m-%d")
        prev = by_date.get(prev_key)
        if not prev:
            continue
        usable_date = (_sane_announce_date(r.get("announce_date"), r["date"])
                        or _month_end_lag_date(r["date"], REVENUE_LAG_DAYS))
        usable_dt = dt.datetime.strptime(usable_date, "%Y-%m-%d")
        if last_usable_dt is not None and usable_dt < last_usable_dt:
            usable_date = last_usable_dt.strftime("%Y-%m-%d")
        else:
            last_usable_dt = usable_dt
        out.append({
            "usable_date": usable_date,
            "yoy_pct": (r["revenue"] - prev) / abs(prev) * 100,
        })
    return out


def roe_series(financials_rows):
    """[{date, net_income, equity}] (ascending, quarterly) -> [{usable_date,
    roe_pct}]. ROE = trailing-4-quarter net income / latest quarter equity,
    annualized by construction (TTM numerator). usable_date = quarter-end +
    FINANCIALS_LAG_DAYS. Needs >=4 quarters of input. get_financials() joins
    income/equity by date INTERSECTION, so a quarter missing either side is
    dropped rather than backfilled -- each 4-row window is checked for
    ~quarterly (80-100 day) consecutive gaps before use, so a dropped
    quarter skips that point instead of silently summing non-adjacent
    quarters as if they were TTM. Non-positive equity is also skipped:
    ROE is undefined (or sign-flipped and misleading) for negative book
    equity."""
    if not financials_rows or len(financials_rows) < 4:
        return []
    out = []
    for i in range(3, len(financials_rows)):
        window = financials_rows[i - 3:i + 1]
        window_dates = [dt.datetime.strptime(r["date"], "%Y-%m-%d") for r in window]
        if any(not (80 <= (window_dates[j + 1] - window_dates[j]).days <= 100) for j in range(3)):
            continue
        equity = window[-1]["equity"]
        if not equity or equity <= 0:
            continue
        ttm_income = sum(r["net_income"] for r in window)
        out.append({
            "usable_date": _shift_date(window[-1]["date"], FINANCIALS_LAG_DAYS),
            "roe_pct": ttm_income / equity * 100,
        })
    return out


def _margin_trend_series(financials_rows, numerator_key):
    """Shared implementation for gross_margin_trend_series()/
    operating_margin_trend_series() -- financials_rows: [{date, revenue,
    numerator_key, ...}] (ascending, quarterly, get_financials()'s extended
    shape). Trend = this quarter's margin (numerator/revenue, %) minus the
    SAME quarter one year ago's -- cancels seasonality the same way
    revenue_yoy_series() does (exact calendar-year match, not position),
    rather than comparing a raw margin level across quarters with different
    seasonal mix. usable_date = quarter-end + FINANCIALS_LAG_DAYS, same
    disclosure-lag convention as roe_series(). A quarter missing revenue or
    `numerator_key` (get_financials() leaves these None rather than
    dropping the row, so ROE stays computable even when a margin figure is
    absent) is skipped for this factor without affecting roe_series()/
    revenue_yoy_series() at all."""
    if not financials_rows or len(financials_rows) < 5:
        return []
    by_date = {r["date"]: r for r in financials_rows}

    def _margin_pct(row):
        revenue, num = row.get("revenue"), row.get(numerator_key)
        if not revenue or num is None:
            return None
        return num / revenue * 100

    out = []
    for r in financials_rows:
        cur_margin = _margin_pct(r)
        if cur_margin is None:
            continue
        cur_dt = dt.datetime.strptime(r["date"], "%Y-%m-%d")
        prev_key = cur_dt.replace(year=cur_dt.year - 1).strftime("%Y-%m-%d")
        prev = by_date.get(prev_key)
        prev_margin = _margin_pct(prev) if prev else None
        if prev_margin is None:
            continue
        out.append({
            "usable_date": _shift_date(r["date"], FINANCIALS_LAG_DAYS),
            "trend_pct": cur_margin - prev_margin,
        })
    return out


def gross_margin_trend_series(financials_rows):
    """YoY change in gross margin (GrossProfit/Revenue, pct points) -- see
    _margin_trend_series(). Candidate quality factor, Phase 6b
    (docs/https-finlab-finance-blog-python-get-tai-memoized-lighthouse.md);
    NOT part of RANKING_WEIGHTS/composite_scores() -- evaluate via
    factor_eval.evaluate_candidate_factor() first, see that function's
    docstring on why adoption is never automatic.

    Evaluated 2026-09 (docs/factor_candidates_momentum_ic_2026-09.md §1, 1821
    codes): IC t-stat below significance at h=20 and h=60 -> EXCLUDED; do not
    re-evaluate without new data."""
    return _margin_trend_series(financials_rows, "gross_profit")


def operating_margin_trend_series(financials_rows):
    """YoY change in operating margin (OperatingIncome/Revenue, pct points)
    -- see _margin_trend_series(). Same candidate-factor status as
    gross_margin_trend_series(), including the 2026-09 verdict: EXCLUDED (IC
    not significant, docs/factor_candidates_momentum_ic_2026-09.md §1)."""
    return _margin_trend_series(financials_rows, "operating_income")


def accrual_quality_series(financials_rows):
    """get_financials()'s extended [{date, net_income, total_assets,
    operating_cash_flow, ...}] (ascending, quarterly) -> [{usable_date,
    accrual_ratio}]. accrual_ratio = (net_income - operating_cash_flow) /
    total_assets -- simplified Sloan (1996) accrual: a company whose
    reported earnings run well above its actual operating cash flow has a
    larger non-cash ("accrual") component, which the accrual-anomaly
    literature finds predicts WORSE future returns (accruals partly reverse/
    earnings quality is lower), so this factor's raw sign is "higher
    accrual_ratio = worse", the opposite of every other factor in this
    module's "bigger number ranks higher" convention -- deliberately NOT
    flipped here (unlike value_series()'s -pbr) because this is still an
    unevaluated candidate (docs/institutional_and_accrual_factor_plan.md
    §2.3): the sign should be decided from the measured IC's sign, not
    guessed in advance the way value_series() could be (value/PBS's sign is
    settled finance theory; the accrual anomaly's sign in THIS market/
    universe is exactly what evaluate_candidate_factor() is for).

    Denominator uses the SAME quarter's total_assets (simplified, not the
    average of period-start/period-end total_assets some accrual papers
    use -- see plan doc's §2.3 "因子定義" on this being an intentional
    simplification to evaluate first, refine only if IC is significant).
    usable_date = quarter-end + FINANCIALS_LAG_DAYS, same disclosure-lag
    convention as roe_series()/_margin_trend_series() -- all three ride the
    same get_financials() quarterly rows. Skips a quarter missing
    total_assets, operating_cash_flow (both None on FinMind's response
    before docs/institutional_and_accrual_factor_plan.md §2.3, or on a row
    where that specific type happened to be absent -- see get_financials()'s
    docstring) or with non-positive total_assets."""
    out = []
    for r in financials_rows or []:
        total_assets, ocf = r.get("total_assets"), r.get("operating_cash_flow")
        if not total_assets or total_assets <= 0 or ocf is None:
            continue
        out.append({
            "usable_date": _shift_date(r["date"], FINANCIALS_LAG_DAYS),
            "accrual_ratio": (r["net_income"] - ocf) / total_assets,
        })
    return out


def value_series(per_pbr_rows):
    """data_sources.get_per_pbr()'s [{date, per, pbr}] (ascending) ->
    [{usable_date, value_score}]. value_score = -pbr so this factor's sign
    convention matches every other factor here (higher raw value = more
    attractive, fed straight into _pct_rank()) -- a LOWER price-to-book is
    the conventionally "cheaper" (better value) stock, so its sign is
    flipped. usable_date == date directly: FinMind's TaiwanStockPER series
    is already trading-date-indexed (see get_per_pbr()'s docstring on why
    no REVENUE_LAG_DAYS/FINANCIALS_LAG_DAYS-style lag is added here). Skips
    a row with no pbr. Candidate value factor, Phase 6b -- same
    evaluate-before-adopt status as the margin-trend functions above."""
    return [{"usable_date": r["date"], "value_score": -r["pbr"]}
            for r in per_pbr_rows if r.get("pbr")]


def institutional_net_by_date(finmind_rows):
    """FinMind TaiwanStockInstitutionalInvestorsBuySell rows -> {date:
    foreign + investment-trust net buy in shares}. Same name matching as
    data_sources.get_institutional() ("foreign" / "investment_trust"
    substrings, case-insensitive); dealer rows are left out (self-trade/
    hedging flow is noise for a "who is accumulating" signal)."""
    out = {}
    for r in finmind_rows:
        d = r.get("date")
        name = (r.get("name") or "").lower()
        if not d or not ("foreign" in name or "investment_trust" in name):
            continue
        out[d] = out.get(d, 0) + int(r.get("buy") or 0) - int(r.get("sell") or 0)
    return out


def institutional_net_ratio_series(net_by_date, bars, window=20):
    """[{usable_date, net_ratio}]: net buy (foreign + trust, see
    institutional_net_by_date()) summed over the trailing `window` bars,
    divided by those bars' summed volume -- "what share of recent volume did
    the big institutions net accumulate". A bar date missing from
    net_by_date counts as 0 net.

    usable_date is the NEXT bar's date, not the bar's own: the day's
    institutional totals are only published after the close, so treating them
    as known on the same date would be look-ahead (unlike value_series()'s
    price-derived PBR, which is genuinely knowable at that close). The last
    bar therefore yields no row. Skips windows with zero total volume."""
    nets = [net_by_date.get(b["date"], 0) for b in bars]
    vols = [b["v"] for b in bars]
    out = []
    net_sum = vol_sum = 0
    for i in range(len(bars) - 1):
        net_sum += nets[i]
        vol_sum += vols[i]
        if i >= window:
            net_sum -= nets[i - window]
            vol_sum -= vols[i - window]
        if i + 1 >= window and vol_sum > 0:
            out.append({"usable_date": bars[i + 1]["date"], "net_ratio": net_sum / vol_sum})
    return out


INSTITUTIONAL_FLOW_WINDOW = 5


def institutional_flow_series(inst_rows, bars, window=INSTITUTIONAL_FLOW_WINDOW):
    """data_sources.get_institutional()'s [{date, foreign, trust, dealer}]
    (ascending, net shares) + this code's own bars -> [{usable_date,
    inst_net_5d}]. inst_net_5d = trailing `window`-day net institutional
    buy/sell (foreign+trust+dealer summed) / trailing `window`-day average
    volume (shares) -- normalizing by volume so large- and small-cap codes'
    raw buy/sell magnitudes don't dominate the percentile rank, same idea as
    liquidity_ok()'s turnover window.

    usable_date = the next trading day AFTER the window's last flow date,
    not that date itself: TaiwanStockInstitutionalInvestorsBuySell is a
    same-day-close disclosure with an unconfirmed exact publish time (see
    docs/institutional_and_accrual_factor_plan.md §2.2's flagged open risk),
    so treating `date` as already-usable the way value_series() does for
    FinMind's intraday-updated PER series would risk look-ahead here.

    Skips a window where any date is missing from `bars` (a trading
    suspension on the institutional side without a matching bar, or the
    window's last date being the most recent bar with no next trading day
    yet) or has zero volume. Candidate factor, same evaluate-before-adopt
    status as value_series()/gross_margin_trend_series() above."""
    if not inst_rows or len(inst_rows) < window:
        return []
    bar_dates = [b["date"] for b in bars]
    vol_by_date = {b["date"]: b["v"] for b in bars}
    out = []
    for i in range(window - 1, len(inst_rows)):
        chunk = inst_rows[i - window + 1:i + 1]
        vols = [vol_by_date.get(r["date"]) or 0 for r in chunk]
        if any(v <= 0 for v in vols):
            continue
        net_sum = sum((r.get("foreign") or 0) + (r.get("trust") or 0) + (r.get("dealer") or 0) for r in chunk)
        avg_vol = sum(vols) / window
        idx = _bar_index_on_or_before(bar_dates, chunk[-1]["date"])
        if idx is None or idx + 1 >= len(bar_dates):
            continue
        out.append({"usable_date": bar_dates[idx + 1], "inst_net_5d": net_sum / avg_vol})
    return out


def _as_of(series, date, key):
    """Latest `series` entry with usable_date <= date, or None. series must
    be sorted ascending by usable_date (both revenue_yoy_series() and
    roe_series() preserve their input's ascending order)."""
    dates = [row["usable_date"] for row in series]
    idx = bisect.bisect_right(dates, date) - 1
    return series[idx][key] if idx >= 0 else None


def _bar_index_on_or_before(dates, date):
    """Index of the latest bar with date <= `date`, or None. `dates` is a
    bars list's date field pre-extracted by the caller so repeated lookups
    across many rebalance dates don't re-walk the list each time."""
    idx = bisect.bisect_right(dates, date) - 1
    return idx if idx >= 0 else None


def liquidity_ok(bars, idx, min_turnover=LIQUIDITY_MIN_TURNOVER, window=LIQUIDITY_WINDOW):
    """True if the `window`-bar average turnover (close*volume) ending at
    bars[idx] exceeds min_turnover. False if fewer than `window` bars
    precede idx."""
    if idx + 1 < window:
        return False
    chunk = bars[idx + 1 - window: idx + 1]
    avg_turnover = sum(b["c"] * b["v"] for b in chunk) / window
    return avg_turnover > min_turnover


def _close(bar):
    """bar["c_adj"] (split/dividend-adjusted) if present, else raw bar["c"]
    -- see this module's docstring on who populates c_adj and why."""
    c_adj = bar.get("c_adj")
    return c_adj if c_adj is not None else bar["c"]


def momentum_pct(bars, idx, days=MOMENTUM_DAYS):
    """`days`-bar percent return ending at bars[idx], or None if idx has
    fewer than `days` bars of history before it.

    Prefers bars[i]["c_adj"] (see this module's docstring), falling back to
    raw "c" for any bar that doesn't carry one -- phantom near a split/large
    stock dividend only for the latter."""
    if idx - days < 0:
        return None
    base = _close(bars[idx - days])
    if not base:
        return None
    return (_close(bars[idx]) - base) / base * 100


MOMENTUM_SKIP_DAYS = 21


def momentum_skip_pct(bars, idx, days, skip=MOMENTUM_SKIP_DAYS):
    """`days`-bar percent return ending `skip` bars BEFORE bars[idx] -- the
    "skip the most recent month" momentum variant (docs/factor_candidates_
    momentum_ic_2026-09.md §2: dropping the latest ~21 bars flips the
    short-term-reversal-contaminated sign). Same _close() (c_adj-first)
    convention as momentum_pct(); skip=0 reproduces it. None if there are
    fewer than days + skip bars of history, or the base close is 0."""
    j = idx - skip
    if j - days < 0:
        return None
    base = _close(bars[j - days])
    if not base:
        return None
    return (_close(bars[j]) - base) / base * 100


def mom60_skip21(bars, idx):
    return momentum_skip_pct(bars, idx, 60)


def mom120_skip21(bars, idx):
    return momentum_skip_pct(bars, idx, 120)


HIGH_PROXIMITY_DAYS = 250


def high250_ratio(bars, idx, window=HIGH_PROXIMITY_DAYS):
    """52-week-high proximity: close at bars[idx] divided by the highest close
    over the trailing `window` bars (idx inclusive), in (0, 1]; 1.0 = closing
    at the window high. Same _close() (c_adj-first) convention as
    momentum_pct(). None if fewer than `window` bars of history, or the
    window high is 0."""
    if idx + 1 < window:
        return None
    hi = max(_close(b) for b in bars[idx + 1 - window: idx + 1])
    if not hi:
        return None
    return _close(bars[idx]) / hi


def ma_cross_signal(bars, idx, short_window=5, long_window=20):
    """Short-window vs long-window moving-average cross, as a signed percent
    gap: (short_ma - long_ma) / long_ma * 100, using _close() (c_adj-first,
    same convention as momentum_pct()). Positive = short MA above long MA
    (recent price action running hotter than its own longer trend), higher
    is "better" -- same "bigger number ranks higher" convention every other
    factor in this module follows, so it drops straight into _pct_rank()/
    composite_scores() unmodified.

    Candidate factor for src/optimize.py's strategy-logic search (see
    EXTRA_FACTOR_FUNCS below) -- reads bars/volume already present on every
    universe_data entry, no extra fetch or cache-schema change needed
    (unlike gross_margin_trend_series()/value_series(), which need a
    separate {usable_date, value} series because of real disclosure-date
    lag; a moving-average cross has no such lag).

    None if idx has fewer than long_window bars of history before it
    (inclusive), or if long_ma computes to 0 (division would be undefined)."""
    if idx + 1 < long_window:
        return None
    short_ma = sum(_close(b) for b in bars[idx + 1 - short_window: idx + 1]) / short_window
    long_ma = sum(_close(b) for b in bars[idx + 1 - long_window: idx + 1]) / long_window
    if not long_ma:
        return None
    return (short_ma - long_ma) / long_ma * 100


def volume_price_divergence(bars, idx, window=20):
    """Price/volume-trend factor over a `window`-bar lookback, split into
    two branches so a plain price decline and a "price up but volume
    didn't confirm it" divergence don't collapse onto the same score range
    (an earlier single-multiplication draft would have scored "price down,
    volume down" the same as "price up, volume up" -- wrong for a long-only
    selection factor):

      price_trend_pct <= 0 (falling): score = price_trend_pct. Volume does
      not participate -- a falling stock belongs near the bottom of the
      ranking regardless of whether volume shrank alongside it.

      price_trend_pct > 0 (rising): score = price_trend_pct +
      volume_change_pct. Volume expansion adds to the score; volume
      contraction (volume_change_pct negative -- the classic "price up,
      volume didn't confirm" divergence) subtracts from it, but only ever
      as an additive discount off a positive base, never reusing the
      falling branch's formula.

    price_trend_pct = window-bar percent return ending at bars[idx] (same
    "days-bar return" shape as momentum_pct(), using _close()).
    volume_change_pct = the latest `window`-bar average volume's percent
    change versus the PRIOR `window`-bar average volume (the window
    immediately before it) -- needs 2*window bars of history; None if
    idx has fewer.

    Same candidate-factor status as ma_cross_signal() above -- reads bars/
    volume directly, no extra fetch/cache-schema change."""
    if idx + 1 < 2 * window:
        return None
    base_price = _close(bars[idx - window])
    if not base_price:
        return None
    price_trend_pct = (_close(bars[idx]) - base_price) / base_price * 100
    if price_trend_pct <= 0:
        # Falling branch never looks at volume at all -- check this BEFORE
        # summing recent/prior_chunk below, not after, so the O(window)
        # volume averages (needed only by the rising branch) aren't computed
        # and discarded on every falling-price code, which is most of the
        # universe's per-date, per-fold, per-candidate calls in a down
        # market.
        return price_trend_pct

    recent_chunk = bars[idx + 1 - window: idx + 1]
    prior_chunk = bars[idx + 1 - 2 * window: idx + 1 - window]
    recent_avg_vol = sum(b["v"] for b in recent_chunk) / window
    prior_avg_vol = sum(b["v"] for b in prior_chunk) / window

    if not prior_avg_vol:
        # No prior volume to compare against -- a percent CHANGE is
        # undefined (not "infinitely bullish"), so this bar can't be scored
        # rather than guessing a value that would silently distort the
        # ranking. Same "can't compute, return None" stance as this
        # function's own insufficient-history branch above.
        return None
    volume_change_pct = (recent_avg_vol - prior_avg_vol) / prior_avg_vol * 100
    return price_trend_pct + volume_change_pct


def value_pbr_factor(entry, date):
    """Latest value_series() score (-PBR) usable as of `date`, or None if the
    code has no PBR history yet (FinMind's TaiwanStockPER only goes back to
    ~2021-10, so earlier dates return None and, via factor_ranks()'s "every
    extra factor must be non-None" filter, drop the code).

    Unlike ma_cross_signal()/volume_price_divergence() this reads a separate
    per-code series rather than bars, so it takes the whole universe_data
    entry -- flagged with needs_entry below so factor_ranks() calls it as
    fn(entry, date) instead of fn(bars, idx). .get() default keeps a cache
    entry from before Phase 6b (no "value_series" key) scoring as None
    instead of raising."""
    return _as_of(entry.get("value_series") or [], date, "value_score")


value_pbr_factor.needs_entry = True

EXTRA_FACTOR_FUNCS = {
    "ma_cross": ma_cross_signal, "vol_price_div": volume_price_divergence, "value_pbr": value_pbr_factor,
    "mom60_skip21": mom60_skip21, "mom120_skip21": mom120_skip21,
    "high250": high250_ratio,
}

REQUIRED_FACTOR_KEYS = frozenset({"revenue", "roe", "momentum"})


def used_extra_factors(weight_dicts):
    """Subset of EXTRA_FACTOR_FUNCS whose key appears in at least one of
    `weight_dicts` (an iterable of {"revenue"/"roe"/"momentum"/"ma_cross"/
    "vol_price_div": weight} dicts, e.g. the several named RANKING_WEIGHTS
    candidates a walk-forward sweep is comparing this cycle).

    Passing THE WHOLE of EXTRA_FACTOR_FUNCS unconditionally into
    factor_ranks() would apply its "every extra factor must be non-None"
    exclusion filter to every code -- including for a caller whose weight
    dict(s) never reference ma_cross/vol_price_div at all, silently
    shrinking the eligible universe with no corresponding term in the
    score. That regression is exactly what this function exists to avoid:
    callers scoring against weight dict(s) that don't reference any extra
    factor get back {} (empty dict, same as omitting extra_factors
    entirely -- factor_ranks()'s own `extra_factors or {}` normalizes both
    the same way), so scripts/run_daily_recommendations.py's live output
    and scripts/run_ranking_optimize.py's monthly RANKING_WEIGHTS sweep
    stay byte-identical to their pre-strategy-logic-search behavior until a
    strategy-search candidate that actually uses one of these factors gets
    manually applied (scripts/apply_strategy_candidate.py)."""
    used_keys = {k for w in weight_dicts for k in w if k in EXTRA_FACTOR_FUNCS}
    return {k: EXTRA_FACTOR_FUNCS[k] for k in used_keys}


# Calendar days of pre-window history a factor needs when a simulation is
# sliced to start at a segment's own first date. 250 trading bars is about
# 350 calendar days; 400 leaves room for long holiday stretches. Only
# factors whose window outruns optimize.ROLLING_MOMENTUM_LOOKBACK_BUFFER_DAYS
# (120) are listed -- the rest are already covered by that default.
EXTRA_FACTOR_LOOKBACK_DAYS = {"high250": 400}


def extra_factor_lookback_days(weight_dicts, default):
    """Calendar-day buffer to slice ahead of a held-out segment so every
    extra factor in `weight_dicts` has its full window at the segment's
    first rebalance: the larger of `default` and the longest need among the
    factors those weights reference."""
    need = [EXTRA_FACTOR_LOOKBACK_DAYS[k] for w in weight_dicts for k in w if k in EXTRA_FACTOR_LOOKBACK_DAYS]
    return max([default, *need])


def _pct_rank(values):
    """{code: number} -> {code: percentile in (0, 1]}, average-rank ties
    (matches pandas .rank(pct=True), which the original FinLab strategy
    uses)."""
    ordered = sorted(values.items(), key=lambda kv: kv[1])
    n = len(ordered)
    ranks = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        avg_rank_pct = ((i + j) / 2 + 1) / n
        for k in range(i, j + 1):
            ranks[ordered[k][0]] = avg_rank_pct
        i = j + 1
    return ranks


def factor_ranks(universe_data, date, extra_factors=None, max_stale_days=None, dark_rank=None):
    """universe_data/date: same shape as composite_scores() below. Returns
    {code: {"revenue": pct, "roe": pct, "momentum": pct, ...}} -- the
    per-factor percentile ranks (see _pct_rank()) for every code that
    passes the liquidity filter and has all required factors available as
    of `date`. composite_scores() is a weighted sum of these numbers;
    factored out into its own function so a caller that wants the
    per-factor breakdown (e.g. a recommendation card's "why this stock
    ranked" detail, scripts/run_daily_recommendations.py) doesn't have to
    duplicate this filter+rank logic.

    extra_factors: optional {name: fn(bars, idx) -> float|None} dict (e.g.
    EXTRA_FACTOR_FUNCS above; a fn with .needs_entry = True is instead called
    as fn(universe_data entry, date), see value_pbr_factor()) -- lets src/optimize.py's strategy-logic
    search compare candidate factors that aren't part of the fixed
    revenue/roe/momentum trio without this function growing a parallel
    code path. None (the default) behaves exactly as before this
    parameter existed: no extra keys computed, no extra filtering, output
    unchanged bit-for-bit. When given, each code additionally needs every
    extra_factors value to be non-None to survive the filter -- same "all
    required factors present" rule the existing three already follow --
    and each extra factor gets its own independent percentile rank, merged
    into the per-code dict under its own key.

    max_stale_days: when set, a code whose latest bar on or before `date` is
    more than this many calendar days old is skipped (halted or delisted --
    liquidity_ok() only looks at the bars it is handed, so without this a
    code that stopped printing months ago keeps passing the filter and can
    occupy a top-N slot it can never be bought in). None (default) keeps the
    old behavior.

    dark_rank: stress-test hook for survivorship studies. When set, a code
    flagged entry["dark"] (price history but no usable revenue/ROE series,
    see scripts/fetch_delisted_factor_run.py) is not skipped for missing
    fundamentals: it joins the momentum ranking normally and gets this fixed
    percentile (0-1) for revenue and ROE. Only dark-flagged codes are
    treated this way (extra_factors values must still be non-None). None
    (default) leaves dark codes unrankable, like any code missing a factor."""
    extra_factors = extra_factors or {}
    raw = {}
    dark = set()
    for code, d in universe_data.items():
        bars, bar_dates = d["bars"], d["bar_dates"]
        idx = _bar_index_on_or_before(bar_dates, date)
        if idx is None or not liquidity_ok(bars, idx):
            continue
        if max_stale_days is not None and (
            dt.date.fromisoformat(date) - dt.date.fromisoformat(bar_dates[idx])
        ).days > max_stale_days:
            continue
        mom = momentum_pct(bars, idx)
        yoy = _as_of(d["revenue_series"], date, "yoy_pct")
        roe = _as_of(d["roe_series"], date, "roe_pct")
        if dark_rank is not None and d.get("dark") and (yoy is None or roe is None):
            dark.add(code)
            yoy = roe = 0.0  # placeholder, replaced by dark_rank below
        if mom is None or yoy is None or roe is None:
            continue
        extras = {
            name: fn(d, date) if getattr(fn, "needs_entry", False) else fn(bars, idx)
            for name, fn in extra_factors.items()
        }
        if any(v is None for v in extras.values()):
            continue
        raw[code] = {"momentum": mom, "yoy": yoy, "roe": roe, **extras}

    if not raw:
        return {}

    mom_rank = _pct_rank({c: v["momentum"] for c, v in raw.items()})
    # Dark codes are ranked out of the revenue/ROE percentile pools: their
    # placeholder values must not shift anyone else's rank.
    yoy_rank = _pct_rank({c: v["yoy"] for c, v in raw.items() if c not in dark})
    roe_rank = _pct_rank({c: v["roe"] for c, v in raw.items() if c not in dark})
    for code in dark:
        yoy_rank[code] = roe_rank[code] = dark_rank
    extra_ranks = {name: _pct_rank({c: v[name] for c, v in raw.items()}) for name in extra_factors}
    return {
        code: {
            "revenue": yoy_rank[code], "roe": roe_rank[code], "momentum": mom_rank[code],
            **{name: extra_ranks[name][code] for name in extra_factors},
        }
        for code in raw
    }


def composite_scores(universe_data, date, weights=None, ranks=None, extra_factors=None):
    """universe_data: {code: {"bars": [...], "bar_dates": [...],
    "revenue_series": [...], "roe_series": [...]}} (bar_dates = bars' date
    field pre-extracted, see _bar_index_on_or_before). Returns {code: score}
    for codes that pass the liquidity filter and have all required factors
    available as of `date` -- percentile ranks are computed within this
    survivor set (filter, then rank) via factor_ranks() above, then
    weighted-summed here as sum(r[k] * w[k] for k in r if k in w): every
    key `r` actually has a rank for AND `w` has a weight for contributes,
    everything else is silently skipped (e.g. a "baseline" weights dict
    with no "ma_cross" key just doesn't add an ma_cross term, no error).
    weights=None/extra_factors=None reduces `r` to exactly the three
    original keys, reproducing the pre-extra_factors sum bit-for-bit (same
    revenue+roe+momentum order, since dicts preserve insertion order).

    weights: optional override for the module-level RANKING_WEIGHTS -- lets
    a caller compare candidate weightings (e.g. src/optimize.py's
    sweep_ranking_weights_walk_forward(), Phase 5 applied to Phase 4's
    rank-IC-derived suggestions, and the strategy-logic search's named
    ma_cross/vol_price_div candidates) without mutating global state. None
    (the default) uses RANKING_WEIGHTS, unchanged behavior for every
    existing caller.

    ranks: optional pre-computed factor_ranks(universe_data, date) result --
    lets a caller that also needs the per-factor breakdown (e.g.
    scripts/run_daily_recommendations.py's recommendation cards) reuse the
    same liquidity-filter + percentile-rank pass instead of this function
    recomputing it. None (the default) computes it here via
    factor_ranks(universe_data, date, extra_factors=extra_factors),
    unchanged behavior for every existing caller.

    extra_factors: forwarded to factor_ranks() when `ranks` isn't already
    given -- see that function's docstring. Ignored (no effect) when
    `ranks` is passed directly, since factor_ranks() is never called in
    that case; the extra keys must already be in `ranks` for their weight
    to apply.

    This is NOT the same rank order as finlab_strategy.py's FinLab-backed
    ranking_position(), which ranks over the WHOLE fetched universe first
    and applies the liquidity/availability filter after
    (`composite.where(liquid & close.notna())`) -- percentile rank of the
    same liquid stock differs depending on whether the denominator is the
    full market or only the survivor set. This module can't reproduce
    rank-then-filter exactly anyway: scripts/run_factor_backtest.py's
    two-stage fetch already drops non-survivors before they ever reach
    universe_data, so a "whole universe" to rank against doesn't exist
    here. Treat the two strategies as comparable in spirit, not bit-for-bit
    identical rankings."""
    ranks = ranks if ranks is not None else factor_ranks(universe_data, date, extra_factors=extra_factors)
    if not ranks:
        return {}
    w = weights if weights is not None else RANKING_WEIGHTS
    return {
        code: sum(r[k] * w[k] for k in r if k in w)
        for code, r in ranks.items()
    }


def top_n(scores, n=RANKING_TOP_N):
    """[code, ...] of the top `n` scores, descending."""
    return [code for code, _ in sorted(scores.items(), key=lambda kv: -kv[1])[:n]]
