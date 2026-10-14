# CONTEXT.md

stock_tracker 專案的詞彙表。只收「這個詞在這個專案裡精確指什麼」，不收設計文件、規格或施工筆記——那些放 `docs/`。

## 現價

字面上「股票現在的價格」，但**目前在專案裡沒有單一權威來源**，依情境取自不同地方：

- `src/data_sources.get_quote(code)` 回傳的即時單筆報價——`pipeline.build_stock_payload()` 轉發成 payload 的 `quote` 欄位，前端多數地方讀 `d.quote.price`。
- 日線圖表最後一筆收盤價——只在圖表繪製時使用（如 `static/index.html:1069-1070` 畫 VAH/VAL 覆蓋線的那段），可能落後於盤中即時報價。
- `src/monitor.py:203` 用當日 tick 序列最後一筆當現價，餵給 `src/alerts.py` 的告警判斷——這是即時監控/告警專用的現價，跟上面兩個不是同一條資料流。

這三者曾經因為前端拿錯來源而顯示不一致（見 `docs/adr/0001-target-price-ownership.md` 背景段）。**刻意不統一它們**——為什麼、以及未來什麼情況該重新打開這個決定，見 `docs/adr/0006-current-price-not-unified.md`（2026-09-08 `/improve-codebase-architecture` 重新核對後補記，原始判斷仍是 ADR 0001 的架構調查候選 11）。

## 訊號的 bt / bt_reason（回測可用性）

`src/analysis.py` `analyze()` 回傳的 `signals` 陣列，每個訊號 dict 帶一個 `"bt": str | None` 欄位：非 `None` 時是 `BACKTEST_SIGNALS`（`analysis.py`）裡的一個鍵，代表這個訊號有歷史勝率統計（`result["backtest"][bt]`）；`bt` 為 `None` 代表「這個訊號沒有回測證據」，但原因不只一種——目前涵蓋 VP（Value Area 三態＋LVN 逼近）、OF（主動買賣/連續/背離）、chip 籌碼面五種、跳空缺口、布林擠壓、Donchian、5日相對強弱排名，共 7 類。

**只有 VP 和 OF 這兩類**額外帶 `"bt_reason": "vp_of_short_window"`（其餘 5 類的 `bt_reason` 不存在／為 `None`），對應 `static/index.html` 的 `buildDetailRow()` 顯示「（VP/OF 訊號資料窗較短，不做回測）」這句提示——前端讀這個欄位決定要不要顯示提示，**不再**用 `sig.text.includes('Value Area'|'主動'|'LVN'|'背離')` 去猜（見架構調查候選：訊號能不能回測的判斷邏輯在前端用文字比對重新猜一次，2026-09-08 已處理）。其餘 5 類目前刻意維持裸訊號、無提示——要不要幫它們也加說明是獨立的產品文案決定，未定案。

`_Avoid_`：不要以為 `bt: None` 只有一種意思，也不要在前端新增訊號時用文字內容猜測分類——這個模式已經證明脆弱（文案改字會讓判斷靜默失效）。任何「這個訊號為什麼不能回測」的新分類，都應該在 `analysis.py` 賦值 `bt`/`bt_reason` 的當下就決定，不是留給消費端事後推敲。

## 目標價（target_price）

`src/analysis.py` 的 `analyze()` 函式回傳的欄位，代表「這支股票下一個該關注的量價分佈關鍵價位」。結構：

```python
"target_price": {"level": float, "basis": "VAH" | "VAL", "as_of": "YYYY-MM-DD"} | None
```

- `basis`：股票 `score`（同一份回傳結果的 `score` 欄位）為正選 `"VAH"`、為負選 `"VAL"`；`score == 0` 或沒有算出 Value Area（見下）時整個 `target_price` 是 `None`。
- `level`：`basis` 對應的 Value Area 邊界（`vp["vah"]` 或 `vp["val"]`）。
- `as_of`：`target_price` 所根據的最後一根日線 K 棒日期——跟 `pipeline.build_stock_payload()` 既有的 `last_date` 欄位算法一致（都是取 `daily["t"]` 最後一筆轉成日期字串）。

**`target_price` 不包含「現價是否已經越過這個水位」的判斷**——那是消費端自己拿 `target_price.level` 跟現價（見上一條「現價」詞條）比較：`basis == "VAH"` 時 `price < level` 才算「目標仍在前方」；`basis == "VAL"` 時 `price > level` 才算。理由見 ADR 0001。

