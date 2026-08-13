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

**全程用完整路徑（`.venv\Scripts\python`），不要打裸 `python`／`pip`**：本機 PATH 上還有另一個專案（`report_generator`）的 venv 排在前面，裸 `python` 會跑到那個環境去，裝套件、建 venv 都會用錯環境。

```powershell
cd C:\Users\william.chen\claude\stock_tracker

# 第一次：用系統內建的 base Python 建 venv（不要用 .venv 自己的 python.exe 重建自己，
# 也不要用裸 python——理由見上一段與下方「疑難排解」）
& "C:\Users\william.chen\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\python.exe" -m venv .venv
# 上面那個路徑不存在的話（Store 版 Python 版本號會變），改用：
# & "C:\Users\william.chen\AppData\Local\Microsoft\WindowsApps\python.exe" -m venv .venv

.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m uvicorn app:app --app-dir src --port 8932
```

瀏覽器開 http://localhost:8932

日常使用直接雙擊 `start_server.bat` 即可（會顯示手機用的網址並以區網模式啟動，且已經是用完整路徑，不受上述 PATH 問題影響）。

### 跑測試

```powershell
$env:PYTHONIOENCODING='utf-8'
.venv\Scripts\python -m pytest tests\ -q
```

全部離線（monkeypatch，不打網路），跑起來應該幾秒內完成。中文輸出如果變亂碼，就是沒設 `PYTHONIOENCODING`。

### 疑難排解

**新開的 PowerShell 視窗裝不了/啟動不了，但上一個視窗明明可以？** 三個獨立成因，可能同時發生：

1. **`.venv` 裡的套件被清空或損毀**（`site-packages` 只剩 `pip`，`.venv\Scripts` 沒有 `uvicorn.exe`/`pytest.exe`）：整個 `.venv` 目錄砍掉重建（`Remove-Item -Recurse -Force .venv`），不要只補 `pip install`——半殘的套件目錄會遮蔽之後重裝的真套件。重建時**不要**用 `.venv\Scripts\python.exe -m venv .venv`（自己重建自己），要用系統內建的 base Python（見上方安裝指令）。
2. **PowerShell ExecutionPolicy 擋 `Activate.ps1`**：這台機器的 Machine/User/CurrentUser policy 都是 `Undefined`，新開的視窗會落在 `Restricted`，`.venv\Scripts\Activate.ps1` 會被拒絕執行（錯誤訊息通常提到「未經數位簽章」或「已停用」）。**本專案不需要 activate**——上面所有指令都直接用完整路徑 `.venv\Scripts\python`，跳過這個問題。若你仍想用 `Activate.ps1`（例如想要 shell prompt 顯示 `(.venv)`），有兩條不改系統設定的路：
   - 改用 `.venv\Scripts\activate.bat`（cmd 版不受 PowerShell 的 ExecutionPolicy 限制）
   - 該次開視窗時用 `powershell -ExecutionPolicy Bypass` 啟動
   - **不要**跑 `Set-ExecutionPolicy` 改系統設定
3. **PATH 上有別的專案的 venv 排更前面**：`where python` 第一個結果如果不是這個專案的 `.venv\Scripts\python.exe`，裸 `python`／`pip` 指令就會默默用錯環境（包括建 venv 本身）。解法同上：一律用完整路徑。

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

**範圍外**：回測資料庫（`data/backtest.db`）已 `.gitignore`，`snapshot.py` 產生的靜態快照裡沒有它，所以決策面板的信心度徽章、展開列的「回測績效」區塊、頁首的回測更新時間，在手機雲端版一律不顯示——這些只在本機 `app.py` 即時模式下可用。

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

## 回測與建議

`run_backtest.py` 建立/更新一個獨立的 SQLite 回測資料庫（`data/backtest.db`，已 `.gitignore`，不會進版控），提供決策面板的信心度徽章與展開列的回測績效區塊。**這是離線批次工具，不會在啟動 `app.py` 或每次分析時自動執行**——資料是手動跑出來的一份快照，過期了要自己重跑。

### CLI 用法

```powershell
# 只讀現有 DB（不打 FinMind），印出各代碼目前有幾筆日線，並重算訊號勝率統計
.venv\Scripts\python src\run_backtest.py

# 完整重建：向 FinMind 重抓日線＋籌碼、重算訊號勝率、逐 bar 重算分數、跑策略回測
.venv\Scripts\python src\run_backtest.py --refresh-data

# 只處理指定代碼（逗號分隔），預設是完整 watchlist.json
.venv\Scripts\python src\run_backtest.py --codes 2330,0050 --refresh-data
```

