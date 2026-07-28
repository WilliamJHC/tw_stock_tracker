# stock_tracker 盤中 Day Trading ＋ 盤後技術分析 開發計畫

> 狀態：草稿（等待 codebase 探索與 API 研究結果回填）

## Context（背景與目標）

使用者現有 `stock_tracker`（`C:\Users\william.chen\claude\stock_tracker`，本地 Python 專案）已接 FinMind API，以 Volume Profile 與 Order Flow 指標做台股**盤後**分析。本計畫要擴充為：

1. **盤中**：常駐監控自選股 10-50 檔，即時計算 day trading 指標，觸發條件時發警報
2. **盤後**：強化短線到中線的技術分析指標
3. **市場範圍**：台股＋幣圈（Binance）一起做
4. **硬性限制**：只用現有帳戶（TradingView Essential、凱基證券戶、IBKR、Binance），**不開新戶**（排除富果 Fugle、永豐 Shioaji 這類要開戶的即時 API）

最終交付物：一份計畫書，交由 Sonnet 5 (medium) 執行。本 session 在計畫書獲同意後即停止，不動手實作。

**本計畫獲同意後的執行步驟（本 session 唯一要做的事）：**
1. 把本計畫內容整理成正式計畫書，寫到 `C:\Users\william.chen\claude\stock_tracker\docs\dev_plan_intraday.md`（含給 Sonnet 5 的執行指引：可複用模組的檔案:行號、每 Phase 驗收條件、使用者待辦清單）
2. 派 fresh-context agent 對計畫書做 read-back 驗證（「照這份文件能不能直接開工？哪一步會卡住？」），修正發現的洞後再驗一次
3. 回報後停止——**不寫任何程式碼**

## 已確認的使用者決策（2026-07-20）

- 盤中模式：常駐監控＋警報（非手動查詢）
- 監控範圍：自選股 10-50 檔
- 資料源限制：不開新戶，只用 FinMind／TWSE OpenAPI／Yahoo／現有四帳戶
- 市場：台股＋幣圈；Binance 免費 API 可先驗證即時架構再套回台股

## 現有 codebase 盤點（2026-07-20 Explore agent 掃描）

架構：`app.py`（FastAPI 本機服務）＋ `pipeline.py`（逐檔抓取→分析→組裝）＋ `data_sources.py`（FinMind→yfinance→TWSE 三層 fallback、當日快取）＋ `analysis.py`（純函式指標引擎）＋ `snapshot.py`（CLI 靜態快照）。tests/ 有 40+ 單元測試（mock HTTP、零真網路）。

**可直接複用（不要重寫）：**
- `data_sources.py:226-274` `get_quote()`：TWSE MIS 即時報價（不快取）→ yfinance 降級——盤中輪詢的現成基礎
- `analysis.py:144-216` `volume_profile()`：POC/VAH/VAL/HVN/LVN，支援日線與分鐘線
- `analysis.py:221-264` Order Flow 雙路線：`of_from_ticks()`（FinMind 付費層）與 `of_from_minutes()`（tick-rule 估算，免費層）；`analysis.py:267-306` `of_summary()`（delta5/ratio5/streak/divergence）
- `analysis.py:459-535` `compute_attention()`：優先級 0-100、行動標籤、觸發事件——警報引擎的現成核心
- `data_sources.py` 快取機制與 `tick_tier_available()` 付費層探測降級
- 權重與門檻集中在 `analysis.py:19-50`（SCORE_WEIGHTS/CHIP_WEIGHTS/ACTION_THRESHOLDS）

**現況缺口（本計畫要補的）：**
1. 無常駐盤中迴圈——現在是 request-driven（打 API 才算一次）
2. 無警報通知管道（只有網頁上的 attention 欄位）
3. 盤中資料粒度不足：free tier 只有 TWSE MIS 快照＋yfinance 5 分 K，無逐筆
4. 無幣圈（Binance）資料源
5. 盤後指標偏「評分」，短中線 setup 類指標（型態、相對強弱、波段結構）待補

## 免費 API 評比與選型（2026-07-20 研究 agent 查證）

