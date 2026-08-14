# -*- coding: utf-8 -*-
"""Reproduces FinLab's AI-agent-guide strategies and prints backtest stats.

Usage:
    python scripts/run_finlab_backtest.py [screening|ranking|all]

Requires FINLAB_API_TOKEN in .env -- see .env.example. Register a free
token at https://ai.finlab.tw or https://finlab.finance.
"""
import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)

import finlab_strategy  # noqa: E402


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    keys = list(finlab_strategy.STRATEGIES) if which == "all" else [which]

    finlab_strategy.login()

    for key in keys:
        report = finlab_strategy.run_backtest(key)
        print(f"\n=== {key} ===")
        print(report.get_stats())


if __name__ == "__main__":
    main()
