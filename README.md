# 台股追蹤器 v2（本地 Python 版）

## 功能

- **今日決策面板**：開首頁最上方，按行動優先級排序告訴你今天該看哪幾檔、為什麼、建議動作（見下方「決策面板與籌碼分」）
- 追蹤清單（`watchlist.json` 持久化）、即時報價、本益比
- K 線圖＋ MA5/20/60 ＋支撐壓力
- **Volume Profile**：POC／Value Area（70%）／HVN･LVN，直方圖疊在 K 線旁
- **Order Flow**：內外盤 delta 與累積 delta
  - FinMind 付費級 token → 逐筆 tick 精確計算
  - 免費級 token → 自動降級為 yfinance 分鐘 K 的 tick-rule 推估（UI 會標示）
- 量價四象限、20 日趨勢推估區間、綜合技術評分、訊號回測勝率
- **籌碼面子分**：法人買賣超／融資融券／外資持股比／借券賣出餘額，與技術分並列顯示，不揉進同一個數字
- 資料來源優先度：**FinMind → yfinance → TWSE**（每檔股票顯示實際來源）

## 安裝與啟動

```powershell
cd C:\Users\william.chen\claude\stock_tracker
python -m venv .venv
.venv\Scripts\Activate.ps1
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m uvicorn app:app --app-dir src --port 8932
```

瀏覽器開 http://localhost:8932

日常使用直接雙擊 `start_server.bat` 即可（會顯示手機用的網址並以區網模式啟動）。

## 目錄結構

```
stock_tracker/
├─ src/              應用程式碼（app / analysis / data_sources / monitor / snapshot …）
│  └─ realtime/      即時報價來源（MIS、KGI、Binance）
├─ tests/            pytest（conftest.py 負責把 src/ 掛上 sys.path）
├─ scripts/          一次性驗證／spike 腳本，不進 pytest
├─ docs/             設計文件與 spike 紀錄（只放 .md/.txt）
├─ static/           前端（index.html、icon、manifest）
├─ cache/            資料快取，可整個刪（gitignored）
├─ dist/             snapshot.py 產出的靜態站（gitignored）
├─ legacy/           舊版單檔 HTML，保留參考
└─ watchlist.json    追蹤清單
```

`src/` 內的模組用 `BASE_DIR`（＝repo 根目錄）定位 `cache/`、`static/`、`watchlist.json`、`.env`，
所以指令一律**從 repo 根目錄執行**（`python src/snapshot.py`、`uvicorn app:app --app-dir src`）。

## 手機存取（同一 Wi-Fi）

1. 電腦雙擊 `start_server.bat` 啟動，畫面會顯示 `手機（同一Wi-Fi）請開 http://192.168.x.x:8932`
2. 手機連同一個 Wi-Fi，瀏覽器開那個網址
3. 想要 App 體驗：手機瀏覽器選單 →「加入主畫面」，會以全螢幕獨立視窗開啟

**第一次使用前，需要開防火牆（一次性設定，要系統管理員權限）：**

用「系統管理員身分」開 PowerShell，執行以下兩行：

```powershell
# 1. 把家用 Wi-Fi 從 Public 改為 Private（比在 Public 網路開 port 安全；公司/公共 Wi-Fi 不要改）
Set-NetConnectionProfile -Name "chenfamily" -NetworkCategory Private

# 2. 開放 8932 port（只對 Private 網路生效；日後移除：netsh advfirewall firewall delete rule name="stock-tracker-8932"）
netsh advfirewall firewall add rule name="stock-tracker-8932" dir=in action=allow protocol=TCP localport=8932 profile=private
```

**手機連不上時，依序檢查：**
1. 手機和電腦是否真的在同一個 Wi-Fi（訪客網路算不同網路）
2. 上面的防火牆規則是否已加、Wi-Fi profile 是否為 Private（`Get-NetConnectionProfile` 查看）
3. 路由器是否開了「AP 隔離／訪客隔離」（公司 Wi-Fi 常見，會擋裝置互連，只能請 IT 開或換網路）
4. 電腦 IP 是否變了（DHCP 會換 IP，重跑 `start_server.bat` 看最新網址）

