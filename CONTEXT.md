# CONTEXT.md

stock_tracker 專案的詞彙表。只收「這個詞在這個專案裡精確指什麼」，不收設計文件、規格或施工筆記——那些放 `docs/`。

## 現價

字面上「股票現在的價格」，但**目前在專案裡沒有單一權威來源**，依情境取自不同地方：

- `src/data_sources.get_quote(code)` 回傳的即時單筆報價——`pipeline.build_stock_payload()` 轉發成 payload 的 `quote` 欄位，前端多數地方讀 `d.quote.price`。
- 日線圖表最後一筆收盤價——只在圖表繪製時使用（如 `static/index.html:1069-1070` 畫 VAH/VAL 覆蓋線的那段），可能落後於盤中即時報價。
- `src/monitor.py:203` 用當日 tick 序列最後一筆當現價，餵給 `src/alerts.py` 的告警判斷——這是即時監控/告警專用的現價，跟上面兩個不是同一條資料流。

這三者曾經因為前端拿錯來源而顯示不一致（見 `docs/adr/0001-target-price-ownership.md` 背景段）。**這次決策刻意不統一它們**——跨 quote/chart/monitor 的現價收斂被列為獨立候選（架構調查候選 11「即時報價跨 4-5 個檔案」，Worth exploring），留待未來另一次決策再處理。

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
