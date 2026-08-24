# stock_tracker 優化計畫書

## Context

起因是讀 finlab 的兩篇文章（[台股資料 API 比較](https://finlab.finance/blog/python-get-taiwan-stock-data)、[量化交易平台](https://finlab.finance/tools/quant-trading)），文章點出兩個「自建資料的隱藏成本」：**除權息還原**與**前視偏差**。對照本專案後，發現前者是真實且嚴重的缺口，後者則已經處理得不錯（`finlab_factors.py` 的 `REVENUE_LAG_DAYS=10` / `FINANCIALS_LAG_DAYS=60` 是正確的保守設計，不需要動）。

在規劃過程中對 DB 現有 10 年 × 23 檔資料做了實測，發現三件事重新排定了優先序：

1. **FinMind `TaiwanStockPrice` 完全未做任何還原——連股票分割都沒有。** `0050` 在 2025-06-18 的收盤價從 188.65 掉到 47.57（1:4 分割），DB 裡是一根 **-74.8%** 的假 K 棒；`2327` 在 2024-08-15 是 -16.5%（純股票股利）。23 檔 10.7 年共 281 個公司行動日，其中 >15% 的假跳空 4 個、10–15% 11 個、7–10% 20 個。這不是 3~5% 的配息漂移偏誤，是資料斷裂。`2330` 實測 raw 總報酬 +1645.5% vs 還原後 +2213.0%（起點因子 0.7546，**低估 32.5%**）。

2. **單檔擇時策略結構上輸給買進持有，且不是參數問題。** 實測 `mean strategy 177.3%` vs `mean buy&hold 2520.4%`，22/23 檔落敗，時間在市場僅 33.5%。`exit_composite` / `atr_mult` / `max_hold_days` 的整個參數空間都掃過，沒有任何一組能接近買進持有（`atr_mult=100`，即不停損，反而優於預設的 2.0——193 筆 ATR 停損全部虧損，平均 -8.29%）。因此「先建參數最佳化器」是錯的下一步。

3. **watchlist 有嚴重倖存者偏誤。** 23 檔平均買進持有 +2520%、中位數 +1035%——這是一份後見之明挑出來的十年大贏家清單。在上面最佳化任何策略都是在對倖存者最佳化。**擴大 universe 的首要理由是消除這個偏誤，不是統計效力。**

但參數調校並非無效：`exit_composite` 從 -10 移到 -40 附近，在 walk-forward 4/4 個 fold 都勝過預設（平均樣本外 294.5% vs 153.1%），且 -60~-30 是一片穩定高原（各 fold argmax 跳動於 -50/-50/-50/-30，但高原位置穩定）。

**預期成果**：一組可信的評估基準線 → 正確的價格序列 → 跑得動全市場的效能 → 誠實定位的策略 → 無倖存者偏誤的因子驗證 → 有防過擬合設計的最佳化框架。

### 已確認的決策

| 議題 | 決定 |
|---|---|
| 還原股價 | **自建，零月費**（TWSE 官方表 + Yahoo 比值，雙來源交叉驗證） |
| 依賴政策 | **維持 stdlib-only**，改用兩項架構重構取代 numpy |
| Qlib / QuantConnect | **都不導入**，以 ~180 行自建 rank-IC 因子評估取代 |
| universe | **先擴到全市場再做最佳化** |
| 單檔擇時策略 | **重新定位為擇時/風控輔助**，不以取代買進持有為目標 |
| finlab 帳號 | 免費版（資料到 2020 年底）→ 當作 2020 年前還原因子的第三方校驗工具 |

### 假設（未經使用者確認，如有誤請指出）

- `docs/` 底下新增計畫書沿用現有慣例（`.md`、完成後移入 `docs/archive/` 並更新 `INDEX.md`）。
- 全市場抓取可以接受一次隔夜長跑（~22 小時，可續跑）。
- 不自行 push、不開 PR；各 Phase 完成後 commit 到本地並回報 branch 與 hash。

---

## Phase 0：評估基準線（無網路、無風險）

不抓任何資料，DB 裡的 10 年 × 23 檔（58,709 筆）已足夠。目的是先有一把可信的尺，之後每個 Phase 都用它量。

**新增 `src/metrics.py`（純 stdlib）**
把目前重複在 `src/backtest.py` 與 `src/portfolio_backtest.py:155` 的 `_compute_metrics` 抽出來共用，並補上目前沒有的指標：CAGR、Sharpe、Sortino、max drawdown、**exposure（時間在市場）**、turnover、rank-IC、Deflated Sharpe Ratio、PBO。全部只需 `math`（`math.erf` 做常態 CDF、牛頓法做反函式）、`statistics`、`itertools`。

**新增 `scripts/report_backtest.py`**
一張表列出每檔的 策略 vs 買進持有 vs exposure，以及依出場原因分解的報酬。**把「vs 買進持有」與 exposure 升級為主要輸出**，讓 Phase 3 的結論無法被忽略。

### 驗證
`python scripts/report_backtest.py` 應重現以下已實測的數字（可直接寫成回歸測試的期望值）：
- `mean strategy 177.3%` / `mean buy&hold 2520.4%` / `beat 1/23`
- `composite_exit n=677 win=39.9% median -1.51%`
- `stop_loss n=193 win=0.0% mean -8.29%`
- `max_hold n=45 win=100.0% mean +50.09%`
- `exposure 33.5%`

風險：無（純新增，不動既有檔案）。

---

## Phase 1：還原股價與資料清洗

### 1a. Schema migration（不需重抓 16.8MB）

SQLite 的 `ALTER TABLE ADD COLUMN` 帶常數 DEFAULT 是 O(1) metadata-only 操作，不重寫既有 58,709 筆 row。在 `src/db.py` 新增 `migrate()`：

```sql
ALTER TABLE daily_bars ADD COLUMN adj_factor REAL;   -- NULL = 尚未計算

CREATE TABLE IF NOT EXISTS corporate_actions (
    code TEXT NOT NULL,
    date TEXT NOT NULL,       -- 除權息交易日
    pre_close REAL,           -- TWSE 官方欄位：除權息前收盤價
    ref_price REAL,           -- TWSE 官方欄位：除權息參考價
    ratio REAL NOT NULL,      -- ref_price / pre_close
    kind TEXT,                -- 'div' | 'right' | 'split'
    source TEXT,              -- 'twse' | 'yahoo_ratio'
    PRIMARY KEY (code, date)
);
```

**設計決策：保留原始價，另存因子。** `o/h/l/c` 維持 raw 不動（下單、`get_quote()`、前端 K 線看的都是原始價）。`adj_factor[i]` 定義為「`close_raw[i] × adj_factor[i]` = 今日尺度的還原價」，最近一段固定為 `1.0`，所以今天的還原價 == 原始價，現有行為零影響。純函式層只讀 float，不知道因子怎麼來。

驗證定義正確性：`2330` 起點因子應 ≈ `0.75461`，且 `17.455 / 0.75461 = 23.13` → +2213%，與實測相符。

### 1b. 還原因子的雙來源（推薦組合）

**主來源：TWSE 除權除息計算結果表**（上市）
`https://www.twse.com.tw/rwd/zh/exRight/TWT49U?startDate=YYYYMMDD&endDate=YYYYMMDD&response=json`
一個 request 可查整年（實測 `20250101-20251231` → `stat=OK, 1358 rows`），**11 年只要 11 個 request**，UTF-8 無需轉碼。直接給 `除權息前收盤價` 與 `除權息參考價` 兩個官方欄位，`ratio = ref/pre` 就是標準比率，不必自己推導。最權威、可稽核。

**補來源：Yahoo `Adj Close` ÷ FinMind 原始收盤價**（上櫃 + TWSE 漏網）
分子全還原、分母全未還原，比值 = 完整累積調整因子。實測 `2327` 起點 0.24939（正確編碼 4 倍累積分割）、`2881` 0.48227、`2330` 0.75461、`2317` 0.73636。

> ⚠ **不要用 `Adj Close ÷ Close`**：Yahoo 的 `Close` 欄位即使帶 `auto_adjust=False` 也**已經還原過分割**，該比值只含股利、不含分割，會留下 -16.5% 的斷裂。
> ⚠ **不要用 Yahoo 的 `Stock Splits` / `Dividends` event 欄位**：實測漏記 `0050` 2025-06 與 `2327` 2025-08 的分割（欄位為 `0.000`，但其 `Close` 序列卻已還原）。

**為什麼需要兩條**：`www.tpex.org.tw`（櫃買）的 SSL 憑證不合規（`certificate verify failed: Missing Subject Key Identifier`），四個候選端點全部連不上，只能 `verify=False`（不採用）。23 檔有 3 檔上櫃（5274 / 4979 / 3081），全市場約一半。Yahoo 比值法補上這塊。

**免費的副產品**：比值序列在無公司行動日**應為逐段常數**。任何非公司行動日的階梯 = FinMind 與 Yahoo 對該日價格不一致。實測抓到 **115 筆日報酬差異 >2pp**（>50pp 2 筆、20–50pp 6 筆、5–20pp 39 筆），集中在上櫃／小型股：`3081`(23) `4979`(13) `6869`(13) `8028`(12) `5274`(10) `2327`(9)。**跨來源交叉驗證不必另外做，它是還原因子計算的自然副產品。**

### 1c. 下游使用規則（重要陷阱）

`liquidity_ok()`（`src/finlab_factors.py:120`）用 `b["c"] * b["v"]` 算成交金額。若餵還原價配原始量，成交金額會被乘上 factor → `0050` 分割前被低估近 4 倍，流動性過濾直接失準。

**規則：報酬／動能／ATR／volume_profile 用還原價；成交金額與流動性過濾用原始價 × 原始量。** bars 同時攜帶 `c`（raw）、`adj_factor`、`c_adj`（衍生），呼叫端明確選用。這條要寫進 `src/adjust.py` 的 docstring 並加測試。

### 1d. 檔案變更

| 檔案 | 動作 | 重點 |
|---|---|---|
| `src/adjust.py` | **新增** | 純 stdlib：由 `corporate_actions` 累積 `adj_factor`、套用到 bars、驗證逐段常數 |
| `scripts/build_adj_factors.py` | **新增** | TWSE 抓取 + Yahoo 比值 + 交叉告警 + 寫 DB（離線批次） |
| `src/db.py` | 改 | `migrate()`、`corporate_actions` 表、`adj_factor` 欄；修 `:2`「2-year」與 `:11` 的失效路徑註解 |
| `src/data_sources.py` | 改 | **修 `:268` 硬寫的 `period="2y"`**（`days` 參數目前被完全忽略，FinMind 掛掉時 10 年靜默退回 2 年）；`get_daily()` 回傳 `adjusted` 旗標 |
| `src/run_backtest.py` | 改 | replay 與 simulate 改吃還原序列；修 `:2` 失效路徑註解 |
| `tests/test_adjust.py` | **新增** | 沿用 `tests/test_data_sources.py` 慣例（`patch("data_sources.requests.get", ...)` + `monkeypatch.setattr(ds, "CACHE_DIR", tmp_path)`） |

**政策合規關鍵**：Yahoo 路徑會拉進 pandas，所以**還原因子的抓取放在 `scripts/build_adj_factors.py`（離線批次），不放 `src/`**。它把結果寫進 SQLite，`src/adjust.py` 只從 DB 讀 float。`src/` 永遠不需要 yfinance 做還原，stdlib-only 完整保留。

### 驗證
1. `pytest` 維持全綠（基準 **253 passed / < 2s**，全 mock 無網路）
2. `python scripts/build_adj_factors.py --verify` → 約 281 個公司行動、約 115 筆跨來源告警
3. **不變量測試（防「算錯比不算更糟」）**：最後一段 factor 必須 == 1.0；factor 必須單調非遞增且逐段常數；階梯只允許出現在 `corporate_actions` 的日期上
4. 單點回歸：`2330` 起點 factor ≈ 0.7546；`0050` 在 2025-06-18 前後還原序列連續（raw 有 -74.8% 斷裂）；`2327` 的 `momentum_pct` 在 2024-08-15 從 -16.5% 變 ≈0%
5. **第三方校驗**：用免費 finlab 帳號的 `etl:adj_close` 比對 2020 年底以前的自建因子（免費版正好涵蓋這段）
6. 重跑 `scripts/report_backtest.py`：`mean buy&hold` 應從 2520.4% 上升約 32%，`beat 1/23` 不變或更差
7. 缺漏 bar（`2327` 缺 35 根、`6442` 缺 20 根，集中在公司行動停牌期）**只標記進 `meta`，不自動內插**——`momentum_pct(days=60)` 與 `max_hold_days` 都數 bar 數而非日曆天數，內插會製造假資料

風險：**中**。最大風險是因子算錯會憑空製造報酬；以上 3 道不變量 + 雙來源交叉 + finlab 第三方校驗共四道防線緩解。

---

## Phase 2：效能重構（全市場的前置條件）

這是「維持 stdlib-only」這個決策的代價與對價：不引入 numpy，改用兩項架構修正拿到高一到兩個數量級的效益。

### 2a. `replay_scores()` 的 O(n²) → O(n)

`src/backtest.py:159-175` 對每根 bar 把 `daily` 切成 `[:i+1]` 再呼叫 `analyze()`，而 `analyze()`（`src/analysis.py:791`）內部 `build_backtest_ctx(daily)` 把 MA5/MA20/MA60/vol20/quadrant **從整段前綴重算一遍**。10 年 ≈ 2500 根 bar → 每檔約 310 萬次 × 十幾個指標。全市場 1800 檔在純 Python 下要數小時。

**做法**：`build_backtest_ctx()` 對完整歷史算一次，`analyze()` 新增選用參數 `ctx=None, at=None`——有給就跳過重建、直接讀索引 `at`。對 `src/pipeline.py`、`src/app.py` 的即時單檔呼叫完全向後相容（不傳就是現行行為）。

**不變量**：重構後 `analyze()` 對同一輸入必須產生**逐位元相同**的分數。用 Phase 0 的基準線當 golden master 測試。

### 2b. 快取九個分項的未加權原始值

`src/analysis.py:796-856` 是 `score += 各分項 * w[key]` 邊算邊累加，**九個分項的原始值沒有被保留**。所以現在調 `SCORE_WEIGHTS` 必須整段重跑 replay。

**做法**：`analyze()` 額外回傳 `result["components"] = {"maPosition": ..., "maAlign": ..., "volumePrice": ..., "trend": ..., "vpPosition": ..., "orderFlow": ..., "gapTracker": ..., "donchianPosition": ..., "rsRanking": ...}`（未加權值），並讓 `score = sum(components[k] * SCORE_WEIGHTS[k])`。`replay_scores()` 把它們寫進 `score_history` 的 9 個新 REAL 欄位（`ALTER TABLE ADD COLUMN` × 9，各 O(1)）。

用 9 個獨立欄位而非 JSON：全市場 1800 × 2380 row 若要 JSON 解析會成為新瓶頸；獨立欄位讓權重搜尋退化成一次點積。

**效益**：調 `SCORE_WEIGHTS` 從「重跑 replay（數小時）」變成「讀欄位做點積」。而 `portfolio_backtest` 是月再平衡，只需要 ~120 個再平衡日的分數（不是 2380 根 bar）→ 1800 × 120 × 9 ≈ 200 萬次乘加，純 Python 一兩秒。**這正是 numpy 沒有必要的原因。**

涉及檔案：`src/analysis.py`（`build_backtest_ctx`、`analyze`）、`src/backtest.py`（`replay_scores`）、`src/db.py`（schema + `upsert_score_history`）、`tests/test_analysis.py` / `tests/test_backtest.py` 補 golden-master。

### 驗證
- `pytest` 全綠，且新增 golden-master 測試：重構前後 `analyze()` 分數逐位元相同
- `sum(components[k] * SCORE_WEIGHTS[k]) == result["score"]` 的恆等測試
- 實測單檔 `replay_scores()` 的 wall-clock 前後對比，記錄加速倍數（預期 100× 量級）

風險：中。`analyze()` 是即時 dashboard 的核心路徑，但 golden-master 不變量測試讓迴歸可偵測。

---

## Phase 3：策略定位修正

**不做搜尋**，只落地已驗證的結論，並把單檔擇時引擎誠實地重新定位。

1. **`exit_composite` 預設 `-10` → `-40`**（`src/backtest.py:20`）。walk-forward 4/4 fold 都勝出（平均 OOS 294.5% vs 153.1%），且 -60~-30 是穩定高原。**採用高原中心 -40，不採用全樣本 argmax -30**（307.8% 是高原 + 雜訊，各 fold argmax 跳動）。
2. **`max_hold_days` 60 → 120**。掃描顯示 120 之後飽和（60→177.3%、90→183.6、120→188.4、250→188.7）。
3. **`atr_mult` 重新評估**。193 筆 ATR 停損勝率 0%、平均 -8.29%，且不會 trail；`atr_mult=100`（等於不停損）的 184.7% 優於預設 2.0 的 177.3%。改成 trailing stop 或直接關閉，**並在 docstring 記錄理由與證據**。
4. **重新定位**：在 `src/backtest.py` 與 `README.md` 明確寫出「本引擎的用途是為 dashboard 產生進場/減碼警示與風控輔助，**不以取代買進持有為目標**」，並附 Phase 0 的實測數字。alpha 期望轉移到多因子投組引擎（`src/portfolio_backtest.py` + `src/finlab_factors.py`）在 Phase 4 的全市場規模上驗證。
5. 補 `src/portfolio_backtest.py` docstring 自承的缺口：整股 lot rounding（1000 股）與 slippage。
6. 修 `docs/archive/INDEX.md:5` 指向 `../backtest_plan.md` 的失效連結。

### 驗證
重跑 `scripts/report_backtest.py`：`mean total_return` 應從 177.3% 升到 300% 量級。`beat_buy&hold` 大概仍是 1–3/23——**這個結果要誠實寫進報告，不要靠調參去追**。

風險：低（都走既有 `simulate(params=...)` 覆寫路徑）。

---

## Phase 4：擴大 universe 到全市場（消除倖存者偏誤）

### 抓取成本（實算，~2,162 檔）

| 資料 | requests | FinMind 免費 600/hr | 富果 3600/hr |
|---|---|---|---|
| 日線 | 2,162 | 3.6 小時 | **36 分鐘** |
| 月營收 | ~800 | 1.3 小時 | 不支援 |
| 財報 + 資產負債 | ~1,600 | 2.7 小時 | 不支援 |
| 籌碼 4 個 dataset | ~8,648 | 14.4 小時 | 不支援 |
| **合計** | ~13,200 | **~22 小時** | 混合後 ~19 小時 |

**富果只加速日線那一段（省 14%），不是瓶頸解方。** `scripts/run_factor_backtest.py:40` 的 `_RateLimiter`（滾動 1 小時 leaky bucket）與 per-code JSON cache 已經讓長跑可續跑，所以一次隔夜工作是可接受的。

### 富果 Fugle：接，但只接一個位置

在 `src/data_sources.py` 新增 `_fugle_daily()`，插在 **FinMind 之後、yfinance 之前**，作為「FinMind 回 429/402 時的長區間 fallback」。它有 2010 年起的上市櫃資料，能真正接住 10 年區間——這正好補上 Phase 1 修 `period="2y"` 之後剩下的缺口。免費方案 60 req/min、不需開戶。

> ⚠ **不要為了接富果而降級 yfinance**：yfinance 是唯一免費提供完整還原序列的來源（Phase 1 的因子基礎設施），降級它會弄壞還原因子。

### 自建 rank-IC 因子評估（取代 Qlib）

Qlib 真正有價值的是 rank-IC 因子評估 + 滾動重訓，純 stdlib 約 180 行即可（複用 `src/finlab_factors.py:142` 現成的 `_pct_rank()`——Spearman 就是對兩組 pct_rank 算 Pearson）：

- rank-IC（每個再平衡日：因子排名 vs 未來 N 日報酬排名）
- IC 的 t-stat 與 ICIR（IC 均值 / IC 標準差）
- 因子衰減曲線（IC 對 horizon 1/5/20/60 日）
- 滾動重估 `RANKING_WEIGHTS`

這直接回答**「營收 YoY 0.4 / ROE 0.3 / 動能 0.3 這組權重是不是拍出來的」**——那是目前最缺證據支撐的一組數字（`src/finlab_factors.py:25`）。

### 驗證
- `python scripts/run_factor_backtest.py --universe all` 完成，DB 涵蓋檔數與日期區間寫進 `meta`
- 全市場的 `mean buy&hold` 應**遠低於** watchlist 的 2520%（若仍接近，代表 universe 過濾有問題）——這是倖存者偏誤已消除的直接證據
- 每個因子的 rank-IC、ICIR、衰減曲線出報告；IC 的 t-stat 未達顯著的因子要標記出來
- `2327` 等公司行動大戶的還原序列在全市場規模下仍通過 Phase 1 的不變量測試

風險：中（22 小時抓取 + 大量新資料的品質未知）。緩解：續跑機制已有；先跑 100 檔抽樣驗證品質再放全量。

---

## Phase 5：自我優化框架

只有在 Phase 4 之後才建，而且要建成「防止自欺」的工具，不是「找最佳解」的工具。

**新增 `src/optimize.py`（純 stdlib）**

- **搜尋：coordinate descent + random restart，不用 full grid。** 4 個策略參數 + 3 個因子權重 + 9 個分項權重，full grid 不可行；coordinate descent 每輪只動一維（O(k·v) 而非 O(v^k)），試驗次數少一個數量級——而 DSR 的懲罰項隨試驗次數 N 增長，**少試就是少罰**。random restart 防局部解。
- **切樣本：walk-forward（anchored + rolling）為主，purged K-fold + embargo 為必要條件。** `max_hold_days` 最長 120 根 bar，訓練/測試邊界必須 purge ≥120 根，否則同一筆交易橫跨兩折 = 洩漏。這在本專案是必要的，不是學術裝飾。
- **懲罰試驗次數**：Deflated Sharpe Ratio（`math.erf` 做 Φ、牛頓法做 Φ⁻¹，γ = 0.5772）；PBO via CSCV（S=8 區塊、`itertools.combinations` 取 C(8,4)=70 組）。
- **回報高原而非單點**：輸出 `{plateau: {range, center, width_frac, fold_argmax, in_plateau_std}, point_best: {..., WARNING}, dsr, pbo, n_trials, vs_buy_hold}`。
- **判定規則**：高原寬度 < 搜尋範圍 20% → 拒絕（視為過擬合尖峰）；PBO > 0.5 → 拒絕；DSR < 0.95 → 標記未達顯著。**永遠附上「與買進持有的差距」。**

配套：`scripts/run_optimize.py`；`src/db.py` 加 `optimization_runs` 表記錄每次試驗（含真實累計 `n_trials`，含被丟棄的試驗，讓 DSR 可事後重算）；`tests/test_optimize.py`。

> ⚠ **不要對 11 個 `BACKTEST_SIGNALS` 做 2^11-1 = 2047 組合掃描**。

### 驗證
用已實測的 walk-forward 結果當黃金測試——框架跑 `exit_composite` 應回報「高原 -60~-30、各 fold argmax [-50,-50,-50,-30]、OOS 平均 294.5% vs 預設 153.1%」。這是可重現的期望值。

風險：**中高**。最大風險是框架本身變成過擬合的加速器。緩解：`n_trials` 強制寫進 DB 且 DSR 必須用真實累計試驗數。

**2026-08-18 補充：完整 16 維（4 策略參數 + 3 因子權重 + 9 分項權重）搜尋，真實資料跑過一次。** 因子權重（RANKING_WEIGHTS）維持用具名候選比較（見上方 `sweep_ranking_weights_walk_forward()`），不納入連續搜尋——Phase 4 的 IC/ICIR 顯著性檢定已經對每個因子做過「有沒有真訊號」的把關，對權重再做連續搜尋只是在同一批雜訊上重學一次。剩下 13 維（`STRATEGY_PARAMS` 4 個 + `SCORE_WEIGHTS` 9 個，都在單檔引擎內）用 `coordinate_descent()` 從現況出發 + 1 次 `random_restart()`，在 8 檔 watchlist 子集合上做 in-sample 搜尋（159 次試驗），找到一組候選（`exit_composite=-60, atr_mult=100, max_hold_days=90, fee_pct=0.3`，分項權重小幅重分配、maPosition/orderFlow 略增）。

用全部 23 檔 watchlist 做 purged walk-forward（4 fold、purge=120 根 bar）驗證這組候選 vs 現況：OOS 平均報酬 338.5% vs 322.8%（88 個可用 code×fold 樣本，勝 48/88=54.5%），DSR=1.00（過門檻），但 **PBO=0.91，遠超過 0.5 門檻——框架判定 reject**。結論：**不採用**這組搜尋出來的候選，維持現有 `STRATEGY_PARAMS`/`SCORE_WEIGHTS`。這正是框架設計要抓的情況——即使 DSR 與勝率都過關，PBO（CSCV 對 159 次試驗的過擬合機率估計）仍能單獨否決，說明 in-sample 搜出來的「改善」大機率是雜訊而非真訊號。兩個候選在這份倖存者偏誤 watchlist 上都遠輸買進持有（均值 2181.7%），與 Phase 0/3 的結論一致，未被此次搜尋推翻。

範圍簡化（因時間成本記錄，非隱藏）：搜尋階段用 8 檔子集合（而非全 23 檔）以控制單次 `replay_scores()` 全歷史重算的耗時（SCORE_WEIGHTS 改變會使分數改變，每個候選權重向量都要重跑一次 replay，23 檔 × 3.8s/檔 對 coordinate descent 的試驗量而言太貴）；OOS 驗證階段換回全 23 檔。此驗證是一次性腳本（`$CLAUDE_JOB_DIR/tmp`，未進 repo），複用 `src/optimize.py` 既有的 `coordinate_descent`/`random_restart`/`purged_walk_forward_splits`/`evaluate_decision`/`metrics.pbo`/`metrics.deflated_sharpe_ratio`，未新增 src 程式碼——這些通用元件已足夠支撐這次驗證，不需要為了跑一次而新增永久 CLI。

---

## Phase 6：全市場因子擴充與自動化最佳化管線

延續 Phase 5 的結論：8/18 那次搜尋正確地用 PBO 否決了單股引擎的過擬合候選，維持原設定——`optimize.py` 這套「防自欺」框架本身沒有問題。這個 Phase 把「持續優化」的對象換成 Phase 5 自己標記為「最缺證據支撐」的那組數字：`finlab_factors.RANKING_WEIGHTS`（營收YoY 0.4 / ROE 0.3 / 動能 0.3）背後的候選因子集合太窄，而且評估這些因子的動能序列本身還帶著已知偏誤。

**範圍界定（2026-08-24 grill-me 收斂）**：這是純內部研究管線。`portfolio_backtest.py` 至今沒有被 `app.py` 引用過一次，選出的 `top_n` 清單目前沒有、這次也刻意不建任何出口送到網頁／Telegram——出口留給未來另一個決策。本 Phase 的 Telegram 通知只是「這次排程套用/拒絕/rollback 了什麼」的事件摘要，不是選股清單。

### 6a：全市場還原因子（`momentum_pct()` 的前置修復）

**必須排在 6b 之前**：`finlab_factors.momentum_pct()` 目前吃 `ds.get_daily()` 回傳的未還原收盤價，遇到除權息會算出假動能（模組 docstring 已記錄這個已知限制）。新因子的 rank-IC 評估如果跑在這份資料上，連帶把動能偏誤混進評估結論，順序反了等於做兩次工。

- `scripts/build_adj_factors.py` 的還原邏輯（Yahoo `Adj Close` 除以 FinMind raw close 比值，`compute_yahoo_ratio_series()`/`detect_steps()`/`adjust.build_adj_factor()`）已經在 Phase 1 驗證過正確性（`0050` 2025-06-18 1:4 分割、`2327` 純股票股利兩個不變量測試案例），且已經跑在 `backtest.db` 的 `daily_bars.adj_factor` 欄位上，但那是 watchlist 規模（23 檔）。
- 把 `build_code()` 的迴圈擴大到 `scripts/run_factor_backtest.py --universe all` 的全市場清單（約 2000 檔以上，Phase 4 已完成的抓取範圍，2148/2148 已知數字）；`fetch_universe_data()` 組 `bars` 時額外套用 `adj_factor`，讓 `momentum_pct()` 收到還原後的 close 序列。`liquidity_ok()` 維持吃原始價（`adjust.py` 既有規則，不受影響）。
- 抓取成本：不需要重跑 Phase 4 的日線抓取本身（bars 已在快取/DB 裡），只需要對每檔額外打一次 Yahoo `Adj Close`（`fetch_yahoo_adj_close()`），沿用 `scripts/run_factor_backtest.py:40` 現成的 `_RateLimiter` 節流。

驗證：`adjust.validate_adj_factor()` 對全市場每一碼的還原序列跑單調性檢查（沿用 Phase 1 的不變量測試），違規清單寫進 log 供人工判讀（既有的「不自動內插」原則，見下方「明確不做」）；抽樣重算 `0050`/`2327` 兩檔確認全市場規模下數字與 watchlist 規模時一致。

### 6b：擴大候選因子集合

先評估、不保證採用，用 Phase 4 已建好的 rank-IC/ICIR/衰減曲線框架（`scripts/report_factor_ic.py`）逐一檢定：

| 類別 | 候選欄位 | 資料來源 | 備註 |
|---|---|---|---|
| 品質 | 毛利率／營益率趨勢 | `get_financials()`（與 `roe_series()` 同來源，`FINANCIALS_LAG_DAYS=60` 沿用） | 與現有 ROE 同屬基本面，抓取成本為 0（資料已在抓取範圍內） |
| 價值 | 本益比、股價淨值比 | `get_financials()` + 日線收盤價 | 需要股本/淨值欄位，確認 FinMind 對應 dataset 覆蓋率 |

IC 的 t-stat 未達顯著的因子標記排除，不進入候選池，這條規則跟 Phase 4 對既有三因子做的把關一致，不因為是「新」因子就放寬。

通過篩選的因子計算函式加進 `src/finlab_factors.py`，跟現有 `revenue_yoy_series()`/`roe_series()` 並列（同樣是純函式、point-in-time 對齊沿用 `REVENUE_LAG_DAYS`/`FINANCIALS_LAG_DAYS` 的既有規則）——這是本 Phase 對 `finlab_factors.py` 唯一的程式碼變更；6d 提到的「保持不變」只針對 `RANKING_WEIGHTS` 常數本身與 override 機制，不代表整個檔案不動。

### 6c：排程自動化

新增 GitHub Actions workflow（`.github/workflows/optimize.yml`，比照 `snapshot.yml` 讀 `environment: Cloudflare` 的 secrets 模式，但這裡只需要 `FINMIND_TOKEN`／`TELEGRAM_BOT_TOKEN`／`TELEGRAM_CHAT_ID`，不涉及 Cloudflare）：

- **平時（每月）**：對既有全市場代碼做增量更新（只抓上次抓取之後的新日線／新一期營收／新一期財報），成本量級接近 `snapshot.yml` 的 light 模式，不是 Phase 4 那次約 19 到 22 小時的全量抓取。
- **定期（每季，未經使用者明講的預設，如有異議可調）**：完整重跑一次 universe 清單本身（`scripts/run_factor_backtest.py --universe all` 的股票代碼枚舉那一步，非日線歷史），補上新上市、清除已下市代碼，避免 universe 漸漸偏離「全市場」的定義。
- 排程內容：重算 rank-IC，`sweep_ranking_weights_walk_forward()` 拿新候選（含 6b 通過篩選的因子）與現行 `RANKING_WEIGHTS` 比較，產生本輪結論。

### 6d：自動套用與 rollback

**沿用既有設計，不新增決策空間**：`sweep_ranking_weights_walk_forward()` 本來就是離散具名候選互相比較，這裡只是把「贏家要不要採用」從人工決定改成規則決定。不對 `RANKING_WEIGHTS` 三個數值做連續搜尋，Phase 5 文件已經寫過理由（同一批雜訊上重學一次），這次沒有理由推翻。

- **持久化位置**：`RANKING_WEIGHTS` 模組常數維持原樣當 fallback default，override 機制維持「純函式、無 I/O」不變（`finlab_factors.py` 本身在 6b 有新增因子函式，但這個 override 掛鉤不因此改變設計）。實際生效的候選寫進 `db.py` 既有的 `meta` 表（`set_meta(conn, "active_ranking_weights", json.dumps({...}))`），呼叫端（`scripts/run_factor_backtest.py`／未來任何消費者）用 `get_meta()` 讀出後，透過 `composite_scores(weights=...)` 既有的 override 參數傳入，這個掛鉤 8/18 那次驗證就已經在用，不需要新增介面。
- **套用條件**：候選必須同時通過 DSR ≥ 0.95、PBO ≤ 0.5、高原寬度 ≥ 搜尋範圍 20%（`optimize.py` 既有三道門檻，不新增不放寬）。
- **rollback**：套用後的下一個排程週期（月），用該週期新產生的樣本外數據比較「新候選」vs「被取代的前一組設定」的 walk-forward 表現；新設定明顯更差（低於前一組，或低於同期買進持有）就把 `meta.active_ranking_weights` 寫回前一組，並將整個事件（採用時間、比較數字、rollback 判定）記進既有的 `optimization_runs` 表。
- **通知**：每次排程跑完（不論結果是「維持現況」「採用新候選」「rollback」）都呼叫 `alerts.py` 現成的 `send_telegram()` 發一則事件摘要。這則通知是事件層級，不是選股清單，範圍已在 grilling 中排除選股清單出口。

這個「拿掉人工審核關卡，靠自動 rollback 補償」的取捨記在 [`docs/adr/0002-auto-apply-ranking-weights.md`](adr/0002-auto-apply-ranking-weights.md)。

驗證：用 8/18 那次已經跑過的一次性腳本（`sweep_ranking_weights_walk_forward()` + `evaluate_decision`）當回歸測試基準，新排程邏輯跑出的判定（採用/拒絕）必須與那次手動跑的結果一致，才能確認自動化沒有改變判定邏輯本身。

風險：**中高**（與 Phase 5 同級，原因相同：框架本身若有 bug，自動套用會放大而非只是產生一次性的誤導結論）。緩解：rollback 機制、每次事件都留痕（`optimization_runs`）、Telegram 通知讓人有機會人工介入，即使沒有強制的人工審核關卡。

---

## 明確不做

| 項目 | 理由 | 替代 |
|---|---|---|
| **QuantConnect / LEAN** | 無原生台股資料，要自寫 custom data feed；其核心價值（美股/選擇權/券商整合）對台股全不適用 | `portfolio_backtest.py` 補 lot rounding + slippage（~40 行） |
| **Microsoft Qlib** | 官方只支援 REG_CN/REG_US，台股要自寫 `dump_bin.py` + calendar + instruments；強制 pandas/numpy/pyarrow + ML 堆疊，與 stdlib-only 政策正面衝突；估 2–3 週 | Phase 4 的 ~180 行 rank-IC 評估。**放棄的是** Alpha158/360 因子庫與現成 LightGBM/LSTM pipeline |
| **現在買 finlab VIP** | NT$8,988/年買一個 11 個免費 request 就能算出的欄位；且不解決真正的問題（策略結構性落後） | 自建 + 免費版當 2020 年前校驗。**條件性重估**：Phase 4 全市場後若自建 point-in-time 對齊出錯率過高再談 |
| **`Adj Close ÷ Close` 當還原因子** | Yahoo 的 `Close` 已還原分割，該比值只含股利 | `Adj Close ÷ FinMind raw close` |
| **Yahoo `Stock Splits`/`Dividends` event 欄位** | 實測漏記 `0050`、`2327` 的分割 | 同上 |
| **Phase 3 之前建最佳化器** | 四個旋鈕整個空間已掃完，沒有一組能接近買進持有 | 先修資料、再修定位 |
| **`exit_composite=-30`（全樣本 argmax）** | 307.8% 是高原 + 雜訊，各 fold argmax 不穩定 | 高原中心 -40 |
| **自動內插缺漏 bar** | `momentum_pct` 與 `max_hold_days` 都數 bar 數，內插製造假資料 | 標記進 `meta`，人工判讀 |
| **為接富果而降級 yfinance** | yfinance 是唯一免費的完整還原序列來源 | 富果插在 FinMind 之後、yfinance 之前 |
| **Phase 6 順便幫多因子引擎補選股清單出口** | `portfolio_backtest.py` 至今沒被 `app.py` 引用過一次，範圍會擴大到 UI/報告設計，是規模不同的另一個決定 | 維持純內部研究管線，出口留給未來單獨評估 |
| **Phase 6 每月全市場全量重抛** | Phase 4 實測全量抓取約 19 到 22 小時，`stock_tracker` 是 private repo，GitHub Actions 免費額度撐不住每月重複這個量級 | 平時增量更新，每季一次全量重抛只補 universe 清單本身（新上市/下市代碼） |

---

## 工作量與風險總表

| Phase | 內容 | 新檔 | 改檔 | 估計行數 | 風險 | 前置 |
|---|---|---|---|---|---|---|
| 0 | 評估基準線 | 2 | 0 | ~370 | 無 | — |
| 1 | 還原股價 + 資料清洗 | 3 | 4 | ~660 | **中** | 0 |
| 2 | 效能重構（O(n²)、分項快取） | 0 | 5 | ~250 | 中 | 0,1 |
| 3 | 策略定位修正 | 0 | 4 | ~150 | 低 | 0,1 |
| 4 | 全市場 + 富果 + rank-IC | 2 | 2 | ~480 | 中 | 1,2 |
| 5 | 自我優化框架 | 3 | 1 | ~590 | **中高** | 全部 |
| 6 | 全市場還原因子 + 因子擴充 + 自動化最佳化管線 | 1 | 3 | ~400 | **中高** | 全部 |
| | **合計** | **11** | **~19** | **~2,900** | | |

**貫穿全程的驗證基準**：`pytest` 必須維持 **253 passed 全綠且 < 2 秒**（全 mock、無網路）。每個 Phase 完成後 commit 到本地（不 push），回報 branch 與 commit hash。

## 關鍵檔案

- `src/data_sources.py` — `:232-280` `get_daily()` 未還原、`:268` 硬寫 `period="2y"`（`days` 被忽略）
- `src/db.py` — schema migration 落點；`:2`「2-year」與 `:11` 失效路徑註解
- `src/analysis.py` — `:791` `build_backtest_ctx()` 是 O(n²) 來源；`:796-856` 分項未保留；`:29` `SCORE_WEIGHTS`
- `src/backtest.py` — `:20` `STRATEGY_PARAMS`；`:159-175` `replay_scores()` 的 per-bar 切片
- `src/finlab_factors.py` — `:131` `momentum_pct()` 是動能偏誤來源；`:120` `liquidity_ok()` 的原始價陷阱；`:142` `_pct_rank()` 可複用做 Spearman；`:25` `RANKING_WEIGHTS` 最缺證據
- `src/portfolio_backtest.py` — `:155` `_compute_metrics` 待抽進 `src/metrics.py`
- `scripts/run_factor_backtest.py` — `:40` `_RateLimiter` 與 per-code cache，是全市場擴大的現成基礎設施
- `scripts/build_adj_factors.py` — `build_code()` 目前只在 watchlist 規模跑過，Phase 6a 要把這個迴圈擴大到全市場
- `src/finlab_factors.py` — 模組 docstring 記錄的「`momentum_pct()` 吃未還原收盤價」已知限制，Phase 6a 的修復對象；`composite_scores(weights=...)` 的 override 參數，Phase 6d 沿用
- `src/db.py` — `set_meta()`/`get_meta()`，Phase 6d 存放 `active_ranking_weights` 的既有機制，不需要 schema migration