**安全註記**：此 App 無帳號驗證，同網段任何人知道網址就能看到追蹤清單並增刪。家用網路通常無妨；若在意，之後可以加簡單的 token 驗證。

**注意**：`start_server.bat` 與 Claude preview（launch.json）都用 8932 port，不要同時啟動。

## 手機雲端版（PC 關機也能看）

架構：GitHub private repo（程式碼＋`watchlist.json`）＋ GitHub Actions 排程執行 `snapshot.py` 產生靜態快照 ＋ Cloudflare Pages Direct Upload（`wrangler` 從 CI 直傳，repo 不連 Cloudflare、保持私有）。

```
GitHub Actions（排程）
  └─ src/snapshot.py --mode full|light ──▶ dist/（index.html + data.json + _headers）
       └─ npx wrangler pages deploy dist ──▶ https://<專案名>.pages.dev
                                                  ▲
                                          手機瀏覽器（純讀，加入主畫面當 App）
```

網址難猜＋`_headers` 帶 `X-Robots-Tag: noindex`＋頁面 `<meta name="robots">`，不需要密碼。手機端**純讀**：新增/刪除按鈕在靜態模式下自動隱藏，watchlist 只能回 PC 改，或用手機 GitHub App／網頁編輯 `watchlist.json`。

### 排程表（`.github/workflows/snapshot.yml`）

| cron（UTC） | 台北時間 | 模式 |
|---|---|---|
| `7,27,47 1-5 * * 1-5` | 09:07–13:47 每 20 分 | light（只重打報價，不重抓 FinMind） |
| `10 0 * * 1-5` | 08:10 | full（早晨重建） |
| `40 13 * * 1-5` | 21:40 | full（盤後：法人/融資券/外資持股/借券已入庫） |

也可以到 GitHub → Actions → snapshot → **Run workflow** 手動選 `full` 或 `light` 立即跑一次。

### GitHub Secrets

4 個 secret 放在 repo 的 **Environment「Cloudflare」**底下（不是直接放在 repo-level secrets）——`.github/workflows/snapshot.yml` 的 job 因此宣告了 `environment: Cloudflare`，缺這行的話 `secrets.*` 會全部解析成空字串。

| Secret | 內容 |
|---|---|
| `FINMIND_TOKEN` | 現有的 FinMind token |
| `CLOUDFLARE_API_TOKEN` | Cloudflare My Profile → API Tokens 建立，權限僅 `Account / Cloudflare Pages / Edit` |
| `CLOUDFLARE_ACCOUNT_ID` | **32 位十六進位字串**（dash.cloudflare.com → Workers & Pages → 右側欄複製；不是帳號名稱，填錯會報 Cloudflare API code 7003） |
| `CF_PAGES_PROJECT` | 隨機專案名（如 `tw-xxxxxxxxxxxx`），部署網址即 `https://<此值>.pages.dev` |

### 用手機改 watchlist

1. 手機裝 GitHub App（或瀏覽器登入 github.com）
2. 開 repo → `watchlist.json` → 編輯 → 加入或刪除股票 → commit 到 `main`
3. 等下一次排程（最多約 20 分鐘），或到 Actions 頁手動 Run workflow（`mode=full`）立即生效

### 故障排查（Actions 紅燈看哪裡）

點進失敗的 run → 展開紅色 ✕ 的那個 step 看訊息。目前遇過兩種：

- **`deploy` 報 `Must specify a project name.`**：workflow job 沒吃到 secrets——檢查 job 是否有 `environment: Cloudflare`（名稱要跟 Settings → Environments 裡的環境名一字不差）。
- **`deploy` 報 `Could not route to /accounts/<一串文字>/... code: 7003`**：`CLOUDFLARE_ACCOUNT_ID` 填成了帳號名稱（如 `xxx@gmail.com's Account`），要換成 32 位十六進位 Account ID。

