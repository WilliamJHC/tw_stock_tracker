# stock_tracker 推薦股票／追蹤清單 UI 改版 開發計畫書

> 版本：v1.0（2026-08-24，grill-me 收斂）
> 執行者：Sonnet 5。本文件自成一體，執行前不需要其他對話脈絡，但**Phase 0 開跑前務必先跟使用者核對一次「已查證事實」章節裡的抓取時數估算**——那是紙上推算，不是實測，落差可能影響整個排程設計。
> 專案路徑：`C:\Users\william.chen\claude\stock_tracker`（Windows 11 + PowerShell 5.1；venv 在 `.venv\`）

---

## 0. 背景與目標

首頁 UI 現況：單一表格＋「今日決策面板」（`static/index.html:202-205` 的 `renderDecisionPanel()`，只顯示追蹤清單裡已經有 attention 訊號的股票，本質是「追蹤清單子集摘要卡片」）。

改版目標，分兩個獨立區塊：

1. **推薦股票**（取代決策面板）：全市場選股策略選出的 5 檔，不限於使用者已追蹤的股票。
2. **追蹤清單**：現有表格，新增一個「標籤」欄位放 `準備進場`/`減碼警示`/`今日關注` 這幾個既有 attention action，取代消失的決策面板卡片。

這個決定**推翻了兩份既有文件當下的假設**，必須誠實記錄（呼應本專案「已知落差不要假裝已解決」的一貫作法，見 `optimize.yml` 註解、`docs/adr/0001`/`0002`）：

- `docs/adr/0002-auto-apply-ranking-weights.md`（2026-08-24 定案，同一天）背景段寫「這條管線目前又是純內部研究管線」——本計畫執行完後這句話不再成立。
- `docs/https-finlab-finance-blog-python-get-tai-memoized-lighthouse.md` 第 321 行「明確不做」表格：「Phase 6 順便幫多因子引擎補選股清單出口」——當時的理由是「範圍會擴大到 UI/報告設計，是規模不同的另一個決定」。本計畫就是那個「未來單獨評估」的時刻，透過 `/grill-me` 走完了一輪決策，不是不假思索地推翻原決定。

Phase 4 會補一份新 ADR 記錄這個範圍變化（見下）。

---

## 1. 已查證事實（grilling 過程中確認，執行者不用重查）

| 議題 | 事實 | 出處 |
|---|---|---|
| 「今日決策面板」的資料範圍 | 只顯示追蹤清單裡有 `attention.priority` 的股票，來自 `pipeline.build_watchlist_payloads()` | `static/index.html:536` `renderDecisionPanel()` |
| 官方選股邏輯是哪一條 | `active_ranking_weights`（Phase 6d 自動套用）透過 `finlab_factors.composite_scores(weights=...)` 生效，**不是** `finlab_strategy.py` 的 finlab 版 `ranking_position()` | `docs/adr/0002` |
| `finlab_factors.py` 依賴 | 純 stdlib，無 pandas/numpy/finlab 依賴；`finlab_strategy.py` 才拉 finlab（連帶 pandas/numpy），刻意跟其他部分隔離 | `requirements.txt:8-10`、`src/finlab_strategy.py` 檔案 docstring |
| numpy 政策 | 專案維持 stdlib-only，用兩項架構重構（O(n²)→O(n) + 9 分項欄位化）換到 numpy 帶來的效益，不引入 numpy | `docs/https-...-lighthouse.md:21-24, 133-155` |
| 全市場排名的計算成本 | `composite_scores()` 是 O(存活檔數 × log n) 的排序＋O(1) 查表，全市場 ~2000 檔一天算一次是毫秒級——**真正貴的是資料抓取，不是排名計算** | `src/finlab_factors.py:265-325` 程式碼本身 |
| 全市場資料快取現況 | `cache/factor_run/*.json`（`report_factor_ic.load_cached_universe()` 讀取）目前**沒有增量更新機制**，只能靠手動全量抓取（~19-22 小時）刷新 | `.github/workflows/optimize.yml` 註解「KNOWN GAP」 |
| FinMind 免費層限制 | 600 req/hr，不能批次查詢；全市場 2162 檔 stage-1（只抓日線）就要 ~2000 次請求 ≈ 3.3 小時 | `scripts/run_factor_backtest.py:10-19` |
| GitHub Actions 額度 | `stock_tracker` 是 **private repo**（`lighthouse.md:322` 提及這點是拒絕「每月全量重拋」的理由之一，但該行本身沒寫出具體分鐘數）；2000 分鐘/月是 GitHub 官方公開的免費方案私有 repo 額度，需與 `snapshot.yml`/`optimize.yml` 共用 | `docs/https-...-lighthouse.md:322`（private repo 這點）＋ GitHub 官方文件（額度數字，實作前建議重新核對是否為當前方案） |
| `get_daily()` 沒有「只抓新資料」的參數 | 目前固定抓 `days` 天回溯窗，不支援「自某日期起」增量查詢；縮小 `days` 只減少單次請求的資料量，**不減少請求數**（600/hr 的限制是算請求次數，不是資料量） | `src/data_sources.py:313` |
| `target_price` 欄位**目前不存在於程式碼裡**（fresh-context read-back 抓到的錯誤，已修正） | `docs/adr/0001`（2026-08-21 已接受）設計了 `analyze()` 該回傳 `target_price: {level, basis: "VAH"\|"VAL", as_of} \| None`，但 ADR 本文明講「這次調查/決策記錄本身不含程式碼變更…實作留給後續的 build 階段」——**至今未實作**。`CONTEXT.md`「目標價」詞條用現在式描述這個欄位，容易誤導成「已經存在」；`static/index.html:559-562` 現在仍是舊的前端自算邏輯（`score` 正負選 `vpVah`/`vpVal`）。本計畫 Phase 2 會把 ADR 0001 的實作補上——本來就是這次新增的兩個消費端（推薦卡片／追蹤清單標籤）要用到的東西，不是額外加戲 | 實測 `src/analysis.py:792-1044` `analyze()` 的 `return` 語句確認無此欄位；`docs/adr/0001` 全文 |
| `build_watchlist_payloads(codes=None)` 不限定追蹤清單 | 接受任意 `codes` 清單，回傳含 `vp`/`score` 的完整 payload（`target_price` 要等 Phase 2 實作 ADR 0001 後才會出現在這份 payload 裡）；RS 排名（跨股比較）在單碼／任意清單呼叫時會被跳過，不影響目標價判斷 | `src/pipeline.py:148-163` |
| `meta` 表的既有慣例 | `db.set_meta(conn, key, json.dumps(...))` / `db.get_meta(conn, key, default=None)`，簡單 KV store，`active_ranking_weights` 已經是這個模式的先例 | `src/db.py:540-549` |

---

## 2. 現有 codebase 可複用清單（不要重寫這些）

| 需求 | 複用對象 | 複用方式 |
|---|---|---|
| 全市場因子排名 | `src/finlab_factors.py` `composite_scores()` + `top_n()` | 直接呼叫，傳入 `weights=從 meta 讀出的 active_ranking_weights` |
| 全市場資料（不重抓） | `scripts/report_factor_ic.py` `load_cached_universe()` | Phase 1 排名腳本直接呼叫，無網路請求 |
| 排程腳本骨架 | `scripts/run_ranking_optimize.py` | Phase 1 新腳本照這個結構寫（讀 meta → 算 → 寫 meta → Telegram 通知），不是從零設計 |
| GitHub Actions workflow 骨架 | `.github/workflows/optimize.yml` | Phase 0/1 的新 workflow 照這個抄：`environment: Cloudflare` 讀 secrets、`actions/cache@v4` 存跨執行狀態 |
| 目標價／VAH/VAL | `docs/adr/0001` 已經設計好但未實作的 `target_price` 欄位規格 | Phase 2 把 ADR 0001 實作進 `analyze()`；推薦股票卡片與追蹤清單標籤都讀這個欄位，不要各自重算 VAH/VAL 選擇邏輯（那正是 ADR 0001 想根除的重複造輪子） |
| 任意股票清單的完整分析 | `pipeline.py` `build_watchlist_payloads(codes)` | 推薦的 5 檔直接傳進去，拿到跟追蹤清單股票同規格的 payload |
| KV 持久化 | `db.py` `get_meta`/`set_meta` | 新推薦名單、`as_of` 時間戳都寫這裡，不新增資料表 |
| Telegram 通知 | `alerts.py` `send_telegram()` | Phase 0/1 排程完成一律發一則摘要，比照 Phase 6d 的「通知是可觀測性，不是審核關卡」 |
| 前端 badge／tag 樣式 | `static/index.html` `.action-badge`/`.tag`/`.score-badge` CSS class | 新標籤欄位、「已追蹤」標記直接套用既有樣式，不新增樣式系統 |

---

## 3. 範圍決策摘要（grilling 定案，逐條列出避免實作時又被重新討論）

1. **資料源**：全市場選股（`finlab_factors.py` FinMind 版），非追蹤清單子集。
2. **語言／依賴**：維持 stdlib-only，不引入 numpy／pandas／finlab 到即時服務路徑。
3. **Phase 0（全市場日線增量更新）排程頻率**：**每週一次**（避開 GitHub Actions 免費額度）。
4. **Phase 1（推薦排名重算）排程頻率**：每日一次——這一步本身很便宜（見上表「全市場排名的計算成本」），可以天天跑，但排出來的名單新鮮度受限於 Phase 0 的每週資料更新，UI 必須誠實標示 `as_of` 日期，不能讓使用者誤以為是即時全新資料。
5. **快取位置**：`db.py` 的 `meta` 表新 key（暫定 `daily_recommendations`），不新增資料表、不另開 JSON 檔。
6. **與追蹤清單重疊**：推薦榜單不排除已追蹤股票，卡片上標「已追蹤」。
7. **推薦股票卡片欄位**：代碼、名稱、現價（即時另抓）、目標價（讀 `analyze()` 的 `target_price`，Phase 2 實作 ADR 0001 之後才有這個欄位）、三因子細項（營收年增/ROE/動能各自 rank）、已追蹤標記、「加入追蹤」按鈕。**不含** P優先級／原因說明／信心%——這些屬於追蹤清單專屬的 attention 系統，推薦股票的資料源不產生這些欄位。
8. **追蹤清單表格「標籤」欄位**：
   - 位置：展開鈕（▼）之後、代碼之前；移除「更新」（最後更新日期）欄，維持總欄數不變。
   - 內容：`準備進場`/`減碼警示`/`今日關注`；無訊號（無變化/無資料）留白，不顯示中性標籤。
   - 可排序，跟其他欄位一致。
   - hover title：把舊決策面板卡片專屬的「P優先級、原因說明、目標價、信心%」四項合併成一行文字放進去。目標價讀 `analysis.target_price`（Phase 2 實作 ADR 0001 之後才存在）——**推薦股票卡片跟追蹤清單標籤用的是同一個欄位、同一套算法，差別只在套用的股票清單不同（推薦的 5 檔 vs. 追蹤清單裡有 attention 訊號的股票），不是兩套邏輯**。P優先級／原因說明／信心% 這三項沒有對應欄位可延伸，維持只在追蹤清單標籤 hover title 出現。
9. **追蹤清單區塊**：表格上方加一個「追蹤清單」小標題，呼應「推薦股票」標題。
10. **文件**：新建 ADR 記錄 Phase 6 管線範圍升級為「對外服務的一部分」，內文提及推翻 `docs/adr/0002`、`lighthouse.md:321` 當下的假設。

---

## 4. Phase 拆解

### Phase 0：全市場日線增量更新器（每週）

**目標**：讓 `cache/factor_run/*.json` 的日線資料維持「至多一週前」的新鮮度，作為 Phase 1 排名的資料基礎。**只處理日線（bars），不處理營收/財報**——後者維持現有月頻缺口，這是本計畫刻意不解決的既有落差（見「明確不做」）。

**涉及檔案**：
- 新增：`scripts/update_universe_bars.py`（暫名）——對 `cache/factor_run/*.json` 裡每一碼呼叫 `ds.get_daily(code, days=30)`（小窗口，只為了拿到最新幾根 bar），跟快取裡既有的 `bars` 陣列做日期去重合併，取代整個 stage-1 全量重抓。
- 新增：`.github/workflows/update_universe_bars.yml`，`cron: '0 20 * * 0'`（每週日 UTC 20:00 ≈ 台北週一 04:00，避開交易時段），沿用 `optimize.yml` 的 secrets/cache 模式。

**實作要點**：
- 沿用 `scripts/run_factor_backtest.py` 現成的 `_RateLimiter`（`FINMIND_HOURLY_LIMIT=600`），不重寫節流邏輯。
- Phase 6a 的還原因子邏輯（`c_adj`，Yahoo 比值）也要在增量合併後重新套用到新 bar 上，否則新資料沒有 `c_adj`、`momentum_pct()` 會靜默退回原始收盤價——檢查 `_augment_bars_with_adj_factor()`（`run_factor_backtest.py`）能不能直接複用於「只有幾根新 bar」的情境，不行的話要另外處理。
- 這一步**不影響請求數量**（`get_daily()` 沒有「只抓新資料」的參數，縮小 `days` 只減少資料量、不減少 600/hr 限制下的請求次數計算），所以耗時估算仍是 ~2000 檔 ÷ 600 req/hr ≈ 3.3 小時，跟現有 stage-1 全量抓取同量級——**增量更新省的是「不用等 revenue/financials 那 3 倍的 Stage 2 請求」，不是省 Stage 1 本身**。這點務必在動工前跟使用者確認過一次估算是否可接受（本文件開頭已提醒）。

**驗證**：
- 跑一次 dry-run（不寫回快取），確認合併後的 `bars` 陣列日期遞增不重複，抽樣比對 `momentum_pct()` 在新資料前後的輸出差異是否合理（不是暴增/歸零）。
- 對照現有 `adjust.validate_adj_factor()` 的單調性檢查（Phase 6a 已有），跑一次確認新合併的 bar 沒有違規。

**風險**：中——`_RateLimiter`/快取合併邏輯是新寫的，一週一次的排程如果中途被 GitHub Actions 超時（單一 job 預設 6 小時上限，3.3 小時應該安全但要留意）需要能安全中斷續跑，比照 `run_factor_backtest.py` 的「per-code JSON 快取讓中斷可續跑」設計。

**前置**：無。

---

### Phase 1：每日推薦排名批次腳本

**目標**：每天用 Phase 0 維護的快取重算一次全市場排名，取 top 5，連同「這份排名基於哪一天的資料」的 `as_of` 一起寫進 `meta`。

**涉及檔案**：
- 新增：`scripts/run_daily_recommendations.py`——結構照抄 `scripts/run_ranking_optimize.py`：讀 `active_ranking_weights`（`meta` 沒有則 fallback `finlab_factors.RANKING_WEIGHTS`）→ `report_factor_ic.load_cached_universe()` 讀快取（無網路）→ `finlab_factors.composite_scores()` + `top_n(scores, 5)` → 寫 `meta`（見下方格式）。**這一步只需要排名，不需要 `target_price`／即時報價**——那些是「服務給使用者看」才需要的東西，屬於 Phase 2（`app.py`/`snapshot.py` 在真正要回應請求／產生快照時，才對這 5 檔呼叫 `pipeline.build_watchlist_payloads()` 拿完整 payload），避免 Phase 1 依賴 Phase 2 才會實作的欄位、造成前置關係倒置。
- 新增：`.github/workflows/daily_recommendations.yml`，`cron` 訂在台股開盤前（例如台北 08:00 前）。

**`meta` 寫入內容**（暫定 key：`daily_recommendations`，JSON 字串）：
```python
{
  "as_of": "<Phase 0 快取裡最新 bar 的日期，不是今天>",
  "generated_at": "<這次排程實際執行的時間戳>",
  "codes": [
    {"code": "2330", "name": "...", "composite_score": 0.87,
     "factor_ranks": {"revenue": 0.9, "roe": 0.8, "momentum": 0.75}}
    # ... 最多 5 筆
  ]
}
```
`as_of` 跟 `generated_at` 分開記，是因為兩者天生不同步（見 Phase 0 的每週頻率 vs 這裡的每日頻率）——UI 呈現時只該給使用者看 `as_of`，避免「每天都在變」的排程時間造成「這是即時資料」的錯覺。

**驗證**：
- `--dry-run` flag（照抄 `run_ranking_optimize.py` 的先例），本機跑一次確認輸出的 5 檔跟手動用 `composite_scores()` 算出來的結果一致。
- 快取是空的（Phase 0 還沒跑過任何一次）時要優雅降級（回傳空清單 + 明確錯誤訊息），不要整個排程失敗。

**風險**：低——排名計算本身是既有函式的直接呼叫，新增的只是排程膠水程式碼。

**前置**：Phase 0 至少成功跑過一次，`cache/factor_run/*.json` 非空。

---

### Phase 2：後端 API／snapshot 串接

**目標**：讓 `app.py`（即時模式）跟 `snapshot.py`（靜態快照模式）都能把 Phase 1 的推薦名單交給前端；順手把 `docs/adr/0001`（已定案未實作）的 `target_price` 欄位補進 `analyze()`——這是推薦卡片與追蹤清單標籤兩個新消費端都要用到的東西，不是額外加戲。

**涉及檔案**：
- 修改 `src/analysis.py`：在 `analyze()` 的 `score` 算出之後、`return`（`analysis.py:1029` 附近，實作時以當時行號為準）之前，新增 `target_price: {"level": float, "basis": "VAH"|"VAL", "as_of": "YYYY-MM-DD"} | None` 欄位，規格與 `score>0`/`score<0`/`score==0`/無 `vp` 四種情況的判斷邏輯完全照 `docs/adr/0001` 決策段。不動既有 `vp`/`vp_position` 欄位。
- 修改 `tests/test_analysis.py`：補上 ADR 0001「影響」段落點名的四種情況測試（`score>0`→VAH、`score<0`→VAL、`score==0`→`None`、無 Value Area→`None`）。
- 修改 `src/app.py`：新增 `@app.get("/api/recommendations")`，讀 `meta` 的 `daily_recommendations`，格式比照 `/api/suggestions` 的「沒資料時優雅降級」寫法（`meta` 是空的就回傳 `{"recommendations": [], "as_of": None}`，不是 404）。
- 修改 `src/snapshot.py`：`run_full()` 讀同一份 `meta`，把推薦名單（含即時報價，比照 `run_light()` 抓 quote 的方式）一起烘進 `data.json` 的新頂層欄位 `recommendations`；`run_light()` 也要對這 5 檔（若存在）刷新報價，不能只刷追蹤清單。

**驗證**：
- `pytest tests/test_analysis.py` 跑過新增的四種 `target_price` 情況，並確認既有測試零回歸（ADR 0001「影響」段落已明講這是非破壞性新增欄位）。
- `pytest tests/test_app.py`（若存在，否則新增對應測試）：mock `db.get_meta` 回傳一份假推薦名單，確認 `/api/recommendations` 回傳格式正確；`meta` 為空時確認回傳空清單而非例外。
- 本機跑 `python -m uvicorn app:app` 手動打 `/api/recommendations` 確認實際輸出，含 `target_price`。
- `snapshot.py --mode full` 本機跑一次，確認 `data.json` 裡 `recommendations` 欄位存在、5 檔都有報價與 `target_price`。

**風險**：低——`target_price` 的邏輯 ADR 0001 已經設計清楚，是把既有算法搬進後端，不是新設計；API/snapshot 串接都是既有模式的延伸。

**前置**：Phase 1（`meta` 裡要有資料可讀，即使是空清單）。

---

### Phase 3：前端 UI 改版

**目標**：`static/index.html` 兩個區塊改版。

**涉及範圍**（單一檔案，行號以目前版本為準，實作時重新定位）：
- 移除 `.decision-panel`／`renderDecisionPanel()`（`static/index.html:202-205`, `:536-596`），替換成「推薦股票」區塊：`fetch('/api/recommendations')`（即時模式）或讀 `snap.recommendations`（靜態模式），渲染卡片（代碼/名稱/現價/目標價/三因子細項/已追蹤標記/加入追蹤按鈕），並顯示 `as_of` 日期（比照現有 `lastUpdate` 元素的呈現方式）。目標價直接讀 payload 的 `analysis.target_price.level`（Phase 2 已實作 ADR 0001），已越過該水位時 `target_price` 是 `None`，卡片不顯示——**不要在前端重算 `bull`/`target`/`targetValid`**，那正是舊 `card()` 函式（`static/index.html:556-562`）的邏輯，ADR 0001 的目的就是把它收進後端、讓前端別再重新發明。
- 表格（`rowView()` / `renderTable()`，`static/index.html:288-682`）：
  - `rowView()` 新增一個欄位讀 `a.target_price`（`a` 是既有的 `d.analysis`），取代原本 `vpVah`/`vpVal` 讓前端自算的用途——`vpVah`/`vpVal` 兩個欄位若圖表覆蓋線還在用（`static/index.html:1069-1070`）則保留，只是不再用來算目標價。
  - 新增「標籤」欄位（`<th>`/`<td>`），位置在展開鈕之後、代碼之前；`colspan` 數字不變（移除「更新」欄補上這個新欄）。
  - 標籤內容讀 `v.action`（既有欄位，`rowView()` 已經有 `action: att ? att.action : null`），套用既有 `ACTION_CLASS`；`action` 為 `null`/`'無變化'` 時該格留白。
  - 標籤 `title` 屬性組合 `優先級 P${v.priority}、${v.reason}、目標 ${fmtP(v.target_price?.level)}`（`priority`/`reason` 是既有欄位，`target_price` 是 Phase 2 新增、直接讀不用重算）。
  - `sortBy('action')` 沿用既有排序機制。
  - 移除「更新」欄（`v.updated`，`static/index.html:678` 那個 `<td class="c-upd">`）。
- 表格上方加「追蹤清單」小標題（比照「推薦股票」標題的 HTML 結構）。
- `colspan` 數字、`disposeAllCharts()` 呼叫時機等既有機制不受影響，照舊。

**驗證**：
- 本機起服務（`uvicorn app:app`），瀏覽器實際打開頁面：推薦股票卡片渲染正確、加入追蹤按鈕可動作、追蹤清單標籤欄位位置正確、hover 顯示 P/原因/目標價、排序功能正常、無訊號股票標籤留白。
- 靜態模式（`snapshot.py` 產出的 `data.json` 版本）也要手動驗證一次，兩種模式不能有一種被漏掉。
- 這是前端大改，**依 CLAUDE.md 規則 6 走「實跑」驗證，不能只憑程式碼審閱判斷完成**。

**風險**：中——`renderTable()`/`rowView()` 是現有邏輯的核心，改動要避免破壞既有排序/篩選/展開功能；建議先跑一次既有測試（若有前端測試）或至少手動走過所有既有互動路徑，確認沒有回歸。

**前置**：Phase 2（`/api/recommendations` 要能回傳資料）。

---

### Phase 4：新 ADR

**目標**：記錄「Phase 6 選股管線從純內部研究升級為對外服務的一部分」這個架構決定，呼應本文件開頭「背景與目標」段落列出的兩處被推翻的假設。

**內容大綱**：
- 背景：`docs/adr/0002` 定案時的假設（純內部研究管線）、`lighthouse.md:321` 當時明確拒絕的理由。
- 決策：接受選股結果進入 UI，範圍限定於「唯讀展示 top 5 + 加入追蹤」，不涉及自動下單／自動調整追蹤清單。
- 為什麼現在可以：這是一次獨立的 `/grill-me` 決策過程，不是順手擴權；資料流已有 Phase 0/1 的新鮮度誠實標示機制，不是「假裝這是即時資料」。
- 考慮過但拒絕的方案：可以列本計畫過程中出現過的替代選項（例如 finlab-backed `ranking_position()`、watchlist 子集版推薦、每日全量抓取）。
- 影響：列出 Phase 0-3 新增/修改的檔案清單（可直接引用本文件的「涉及檔案」）。

**驗證**：per CLAUDE.md 規則 6，新建文件屬於「新建或大改」，寫完後派一個 fresh-context agent 做 read-back，確認引用路徑（`docs/adr/0002` 等）真的存在、內文邏輯站得住腳。

**前置**：Phase 0-3 的實際檔案異動已經發生（ADR 要記錄「做了什麼」，不是「打算做什麼」）——所以 Phase 4 應該放在 Phase 0-3 之後，而不是之前。

---

## 5. 明確不做／延後（本計畫刻意排除，避免實作時範圍蔓延）

| 項目 | 理由 |
|---|---|
| 真正的「每日」全市場資料更新 | GitHub Actions 免費額度撐不住（見「已查證事實」表），改為 Phase 0 每週更新 + 誠實標示 `as_of` |
| 營收/財報的增量更新 | 現有已知缺口（`lighthouse.md:287` 規劃但未實作），跟本計畫的「日線」增量更新是不同範圍，不在這次一併解決 |
| `finlab_strategy.py`（finlab 版 `ranking_position()`） | 官方選股邏輯已確認是 `finlab_factors.py`（見已查證事實表），沒有理由改用需要額外登入依賴的版本 |
| 推薦股票卡片的信心%／歷史勝率 | 舊決策面板的 `confBadge` 來自 `build_suggestions()`（回測驅動、watchlist 專屬），推薦股票的資料源是全市場因子排名，兩者不是同一套統計基礎，不能直接搬過去用 |
| 自動把推薦股票加進追蹤清單 | 卡片只提供「加入追蹤」按鈕，使用者手動點——維持「使用者決定要追蹤什麼」的既有設計，不引入自動化決策 |
| numpy／pandas 進即時服務路徑 | 已查證：全市場排名本身的計算成本用 stdlib 已經是毫秒級，真正的瓶頸在資料抓取（I/O），numpy 對此沒有幫助 |

---

## 6. 給執行者的紀律（沿用專案 CLAUDE.md 守則）

- 每個 Phase 結束都要照該 Phase 的「驗證」小節實際跑過，回報只用三態：已驗證（附方式與結果）／已完成但未驗證／未完成（附卡點）。
- 不動 Phase 範圍外的檔案；順手發現的問題記下來回報，不要改。
- Phase 0 開跑前，務必跟使用者核對一次「抓取時數估算」是否還成立（FinMind 限制、universe 檔數都可能隨時間變動）。
- `git commit` 可以自己做，但不自行 `push`、不開 PR；每個 Phase 完成後把分支名與 commit hash 寫進回報。
