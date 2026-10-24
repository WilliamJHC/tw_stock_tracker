# 台股追蹤器 v3.0（本地 Python 版）

## 功能

- **今日決策面板**：開首頁最上方，按行動優先級排序告訴你今天該看哪幾檔、為什麼、建議動作（見下方「決策面板與籌碼分」）
- 追蹤清單（`watchlist.json` 持久化）、即時報價、本益比
- K 線圖＋ MA5/20/60 ＋支撐壓力（用 TradingView 開源的 [Lightweight Charts](https://github.com/tradingview/lightweight-charts) 繪製，函式庫檔案放在 `static/vendor/`，離線可用不打 CDN）
- **Volume Profile**：POC／Value Area（70%）／HVN･LVN，直方圖疊在 K 線旁
- **Order Flow**：內外盤 delta 與累積 delta
  - FinMind 付費級 token → 逐筆 tick 精確計算
  - 免費級 token → 自動降級為 yfinance 分鐘 K 的 tick-rule 推估（UI 會標示）
- 量價四象限、20 日趨勢推估區間、綜合技術評分、訊號回測勝率
- **籌碼面子分**：法人買賣超／融資融券／外資持股比／借券賣出餘額，與技術分並列顯示，不揉進同一個數字
- **兩種選股方式**：全市場多因子排名（推薦股票）＋強勢股回調買法（強勢回調面板，見下方同名一節）
- 資料來源優先度：**FinMind → yfinance → TWSE**（每檔股票顯示實際來源）

## 安裝與啟動

**全程用完整路徑（`.venv\Scripts\python`），不要打裸 `python`／`pip`**：本機 PATH 上還有另一個專案（`report_generator`）的 venv 排在前面，裸 `python` 會跑到那個環境去，裝套件、建 venv 都會用錯環境。

```powershell
cd C:\Users\william.chen\claude\tw_stock_tracker

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
tw_stock_tracker/
├─ src/              應用程式碼（app / analysis / data_sources / monitor / snapshot …）
│  └─ realtime/      即時報價來源（MIS、KGI、Binance）
├─ tests/            pytest（conftest.py 負責把 src/ 掛上 sys.path）
├─ scripts/          一次性驗證／spike 腳本，不進 pytest
├─ docs/             設計文件與 spike 紀錄（只放 .md/.txt）
├─ static/           前端（index.html、icon、manifest）
│  └─ vendor/        第三方前端函式庫（Lightweight Charts），進版控以保離線可用
├─ worker/           Cloudflare Worker：TradingView 警報轉發 Telegram
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

# 2. 開放 8932 port（只對 Private 網路生效；日後移除：netsh advfirewall firewall delete rule name="tw-stock-tracker-8932"）
netsh advfirewall firewall add rule name="tw-stock-tracker-8932" dir=in action=allow protocol=TCP localport=8932 profile=private
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

## 全市場因子排名法（多因子擇股，`scripts/run_factor_backtest.py`）

`src/finlab_factors.py` 的 `RANKING_WEIGHTS`（營收 YoY／ROE／動能）用哪組權重，
不是拍腦袋定案——`.github/workflows/optimize.yml` 每月重跑一次 rank-IC 評估，
把新算出的建議權重跟目前生效的權重做 purged walk-forward 比較，通過 DSR/PBO/
高原三道門檻、且樣本外平均報酬嚴格高於目前生效的權重，才自動套用（見 `docs/adr/0002-auto-apply-ranking-weights.md`）；
套用後下一週期表現變差會自動退回前一組。這個 workflow 本身只重新評估既有快取
（`cache/factor_run/`，靠 `actions/cache` 跨執行留存），不會自己重抓資料；快取
的更新分兩條線：日線由每週的 `update_universe_bars_part1/2` 負責，月營收與季財報
由 `update-fundamentals`（見下）負責。

**月營收／季財報的定期更新**（`.github/workflows/update_fundamentals.yml`，腳本
`scripts/update_fundamentals.py`）：每月 11 日（營收截止日 10 日之後）與 15 日（補抓
遲交）、5/16、4/1 各跑一次，8/15、11/15 由每月 15 日那班順便涵蓋（`--kind auto`
會處理所有到期的種類，營收優先）。做法是每檔重抓近 760 天營收（1 個請求）／900 天
財報（3 個請求），重算後**併入**快取裡既有的完整歷史序列（不是覆蓋，否則回測歷史會被截斷）。
哪些檔「到期」由目標期別決定（營收＝10 日後該公布的最新月份；財報＝法定截止日
5/15、8/14、11/14、3/31 之後的最新一季），已追上就不再抓，所以漏跑一次、被中斷、
手動重跑都安全；該期尚未公布的檔每天最多重試一次，不會無限接力。成本：腳本自己的
限流是每小時 450 個請求（FinMind 上限 600，留空間給共用同一 token 的其他 job），營收
全市場約 4 小時；財報約 12 小時，會分數輪自動接力。若 FinMind 每小時額度仍被用完
（HTTP 402），腳本會辨識出來、每 10 分鐘探測一次等額度重置（單次最多等 60 分鐘，因為
睡著的 job 仍佔著共用的 concurrency group），不會把整批當成「無資料」失敗掉；若因 token
失效／服務異常、或某檔在探測成功後仍反覆 402（某個資料集單獨受限）而中止，已更新的部分
仍會存進快取（腳本以 exit 0 結束並標記 `aborted`，由 workflow 最後一步把 job 標為失敗；
逾時或手動取消時 save 步驟也會在確認快取完整後執行）。每輪預算 270 分鐘，留 80 分鐘
餘裕給限流休眠與存檔。與每週日線更新共用
`update-universe-bars` concurrency group（兩者都讀寫同一份快取，不能並行）；
若週更日線正在排隊，接力會讓路不派下一輪（GitHub 同一 group 只留一個排隊中的 run，
新的會把舊的擠掉），剩餘項目由下一次排程接手。抓不到資料（含暫時性錯誤）的檔不寫
期別標記、下次再試；歷史不足以建序列的檔（新上市）則該期直接標記完成。
**第一次**（快取剛播種完）所有檔都沒有期別標記，會整批重抓一輪營收＋財報再進入常態。
這條路徑已用真實 FinMind 對單檔實測過合併結果；雲端 dry-run（limit=20）通過，完整正式跑
第一次因額度用完（402）中止，上述額度處理是那次之後補的，尚待雲端重跑驗證。

**雲端快取的初次播種**：`cache/` 不進版控，雲端的 `update-universe-bars-state-*`
快取血統一開始並不存在，`optimize.yml`／`strategy_search.yml`／
`daily_recommendations.yml` 在空快取下只會空跑，每週的 `update_universe_bars_part1/2`
則會因為「找到 0 檔」而失敗。要在雲端建立這份快取，到 Actions 手動執行
**seed-factor-run**（`.github/workflows/seed_factor_run.yml`，腳本
`scripts/seed_factor_run.py`）：它在 Actions 內分段全量抓取（每輪約 5 小時，
有進展就自動接力下一輪；每檔存活代碼約 9 個 FinMind 請求〔含法人買賣超與現金流量表〕，
全部約需 5–7 輪、合計約 25–30 小時，這是依請求數推算的估計、未實跑過），完成前的進度存在
`factor-run-seed-*`（其他 workflow 讀不到，避免用殘缺資料自動調權重），完成後才存成
`update-universe-bars-state-seed-*`，之後由每週更新接手。需要 Environment「Cloudflare」
底下的 `FINMIND_TOKEN`。

Secrets 沿用「手機雲端版」同一個 Environment「Cloudflare」底下的
`TELEGRAM_BOT_TOKEN`／`TELEGRAM_CHAT_ID`（跟 `deploy-worker.yml` 用同一組），
不需要另外設定。

**盤勢自適應權重**：每日推薦排名（`scripts/run_daily_recommendations.py`）
會依當下大盤（TAIEX）盤勢——多頭／空頭／盤整，`src/market_regime.py`——
從 `meta.active_ranking_weights_by_regime` 找對應那組權重，找不到（尚未
有人工核准的候選，或該盤勢樣本數不足被跳過）就 fallback 回上面這組全域
`active_ranking_weights`，行為不變。候選同樣走人工審核，不是 ADR 0002 的
自動套用——見 `docs/adr/0004-regime-adaptive-strategy.md`、
`docs/regime_strategy_research.md`（因子在各盤勢下的實測 IC/勝率）。

### 故障排查（Actions 紅燈看哪裡）

點進失敗的 run → 展開紅色 ✕ 的那個 step 看訊息。目前遇過兩種：

- **`deploy` 報 `Must specify a project name.`**：workflow job 沒吃到 secrets——檢查 job 是否有 `environment: Cloudflare`（名稱要跟 Settings → Environments 裡的環境名一字不差）。
- **`deploy` 報 `Could not route to /accounts/<一串文字>/... code: 7003`**：`CLOUDFLARE_ACCOUNT_ID` 填成了帳號名稱（如 `xxx@gmail.com's Account`），要換成 32 位十六進位 Account ID。

`check secrets` 這個 step 會提早擋掉以上兩種錯誤並印出明確訊息，比等 wrangler 報隱晦錯誤好定位。

### 手機加入主畫面

手機瀏覽器開部署網址 → 選單「加入主畫面」→ 以獨立視窗（standalone）開啟，manifest 沿用本機版的 `static/manifest.json`。

## 強勢股回調買法（第二種選股法，`scripts/run_pullback_scan.py`）

> **已停用（2026-10-24）**：日線代理回測未通過（平均 net_r −0.045），排程 step 設 `if: false`，`monitor.py` 預設不載入回調清單（`.env` 設 `PULLBACK_MONITOR=1` 才開）。下列是停用前的設計，程式與測試保留。

和多因子排名並列、互不影響的另一種方式：篩近 1/3/6 月漲幅 >30% 且排名前 30% 的強勢股 →
要求整理中低點抬高、波動收窄、重新站上走平的 9/21 日 EMA → 把 EMA9/21/50、整理區高點起算的
AVWAP、前高、前低、未補缺口合成「支撐區」→ 盤中回到支撐區、下探後回升、突破前一根分 K 高點才進場；
停損放觸發 K／前一根 K 低點，股數＝帳戶 × 0.5% ÷ 每股風險，3R、5R 分批，剩餘看 EMA9。
**這套方法尚未回測驗證**，畫面與 Telegram 都有標示。完整對照表、台股化調整與已知限制見
`docs/pullback_strategy.md`，為什麼獨立成一條見 `docs/adr/0007-pullback-as-second-selection-method.md`。

- **排程**：`daily_recommendations.yml` 的第二個 step（台北週一～五 07:00），結果寫進 `meta.pullback_watchlist`，發一則 Telegram 摘要。
- **網頁**：首頁「強勢回調」面板（即時模式 `GET /api/pullback`；雲端版在 `data.json` 的 `pullback`）。右上角填帳戶規模就會算股數，數字只存在你的瀏覽器。
- **盤中觸發**：`.venv\Scripts\python src\monitor.py` 會自動把清單上的股票一起輪詢，用 5 分 K 判斷「回到支撐區／觸發／跌穿」，每檔每天各最多一則。`.env` 設 `PULLBACK_ACCOUNT_SIZE` 會在觸發警報附上股數。注意本機 monitor 讀本機的 `backtest.db`，雲端排程的清單不會同步下來——開盤前先在本機跑一次 `run_pullback_scan.py`。
- **手動**：`.venv\Scripts\python scripts\run_pullback_scan.py --universe watchlist`（或 `--codes 2344,3081`）只看不寫；全市場加 `--dry-run`。

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

`run_backtest.py` 建立/更新一個獨立的 SQLite 回測資料庫（`data/backtest.db`，已 `.gitignore`，不會進版控），提供決策面板的信心度徽章與展開列的回測績效區塊。**這是離線批次工具，不會在啟動 `app.py` 或每次分析時自動執行**——資料是快照，過期了要重跑；2026-09-21 起有雙軌雙週自動重跑（各自獨立、互不同步）：

- **本機**：Windows 工作排程每月 1、15 號 07:30 執行 `scripts\run_backtest_biweekly.bat`（`--refresh-data`），更新的是這台機器磁碟上的 `data/backtest.db`，也就是本機 `app.py` 讀到的那份。一次性註冊：`powershell -ExecutionPolicy Bypass -File scripts\register_backtest_task.ps1`（機器要開著且連網才會準時跑；漏跑只會讓「上次回測」時間停在上一次，不會出錯）。log 在 `logs/run_backtest_biweekly.log`。
- **GitHub Actions**：`.github/workflows/run_backtest.yml` 每月 1、15 號跑同一支腳本，寫回 Actions 自己 cache 的那份 `backtest.db`（跟 `optimize.yml`/`daily_recommendations.yml` 共用 `optimize-state-` 血統）。這份**不會**同步回本機，也不影響靜態快照/雲端版——雲端版本來就沒有回測面板（見下方「四項已知限制」第 3 點）。

也可以隨時手動重跑（見下方 CLI 用法），不用等排程。

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

### 四項已知限制（務必理解後再看信心度數字）

1. **回測不含 Order Flow**：OF 佔綜合評分權重 15/100，但 2 年前的逐筆／分鐘資料無法重建（yfinance 只給近 60 天 5 分 K，FinMind tick 是付費層且一次只能抓一天）。`replay_scores()` 重算歷史分數時 OF 一律當作貢獻 0，`meta.of_excluded=1` 記錄這件事。這代表回測分數比即時分數更容易落在中性區間，兩者不能直接比較誰「更準」。
2. **倖存者偏誤**：`watchlist.json` 是現在這份名單，回測「過去兩年」用的是「現在還留在清單上」的股票，不是「兩年前實際存在的候選池」。曾經在清單上但已刪除的股票不會出現在回測裡，實際勝率可能被高估。
3. **靜態快照模式不支援回測**：手機雲端版（見上方「手機雲端版」一節）用 GitHub Actions 產生的靜態快照沒有 `data/backtest.db`（已 gitignore，乾淨 checkout 裡不存在），所以決策面板的信心度徽章、展開列的回測績效區塊、頁首的回測更新時間，在靜態快照模式一律不顯示，只有本機 `app.py` 即時模式才有。
4. **`backtest.simulate()` 的定位是進出場警示，不是取代買進持有**：這是單檔、單倉位的擇時引擎，實測（10 年、23 檔 watchlist、還原股價前）平均策略總報酬 177.3% vs 平均買進持有 2520.4%，23 檔僅 1 檔勝出買進持有，時間在市場僅 33.5%——結構性落後，不是參數沒調好（`docs/archive/*lighthouse.md` Phase 0/3：四個旋鈕的整個空間已掃過）。Phase 3 把 `exit_composite`/`max_hold_days`/`atr_mult` 調到已驗證更優的設定後，平均總報酬升到 ~300% 量級，但買進持有多數情況仍領先，這是誠實的結論，不會靠繼續調參去追。真正負責產生 alpha 的是另一顆引擎：`portfolio_backtest.py` + `finlab_factors.py` 的多因子、多檔位排名策略，設計上要在 Phase 4 全市場規模才能發揮分散化帶來的優勢，單檔擇時引擎結構上做不到。

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
cd C:\Users\william.chen\claude\tw_stock_tracker
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
- TradingView webhook 已實作，見下方「TradingView 警報轉發」一節（跑在 Cloudflare Worker 上，與 `monitor.py` 各自獨立）。

## TradingView 警報轉發（Cloudflare Worker）

把 TradingView 的警報轉進跟盤中監控同一個 Telegram 聊天室。做成 Worker 而不是 `app.py` 的
endpoint，是因為 Worker 24 小時常駐，PC 關機時警報照樣會到；Cloudflare 免費方案每天 10 萬次
請求，這個用量（一天最多幾十則警報）完全在免費額度內。

程式碼在 `worker/tv-webhook.js`，部署設定在 `worker/wrangler.toml`。

### 第一次部署

1. **建一個 Workers 專用的 Cloudflare API Token**（跟現有那個 Pages 部署用的分開，權限最小化）：
   Cloudflare dashboard → My Profile → API Tokens → Create Token → 權限選 `Account / Workers Scripts / Edit`
2. **設一個難猜的共享密鑰**（TradingView 要用它證明身分，例如用 `python -c "import secrets; print(secrets.token_urlsafe(32))"` 產生）
3. 到 GitHub repo → Settings → Environments → **Cloudflare**（跟 snapshot 用同一個環境）新增 3 個 secret：

   | Secret | 內容 |
   |---|---|
   | `CLOUDFLARE_WORKER_API_TOKEN` | 步驟 1 建的 token |
   | `TV_WEBHOOK_SECRET` | 步驟 2 的共享密鑰 |
   | `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | 跟 `.env` 裡同一組（Worker 端要自己有一份） |

   （`CLOUDFLARE_ACCOUNT_ID` 沿用現有那個，不用重設。）
4. GitHub → Actions → **deploy-tv-webhook** → Run workflow。成功後 Worker 網址是
   `https://tv-webhook.<你的帳號子網域>.workers.dev`，實際網址會印在該次 run 的 log 裡。

### TradingView 端設定

建警報時把 Webhook URL 填成 Worker 網址，訊息二選一：

- **JSON**（密鑰放 body，網址不用帶參數）：
  ```json
  {"secret": "你的TV_WEBHOOK_SECRET", "message": "{{ticker}} 突破 {{close}}"}
  ```
- **純文字**（密鑰放網址）：URL 填 `https://tv-webhook.xxx.workers.dev/?token=你的TV_WEBHOOK_SECRET`，
  訊息欄隨意打字即可。

TradingView 的警報介面不能自訂 HTTP header，所以密鑰只能走 body 或查詢字串——兩種都支援。
轉到 Telegram 的訊息會加上 `[TV]` 前綴，跟 `monitor.py` 自己發的警報區分。

### 部署後驗證

```powershell
# 401：沒帶密鑰應該被擋
curl -X POST https://tv-webhook.xxx.workers.dev/ -d "hello"

# 200：帶對密鑰，手機上應該收到一則 [TV] 開頭的 Telegram 訊息
curl -X POST "https://tv-webhook.xxx.workers.dev/?token=你的密鑰" -d "測試訊息"
```

**若回應是 200 但手機沒收到訊息**：不是 Worker 的問題（密鑰驗證那關已經過了），八成是 GitHub secrets 裡的
`TELEGRAM_BOT_TOKEN`／`TELEGRAM_CHAT_ID` 跟 `.env` 對不上（手動輸入時漏字、多打空格）。排查方式：先用 `.env`
裡那組憑證直接呼叫 `https://api.telegram.org/bot<TOKEN>/sendMessage` 確認憑證本身有效、手機收得到，
確認後再回 GitHub 把這兩個 secret 改成跟 `.env`完全一致（複製貼上，不要手打），重新跑一次
**deploy-tv-webhook**（部署時會重推 secrets 到 Worker）。2026-08-13 首次部署就踩過這個坑。

## 免責

技術評分、訊號、回測勝率與推估區間均為歷史統計整理，屬機率傾向而非預測，不構成投資建議。
