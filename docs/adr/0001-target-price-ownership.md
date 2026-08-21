# 0001. 目標價（VAH/VAL）判斷收進 analyze()，不含突破有效性判斷

## 狀態

已接受（2026-08-21，architecture 調查 → grilling 收斂）

## 背景

「現在的價格是否已經越過某個關鍵水位」這個判斷，在 stock_tracker 裡至少獨立寫了 5 次：`src/analysis.py`（VAH/VAL）、`src/alerts.py`（VWAP/ORB 突破）、`src/intraday.py`（ORB breakout flag）、`src/pipeline.py`（現價來源切換）、`src/monitor.py`/`src/intraday_feedback.py`（tick 現價）、`static/index.html`（前端自行重算目標價是否有效）。已造成兩次生產 bug：`daab540`（線圖現價標籤盤中顯示昨收，前端拿錯資料源）、`788e207`（目標價已被突破，面板仍顯示為目標，因為沒有任何一處比對過現價與目標水位）。

## 決策

在 `src/analysis.py` 的 `analyze()` 裡，`score` 算出之後（`analysis.py:1016-1017`）、`return`（`analysis.py:1029`）之前，新增回傳欄位：

```python
"target_price": {"level": float, "basis": "VAH" | "VAL", "as_of": "YYYY-MM-DD"} | None
```

`score > 0` 選 VAH、`score < 0` 選 VAL，`score == 0` 或沒有 Value Area 時整個為 `None`。既有的 `vp`/`vp_position` 欄位不變。**`target_price` 不含「現價是否已越過這個水位」的布林判斷**——這件事留給消費端自己拿 `level` 跟現價比較（見 `CONTEXT.md` 的「目標價」詞條）。

## 為什麼這樣分界

「該選 VAH 還是 VAL」需要知道 `score` 的計算方式與正負號意義，是容易被重新發明錯的領域邏輯（實際上已經被 5 個地方各自重新發明），這部分收進後端。「現價有沒有超過已選定的目標數字」只是拿兩個已經在同一份 payload 裡的數字（`level`、現價）比大小，不需要額外領域知識，不算需要集中的複雜邏輯。

## 考慮過但拒絕的方案

**加一個 `valid: bool` 欄位，由後端判斷目標是否已被突破。**
最初的候選提案（源自 `788e207` 的教訓）。拒絕理由：判斷「是否突破」不需要 `analyze()` 提供任何它才知道的資訊——消費端已經同時拿得到 `level` 跟現價。而且兩個實際消費情境都不需要它：(a) 網頁看線圖時使用者肉眼就能判斷；(b) Telegram 通知本身是事件觸發，收到通知就代表已經突破，通知的存在就是那個訊號。加這個欄位不會消除任何重複邏輯，純粹是圖方便。

**這次順便統一「現價」本身跨 quote/chart/monitor 的來源分歧。**
拒絕理由：範圍會擴大到 `monitor.py`/`intraday_feedback.py`/`data_sources.py`，是規模不同的另一個問題（架構調查候選 11「即時報價跨 4-5 個檔案」，判定 Worth exploring）。`daab540` 那個 bug 的成因（前端選錯現價來源）跟這次要收斂的「VAH/VAL 該選哪個」是不同的程式碼路徑，不需要綁在同一次改動裡解決。留給未來單獨評估。

**把 `alerts.py` 的 VWAP/ORB 突破判斷也併入同一個介面。**
拒絕理由：資料來源（即時 tick/分鐘級盤中指標 vs 日線/量價分佈）、更新頻率、消費端（Telegram 推播 vs 決策面板顯示）都不同，只是恰好都用「突破」這個詞。沒有證據顯示這兩者的邏輯曾經或需要互相同步。

## 影響

- `src/analysis.py`：`analyze()` 新增一個回傳欄位，不動既有欄位——對 `tests/test_analysis.py` 現有測試無破壞性影響，需要新增 `target_price` 的測試（`score>0`/`score<0`/`score==0`/無 `vp` 四種情況）。
- `static/index.html`：`renderDecisionPanel()` 目前自行推導 `bull`/`target`/`targetValid` 的那段邏輯（`index.html:556-562`），改為讀 `analysis.target_price` 並自行比較是否已突破（規則見 `CONTEXT.md`），不再自己判斷該選 VAH 還是 VAL。
- 這次調查/決策記錄本身不含程式碼變更（架構調查 skill 的既有規範）；實作留給後續的 build 階段。
