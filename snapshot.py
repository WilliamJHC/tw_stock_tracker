# -*- coding: utf-8 -*-
"""Build a static snapshot (dist/) of the stock tracker for cloud hosting.

Run:  .venv\\Scripts\\python snapshot.py --mode full --out dist
"""
import argparse
import json
import os
import shutil
import sys
import time
from zoneinfo import ZoneInfo
import datetime as dt

import data_sources as ds
import pipeline as pl

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TAIPEI = ZoneInfo("Asia/Taipei")

HEADERS_CONTENT = """/*
  X-Robots-Tag: noindex, nofollow, noarchive
/data.json
  Cache-Control: no-store
/
  Cache-Control: no-cache
/index.html
  Cache-Control: no-cache
"""


def now_iso():
    return dt.datetime.now(TAIPEI).isoformat()


def build_config():
    import analysis as an
    return {
        "score_bands": an.SCORE_BANDS,
        "score_labels": an.SCORE_LABELS,
        "action_thresholds": an.ACTION_THRESHOLDS,
        "chip_weights": an.CHIP_WEIGHTS,
    }


def assemble_site(out_dir):
    """Copy index.html + static assets and write _headers into out_dir."""
    os.makedirs(out_dir, exist_ok=True)
    shutil.copy2(
        os.path.join(BASE_DIR, "static", "index.html"),
        os.path.join(out_dir, "index.html"),
    )
    dist_static = os.path.join(out_dir, "static")
    os.makedirs(dist_static, exist_ok=True)
    for name in ("manifest.json", "icon.png"):
        shutil.copy2(os.path.join(BASE_DIR, "static", name), os.path.join(dist_static, name))
    with open(os.path.join(out_dir, "_headers"), "w", encoding="utf-8") as f:
        f.write(HEADERS_CONTENT)


def run_full(out_dir):
    watchlist = pl.load_watchlist()
    stocks = {}
    ok, fail = 0, 0
    for s in watchlist:
        code = s["code"]
        try:
            stocks[code] = pl.build_stock_payload(code)
            ok += 1
        except Exception as e:
            stocks[code] = {"error": str(e)}
            fail += 1

    ts = now_iso()
    payload = {
        "version": 1,
        "mode": "full",
        "full_generated_at": ts,
        "quotes_updated_at": ts,
        "tick_tier": ds.tick_tier_available(),
        "config": build_config(),
        "watchlist": watchlist,
        "stocks": stocks,
    }

    assemble_site(out_dir)
    with open(os.path.join(out_dir, "data.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

    return ok, fail, len(watchlist)


def main():
    parser = argparse.ArgumentParser(description="產生 stock_tracker 靜態快照")
    parser.add_argument("--mode", choices=["full", "light"], required=True)
    parser.add_argument("--out", default="dist")
    parser.add_argument("--base-url", default=None, help="light 模式：從此網址取上次快照當基底")
    parser.add_argument("--base-file", default=None, help="light 模式：從此本機檔案取上次快照當基底")
    args = parser.parse_args()

    out_dir = os.path.join(BASE_DIR, args.out) if not os.path.isabs(args.out) else args.out
    t0 = time.time()

    if args.mode == "full":
        ok, fail, total = run_full(out_dir)
    else:
        sys.exit("light 模式尚未實作（見計畫書 Phase 2）")

    elapsed = time.time() - t0
    print(f"[snapshot] mode={args.mode} 成功={ok} 失敗={fail}/{total} 耗時={elapsed:.1f}s")

    if fail > total / 2:
        print(f"[snapshot] 失敗檔數過半（{fail}/{total}），視為失敗", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
