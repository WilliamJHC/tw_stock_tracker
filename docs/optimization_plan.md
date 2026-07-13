# stock_tracker 優化計畫書：機構投資人決策面板 + FinMind 籌碼資料擴充

- 撰寫日期：2026-07-13
- 執行者：Sonnet 5 (medium effort)，逐 Phase 執行，每 Phase 有獨立驗收條件，Phase 之間允許中斷
- 專案根目錄：`C:\Users\william.chen\claude\stock_tracker\`（下文相對路徑皆以此為根）
- 環境：Windows 11 + PowerShell 5.1；Python 虛擬環境在 `.venv\`；跑 Python 遇中文亂碼先設 `$env:PYTHONIOENCODING='utf-8'`

## 0. 背景與目標

使用者是機構投資人視角，要把這個台股追蹤器從「資訊羅列」改造成「高效決策」：打開首頁就知道**今天該關注哪幾檔、為什麼（一行理由）、建議動作**；細節收合到第二層。同時填上 FinMind 免費 token 並擴充三個籌碼面資料集，技術債一併清理。

現況重點（已於 2026-07-13 完整掃描確認）：
- `app.py`（141 行）：FastAPI 入口，serve `static/index.html`；API 為 `/api/watchlist`、`/api/analysis/{code}`
- `analysis.py`（494 行）：純函式指標引擎——MA、`volume_profile()`（POC/VAH/VAL/HVN/LVN）、`of_from_minutes()`（tick-rule 估算）、`of_from_ticks()`（付費 tick 版）、`of_summary()`、`analyze()` 綜合評分 -100~100 配多空標籤
- `data_sources.py`（353 行）：FinMind 為主要資料源（TaiwanStockPrice / TaiwanStockPER / TaiwanStockPriceTick / TaiwanStockInstitutionalInvestorsBuySell），降級 yfinance → TWSE；`_finmind()` helper 在 data_sources.py:66-81；當日快取到 `cache/`
- `static/index.html`（633 行）：單檔前端，watchlist 表格 + 展開明細（手刻 SVG K 線圖 + 訊號清單 + 量化摘要卡）
- `tests/test_analysis.py`：13 個純函式測試全過（執行前先跑一次建立基準）
- `watchlist.json`：13 檔股票
- `.env` 的 `FINMIND_TOKEN` 目前為空；**使用者已有免費註冊 token（600 次/hr，無逐筆 tick 權限）**，token 由使用者自行填入，本計畫只需提供填寫位置與驗證指令
- 遺留檔：`taiwan_stock_tracker.html`（1271 行 v1）、`taiwan_stock_tracker.bak.html`（593 行）；非 git repo、無 .gitignore

使用者已確認的決策（不要重新提問）：
1. 首頁主形態＝**決策總覽面板**（行動優先級排序）
2. 擴充資料集＝融資融券、外資持股比率、借券賣出餘額（**不含**月營收）
3. 技術債一併清理（git init、遺留 HTML 歸檔、data_sources 測試、評分門檻單一來源化）
4. 逐筆 tick 不可用 → Order Flow 維持現有分鐘 K 估算路徑，**不動**

範圍界線：不引入前端框架、不引入資料庫、不加新依賴（含 python-dotenv）；`analysis.py:6-7` 註記的 monte_carlo/knn/pe_range 移植**不在本次範圍**；前端維持單檔手刻。

## 1. 已實查確認的事實（可直接採用）

### 1.1 三個新 FinMind 資料集（2026-07-13 向 finmind.github.io/tutor/TaiwanMarket/Chip/ 官方文件確認，皆為免費帳號可用）

| dataset 參數值 | 用途 | 關鍵欄位 |
|---|---|---|
| `TaiwanStockMarginPurchaseShortSale` | 融資融券 | `date`, `MarginPurchaseTodayBalance`, `MarginPurchaseYesterdayBalance`, `ShortSaleTodayBalance`, `ShortSaleYesterdayBalance` |
| `TaiwanStockShareholding` | 外資持股 | `date`, `ForeignInvestmentSharesRatio`, `ForeignInvestmentShares`, `NumberOfSharesIssued` |
| `TaiwanDailyShortSaleBalances` | 借券賣出餘額（比 `TaiwanStockSecuritiesLending` 逐筆明細更適合做餘額訊號） | `date`, `SBLShortSalesCurrentDayBalance`, `SBLShortSalesPreviousDayBalance`, `SBLShortSalesShortSales`, `SBLShortSalesReturns` |

呼叫方式與現有 `_finmind(dataset, data_id, start_date)`（data_sources.py:66）完全相同。

**單位結論（2026-07-13 已用 2330 真實 API 回應實測定案，執行時直接採用，仍建議抽一筆複驗）**：
- 融資融券（`TaiwanStockMarginPurchaseShortSale`）：單位為「**張**」（2330 融資餘額實測 33,061，數萬級）→ getter 內 **× 1000 轉成股**再回傳。
- 借券賣出餘額（`TaiwanDailyShortSaleBalances`）：單位**已是「股」，不需轉換**（2330 `SBLShortSalesCurrentDayBalance` 實測 11,358,514，千萬級；佐證：同回應的 `MarginShortSalesQuota` 恰為 margin dataset `ShortSaleLimit` 的 1000 倍）。**注意：不要誤判為張再 ×1000**——那會得出超過流通股本 43% 的荒謬值，讓 sbl 組件永遠打滿分。
- 日線成交量 `v`：股。
各 getter docstring 註明原始單位與轉換。

### 1.2 API 額度預算（結論：600 次/hr 足夠，快取策略不用改）

每檔冷刷新（快取未命中）FinMind 呼叫：daily 1 + PER 1 + 法人 1 + 融資券 1 + 外資持股 1 + 借券 1 = 6 次。13 檔 × 6 = 78 次，加 `tick_tier_available()` 探測 1 次 ≈ **79 次/次冷刷新**。
- 每小時可承受約 7 次全冷刷新（600 ÷ 79）；現有 `_cache_get` 以日曆日為 key，同一天第二次起 FinMind 呼叫數為 0。
- 結論：沿用現有當日快取，新資料集照抄 `get_institutional` 的快取模式（cache key：`margin_{code}`、`sh_{code}`、`sbl_{code}`）。
- 前端「重新整理全部」不繞過伺服器當日快取——這是特性不是 bug（盤中報價 `get_quote` 走 TWSE MIS 不經快取，仍即時）。不需要加 force-refresh。

## 2. 實作 Phases

> 通則：每 Phase 結束 git commit 一次（commit message 繁體中文）。測試指令一律 `.venv\Scripts\python -m pytest tests -q`。啟動伺服器：`.venv\Scripts\python -m uvicorn app:app --port 8932`。

### Phase 0：技術債基盤（無行為變更）

改動：
1. `git init`；新增 `.gitignore`：`.venv/`、`cache/`、`__pycache__/`、`.pytest_cache/`、`.env`、`desktop.ini`。首次 commit。
2. 建 `legacy/` 子目錄，把 `taiwan_stock_tracker.html`、`taiwan_stock_tracker.bak.html` 移入。`analysis.py:6-7` 註解裡引用的舊檔路徑改成 `legacy/taiwan_stock_tracker.html`（歸檔不刪除——未來移植 monte_carlo/knn 還要參考）。
3. 重寫 `_load_token()`（data_sources.py:26-39）：優先讀 `os.environ.get("FINMIND_TOKEN")`；否則逐行解析 `.env`——跳過空行與 `#` 開頭行、以第一個 `=` 切割、strip 兩側空白與成對引號。**不引入 python-dotenv**。
4. `app.py:19-25` 的 `DEFAULT_WATCHLIST` 同步為 `watchlist.json` 現有 13 檔。
5. README 補一節「FinMind token 設定」：編輯 `.env` 填入 `FINMIND_TOKEN=<token>`（使用者自行填），並附下方驗證指令。