| 資料源 | 免費層能拿到什麼 | 盤中即時 tick/五檔 | 申請門檻 | 本計畫的角色 |
|---|---|---|---|---|
| FinMind（現用） | 日K、法人/融資券/借券等籌碼盤後資料 | **不行**——tick/快照官方文件明寫僅限付費 Sponsor | 免費註冊（已有） | 盤後資料主源（維持現狀） |
| TWSE/TPEx OpenAPI | 日/月級歷史統計 | 不行（純盤後） | 免申請 | 盤後備援（維持現狀） |
| TWSE MIS（mis.twse.com.tw，非官方端點，現有 `get_quote()` 已在用） | 約 5 秒級即時報價＋五檔 | 可以，但非公開端點，有被擋 IP/改版風險 | 免申請 | **盤中主源**（唯一符合「不開新戶」的台股即時管道），必須做輪詢節流＋斷線降級 |
| yfinance | 日K、5分K（延遲） | 延遲行情 | 免申請 | 盤中降級備援＋盤後分鐘K |
| Binance | REST/WebSocket 即時逐筆/K線/深度，**免帳戶** | 可以，最完整 | 無 | **幣圈主源**，也是驗證常駐即時架構的試金石 |
| IBKR（已有戶） | 15-20 分鐘延遲行情免費；即時要另訂閱付費數據包 | 延遲可、即時要錢 | 已有帳戶 | 本期不整合（延遲行情對 day trading 無意義），留在 roadmap |
| Fugle / Shioaji | 免費層有即時 WS，但需開富果/永豐戶 | 可以 | **要開新戶——使用者已排除** | 不採用；計畫書附錄留升級路徑 |
| **凱基 KGI SUPER PY**（superpy.kgieworld.com.tw） | 即時行情、逐筆、五檔、盤後數據、K線、回測（官方 Python API，.NET 元件走 pythonnet，Windows 限定） | **可以，真 tick＋五檔** | 已有凱基戶＋線上簽署 API 同意書；**費用/門檻官網未揭露，需洽客服 (02)2389-0088** | **台股盤中升級源**（雙軌之二，接口預留） |
| TradingView Essential | webhook alert 官方確認含在 Essential（20 價格＋20 技術警報，共 40 個）；但無官方資料拉取 API，台股行情預設延遲 15 分鐘 | 不適合當資料源 | 已有訂閱 | 輔助警報管道（可選，Phase 5）；不當資料源 |

**選型結論（使用者已確認 2026-07-20）：**
- 台股盤中：**雙軌**。第一軌 TWSE MIS 快照輪詢（5-10 秒級）立即可用，把監控系統先蓋起來；第二軌預留 KGI SUPER PY adapter 接口，待使用者洽凱基確認費用並簽署 API 同意書後接上，升級到真 tick＋五檔。架構上以統一的 RealtimeSource 介面實現可插拔。
- 台股盤後：維持 FinMind（現有 pipeline 不動）。
- 幣圈：Binance WebSocket（aggTrade＋depth＋kline），免帳戶、免申請、24 小時可測——用它先驗證常駐即時架構，再套回台股。
- 警報管道：**Telegram Bot**（使用者需自行跟 BotFather 建 bot 拿 token）。
- TradingView Essential：僅作為可選的輔助警報來源（webhook 接收端，Phase 5），不當資料源。

## 指標選型

### 盤中 Day Trading（分鐘級，資料粒度決定可行性）

| 指標 | 台股（MIS 快照級） | 幣圈（Binance tick 級） | 說明 |
|---|---|---|---|
| VWAP ＋ 標準差通道 | ✅ 可做 | ✅ | day trading 錨點指標，判斷多空成本線；現有 codebase 沒有，**新增** |
| 開盤區間突破（ORB 15/30min） | ✅ | ✅ | 台股 09:00-09:30 區間突破，經典日內 setup |
| 即時 Volume Profile（當日） | ✅（分鐘累積） | ✅ | 複用 `volume_profile()` 餵當日分鐘資料，找日內 POC/VA |
| Order Flow delta（CVD 累積量差） | ⚠️ 估算級（快照內外盤/tick-rule） | ✅ 真 tick（aggTrade 有主動方向） | 複用 `of_from_minutes()` 邏輯改成即時累積；Binance 走新的 tick 版 |
| 五檔掛單失衡（bid/ask imbalance） | ✅（MIS 有五檔） | ✅（depth stream） | **新增**：大單掛牆、抽單偵測 |
| 相對大盤強弱（RS vs 加權指數/BTC） | ✅ | ✅ | 日內選強汰弱的核心 |
| 量能異常（相對 20 日同時段量） | ✅ | ✅ | 台股日內量能有 U 型結構，要用「同時段基準」不是全日平均 |
| MACD/KD/RSI 等震盪指標 | 不建議當主指標 | 同左 | 分鐘級雜訊高，只做輔助過濾 |

