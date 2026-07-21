# stock_tracker 盤中 Day Trading ＋ 盤後技術分析 開發計畫書

> 版本：v1.0（2026-07-20 定稿，經使用者核准）
> 執行者：Sonnet 5 (medium)。本文件自成一體，執行前不需要其他對話脈絡。
> 專案路徑：`C:\Users\william.chen\claude\stock_tracker`（Windows 11 ＋ PowerShell 5.1；Python 虛擬環境在 `.venv\`）

---

## 0. 背景與目標

本專案現況：已接 FinMind API，以 Volume Profile 與 Order Flow 指標做台股**盤後**分析（FastAPI 本機服務＋靜態快照 CLI）。本計畫擴充為：

1. **盤中**：常駐監控自選股 10-50 檔，即時計算 day trading 指標，觸發條件時發 Telegram 警報
2. **盤後**：強化短線（1-5 日）到中線（2-8 週）技術分析指標
3. **市場**：台股＋幣圈（Binance）
4. **硬性限制**：不開新證券戶（排除富果 Fugle、永豐 Shioaji）；只用免費資料源＋使用者既有帳戶（凱基證券戶、TradingView Essential、IBKR、Binance）

### 給執行者的三條紀律（沿用專案 CLAUDE.md 守則）

- 每 Phase 結束跑 `$env:PYTHONIOENCODING='utf-8'; .venv\Scripts\python -m pytest tests -q`，既有測試零回歸（2026-07-20 基準：55 passed）
- 回報只用三態：已驗證（附方式與結果）／已完成但未驗證／未完成（附卡點）
- 不動 Phase 範圍外的檔案；順手發現的問題記下來回報，不要改

---

## 1. 資料源選型（已查證、已由使用者確認）

| 資料源 | 角色 | 關鍵事實 |
|---|---|---|
| TWSE MIS（`mis.twse.com.tw`） | **台股盤中主源（第一軌）** | 約 5 秒級即時報價＋五檔；非官方公開端點，有被擋 IP/改版風險，必須節流＋降級。現有 `data_sources.py` 的 `get_quote()` 已在用此端點 |
| 凱基 KGI SUPER PY（superpy.kgieworld.com.tw） | **台股盤中升級源（第二軌，本期只留接口）** | 官方 Python API：真逐筆＋五檔＋K線＋回測。.NET 元件走 pythonnet，Windows 限定。使用者已有凱基戶，但需線上簽 API 同意書＋洽客服確認費用——**未完成前只寫 stub** |
| Binance 公開 API | **幣圈主源** | REST＋WebSocket 免帳戶免 key：aggTrade（含主動買賣方向）、depth、kline。三個市場中資料品質最好 |
| FinMind（免費層） | 盤後主源（維持現狀） | 免費層**拿不到盤中資料**（tick 限付費 Sponsor）；額度官方 600 次/hr（實測約 300）。現有快取策略不動 |
| yfinance | 降級備援 | 盤中降級鏈末端＋盤後 5 分 K |
| TradingView Essential | 可選輔助警報（Phase 5） | 含 40 個 webhook 警報（20 價格＋20 技術）；台股行情延遲 15 分、無資料拉取 API——**只能當警報輸入，不能當資料源** |
| IBKR | 本期不做 | 免費僅 15-20 分延遲行情，對 day trading 無意義；列入附錄升級路徑 |

## 2. 現有 codebase 可複用清單（不要重寫這些）

| 模組 | 位置 | 複用方式 |
|---|---|---|
| MIS 即時報價 | `data_sources.py:226-274` `get_quote()` | Phase 1 的 `MISSource` 包裝此邏輯並擴充五檔解析 |
| Volume Profile | `analysis.py:144-216` `volume_profile()` | Phase 2 的當日 VP 直接餵當日分鐘資料呼叫 |
| Order Flow tick-rule 估算 | `analysis.py:221-244` `of_from_minutes()` | Phase 2 即時 CVD 的估算邏輯來源 |
| OF 統計 | `analysis.py:267-306` `of_summary()` | delta/streak/divergence 演算法參考 |
| 警報優先級引擎 | `analysis.py:459-535` `compute_attention()` | Phase 3 盤中警報規則引擎沿用其「事件觸發＋優先級」模式 |
| 權重/門檻集中管理模式 | `analysis.py:19-50`（SCORE_WEIGHTS 等） | Phase 2/4 新指標照此模式集中參數 |
| 快取機制 | `data_sources.py`（`_cache_get`，當日快取） | Phase 3 盤中資料落地沿用 `cache/` 目錄慣例 |
| 測試風格 | `tests/test_*.py`（mock HTTP、`_fake_response()`、autouse 快取隔離 fixture） | 所有新測試照此風格，零真網路 |
| watchlist | `watchlist.json`（2026-07-20 時點 18 檔，以檔案實際內容為準） | 盤中監控直接讀同一份清單 |

現有盤後 pipeline（`pipeline.py`、`app.py`、`snapshot.py`）在 Phase 0-3 **完全不動**。

## 3. 指標規格

### 3.1 盤中 Day Trading（新模組 `intraday.py`）

| 函式 | 指標 | 台股（MIS 快照級） | 幣圈（Binance tick 級） |
|---|---|---|---|
| `vwap_bands()` | VWAP＋1σ/2σ 通道 | ✅ | ✅ |
| `opening_range()` | ORB 15/30 分鐘區間突破（台股 09:00 起算） | ✅ | ✅（UTC 日界或自訂錨點） |
| `intraday_vp()` | 當日 Volume Profile（複用 `volume_profile()`） | ✅ 分鐘累積 | ✅ |
| `realtime_cvd()` | 累積量差 CVD | ⚠️ 估算：快照量增量×內外盤方向（tick-rule） | ✅ 真 tick：aggTrade 的 `m` 欄位即主動方向 |
| `depth_imbalance()` | 五檔/深度失衡、大單掛牆與抽單偵測 | ✅ MIS 五檔 | ✅ depth stream |
| `relative_strength()` | 相對強弱 | vs 加權指數（MIS 可取 t00 大盤） | vs BTC |
| `volume_anomaly()` | 量能異常 | 相對 20 日**同時段**基準（台股日內量能是 U 型，不能用全日平均） | 同左 |

- 台股 CVD 是估算級，UI 與警報文案必須標註「估算」
- 試撮期（08:30-09:00）資料處理原則：落地存檔但**不計入** VWAP/CVD/當日 VP 等連續盤指標的累積，正式盤 09:00 起才開始累積；試撮期只用於觀察開盤前掛單失衡與模擬撮合價變化
- KD/RSI/MACD 等震盪指標不做盤中主指標（分鐘級雜訊高），不在本期範圍

### 3.2 盤後短中線（擴充 `analysis.py`）

- 保留既有：MA5/20/60、120 日 VP、OF delta5/streak/divergence、量價四象限、籌碼分
- 新增短線（1-5 日）：ATR、爆量長紅/長黑 K 棒型態、跳空缺口追蹤、5 日 RS 排名（相對 watchlist 內其他標的）
- 新增中線（2-8 週）：布林通道擠壓（squeeze）、Donchian 位置（55/120 日高低點距離）、MA20 乖離率
- 全部純函式＋權重制，併入 SCORE_WEIGHTS（重新配平，總和維持 100）

## 4. 分階段執行計畫

> 順序刻意「幣圈先行」：Binance 免申請、24 小時有行情，先把常駐架構的坑（WS 重連、增量計算、警報去重）踩完，再套回只有交易時段能測的台股。

### Phase 0：前置與 spike（半天）

使用者待辦（平行進行，不阻塞 Phase 1-2）：
1. 洽凱基客服（(02)2389-0088）確認 SUPER PY 費用/門檻，線上簽署 API 同意書
2. 跟 Telegram BotFather 建 bot，把 `TELEGRAM_BOT_TOKEN` 與 `TELEGRAM_CHAT_ID` 加進 `.env`（照 `.env.example` 慣例同步更新範本）

開發 spike：
- MIS 輪詢壓測：watchlist 檔數 × 5 秒間隔連續 1 小時（盤中時段執行），記錄成功率與是否被擋，決定安全輪詢參數
- Binance WS smoke test：訂閱 2-3 個交易對 aggTrade＋depth 跑 30 分鐘，驗證斷線重連
- **若開工時間不在台股交易時段**：不要等——先用保守預設值（MIS 支援一次請求帶多檔：每輪整批一次請求、批次間隔 ≥10 秒，即單檔更新頻率同為 ≥10 秒）繼續 Phase 1-2，MIS 壓測排到下一個交易日盤中補跑，跑完再回頭校正 `MISSource` 節流參數

**驗收**：spike 結論（含建議輪詢參數）寫進 `docs/spike_notes.md`；壓測若延後，在該檔註明「暫用保守預設值，待 YYYY-MM-DD 盤中補測」。

### Phase 1：資料層 — `realtime/` 可插拔介面（1-2 天）

- 統一資料模型：`Quote`／`Trade`／`Depth` dataclass
- `RealtimeSource` 抽象介面：`subscribe(codes)`、輪詢或 callback（`on_quote/on_trade/on_depth`）
- 三個 adapter：
  - `MISSource`：輪詢式，含節流（用 Phase 0 參數）與 yfinance 降級
  - `BinanceSource`：WebSocket，自動重連（指數退避），免 key
  - `KGISource`：**stub＋TODO 文件**，介面對齊另兩個 adapter，等使用者簽約後實作
- **驗收**：mock 資料餵 adapter 的單元測試通過；`data_sources.py` 零改動（`MISSource` 用 import 複用，不搬移原函式）。

### Phase 2：盤中指標引擎 `intraday.py`（2-3 天）

- 實作 3.1 的七個純函式，參數集中在 `INTRADAY_WEIGHTS`／`INTRADAY_THRESHOLDS`
- **驗收**：每個指標至少一個手算小案例測試；`pytest tests -q` 全綠。

### Phase 3：常駐監控 `monitor.py`＋Telegram 警報（2-3 天）

- 排程：台股 **08:30**-13:30（08:30-09:00 為試撮集合競價時段，MIS 在此期間提供模擬撮合價，指標計算需區分試撮與正式盤）、幣圈 24h，非交易時段休眠
- 主迴圈：拉新資料→增量更新指標→過警報規則→推 Telegram（直接 requests 打 Bot API，不加重依賴）
- 警報規則：沿用 `compute_attention()` 的事件觸發＋優先級模式；**去重與冷卻**（同檔同事件 N 分鐘內不重發，N 可設定）
- 韌性：斷線降級鏈（MIS→yfinance）與恢復；當日資料落地 `cache/intraday_{code}_{date}.json` 供盤後複盤
- **驗收**：用錄製行情做 replay 測試（不依賴開盤時段）；Binance 端實跑 30 分鐘，至少觸發一則 Telegram 警報——送達驗證標準：Bot API `sendMessage` 回 HTTP 200 且回應 JSON `ok: true`，並把該回應與警報內容記進回報（使用者事後可在手機上核對）。

### Phase 4：盤後短中線指標強化（1-2 天）

- 實作 3.2 新指標進 `analysis.py`，併入評分；`static/index.html` 增顯示欄位
- **驗收**：新指標手算案例測試＋既有測試（現況 55 個）零回歸；拿實際公司代碼跑一次，檢查標準：所有新欄位非 null/非 NaN、落在各自定義域（ATR>0、乖離率為百分比、RS 排名在 1~watchlist 檔數之間、Donchian 位置在 0-100%），並抽一檔用收盤資料手算 ATR 核對。

### Phase 5：整合與可選項（1-2 天）

- 盤中落地分鐘資料回饋盤後（取代 yfinance 5 分 K 餵 `of_from_minutes()`）
- （可選）TradingView webhook 接收端：`app.py` 加 `/webhook/tv` endpoint，把 Essential 警報轉發進同一 Telegram 管道
- （可選）KGI SUPER PY adapter 實作——僅在使用者已完成簽約時
- 文件：README 更新＋操作手冊（啟動監控、調警報門檻）
- **驗收**：文件派 fresh-context agent read-back。

## 5. 已知限制與風險

- **MIS 非官方端點**：可能被擋 IP 或改版。緩解：節流、降級鏈、KGI 升級路徑
- **台股盤中 CVD 是估算**：精度不如真 tick，僅供方向參考，文案標註
- **KGI 費用未知**：若門檻不合適，系統以 MIS 軌完整運作，不阻塞
- **FinMind 額度**：50 檔冷刷新約 300 次請求，貼近實測上限——維持現有當日快取、錯峰執行

## 6. 附錄：未來升級路徑（本期不做）

- 富果 Fugle／永豐 Shioaji：願意開戶的話，免費層即有正式 WebSocket 逐筆/五檔
- IBKR 美股：訂閱即時數據包後可循 `RealtimeSource` 介面加 adapter
- TradingView 台股無延遲行情：約 US$20/月加購（第三方來源說法，金額以官方為準）