驗收：
- `git status` 乾淨，且 `.env`、`cache/`、`.venv/` 不在追蹤內
- `.venv\Scripts\python -m pytest tests -q` → 13 passed（與基準相同）
- token 驗證指令（README 也放這條，使用者填完 token 自測）：
  ```powershell
  .venv\Scripts\python -c "import data_sources as ds; print(ds.FINMIND_TOKEN[:8] or 'NO TOKEN'); print(len(ds._finmind('TaiwanStockPrice','2330','2026-07-01')), 'rows')"
  ```
- 開 `http://localhost:8932` 首頁行為與改前一致

### Phase 1：資料層擴充 + data_sources 測試

改動一：`data_sources.py` 在 `get_institutional` 後新增三個 getter，照抄其結構（當日快取、失敗回 None、cache `[]` 表示已知缺資料、數值一律 `int(float(x or 0))` 防禦性轉型——FinMind 偶爾回字串或 null）：

```python
def get_margin(code, days=40):
    """Margin purchase / short sale balances via TaiwanStockMarginPurchaseShortSale.
    Returns [{date, margin_balance, margin_delta, short_balance, short_delta}] in shares
    (source unit is lots; multiply by 1000), oldest first, or None on failure."""

def get_shareholding(code, days=40):
    """Foreign shareholding via TaiwanStockShareholding.
    Returns [{date, foreign_ratio, foreign_shares}] (ratio in %), oldest first, or None."""

def get_sbl(code, days=40):
    """SBL short sale balances via TaiwanDailyShortSaleBalances.
    Returns [{date, sbl_balance, sbl_sales, sbl_returns}] in shares (source is already
    in shares — do NOT multiply by 1000), oldest first, or None."""
```
- `margin_delta` = `MarginPurchaseTodayBalance − MarginPurchaseYesterdayBalance`（同一列即有，不需跨列差分）；`short_delta` 同理。皆以「股」回傳（張 ×1000）。
- 單位已於 §1.1 實測定案（融資券＝張需 ×1000；SBL＝股不轉換）。實作時抽 2330 各一筆複驗數量級即可，不要重新推導。