`check secrets` 這個 step 會提早擋掉以上兩種錯誤並印出明確訊息，比等 wrangler 報隱晦錯誤好定位。

### 手機加入主畫面

手機瀏覽器開部署網址 → 選單「加入主畫面」→ 以獨立視窗（standalone）開啟，manifest 沿用本機版的 `static/manifest.json`。

## 決策面板與籌碼分

**技術分**（`-100~100`）：MA 位置/排列、量價四象限、20日趨勢、Volume Profile 位置、Order Flow，權重見 `analysis.py` 的 `SCORE_WEIGHTS`。

**籌碼分**（`-100~100`，獨立子分，不併入技術分）：法人買賣超（權重40）／融資融券（20）／外資持股比（20）／借券賣出餘額（20），任一項缺資料時按可用權重歸一，四項全缺則整體為 `null`（明細卡會標示「N/4 項資料」）。權重常數在 `analysis.py` 的 `CHIP_WEIGHTS`。

**行動優先級**（決策面板排序依據）：

```
綜合分 = 0.6 × 技術分 + 0.4 × 籌碼分（籌碼分缺資料時 = 技術分）
優先級 = min(100, round(0.6 × |綜合分| + 事件加分))
```

事件加分：today 突破/跌破 MA60（各+15）、OF 量價背離（+15）、爆量>2倍均量（+10）、OF 或外資連續同向天數 ≥3（+10）、籌碼任一組件訊號強烈（+10）、價格在 Value Area 外（+5）。

行動判斷（由上而下取第一個命中）：綜合分≥40 且優先級≥50 →「準備進場」；綜合分≤-40 且優先級≥50 →「減碼警示」；優先級≥50 →「今日關注」；優先級≥25 →「觀察」；否則「無變化」。

**可調參數位置**（皆為產品判斷的合理預設，非唯一最優解）：
- `analysis.py` 的 `SCORE_BANDS`／`SCORE_LABELS`：技術分與籌碼分的多空標籤分界（前端經 `/api/config` 同步取用，改這裡前端會自動跟著變）
- `analysis.py` 的 `CHIP_WEIGHTS`：四個籌碼組件的權重
- `analysis.py` 的 `ACTION_THRESHOLDS`：行動優先級的 40/50/25 門檻
- `analysis.py` 的 `compute_attention()` 函式內：各事件加分的分值與各組件滿分刻度（法人 10%、融資 5%、外資持股 1pp、借券 5%）寫在 `chip_analysis()` 內

## 設定

- `.env`：`FINMIND_TOKEN=你的token`（https://finmindtrade.com 免費註冊後在會員頁取得）。
  沒填也能跑（FinMind 匿名額度較低或自動退到 yfinance），填了升到 600 次/hr。
  填完後用這行驗證 token 有生效、且能正常打 API：
  ```powershell
  .venv\Scripts\python -c "import sys; sys.path.insert(0,'src'); import data_sources as ds; print(ds.FINMIND_TOKEN[:8] or 'NO TOKEN'); print(len(ds._finmind('TaiwanStockPrice','2330','2026-07-01')), 'rows')"
  ```
- `watchlist.json`：追蹤清單，可直接編輯，格式 `[{"code": "2330", "name": "台積電"}, ...]`。
  上櫃股票直接填代碼即可（FinMind/TWSE 原生支援，yfinance 備援會自動嘗試 .TWO）。
- `cache/`：日線等資料的當日快取，可隨時整個刪掉。

## 盤中監控（monitor.py）

常駐輪詢監控：抓報價／逐筆 → 累積 CVD／VWAP／開盤區間（ORB）／委買賣簿失衡等盤中指標 → 觸發到門檻就發 Telegram 警報 → 同時把每輪快照寫進 `cache/intraday_{code}_{date}.json`，供盤後分析（`pipeline.py`）優先讀取，減少對 yfinance 的依賴（見下方「已知限制」）。

