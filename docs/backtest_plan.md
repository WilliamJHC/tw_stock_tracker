# stock_tracker 優化計畫書：venv 修復 + 2 年回測資料庫 + Dashboard 建議

- 撰寫日期：2026-07-27
- 執行者：Sonnet 5 (medium effort)，逐 Phase 執行，Phase 之間允許中斷
- 專案根目錄：`C:\Users\william.chen\claude\stock_tracker\`（下文相對路徑皆以此為根），git repo、分支 `main`
- 環境：Windows 11 + PowerShell 5.1。**所有 Python 一律用完整路徑 `.venv\Scripts\python.exe`，不要打裸 `python`**（原因見 Phase 0 成因 3）。跑 Python 遇中文亂碼先設 `$env:PYTHONIOENCODING='utf-8'`

---

## Context（為什麼要做這件事）

兩個需求：

1. **README 的安裝步驟跑不起來。** 使用者照 `README.md:18-28` 做無法成功啟動 venv。實測後確認**三個獨立成因**同時存在（見 Phase 0），不是單一問題，所以只重跑一次 `pip install` 修不好。

2. **目前的「回測」只是雛形，且不落地。** `analysis.py:517` 的 `BACKTEST_SIGNALS`（11 個訊號述詞）＋ `analysis.py:536` 的 `backtest_signal()` 只在每次 `analyze()` 呼叫時**臨時重算當下觸發的那幾個訊號**，算完丟進 `result["backtest"]` 就沒了。專案**完全沒有資料庫**（只有 `cache/` 的當日 JSON 快取）。所以現在無法回答「照這系統做兩年到底賺不賺」「這個訊號在這檔股票上歷史勝率多少」。

預期成果：venv 照 README 就能起；有一個 SQLite 回測資料庫存放 19 檔 watchlist 股票近 2 年的日線、籌碼、每日重算分數、訊號勝率與策略交易明細；dashboard 的今日決策面板每檔多出「信心度 ＋ 樣本數 ＋ 歷史證據」，展開列多出「回測績效」區塊。

### 已確認的決策（使用者已拍板，不要再問）

| 項目 | 決定 |
|---|---|
| 回測範圍 | **兩種都做**：A 訊號勝率統計、B 策略績效回測（權益曲線／交易明細） |
| 建議形式 | 排序清單 ＋ 信心度 ＋ 歷史證據（沿用現有決策面板，**不做**進出場價位建議） |
| venv 修法 | **改寫 README 不用 activate**，零系統設定變更（不改 ExecutionPolicy） |
| 資料更新 | 手動 CLI，前端標示資料時間與過期警示（**不做**啟動時自動重算、**不做**增量更新） |

---

## Phase 0：修復 venv 與 README 安裝節

### 三個成因（都要處理，缺一個仍會壞）

1. **`.venv` 的套件被清空。** `.venv\Lib\site-packages\` 只剩 `pip` 與 `pip-26.1.2.dist-info`，外加三個**沒有 dist-info 的孤兒目錄** `anyio\`、`google\`、`_pytest\`（只剩 `__pycache__`，無 `.py`）。`requirements.txt` 的 fastapi／uvicorn／requests／yfinance／pytest **全部不存在**，`.venv\Scripts\` 也沒有 `uvicorn.exe`、`pytest.exe`。孤兒目錄會遮蔽（shadow）之後裝的真套件，所以**不能只補 `pip install`，要整個砍掉重建**。
2. **PowerShell ExecutionPolicy。** `Get-ExecutionPolicy -List` 顯示 MachinePolicy／UserPolicy／CurrentUser／LocalMachine **全是 `Undefined`**，Windows 客戶端會落到 `Restricted`。所以 `README.md:21` 那行 `.venv\Scripts\Activate.ps1` 在**新開的 PowerShell 視窗會被擋**。（診斷當下能跑只因為該 process 範圍是 `Bypass`。）
3. **PATH 汙染。** `where python` 第一順位是 `C:\Users\william.chen\claude\report_generator\.venv\Scripts\python.exe`，stock_tracker 的 venv 排第二。**在 stock_tracker 目錄下打 `python` 跑的是 report_generator 的環境**，所以 `README.md:20` 的 `python -m venv .venv` 是用錯誤的 base 建立 venv，而且之後任何裸 `python`／`pip` 指令都默默跑錯環境。

### 步驟

1. 先記下 base python（**建立新 venv 前先讀，砍掉後就查不到了**）：現有 `.venv\pyvenv.cfg` 的 `home =` 值為
   `C:\Users\william.chen\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0`
   （Microsoft Store 版 Python 3.13.14）。先 `Test-Path "$home\python.exe"` 驗證存在。若不存在，退而用 `C:\Users\william.chen\AppData\Local\Microsoft\WindowsApps\python.exe`。

2. 重建（**這是破壞性動作，執行前確認 `.venv` 沒有任何手動裝的東西 —— 已確認只剩 pip 與孤兒目錄**）：
   ```powershell
   $base = "C:\Users\william.chen\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\python.exe"
   Remove-Item -Recurse -Force "C:\Users\william.chen\claude\stock_tracker\.venv"
   & $base -m venv "C:\Users\william.chen\claude\stock_tracker\.venv"
   .\.venv\Scripts\python.exe -m pip install --upgrade pip
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```
   若 Store 版 Python 建 venv 失敗（WindowsApps 重導向器偶有此問題），退路：請使用者安裝 python.org 版 3.13 後重跑。**不要**用 `.venv\Scripts\python.exe -m venv .venv` 自我重建 —— 現有 `pyvenv.cfg` 的 `command` 欄顯示上次就是這樣做的，很可能正是壞掉的原因。

3. 驗證（三項都要，貼出實際輸出）：
   ```powershell
   .\.venv\Scripts\python.exe -c "import fastapi, uvicorn, requests, yfinance, pytest; print('deps ok')"
   $env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m pytest tests\ -q
   .\.venv\Scripts\python.exe -m uvicorn app:app --port 8932   # 另開視窗 curl http://localhost:8932 確認 200
   ```
   `tests\` 現有 11 檔、約 170 個 test function，全部離線（monkeypatch，不打網路）。`.pytest_cache` 記錄的上次失敗 `test_sbl_excluded_when_vol20_missing` 是**過期快取**（該測試已改名為 `tests/test_chip.py:125` 的 `test_sbl_and_institutional_excluded_when_vol20_missing`），重跑會自動清掉，不用管。

4. **改寫 `README.md:16-28` 的「安裝與啟動」節。** 注意：`README.md` 目前有**未提交的修改**，先 `git diff README.md` 看內容再改，不要直接覆蓋掉使用者的編輯。新內容要點：
   - 全面改用完整路徑，**刪掉 `Activate.ps1` 那行**（README 其餘章節 `:22, :23, :141, :155` 本來就都用完整路徑，只有安裝節不一致——把它對齊）
   - `python -m venv .venv` 改成用明確的 base python 路徑，並加一句說明「不要用裸 `python`，本機 PATH 上有另一個專案的 venv 排在前面」
   - 新增「跑測試」指令（README 目前完全沒寫）
   - 新增一小節「疑難排解」，記錄上述三個成因與各自症狀，讓下次壞掉能自我診斷；若使用者仍想用 `Activate.ps1`，註明兩條不改系統設定的路：改用 `.venv\Scripts\activate.bat`（cmd 版不受 ExecutionPolicy 管），或該視窗開 `powershell -ExecutionPolicy Bypass`
   - **不要**建議 `Set-ExecutionPolicy`（使用者已明確選擇不改系統設定）

5. `start_server.bat:16` 已經是 `.venv\Scripts\python.exe`，**不用改**。

---

## Phase 1：SQLite 資料層 `db.py`

用 stdlib `sqlite3`，**不加任何新依賴**（`requirements.txt` 不動）。DB 檔放 `data/backtest.db`，並在 `.gitignore` 加一行 `data/`。

`db.py` 是純資料存取層（開連線、建表、讀寫），不含任何分析邏輯。提供 `connect(path=None)`、`init_db(conn)`（`CREATE TABLE IF NOT EXISTS`，可重複執行）、以及各表的 upsert／query 函式。全部用 `INSERT OR REPLACE` 搭配 PK 做冪等寫入。

Schema（欄位命名注意：`foreign` 是 SQL 保留字，用 `foreign_net`）：

| 表 | PK | 用途 |
|---|---|---|
| `daily_bars(code, date, o, h, l, c, v, source)` | `(code, date)` | 2.5 年日線，避免重跑時重打 FinMind |
| `chip_daily(code, date, foreign_net, trust_net, margin_balance, margin_delta, short_balance, short_delta, foreign_ratio, foreign_shares, sbl_balance, sbl_sales, sbl_returns)` | `(code, date)` | 2 年籌碼，四個來源合併成一張寬表 |
| `score_history(code, date, tech_score, chip_score, composite, priority, action, signals_json)` | `(code, date)` | 每日重算分數（Phase 4 產出） |
| `signal_stats(code, signal, horizon, n, wins, win_rate, avg_ret, computed_at)` | `(code, signal, horizon)` | 訊號勝率統計（Phase 3 產出） |
| `strategy_runs(run_id, code, params_json, n_trades, win_rate, avg_ret, total_return, buy_hold_return, max_drawdown, start_date, end_date, computed_at)` | `(run_id, code)` | 策略績效摘要（Phase 4 產出） |
| `trades(run_id, code, entry_date, entry_price, exit_date, exit_price, ret_pct, exit_reason, hold_days)` | 無（加 index on `(run_id, code)`） | 交易明細 |
| `meta(key, value)` | `key` | `last_run_at` / `last_run_id` / `params_json` / `of_excluded` |

`meta.last_run_at` 是前端「回測資料更新於 X」的唯一來源。

---

## Phase 2：資料匯入（2 年歷史）

寫進 `run_backtest.py` 的匯入階段。

### 抓多久

- **日線抓 1000 天，不是 730。** 因為 `analysis.volume_profile()` 預設 `lookback=120`、`ma60` 需要 60 根，重算分數要 warmup 120 根。730 天 ≈ 490 根交易日，扣掉 warmup 只剩 ~370 根分數（約 1.5 年）。要拿到**完整 2 年的分數**，日線必須多抓約半年 → `ds.get_daily(code, days=1000)`。
- 籌碼抓 730 天即可（籌碼只需對齊有分數的那段）。

### 快取鍵衝突（**必須處理，否則會默默汙染線上路徑**）

`data_sources.py` 的四個籌碼 getter（`get_institutional:360`、`get_margin:390`、`get_shareholding:425`、`get_sbl:453`）**快取鍵不含 `days`**（`f"inst_{code}"`）。若回測用 `days=730` 去抓，會把 2 年資料寫進線上路徑正在讀的 `cache/inst_2330.json`，讓 `pipeline.build_stock_payload()` 之後拿到 730 天而非 40 天的籌碼。

**做法**：給這四個 getter 與 `get_daily()` 各加一個 `cache=True` 參數，`cache=False` 時跳過 `_cache_get`／`_cache_put`。每個函式 2 行改動、預設值不變、向後相容。回測匯入一律傳 `cache=False`，DB 本身就是回測的快取。
**不要**改成把 `days` 併進快取鍵（會讓現有 136 個快取檔全部失效），**更不要**在 `run_backtest.py` 裡直接呼叫 `ds._finmind()` 自己解析 —— 那會複製單位換算邏輯（`get_margin` 原始單位是張要 ×1000、`get_sbl` 已是股**不可**再乘，見 `data_sources.py:406-409, 454-456`），是最容易默默算錯的地方。

### 流程與速率

`--refresh-data` 時才打 FinMind，否則直接讀 DB。每檔 5 次呼叫（price + 4 籌碼）× 19 檔 = 95 次，遠低於 FinMind 600 次/hr 上限。`_finmind()` 已有重試與 402/401/403 不重試的處理（`data_sources.py:96-115`），沿用即可。逐檔印進度，單檔失敗記錄後繼續（沿用 `pipeline.build_watchlist_payloads():176` 的 per-code try/except 慣例），最後彙總哪幾檔失敗。

`0050` 是 ETF，籌碼資料可能整批缺 —— 這是正常的，`chip_analysis()` 本來就支援缺項（`analysis.py:652-654` 按可用權重歸一），四項全缺回 `None`。不要當成錯誤。

---

## Phase 3：訊號勝率統計（回測 A）

### 先做一個小重構

`analysis.py:765-771` 在 `analyze()` 內部組出 `backtest_signal()` 需要的 `ctx`：

```python
ma5 = ma_series(closes, 5); ma20 = ma_series(closes, 20); ma60 = ma_series(closes, 60)
vol20 = ma_series(vols, 20)
quadrant = [quadrant_at(closes, vols, vol20, i) for i in range(n)]
ctx = {"closes": ..., "vols": ..., "opens": ..., "ma5": ..., "ma20": ..., "ma60": ..., "vol20": ..., "quadrant": ...}
```

把這段抽成 `analysis.build_backtest_ctx(daily)` 並讓 `analyze()` 呼叫它。這是唯一能讓 `backtest.py` 重用同一份 ctx 而不複製邏輯的做法，且 `tests/test_analysis.py:136` 的 `test_score_weights_sum_to_100` 等既有測試會守住沒有行為改變。

同時給 `analyze()` 加一個 `with_backtest=True` 參數：`False` 時跳過 `analysis.py:928-932` 那段迴圈。Phase 4 重算 490 根 bar 時要關掉它（否則整體是 O(n²)）。

### `backtest.py` 的 A 部分

`signal_win_rates(daily, horizons=(5, 10, 20))`：對 `BACKTEST_SIGNALS` 的全部 11 個訊號 × 3 個持有天期呼叫既有的 `an.backtest_signal(ctx, fn, horizon=h, gap=5)`，回傳 `[{signal, horizon, n, wins, win_rate, avg_ret}]`，寫進 `signal_stats` 表。

注意 `backtest_signal()` 已內建 `gap=5` 的重疊樣本抑制（`analysis.py:545-547`），不要另外再做。VP／OF 類訊號的 `bt` 是 `None`、本來就不可回測（`analysis.py:513-516` 有註解說明，前端標「回測不適用」），維持原狀。

---

## Phase 4：分數重算與策略回測（回測 B）

### 硬限制：Order Flow 無法回溯（**必須在程式與 UI 都標註**）

`orderFlow` 在 `SCORE_WEIGHTS` 佔 15/100（`analysis.py:29-33`），但它需要分鐘 K 或逐筆 tick：yfinance 只給 60 天 5 分 K（`data_sources.py:330`），FinMind tick 要付費層且一次一天。**2 年前的 OF 重建不了。**

處理方式（零程式改動）：重算時 `analyze(daily_slice, minute=None, tick_days=None, ...)`，`of` 自然是 `None`，`analysis.py:864` 那段就整段跳過。實際效果等同「OF 貢獻視為 0」，分數會系統性地被壓向中性。

**為了讓歷史與今日可比**，`build_suggestions()`（Phase 5）在做信心度校準時，也必須用同一套「不含 OF」的分數當基準，不是用線上那個含 OF 的分數。線上 UI 主畫面仍照舊顯示含 OF 的完整分數 —— 兩者用途不同，不要混用。DB 的 `meta.of_excluded=1` 記錄這件事。

### 前視偏誤防護（**這是回測會說謊的頭號來源，四項都要做**）

1. **日線切片**：第 i 根 bar 用 `daily["c"][:i+1]` 等切片。`analyze()` 只讀 index `-1` 當「今天」（`analysis.py:772 last = n - 1`），所以切片天然安全。
2. **籌碼延遲一個交易日**：三大法人／融資券／借券餘額都是**當日收盤後才公布**。用 D 日籌碼去評 D 日的分數是前視。重算第 i 根 bar 時，籌碼只能取 `date <= 前一根 bar 的日期`。
3. **成交在下一根 bar 的開盤價**：訊號在 D 日收盤產生，最快只能 D+1 開盤成交。`simulate()` 的進出場價一律用 `bars[i+1]["o"]`，不是 `bars[i]["c"]`。
4. **RS 排名**：用同一日全 watchlist 收盤價算 5 日報酬（`an.rs_ranking()`，`pipeline.py:160-170` 有現成寫法），收盤後可得、次日開盤成交，安全。

還有一項**無法消除**的偏誤要誠實揭露：watchlist 是「今天」的清單，回測「過去兩年」= **倖存者偏誤**。這句話要寫進 UI 的免責文字。

### `replay_scores(...)`

對每根 bar i（從 warmup=120 到 n-1）：切片日線 → 依上述規則切籌碼 → `an.analyze(..., minute=None, tick_days=None, rs_info=rs_by_date.get(date), with_backtest=False)` → `an.compute_attention(result["score"], result["chip"], result)` → 產出 `{date, tech_score, chip_score, composite, priority, action, signals}` 寫進 `score_history`。

**效能**：約 490 根 × 19 檔 ≈ 9,300 次 `analyze()`。先實測，若總時長 < 15 分鐘就不要優化（使用者已接受「完整重算數分鐘」）。真的太慢再看 `fractal_pivots()` 與 `gap_tracker()` 這兩個每次都掃全歷史的函式。逐檔印進度與耗時。

### `simulate(score_rows, bars, params)`

純函式，多方單一持倉、不加槓桿。

- **進場**：`action == "準備進場"` 且目前空手 → 隔日開盤買進
- **出場**（由上而下取第一個命中）：`action == "減碼警示"`／`composite <= exit_composite`（預設 -10）／停損 `entry - atr_mult × ATR`（預設 2.0）／持有滿 `max_hold_days`（預設 60）→ 隔日開盤賣出
- **交易成本預設開啟**：`fee_pct=0.6`（來回手續費 0.1425%×2 ＋ 證交稅 0.3%）。**不要**預設為 0 —— 忽略成本會系統性高估績效
- **輸出**：`trades` 明細 ＋ 摘要 `{n_trades, win_rate, avg_ret, total_return（複利）, buy_hold_return, max_drawdown, start_date, end_date}`
- `buy_hold_return` 用同一段期間、同樣扣一次來回成本，才是公平對照

所有參數收進一個 `STRATEGY_PARAMS` dict 常數（比照 `analysis.py:61 ACTION_THRESHOLDS` 的既有風格），並整包序列化進 `strategy_runs.params_json`，讓「這個數字是用什麼參數跑出來的」可回溯。

---

## Phase 5：建議 API

### `pipeline.py` 新增 `build_suggestions(codes=None)`

對每檔：取今日 live 的 `attention`（現有 `build_stock_payload()` 已含）→ 從 DB 撈這檔的 `signal_stats` 與最新一筆 `strategy_runs` → 算信心度。

信心度算法（放 `backtest.py`，純函式好測）：
- 取今日觸發、且 `s["bt"]` 非 `None` 的訊號（VP／OF 類沒有回測依據，不計入）
- 查 `signal_stats[(code, signal, 20)]`，只採用 `n >= MIN_SAMPLES`（預設 8）的
- `confidence = Σ(nᵢ × win_rateᵢ) / Σ(nᵢ)`，`expected_ret = Σ(nᵢ × avg_retᵢ) / Σ(nᵢ)`（**依樣本數加權**，避免 n=2 的雜訊主導）
- 若全部訊號都不足樣本 → `confidence = None`、`note = "樣本不足"`。**不要**在樣本不足時硬給數字
- 輸出 `{code, name, action, priority, confidence, expected_ret, sample_n, strategy: {total_return, buy_hold_return, win_rate, max_drawdown, n_trades}, evidence: [{signal, horizon, n, win_rate, avg_ret}], data_as_of}`

DB 不存在或空的時候要**優雅降級**：回傳 `confidence=None` 與 `data_as_of=None`，不要丟例外。使用者可能還沒跑過 `run_backtest.py`。

### `app.py` 新增兩個端點

- `GET /api/suggestions` → `pl.build_suggestions()`，附 `meta.last_run_at`
- `GET /api/backtest/{code}` → 該檔完整回測：訊號勝率表（全 11 訊號 × 3 天期）、策略摘要、交易明細

沿用 `app.py:50-58` 現有的 `ds.SourceError → 404`／`ValueError → 422` 錯誤處理慣例。

---

## Phase 6：Dashboard

改 `static/index.html`（848 行，原生 HTML + Vanilla JS + 手繪 SVG，**沒有前端框架也不要引入**）。

1. `init()`（`:330`）在 live 模式下多 fetch `/api/suggestions`。**靜態快照模式（`applyStaticSnapshot`，`:308`）不含回測** —— DB 已 gitignore，GitHub Actions 在乾淨 checkout 中沒有 DB。快照模式下整個回測 UI 隱藏。這是**明確的範圍外項目**，README 要註明。
2. 決策面板（`renderDecisionPanel`，`:440`）每列加：信心度徽章（依 confidence 上色）＋ 樣本數 `n`。`confidence` 為 `None` 時顯示灰色「樣本不足」，**不要**顯示 0% 或空白。
3. 展開明細列（`buildDetailRow`，`:567`）加一個「回測績效」區塊：
   - 策略摘要小表：2 年總報酬 vs 買進持有、勝率、最大回撤、交易次數
   - 訊號勝率表：訊號／樣本數／勝率／平均報酬（20 日）；今日有觸發的那幾列標記出來
4. 頁首顯示「回測資料更新於 YYYY-MM-DD（N 天前）」；超過 7 天加 stale 警示樣式；DB 不存在時顯示「尚未執行回測，請跑 `run_backtest.py`」。
5. 免責文字補三句（沿用 `README.md:185` 現有的免責語氣）：**回測未含 Order Flow**（權重 15/100）、**watchlist 為現時清單故存在倖存者偏誤**、**已扣 0.6% 來回交易成本**。

沿用檔案既有的 CSS variable token（`:11-160` 的 `--color-text-primary` 等），不要另立一套配色。

---

## Phase 7：README 與測試

### 測試（沿用既有慣例：離線、手算 fixture、每檔開頭 `sys.path.insert(0, ...)`，無 `conftest.py`）

`tests/test_db.py`：
- `init_db()` 重複執行冪等
- upsert 同 PK 覆蓋而非重複列
- 各表寫入→讀回 round-trip

`tests/test_backtest.py`：
- `simulate()` 用手工造的單調價格序列 ＋ 腳本化的 action 序列 → 驗出已知的交易明細與報酬
- **成交價驗證**：進場價必須等於 `bars[i+1]["o"]`（前視偏誤防護 3）
- **籌碼延遲驗證**：斷言重算 D 日分數時看不到日期為 D 的籌碼列（前視偏誤防護 2）
- 交易成本有被扣、`max_drawdown` 在已知權益曲線上算對
- `signal_win_rates()` 在手算序列上得出預期的 n／勝率
- 信心度：樣本數加權平均算對；`n < MIN_SAMPLES` 時回 `None` ＋「樣本不足」

`tests/test_pipeline.py` 擴充：DB 不存在／空表時 `build_suggestions()` 優雅降級不丟例外。

### README 新增「回測與建議」節

CLI 用法（`--years` / `--codes` / `--refresh-data`）、DB 路徑與各表意義、**三項限制的完整說明**（OF 缺口、倖存者偏誤、靜態快照模式不支援）、資料多久該重跑一次。

---

## 驗證（依 CLAUDE.md 第 6 條：做完必須驗證，且驗證不自驗）

回報時只能用「已驗證（附方式與結果）／已完成但未驗證／未完成（附卡點）」三種狀態，禁止「99% 完成」「✅ 生產就緒」這類無證據宣告。

1. `$env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m pytest tests\ -q` → 全部通過，**貼出實際輸出**（含新舊測試總數）
2. 實跑 `.\.venv\Scripts\python.exe run_backtest.py --years 2 --refresh-data` → **貼出 19 檔的實際摘要表**與總耗時
3. **獨立交叉驗證**：任取一檔，用 `cache/daily_*.json` 或另開 Python 獨立算出 `(last_close / first_close - 1)`，與回測輸出的 `buy_hold_return` 對照（差異應只來自交易成本）。這一步是抓「回測整體算錯」的最便宜手段，不可省略
4. 起 uvicorn，`curl http://localhost:8932/api/suggestions` 與 `/api/backtest/2330` → 貼出 JSON
5. 用 Claude Preview 瀏覽器開 `http://localhost:8932`，截圖決策面板的信心度欄與展開後的回測績效區塊（**localhost 不受 Preview 的外部網路封鎖影響**，可直接測）
6. **派 fresh-context agent 做 README read-back**：在全新 context 下照改寫後的「安裝與啟動」節逐字執行一遍，確認在**新開的 PowerShell 視窗**（非 Bypass 範圍）能成功建 venv、裝套件、起服務。這是 Phase 0 是否真的修好的唯一判準 —— 自己在當前 session 驗不算數（診斷當下的 process 是 `Bypass`）

---

## 範圍外（明確不做）

- 進出場價位建議（ATR／VP 算停損目標價）—— 使用者選了「排序清單＋信心度＋證據」
- 啟動時自動重算、增量更新、排程 —— 使用者選了手動 CLI
- 修改 ExecutionPolicy 等任何系統設定
- 靜態快照（`snapshot.py` / Cloudflare 手機版）支援回測資料
- 盤中／加密貨幣路徑（`monitor.py`、`realtime/`）—— 本次不動
- `.claude/worktrees/fix-review-findings` 分支上那組未合併的 FinMind 429 重試修正 —— 那是獨立的一件事，本次不處理，但若 Phase 2 匯入時撞到 FinMind 限流，回報一句話即可