改動二：新增 `tests/test_data_sources.py`（mock HTTP，不打真網路）：
- 用 `unittest.mock.patch("data_sources.requests.get")` 回傳假 response（`status_code=200`、`.json()` 回 `{"status": 200, "data": [...]}`)。
- 用 pytest `tmp_path` + `monkeypatch.setattr(ds, "CACHE_DIR", str(tmp_path))` 隔離快取。
- 覆蓋至少：`_finmind` 收到錯誤 status 時拋 `SourceError`；`get_margin`/`get_shareholding`/`get_sbl` 正常解析、欄位映射與單位轉換；`_load_token` 三種輸入（環境變數優先、值帶引號、檔內含註解行）。
- 防呆：patch 後若 `requests.get` 被未預期呼叫，用 `side_effect` 拋例外，確保無真網路。

驗收：`.venv\Scripts\python -m pytest tests -q` 全過，新增 ≥8 個測試。不需啟動伺服器。

### Phase 2：籌碼分析純函式（analysis.py）

設計原則：**籌碼分是獨立子分數，與技術分並列顯示，不揉進同一個數字**（避免資訊互相稀釋）。技術分的 `SCORE_WEIGHTS` 與計算完全不動。

改動一：門檻單一來源化（前置）。把 analysis.py:474-475 的硬編門檻抽成模組級常數：
```python
SCORE_BANDS = [60, 20, -20, -60]           # boundaries: strong-bull / bull / neutral / bear / strong-bear
SCORE_LABELS = ["強多", "偏多", "中性", "偏空", "強空"]

def label_for(score): ...                   # shared by analyze() and chip_analysis()
```
既有 `analyze()` 改用 `label_for()`。**邊界語意必須沿用現行 analysis.py:474-475 的不對稱規則**：`score >= 60 → 強多`、`>= 20 → 偏多`、`> -20 → 中性`、`> -60 → 偏空`、否則強空（即 +20 落偏多、-20 落偏空）。前端改用 `CFG.score_bands` 時也要複製同一語意，不要寫成對稱的 `>=`。