### 盤後短線到中線（日K 級，多數可複用現有引擎）

- **既有（保留）**：MA5/20/60 排列、120 日 Volume Profile、OF delta5/streak/divergence、量價四象限、籌碼分（法人/融資券/外資持股/借券）
- **新增（短線 1-5 日）**：ATR（波動度與停損距離）、爆量長紅/長黑 K 棒型態、跳空缺口追蹤、5 日 RS 排名
- **新增（中線 2-8 週）**：布林通道擠壓（squeeze，盤整突破前兆）、55/120 日高低點距離（Donchian 位置）、MA20 乖離率、週線級 VP 位置
- **原則**：全部進 `analysis.py` 純函式＋權重制，沿用 SCORE_WEIGHTS 模式，可測試、可調參

## 分階段開發計畫（交付給 Sonnet 5 medium 執行）

> 執行順序刻意讓「幣圈先行」：Binance 免申請、24 小時有行情，最適合把常駐架構的坑（WebSocket 重連、增量計算、警報去重）先踩完，再套回只有交易時段能測的台股。

### Phase 0：前置與 spike（半天）
- 使用者待辦（與開發平行進行，不阻塞 Phase 1-2）：
  1. 洽凱基客服確認 SUPER PY 費用/門檻，線上簽署 API 同意書
  2. 跟 Telegram BotFather 建 bot，把 token 與 chat_id 放進 `.env`
- 開發 spike：
  - MIS 輪詢壓力測試：50 檔、5 秒間隔、連續 1 小時，確認不被擋（記錄實際可行的輪詢節奏）
  - Binance WebSocket smoke test：訂閱 2-3 個交易對的 aggTrade＋depth，跑 30 分鐘確認斷線重連
- 產出：spike 結論寫進 `docs/`，決定 MIS 安全輪詢參數

### Phase 1：資料層 — RealtimeSource 可插拔介面（1-2 天）
- 新增 `realtime/` 模組，定義統一資料模型（Quote/Trade/Depth dataclass）與 `RealtimeSource` 抽象介面（subscribe/poll、on_quote/on_trade/on_depth callback）
- 實作三個 adapter：
  - `MISSource`：輪詢式，包裝現有 `data_sources.py:226-274` 的 MIS 邏輯＋五檔解析，含節流與 yfinance 降級
  - `BinanceSource`：WebSocket（官方公開端點，免 key），aggTrade/depth/kline stream，自動重連
  - `KGISource`：**只寫接口 stub＋TODO 文件**（SUPER PY 是 .NET 元件，等使用者簽約後才能實測），介面對齊另兩個 adapter
- 現有盤後 `data_sources.py` 完全不動
- 測試：mock 資料餵 adapter，驗證統一資料模型輸出

### Phase 2：盤中指標引擎 `intraday.py`（2-3 天）
- 全部寫成純函式（沿用 `analysis.py` 風格），吃統一資料模型：
  - `vwap_bands()`：VWAP＋1σ/2σ 通道
  - `opening_range()`：ORB 15/30 分鐘區間與突破判定
  - `intraday_vp()`：當日 Volume Profile——**複用 `analysis.py:144-216` volume_profile()**，餵當日累積分鐘資料
  - `realtime_cvd()`：累積量差——台股 MIS 用快照增量×內外盤方向估算（複用 `of_from_minutes()` 的 tick-rule 思路）；Binance 用 aggTrade 的真實主動方向
  - `depth_imbalance()`：五檔/深度失衡、大單掛牆偵測
  - `relative_strength()`：vs 加權指數（台股）/ vs BTC（幣圈）
  - `volume_anomaly()`：相對 20 日**同時段**量能（處理台股日內 U 型量能結構）