`_Avoid_`：不要叫它「突破價」或「壓力/支撐價」——那是 `src/alerts.py` VWAP/ORB 告警用的詞，指的是完全不同的一組即時盤中指標，跟這裡的 Value Area 目標價無關（也是 ADR 0001 明確決定不合併的兩件事）。

## VAH / VAL

`src/analysis.py` `volume_profile()` 回傳的 `vp` dict 裡的兩個邊界：`vp["vah"]`（Value Area High，價值區上緣）、`vp["val"]`（Value Area Low，價值區下緣）。`analyze()` 把整個 `vp` 存進回傳結果的 `vp` 欄位（`src/analysis.py:1035`），前端直接讀 `a.vp.vah`/`a.vp.val` 畫圖表覆蓋線（`static/index.html:1069-1070`）——這條既有路徑不受「目標價」欄位新增影響。

## Value Area（價值區）

`volume_profile()` 依成交量分佈算出的「主要成交量集中的價格區間」，`vp` dict 除了 `vah`/`val` 還有 `poc`（Point of Control，成交量最大的單一價位）跟 `lvn`（Low Volume Node，成交量真空區價位清單）。`vp_position`（`analyze()` 回傳的另一個欄位）記錄現價相對這個區間的位置：`"above_va"` / `"inside_va"` / `"below_va"`。

## 基準日（as_of）

指某個數值「根據哪一天的資料算出來」，不代表這個數值現在是否仍然成立。目前唯一使用處是 `target_price.as_of`（見上）。刻意跟「這個數值是否已經失效／被突破」分開——後者不是這個詞要回答的問題，見 ADR 0001。

## 盤勢／市場狀態（market regime）

`src/market_regime.py` 的 `classify_regime()`/`regime_series()` 對**大盤（TAIEX）**算出的三態分類：`"bull"`（多頭）/`"bear"`（空頭）/`"consolidation"`（盤整），依大盤收盤價 vs MA20 vs MA60 排列 + MA20 斜率判斷，細節與門檻值見該模組 docstring、`docs/adr/0004-regime-adaptive-strategy.md`、`docs/regime_strategy_research.md`。

**`_Avoid_`：不要跟「均線多頭/空頭排列」搞混——那是 `src/analysis.py`/`src/breakout_strategy.py` 既有的**個股**層級判斷（`ma5 > ma20 > ma60`），跟這裡的「盤勢」是完全不同層級的概念：一支個股可以在大盤「空頭」盤勢裡仍然呈現「均線多頭排列」，兩者不互相蘊含。

`effective_regime_series()` 是「防抖」後的版本（連續 N 天同一原始分類才算切換），純函式、不持久化狀態，每次呼叫都對同一段歷史重算；`current_regime()` 是給只要「現在是什麼盤勢」的呼叫端用的便利函式。目前接了兩處：`finlab_factors.RANKING_WEIGHTS`（分盤勢版本走人工審核，`meta.active_ranking_weights_by_regime`）、`backtest.STRATEGY_PARAMS`（分盤勢版本 `STRATEGY_PARAMS_BY_REGIME`，寫死進 code，不建審核流程）。

## 支撐區（強勢回調）／ R

`src/pullback_strategy.py` 的兩個詞（第二種選股法，`docs/pullback_strategy.md`）：

- **支撐區**（`setup["zone"]`，`{"low", "high", "score", "kinds", "levels"}`）：EMA9/21/50、以整理區階段高點為錨的 AVWAP、前高、前低、未補缺口中，彼此相距 1.5% 內聚在一起的那一段價格；`score` 是聚在一起的價位種類數（AVWAP/EMA21/前高至少兩個重疊再 +1）。價位都在「今天的價格尺度」上（`c_adj` 還原後），可以直接當下單價。
- **R**：一筆計畫的每股風險＝進場價 − 停損價。3R、5R 是分批出場的目標價；`net_r` 是扣掉台股手續費＋證交稅後實際值多少 R。

`_Avoid_`：不要把「支撐區」跟 `target_price`（VAH/VAL，量價分佈）或 `alerts.py` 的 VWAP/ORB「支撐/壓力」混用——三者是不同演算法、不同用途。多因子推薦卡的「目標」是 VAH/VAL，強勢回調卡的「目標」是 3R/5R，刻意不共用欄位。