改動二：新增籌碼分。
```python
CHIP_WEIGHTS = {"institutional": 40, "margin": 20, "shareholding": 20, "sbl": 20}

def chip_analysis(inst, margin, shareholding, sbl, vol20_last):
    """Returns {"score": int(-100..100), "label": str, "components": {...},
    "signals": [...], "coverage": [available component names]} or None if no data at all."""
```

各組件先算 `c ∈ [-1, 1]`（`clamp(x) = max(-1, min(1, x))`）再乘權重；`vol20_last` 為 20 日均量（**股**）：

| 組件 | 權重 | 公式 | 說明 |
|---|---|---|---|
| institutional | 40 | `net5 = 近5日(foreign+trust)淨買賣合計`；`c = clamp(net5 / (5*vol20_last) / 0.10)` | 5 日法人淨買超佔 5 日總量 10% → 滿分；外資連續同向 ≥3 日再加 ±0.2 後 clamp |
| margin | 20 | `chg5 = (margin_balance[-1] − margin_balance[-6]) / margin_balance[-6]`（不足 6 筆用首筆）；`c = clamp(−chg5 / 0.05)` | 機構視角：融資增＝散戶籌碼凌亂＝負向；5 日 ±5% → 滿分。另若券資比 `short_balance/margin_balance > 0.30` 追加 signal「券資比偏高，留意軋空」（bias 0，不計分） |
| shareholding | 20 | `d20 = foreign_ratio[-1] − foreign_ratio[-20]`（百分點，不足 20 筆用首筆）；`c = clamp(d20 / 1.0)` | 外資持股比 20 日 +1pp → 滿分 |
| sbl | 20 | `c = clamp(−(sbl_balance[-1] − sbl_balance[-6]) / (5*vol20_last) / 0.05)`（不足 6 筆用首筆） | 借券餘額增＝空方布局＝負向 |

**除零防護（必做，否則撞 Phase 3 的 0050 驗收）**：margin 的分母 `margin_balance[-6]`（或首筆）為 0、或整列餘額全 0（ETF 與無融資交易個股會發生）→ margin 組件**視同缺資料**，不參與歸一化；券資比計算同樣先檢查 `margin_balance > 0`。`vol20_last` 為 0 **或 None**（日線不足 20 根時會發生）時 institutional 與 sbl 組件亦視同缺資料。

缺資料處理：某組件輸入為 None/空/觸發除零防護 → 該組件不參與，按可用權重歸一：`score = round(100 * Σ(w_i*c_i) / Σ(w_i available))`；`coverage` 列出參與組件（前端顯示「籌碼分（3/4 項資料）」）。四項全缺回 None。label 用 `label_for()`。

signal 文案（沿用既有 signals 風格：`{text, bias, bt}`，bias ±1/0、bt=None）：「法人 5 日淨買/賣超 X 張」「融資 5 日增/減 X%」「外資持股比 20 日 +/−X pp」「借券賣出餘額 5 日增/減 X 張」。門檻：|c| ≥ 0.3 才出 signal（弱訊號不洗版）。

改動三：`analyze()` 簽名擴充（全部預設值，向後相容）：
```python
def analyze(daily, minute=None, tick_days=None, inst=None,
            margin=None, shareholding=None, sbl=None): ...
```
內部呼叫 `chip_analysis(...)`，結果加進回傳 dict 的 `"chip"` 鍵；原 inst 訊號段（analysis.py:453-470）移入 `chip_analysis`，但 `analyze()` 回傳中**保留 `"inst"` 摘要鍵**，以免前端既有量化摘要卡壞掉。

改動四：新增行動優先級 `compute_attention(tech_score, chip, result)`，回傳 `{"priority": 0..100, "action": str, "reason": str, "triggers": [str]}`：

