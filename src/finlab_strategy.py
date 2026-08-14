# -*- coding: utf-8 -*-
"""Reproduces the two chat-derived strategies from FinLab's AI-agent guide.

Source: https://finlab.finance/blog/ai-agent-quant-trading-guide

  - screening_position(): simple threshold filter (revenue YoY > 20% & ROE >
    10%, liquid names only). The article's first strategy.
  - ranking_position(): composite percentile-rank of revenue growth (40%) +
    ROE (30%) + 60-day momentum (30%), liquid names only, monthly-rebalanced
    top-N equal-weight. The article's best-performing strategy.

Deliberately separate from src/data_sources.py: FinLab is a distinct data
source and library (whole-market, pandas-based) with its own login, not an
extension of the FinMind/watchlist-scoped pipeline used by the rest of the
app -- see docs/optimization_plan.md for why the app otherwise stays
stdlib-only.
"""
import os

import env_utils
# finlab_factors.py is pure stdlib (no pandas/finlab import), so importing
# its constants here doesn't add a dependency -- keeps this module and the
# FinMind-native port in src/finlab_factors.py from drifting apart if a
# threshold is tuned in one place and not the other.
from finlab_factors import (
    LIQUIDITY_MIN_TURNOVER,
    MOMENTUM_DAYS as RANKING_MOMENTUM_DAYS,
    RANKING_TOP_N,
    RANKING_WEIGHTS,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_token():
    return env_utils.load_env_value("FINLAB_API_TOKEN", BASE_DIR)


def login():
    """Logs into FinLab using FINLAB_API_TOKEN from .env / the environment.

    Raises RuntimeError with a setup hint if no token is configured.
    """
    token = _load_token()
    if not token:
        raise RuntimeError(
            "FINLAB_API_TOKEN not set. Register a free token at "
            "https://ai.finlab.tw (or https://finlab.finance) and put it "
            "in .env -- see .env.example."
        )
    import finlab

    finlab.login(token)


def _load_factors():
    """Fetches the raw series shared by both strategies: close, revenue YoY
    growth, ROE (all reindexed to daily trading dates), and a liquidity mask."""
    from finlab import data

    close = data.get("price:收盤價")
    volume = data.get("price:成交股數")
    revenue_yoy = data.get("monthly_revenue:去年同月增減(%)")
    roe = data.get("fundamental_features:ROE稅後").index_str_to_date()

    rev_d = revenue_yoy.reindex(close.index, method="ffill")
    roe_d = roe.reindex(close.index, method="ffill")
    liquid = (close * volume).rolling(20).mean() > LIQUIDITY_MIN_TURNOVER
    return close, rev_d, roe_d, liquid


def screening_position():
    """Threshold-filter strategy: revenue YoY > 20% & ROE > 10%, liquid
    names only."""
    _, rev_d, roe_d, liquid = _load_factors()
    return (rev_d > 20) & (roe_d > 10) & liquid


def ranking_position(top_n=RANKING_TOP_N):
    """Composite multi-factor ranking strategy: revenue-growth rank * 0.4 +
    ROE rank * 0.3 + 60-day momentum rank * 0.3, liquid names only, top
    `top_n` equal-weighted."""
    close, rev_d, roe_d, liquid = _load_factors()
    rev_rank = rev_d.rank(axis=1, pct=True)
    roe_rank = roe_d.rank(axis=1, pct=True)
    mom_rank = close.pct_change(RANKING_MOMENTUM_DAYS).rank(axis=1, pct=True)

    composite = (
        rev_rank * RANKING_WEIGHTS["revenue"]
        + roe_rank * RANKING_WEIGHTS["roe"]
        + mom_rank * RANKING_WEIGHTS["momentum"]
    )
    return composite.where(liquid & close.notna()).is_largest(top_n)


STRATEGIES = {
    "screening": (screening_position, "篩選法"),
    "ranking": (ranking_position, "排名法多因子"),
}


def run_backtest(strategy_key):
    """Runs finlab.backtest.sim() for the named strategy ('screening' or
    'ranking') and returns the resulting Report."""
    from finlab.backtest import sim

    if strategy_key not in STRATEGIES:
        raise ValueError(
            f"unknown strategy: {strategy_key!r}, expected one of {sorted(STRATEGIES)}"
        )
    build_position, name = STRATEGIES[strategy_key]
    return sim(build_position(), resample="M", name=name)