- 權重與門檻集中成 `INTRADAY_WEIGHTS`/`INTRADAY_THRESHOLDS`（沿用 `analysis.py:19-50` 模式）
- 測試：每個指標用手算小案例驗證（沿用現有測試風格，mock 不打網路）

### Phase 3：常駐監控迴圈＋Telegram 警報（2-3 天）
- 新增 `monitor.py`：
  - 排程：台股 08:45-13:30（含試撮）、幣圈 24h；非交易時段自動休眠
  - 主迴圈：每輪拉新資料→增量更新指標→過警報規則→觸發即推 Telegram
  - 警報規則引擎：沿用 `compute_attention()`（`analysis.py:459-535`）的「事件觸發＋優先級」模式做盤中版；**去重與冷卻**（同一檔同一事件 N 分鐘內不重發）
  - Telegram 推播：直接打 Bot API（requests，不引入重依賴）
  - 韌性：資料源斷線→降級鏈（MIS→yfinance）→恢復重連；當日資料落地 `cache/intraday_{code}_{date}.json` 供盤後複盤
- 測試：用錄製的行情資料做 replay 測試（不依賴開盤時段）

### Phase 4：盤後短中線指標強化（1-2 天）
- `analysis.py` 新增純函式：ATR、布林 squeeze、Donchian 位置（55/120 日）、MA20 乖離率、5 日 RS 排名、跳空缺口追蹤、爆量長紅/長黑型態
- 併入 SCORE_WEIGHTS 評分（權重重新配平，總和維持 100）
- 前端 `static/index.html` 增顯示欄位
- 測試：每個新指標手算案例＋既有 40+ 測試不得變紅

### Phase 5：整合與可選項（1-2 天）
- 盤中落地資料回饋盤後分析（用當日真實分鐘資料取代 yfinance 5 分 K 餵 `of_from_minutes()`）
- （可選）TradingView webhook 接收端：本機 FastAPI 加一個 `/webhook/tv` endpoint，接收 Essential 的 40 個警報轉發進同一個 Telegram 管道
- （可選）KGI SUPER PY adapter 實作——若使用者已完成簽約
- 文件：README 更新、操作手冊（怎麼啟動監控、怎麼調警報門檻）

## 驗證方式（每 Phase 結束時執行）

1. **單元測試**：`$env:PYTHONIOENCODING='utf-8'; .venv\Scripts\python -m pytest tests -q`——新增測試通過且既有 40+ 測試零回歸
2. **實跑驗證**：
   - Binance 路線：任何時間可實測（跑 30 分鐘常駐，親眼看 Telegram 收到警報）
   - 台股路線：盤中時段實測 MIS 輪詢＋警報；盤後用 replay 測試補足
3. **文件 read-back**：計畫書與操作手冊派 fresh-context agent 讀檔驗證「照著能不能執行」（CLAUDE.md 守則 6）
4. **回報三態**：每 Phase 完成時聲明「已驗證（附輸出）／已完成但未驗證／未完成（附卡點）」

## 已知限制與風險（誠實條款）

- MIS 是非官方端點：可能被擋 IP 或改版。緩解：節流（spike 決定安全參數）、降級鏈、KGI 升級路徑
- MIS 快照級 Order Flow 是**估算**，精度不如真 tick——台股盤中 CVD 僅供方向參考，計畫書會在 UI/警報文案標註「估算」；Binance 端是真 tick 無此問題
- KGI SUPER PY 費用未知：若門檻不合適，系統仍以 MIS 軌完整運作，不阻塞
- FinMind 免費層 600 次/hr（實測約 300）：盤後 pipeline 已有當日快取，50 檔冷刷新約 300 次，需錯峰執行（維持現有快取策略即可）

## 附錄：未來升級路徑（本期不做）

- 富果 Fugle / 永豐 Shioaji：若未來願意開戶，免費層即有正式 WebSocket 逐筆/五檔
- IBKR 美股：免費僅 15-20 分延遲行情，對 day trading 無意義；若訂閱即時數據包可循 RealtimeSource 介面加 adapter
- TradingView 台股無延遲行情：約 US$20/月加購（第三方來源，金額請以官方為準）