```
composite = 0.6 * tech_score + 0.4 * chip_score    # chip 為 None 時 composite = tech_score

trigger_points（事件加分，逐項累加）：
  +15  今日突破/跌破 MA60（signals 內含對應項）
  +15  OF 量價背離（of.divergence 非 None）
  +10  爆量 > 2 倍均量
  +10  |OF streak| >= 3 或 |外資連續同向天數| >= 3（外資 streak 由 chip_analysis 從 inst
       序列計算並放進 components，compute_attention 從 chip["components"] 取）
  +10  籌碼任一組件 |c| >= 0.5（融資急增、借券急增等）
  + 5  價格在 Value Area 之外（above_va / below_va）

priority = min(100, round(0.6 * |composite| + trigger_points))

action（由上往下取第一個命中）：
  composite >= 40 且 priority >= 50  → "準備進場"
  composite <= -40 且 priority >= 50 → "減碼警示"
  priority >= 50                     → "今日關注"
  priority >= 25                     → "觀察"
  否則                               → "無變化"

reason：取 trigger_points 最高的事件的一句話（平手時取上表由上而下第一個命中者）；
       無事件則取 signals 中 |bias| 最強的第一條 text。
```
以上權重與門檻（0.6/0.4、50/25、各組件滿分刻度 10%/5%/1pp/5%）是**產品判斷的合理預設**，README 要標註「可調參數」與所在位置，不需再問使用者。

改動五：新增 `tests/test_chip.py`（手算 fixture）：四組件各自滿分/負分/clamp、缺資料歸一化（只有 inst 時 `score = round(100*c)`）、**融資餘額全 0 不拋例外且該組件不入 coverage**、`label_for` 邊界值（60/20/-20/-60 恰好落點，比對現行不對稱語意）、`compute_attention` 的 action 分界（composite=40、priority=50 邊界）。

驗收：`.venv\Scripts\python -m pytest tests -q` 全過（新增 ≥10 個測試）；**既有 13 個測試一字不改仍過**（驗證向後相容）。

### Phase 3：API 接線（app.py）

改動：
1. `api_analysis()`（app.py:92-141）：在 `inst = ds.get_institutional(code)` 後加 `margin = ds.get_margin(code)`、`sh = ds.get_shareholding(code)`、`sbl = ds.get_sbl(code)`（getter 已吞例外回 None，不需 try），全部傳入 `an.analyze(...)`。回傳 JSON 增加：`analysis.chip`、`attention = an.compute_attention(result["score"], result.get("chip"), result)`。
2. 新增 `GET /api/config`：回傳
   ```json
   {"score_bands": [60, 20, -20, -60],
    "score_labels": ["強多", "偏多", "中性", "偏空", "強空"],
    "action_thresholds": {"composite": 40, "priority_act": 50, "priority_watch": 25},
    "chip_weights": {"institutional": 40, "margin": 20, "shareholding": 20, "sbl": 20}}
   ```
   值一律取自 analysis.py 的常數（`an.SCORE_BANDS` 等），不要在 app.py 重抄數字。前端一切門檻改由此取得（`action_thresholds` 本次前端只顯示不判斷，先回傳備用）。

驗收（啟動伺服器後）：
- `curl.exe -s http://localhost:8932/api/config` → 看到 `score_bands` 為 `[60,20,-20,-60]`
- `curl.exe -s http://localhost:8932/api/analysis/2330` → JSON 含 `analysis.chip.score`（整數）、`analysis.chip.coverage`（非空陣列）、`attention.priority`、`attention.action`、`attention.reason`
- `curl.exe -s http://localhost:8932/api/analysis/0050` → ETF 籌碼資料可能缺項，確認 `chip` 為 null 或 coverage 缺項，但**不回 500**
- 若 FinMind 回 402/429（額度）導致無法實打，按風險 2 處理：記錄後將本段實打驗收延到 Phase 5，先以 mock 測試放行

### Phase 4：前端決策總覽面板（static/index.html）

全部在單檔內改，不引框架、不加 build 流程。