**啟動方式**：

```powershell
cd C:\Users\william.chen\claude\stock_tracker
.venv\Scripts\python src\monitor.py
```

不帶參數執行 `monitor.py` 的 `main()`：追蹤清單抓自 `watchlist.json`（與網頁 UI 共用），台股盤中時間內（08:30–13:30，含試撮）每 30 秒輪詢一次，永久跑到手動關閉（Ctrl+C）為止。若要寫腳本自訂追蹤清單、輪詢間隔或跑固定輪數（例如測試用），改成 `import monitor` 後呼叫 `monitor.main(codes=[...], sleep_seconds=..., max_iterations=...)`；細節見 `monitor.py` 檔頭 docstring。

**加密貨幣（Binance）追蹤**：目前僅接好資料層與指標計算（`realtime/`、`intraday.py`），`main()` 尚未接上即時 WebSocket 推送迴圈（`crypto_codes` 預設空清單）；`monitor.py` 檔頭 docstring 的「KNOWN SIMPLIFICATION」段落有記錄，屬 Phase 5 待補項。

**怎麼調整警報門檻**：

- `alerts.py` 第 23 行 `ALERT_RULES`：`cvd_window`（CVD 反轉偵測用的比較窗格數）與 `priorities`（各警報類型：`vwap_breakout`／`orb_breakout`／`cvd_reversal`／`depth_wall`／`volume_anomaly` 的優先分數，決定要不要送、送幾分）。
- `intraday.py` 第 23 行 `INTRADAY_THRESHOLDS`：指標本身觸發的門檻，例如委買賣簿失衡比例（`imbalance_side`）、大單牆倍數（`wall_multiple`）、委託單被抽掉的比例（`pulled_order_pct`）、爆量倍數（`volume_anomaly_ratio`）。改這裡會影響 `ALERT_RULES` 的警報是否觸發（門檻在指標層，優先分數在警報層，兩層都可以調）。

**Telegram 設定**：

1. 跟 Telegram 的 BotFather 對話建立一個 bot（`/newbot`），取得 bot token；再跟該 bot 對話一次取得你的 `chat_id`（例如用 `https://api.telegram.org/bot<token>/getUpdates` 查）。
2. 在專案根目錄的 `.env`（`.env.example` 有範本）加兩行：
   ```
   TELEGRAM_BOT_TOKEN=你的bot token
   TELEGRAM_CHAT_ID=你的chat id
   ```
3. 沒填也不會壞：`alerts.py` 的 `send_telegram()` 讀不到就跳過送出、不丟例外，`monitor.py` 照常記錄快照與 log，只是不會實際推播。

**已知限制**：
- 台股 CVD／Order Flow 皆為**估算級**，不是真逐筆內外盤：盤中即時走 tick-rule 推估（`intraday.py`），盤後走 `analysis.py` 的 `of_from_minutes()`（yfinance 5 分 K 的 tick-rule 推估）或 `of_from_ticks()`（FinMind 付費 tick，僅付費 token 可用）。
- `pipeline.py` 現在會優先讀當日 `cache/intraday_{code}_{date}.json`（`intraday_feedback.py` 轉換）取代 yfinance 分鐘 K，但這個快取目前**沒有真實成交量欄位**（`monitor.py` 的 `_persist_snapshot()` 尚未記錄逐筆量），只能用「每分鐘快照筆數」當活躍度代理值，比真實量更粗略；沒有當日快取（例如監控沒開過、非交易日）時自動退回 yfinance。
- KGI SUPER PY（券商 API）尚未串接——需要使用者完成簽約後才會排入開發。
- TradingView webhook 尚未實作——計畫書列為可選項，目前優先度較低，暫緩。

## 免責

技術評分、訊號、回測勝率與推估區間均為歷史統計整理，屬機率傾向而非預測，不構成投資建議。