沒有 `--codes` 時處理完整追蹤清單（19 檔），`--refresh-data` 時每檔要打 5 次 FinMind（日線 + 4 種籌碼），19 檔 ≈ 95 次，遠低於 FinMind 600 次/hr 上限；不加 `--refresh-data` 則完全不連網，只用 DB 裡既有的日線重算訊號勝率／分數／策略。實測 19 檔完整重算（不含資料匯入）約 1 分鐘。

### 資料庫內容（`data/backtest.db`，schema 見 `db.py`）

| 表 | 內容 | 由誰產生 |
|---|---|---|
| `daily_bars` | 日線 OHLCV（約 1000 天，含 warmup 緩衝） | 匯入階段 |
| `chip_daily` | 法人／融資融券／外資持股／借券賣出餘額（合併寬表，約 730 天） | 匯入階段 |
| `score_history` | 逐日重算的技術分／籌碼分／綜合分／優先級／行動／當日訊號 | 分數重算（`backtest.replay_scores()`） |
| `signal_stats` | 11 個訊號 × 3 個持有天期（5/10/20日）的歷史勝率與平均報酬 | 訊號統計（`backtest.signal_win_rates()`） |
| `strategy_runs` | 單方策略回測摘要（總報酬／買進持有／勝率／最大回撤／交易次數） | 策略回測（`backtest.simulate()`） |
| `trades` | 策略回測的逐筆交易明細 | 策略回測 |
| `meta` | `last_run_at`／`last_run_id`／`params_json`／`of_excluded` | 每次完整跑完後更新 |

`GET /api/suggestions`（決策面板信心度）與 `GET /api/backtest/{code}`（該檔完整回測明細）讀這個 DB；DB 不存在或空表時兩者都優雅降級（`confidence`/`data_as_of` 回 `null`），不會出錯，只是代表還沒跑過 `run_backtest.py`。

### 三項已知限制（務必理解後再看信心度數字）

1. **回測不含 Order Flow**：OF 佔綜合評分權重 15/100，但 2 年前的逐筆／分鐘資料無法重建（yfinance 只給近 60 天 5 分 K，FinMind tick 是付費層且一次只能抓一天）。`replay_scores()` 重算歷史分數時 OF 一律當作貢獻 0，`meta.of_excluded=1` 記錄這件事。這代表回測分數比即時分數更容易落在中性區間，兩者不能直接比較誰「更準」。
2. **倖存者偏誤**：`watchlist.json` 是現在這份名單，回測「過去兩年」用的是「現在還留在清單上」的股票，不是「兩年前實際存在的候選池」。曾經在清單上但已刪除的股票不會出現在回測裡，實際勝率可能被高估。
3. **靜態快照模式不支援回測**：手機雲端版（見上方「手機雲端版」一節）用 GitHub Actions 產生的靜態快照沒有 `data/backtest.db`（已 gitignore，乾淨 checkout 裡不存在），所以決策面板的信心度徽章、展開列的回測績效區塊、頁首的回測更新時間，在靜態快照模式一律不顯示，只有本機 `app.py` 即時模式才有。

### 多久該重跑一次

決策面板頁首會顯示「回測資料更新於 X（N 天前）」，超過 7 天會變成警示色。籌碼資料（法人／融資券／外資持股／借券）每個交易日都有新的一筆，建議收盤後（約 21:00 之後，待三大法人與融資券資料入庫）跑一次 `--refresh-data`；純粹想更新訊號勝率／策略回測、不想重打 FinMind，可以不加 `--refresh-data` 直接跑（用 DB 既有日線重算）。

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

**加密貨幣（Binance）追蹤**：`main(crypto_codes=[...])` 會啟動一個背景的 Binance WebSocket 任務（公開的 aggTrade＋深度串流，**不需要 API key**），24 小時常駐，與台股 MIS 輪詢並行不互相卡住。`crypto_codes` 預設空清單（不啟用）；要開啟需寫腳本呼叫 `monitor.main(crypto_codes=["BTCUSDT", ...])`，細節見 `monitor.py` 檔頭 docstring 與 `_main_loop()`。

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