改動：
1. `init()` 先 `fetch('/api/config')` 存全域 `CFG`；index.html:310-311、322-323、368 三處硬編評分門檻改用 `CFG.score_bands`（消除前後端重複）。
2. 在 stats-grid（index.html:155-161）之後、search-filter（index.html:163 附近）之前插入「**今日決策面板**」`<div id="decisionPanel">`：
   - 依 `attention.priority` 降冪列出 action ≠ "無變化" 的股票，每檔一張橫向卡：`[action badge] 代碼 名稱｜priority｜一行理由（attention.reason）｜技術分 badge + 籌碼分 badge`
   - action badge 配色：準備進場＝紅系（沿用 `.score-sb` 色，台股紅漲）、減碼警示＝綠系、今日關注＝琥珀、觀察＝灰
   - 卡片 `onclick` → 呼叫既有 `toggleDetail(code)` 並 `scrollIntoView` 至該表格列（第二層明細完全沿用現有展開）
   - "無變化" 股票收進面板底部 `<details><summary>無變化 N 檔</summary>代碼列表</details>`
   - 資料未載完時面板顯示已載入部分＋loading 計數；每次 `loadStock` 完成後重繪（掛進現有 `updateStats()`/`renderTable()` 流程）
3. 表格增加「籌碼分」欄（`rowView` 加 `chipScore`，可排序，樣式沿用 score-badge；chip 為 null 顯示「—」）；預設排序 `sortKey` 從 `'code'` 改為 priority 降冪（`rowView` 加 `priority`）。
4. 展開明細的量化摘要區（`buildDetailRow`，index.html:435-439 法人卡附近）追加「籌碼面」卡：四組件各一行（法人 5 日、融資 5 日變化、外資持股比 20 日變化、借券餘額 5 日變化）＋ coverage 註記。
5. **手機版**：新欄位要同步加進 index.html:96-135 的 `grid-template-areas`，否則手機卡片式排版會掉版。

驗收（開 `http://localhost:8932` 目測）：
- 首屏最上方是決策面板：按 priority 排序、每檔一行理由、action badge 配色正確；無變化股票收合
- 點卡片跳到對應表格列並展開明細；明細內出現籌碼面卡
- 表格有籌碼分欄且可排序；預設按 priority 降冪
- DevTools 375px 寬度不跑版
- 改後端 `SCORE_BANDS` 重啟，前端徽章分界跟著變（驗證單一來源）

### Phase 5：收尾

1. README 更新：決策面板與籌碼分說明、優先級公式摘要、**可調參數位置**（`SCORE_BANDS`、`CHIP_WEIGHTS`、`compute_attention` 內門檻）、token 設定與驗證指令。
2. 全量實測：請使用者填入 token 後，刪 `cache/` 內容全刷 13 檔，確認無 402/429（額度內）、無 500；抽 2-3 檔打開明細目測籌碼面數字合理（與 FinMind 網站或券商 App 對照數量級）。
3. 最終 git commit。

## 3. 風險與假設（執行時對照）

1. **單位轉換**：已於 §1.1 實測定案（融資券＝張 ×1000；SBL＝股不轉換；chip 公式中 `vol20_last` 以股為準）。實作時抽 2330 複驗一筆即可。
2. **token 未填期間的執行假設**：token 填寫排在 Phase 5 由使用者處理，但 Phase 1-4 需要真實 FinMind 回應。已實測（2026-07-13）：三個新 dataset **匿名（無 token）皆可存取**（HTTP 200），所以不是硬卡點；但匿名額度較低（一般認知為 300 次/hr，此數字未實測、低信心），§1.2 的額度預算建立在有 token（600 次/hr）之上。執行 Phase 1-4 遇 402/429 時：記錄下來、改用 §1.1 的欄位定義繼續實作（測試本來就是 mock 不打網路），把實打驗證留到 Phase 5。
3. ETF（0050）與部分個股缺融資/外資持股資料，或餘額整列為 0 → 已設計 coverage 歸一化與除零防護路徑（見 Phase 2），需有測試覆蓋。
4. FinMind 欄位偶回字串或 null → getter 一律防禦性轉型 `int(float(x or 0))`。
5. 排序與 action 門檻是產品判斷的合理預設，README 標可調，不保證最優——執行者**不要**自行調整這些數值，照計畫實作。
6. `tick_tier_available()` 探測在免費 token 下仍會打 1 次 FinMind（會失敗降級），額度影響可忽略，不處理。
7. 執行者回報格式遵守專案守則：只允許「已驗證（附方式與結果）／已完成但未驗證／未完成（附卡點）」三態。
