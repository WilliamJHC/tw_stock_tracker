# -*- coding: utf-8 -*-
"""Build a static snapshot (dist/) of the stock tracker for cloud hosting.

Run:  .venv\\Scripts\\python snapshot.py --mode full --out dist
"""
import argparse
import copy
import json
import os
import shutil
import sys
import time
from zoneinfo import ZoneInfo
import datetime as dt

import requests

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
        "data_tier": ds.data_tier(),
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
    codes = [s["code"] for s in watchlist]
    # build_watchlist_payloads() batches the whole watchlist together so it
    # can compute 5-day relative-strength ranking across stocks (an.rs_ranking()),
    # which a per-code build_stock_payload() call can't see on its own.
    stocks = pl.build_watchlist_payloads(codes)
    ok = sum(1 for v in stocks.values() if "error" not in v)
    fail = len(stocks) - ok

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


def load_base(base_file, base_url):
    """Fetch the previous snapshot to use as the light-mode base. Returns None on failure."""
    if base_file:
        try:
            with open(base_file, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError) as e:
            print(f"[snapshot] 讀取 --base-file 失敗：{e}", file=sys.stderr)
            return None
    if base_url:
        try:
            r = requests.get(base_url.rstrip("/") + "/data.json", timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"[snapshot] 下載 --base-url 失敗：{e}", file=sys.stderr)
            return None
    return None


def merge_light(base, quotes, now_iso):
    """Overwrite per-stock quote (and quote-carried name) only; set mode=light and
    quotes_updated_at=now_iso; keep full_generated_at / analysis / attention / chart untouched."""
    out = copy.deepcopy(base)
    out["mode"] = "light"
    out["quotes_updated_at"] = now_iso
    for code, stock in out.get("stocks", {}).items():
        if not isinstance(stock, dict) or "error" in stock:
            continue
        q = quotes.get(code)
        if q:
            stock["quote"] = q
            if q.get("name"):
                stock["name"] = q["name"]
    return out


def run_light(out_dir, base):
    watchlist = base.get("watchlist", [])
    quotes = {}
    ok, fail = 0, 0
    for s in watchlist:
        code = s["code"]
        try:
            q = ds.get_quote(code)
        except Exception:
            q = None
        if q:
            quotes[code] = q
            ok += 1
        else:
            fail += 1  # keep the stock's previous quote untouched

    payload = merge_light(base, quotes, now_iso())
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

    actual_mode = args.mode
    if args.mode == "full":
        ok, fail, total = run_full(out_dir)
    else:
        base = load_base(args.base_file, args.base_url)
        if base is None:
            print("[snapshot] 取不到 light 基底，改跑 full", file=sys.stderr)
            ok, fail, total = run_full(out_dir)
            actual_mode = "full"
        else:
            ok, fail, total = run_light(out_dir, base)

    elapsed = time.time() - t0
    print(f"[snapshot] mode={actual_mode} 成功={ok} 失敗={fail}/{total} 耗時={elapsed:.1f}s")

    if fail > total / 2:
        print(f"[snapshot] 失敗檔數過半（{fail}/{total}），視為失敗", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
