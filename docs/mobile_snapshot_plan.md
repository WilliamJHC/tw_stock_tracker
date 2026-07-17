# stock_tracker 手機獨立查看計畫書：雲端排程快照 + 靜態頁

- 撰寫日期：2026-07-14
- 執行者：Sonnet 5 (medium effort)，逐 Phase 執行，每 Phase 有獨立驗收條件，Phase 之間允許中斷
- 專案根目錄：`C:\Users\william.chen\claude\stock_tracker\`（下文相對路徑以此為根）
- 本機環境：Windows 11 + PowerShell 5.1；venv 在 `.venv\`；跑 Python 遇中文亂碼先設 `$env:PYTHONIOENCODING='utf-8'`

## 0. 背景、目標與前置依賴

現況：追蹤器是本機 FastAPI（`app.py`，uvicorn port 8932），手機只能在**同網段且 PC 開機**時查看。使用者（機構投資人）要求：**PC 關機也能隨時在手機上看**決策面板與各檔分析。

使用者已確認的決策（不要重新提問）：
1. 完全獨立於 PC：雲端排程自動產頁，免伺服器費用
2. 更新頻率：盤中（台北 09:00-13:30）每約 20 分一次 + 盤後完整更新
3. 隱私：難猜網址＋不被搜尋引擎索引即可，**不需要**密碼
4. 手機**只讀**：不做雲端寫入；改 watchlist 回 PC 改或用 GitHub 手機 App 改 `watchlist.json`

**前置依賴：`docs/optimization_plan.md` 已全部執行完成**（2026-07-14 掃描確認：git repo 已建、分支名 **master**、尚無 remote、Phase 0-5 commits 在案、最後 commit d806834；watchlist 現為 **16 檔**；`pipeline.py` 尚不存在；`app.py:100-156` 的 `api_analysis` 已含完整流程 daily→minute→inst/margin/shareholding/sbl→`analyze()`→`compute_attention()`→quote/PE→chart；`data_sources._load_token()` 已優先讀環境變數 `FINMIND_TOKEN`（data_sources.py:25-45），Actions 上設環境變數即可**零改碼**；`.gitignore` 已排除 `.env`/`cache/`/`.venv/`；前端 `static/index.html` 現為 742 行，`init()` 依序 fetch `/api/config`→`/api/watchlist`→逐檔 `/api/analysis/{code}`；PWA manifest 在 `static/manifest.json`）。所有 Phase 都以此現況為基準，無「前置未完成」分支。

範圍界線：不引前端框架、不引資料庫；新依賴只允許部署工具鏈必要的（wrangler 由 Actions `npx` 臨時安裝，**不進** requirements.txt）；不做帳號驗證（隱私模型見 §6）。

## 1. 定案架構

**GitHub private repo（程式＋watchlist）＋ GitHub Actions cron（產快照）＋ Cloudflare Pages Direct Upload（wrangler CLI 直傳靜態檔，不連 repo）。**

理由（外部事實已於 2026-07-14 查證，來源見 §8）：
- GitHub Pages 免費版**只支援公開 repo**（私有需 Pro）→ 公開會曝光 watchlist，排除。
- Cloudflare Pages **Direct Upload**：用 `wrangler pages deploy` 從 CI 直接上傳建好的 `dist/`，repo 完全不用連給 Cloudflare、保持私有。免費額度：無限頻寬與請求、單站 2 萬檔、單檔 25 MiB（data.json 約 0.5-1.5 MB，16 檔綽綽有餘）；「500 builds/月」限制文件僅針對 Git 整合建置，Direct Upload 不走其建置系統（即使被最嚴格解讀計入，本設計約 374 次部署/月也 < 500，雙保險）。
- 隱私：隨機專案名 `https://tw-<隨機12碼>.pages.dev` ＋ `dist/_headers` 的 `X-Robots-Tag: noindex` ＋ index.html 的 `<meta name="robots">`。**不要加 robots.txt Disallow**——會阻止爬蟲讀到 noindex，反而可能留下 URL-only 索引。
- 次選為 Netlify（CLI 部署同樣可行），但頻寬 100GB/月且 token 權限較粗，不採。

### 資料流

```
GitHub private repo (master)
  ├─ 程式碼 + watchlist.json（手機用 GitHub App 改）
  └─ .github/workflows/snapshot.yml
       │  cron（UTC）：盤中 light ×15/交易日、full ×2/日
       ▼
GitHub Actions runner (ubuntu, secrets: FINMIND_TOKEN 等)
  ├─ [full]  snapshot.py --mode full
  │           └─ pipeline.build_stock_payload(code) ×16
  │                ├─ FinMind（日線/PER/法人/融資券/外資持股/借券）96 次 + tick 探測 1 次
  │                ├─ yfinance 5 分 K（OF/VP 用，失敗自動降級 minute=None）
  │                └─ analysis.analyze() + compute_attention()
  ├─ [light] snapshot.py --mode light
  │           ├─ 下載 https://<專案名>.pages.dev/data.json（上次 full 的結果為基底）
  │           └─ 只打 TWSE MIS 報價 ×16（0 次 FinMind），merge_light 合併
  ├─ 組裝 dist/：index.html + static/ + data.json + _headers
  └─ npx wrangler pages deploy dist（secrets: CLOUDFLARE_API_TOKEN / ACCOUNT_ID）
       ▼
https://tw-<隨機名>.pages.dev  ←─ 手機 PWA（加入主畫面），純靜態，與 PC 無關
```

兩個關鍵設計決定（不要改）：
- **light 的基底來源＝目前線上部署的 data.json**（部署本身就是持久層）。比 actions/cache 可靠（無驅逐與 key 管理問題）；下載失敗（如首次執行）→ log 警告後自動 fallback 跑 full。
- **light 只更新報價**：FinMind 的日線/籌碼本來就盤後才更新，盤中重抓浪費額度得到相同結果；分析分數盤中唯一會動的輸入是即時報價。light = 16 次 TWSE MIS（無 token、不消耗 FinMind）+ 合併 + 部署，一分鐘內跑完。

## 2. 實作 Phases

> 通則：每 Phase 結束 git commit（訊息繁體中文）。測試指令 `.venv\Scripts\python -m pytest tests -q`（先跑一次記基準）。

### Phase 0：抽共用管線 `pipeline.py`（純本機，無雲端依賴）

改動：
1. 新增 `pipeline.py`：把 `app.py` 的 `load_watchlist()`、`save_watchlist()`、`DEFAULT_WATCHLIST`、`CHART_BARS`、`TICK_OF_DAYS`、`_fetch_tick_days()`，以及 `api_analysis`（app.py:100-156）的函式主體搬進來，主體改名 `build_stock_payload(code) -> dict`。
   - 錯誤語意：抓不到日線時讓 `ds.SourceError` 原樣拋出；歷史不足（`analyze()` 回 None）時 `raise ValueError(f"{code} 歷史資料不足")`。
   - 回傳 dict 與現行 `/api/analysis/{code}` 回應**完全一致**（code/name/quote/pe/daily_source/last_close/last_date/ma60_last/chart/analysis/attention）。
   - **不得 import fastapi**（snapshot 在 Actions 上不裝 fastapi）。
2. `app.py` 改薄包裝：`api_analysis` 呼叫 `pipeline.build_stock_payload`，把 `SourceError` 轉 HTTP 404、`ValueError` 轉 422；watchlist 端點改用 pipeline 的函式。app.py 應縮到約 60 行。
3. 順手 commit 目前未入庫的 `watchlist.json` 修改（16 檔）。

驗收：
```powershell
$env:PYTHONIOENCODING='utf-8'
.venv\Scripts\python -m pytest tests -q          # 全過，數量不少於基準
.venv\Scripts\python -c "import pipeline; p=pipeline.build_stock_payload('2330'); print(p['analysis']['score'], p['attention']['action'])"
# 啟動 uvicorn 後：curl.exe -s http://localhost:8932/api/analysis/2330 回 200 且欄位鍵集合與改前相同
```

### Phase 1：`snapshot.py` full 模式 + 前端雙模式（純本機）

改動一：新增 `snapshot.py`。CLI：`--mode full|light`、`--out DIR`（預設 `dist`）、`--base-url URL`、`--base-file PATH`。full 模式：
- 讀 watchlist → 逐檔 `try: build_stock_payload(code)`，單檔失敗記 `{"error": "<訊息>"}` 繼續跑 → 輸出 `dist/data.json`：
  ```json
  { "version": 1, "mode": "full",
    "full_generated_at": "<台北時間 ISO>", "quotes_updated_at": "<同上>",
    "tick_tier": false,
    "config": { "score_bands": ..., "score_labels": ..., "action_thresholds": ..., "chip_weights": ... },
    "watchlist": [...],
    "stocks": { "2330": { ...payload 或 {"error": "..."} }, "...": {} } }
  ```
- 時間戳一律 `datetime.now(ZoneInfo("Asia/Taipei"))`（不依賴 TZ 環境變數，本機 Windows 測試才一致）。
- 組站：複製 `static/index.html` → `dist/index.html`；`static/manifest.json` 與 `static/icon.png` → `dist/static/`（保持原路徑，前端 `/static/...` 引用不用改）；寫 `dist/_headers`：
  ```
  /*
    X-Robots-Tag: noindex, nofollow, noarchive
  /data.json
    Cache-Control: no-store
  /
    Cache-Control: no-cache
  /index.html
    Cache-Control: no-cache
  ```
  （首頁規則要寫 `/`——Cloudflare Pages 會把 `/index.html` 308 轉址到 `/`，只寫 `/index.html` 的話首頁吃不到 no-cache；兩條都寫保險。）
- 結尾印統計行（模式／成功檔數／失敗檔數／耗時）；失敗檔數過半（16 檔中 >8）時 exit 1（讓 Actions 亮紅燈，避免部署一頁空資料）。

改動二：`static/index.html` 雙模式：
1. `<head>` 加 `<meta name="robots" content="noindex, nofollow">`（本機也帶著，無害）。
2. `init()` 開頭先 `fetch('data.json', {cache: 'no-store'})`：
   - 200 → **靜態模式**：從 snapshot 填 `CFG`、watchlist、各檔資料與失敗清單；`document.body.classList.add('static-mode')`；更新時間顯示「分析 MM/DD HH:mm・報價 HH:mm」（取 `full_generated_at` 與 `quotes_updated_at`）。
   - 非 200 或例外 → 走現有 API 流程（本機 FastAPI 對 `/data.json` 回 404，行為不變）。
3. CSS 加 `body.static-mode .add-bar, body.static-mode .del-btn { display: none }`（選擇器以實際 class 名為準，對照現檔）；靜態模式下「重新整理」改為重新 fetch `data.json` 再渲染。
4. `.gitignore` 加 `dist/`。

驗收：
```powershell
$env:PYTHONIOENCODING='utf-8'
.venv\Scripts\python snapshot.py --mode full --out dist    # exit 0，統計行顯示 16 檔中成功數
.venv\Scripts\python -m http.server 8801 --directory dist
# 瀏覽器開 http://localhost:8801 → 決策面板/表格/展開明細正常、新增列與刪除鈕消失、顯示「分析…・報價…」
# 再開本機 uvicorn 版 → 行為與改動前相同（新增/刪除功能存在）
```

### Phase 2：light 模式（純本機）

改動：
1. `snapshot.py` 增 light 模式：基底取自 `--base-file`（本機測試用）或 `--base-url` + `/data.json`；取不到 → log 警告後**自動改跑 full**。
2. 合併寫成純函式（放 snapshot.py，可被測試 import）：
   ```python
   def merge_light(base: dict, quotes: dict, now_iso: str) -> dict:
       """Overwrite per-stock quote (and quote-carried name) only; set mode=light and
       quotes_updated_at=now_iso; keep full_generated_at / analysis / attention / chart untouched."""
   ```
3. 報價只用 `ds.get_quote()`（TWSE MIS→yfinance 降級），單檔失敗保留舊 quote。
4. 新增 `tests/test_snapshot.py`：對 `merge_light` 至少 3 個純函式測試（覆寫 quote、保留 analysis 與 full_generated_at、缺 quote 的檔不動）。

驗收：
```powershell
.venv\Scripts\python snapshot.py --mode full --out dist
.venv\Scripts\python snapshot.py --mode light --out dist --base-file dist\data.json   # < 60 秒
# 檢查 dist\data.json：mode=light、quotes_updated_at 變新、full_generated_at 不變、抽一檔 analysis.score 不變
.venv\Scripts\python -m pytest tests -q
```

### Phase 3：雲端開通（幾乎全是使用者手動，執行者只協助與確認）

使用者要做的事（依序，README 也要收錄這份清單）：
1. GitHub 建 **private** repo（名稱隨意，例：`stock-tracker`）。
2. 本機推送：`git remote add origin <repo URL>` → `git push -u origin master`（**分支名是 master**，不是 main）。
3. 註冊/登入 Cloudflare（免費方案），dashboard 複製 **Account ID**。
4. My Profile → API Tokens → Create Token → Custom：權限僅 `Account / Cloudflare Pages / Edit` → 複製 token。
5. 產生隨機專案名（PowerShell）：
   ```powershell
   'tw-' + -join ((48..57)+(97..122) | Get-Random -Count 12 | ForEach-Object {[char]$_})
   ```
   記下（例：`tw-x7k2m9qa4fpz`）。**不需要**在 Cloudflare 網頁手動建專案——workflow 首跑會自動建。
6. GitHub repo → Settings → Secrets and variables → Actions，建立 4 個 secrets：

   | Secret | 內容 |
   |---|---|
   | `FINMIND_TOKEN` | 現有免費 token（程式已優先讀環境變數，零改碼） |
   | `CLOUDFLARE_API_TOKEN` | 步驟 4 的 token |
   | `CLOUDFLARE_ACCOUNT_ID` | 步驟 3 的 Account ID |
   | `CF_PAGES_PROJECT` | 步驟 5 的隨機專案名（放 secret 讓 URL 不進 log 與程式碼） |

驗收：repo 網頁看得到程式碼且為 Private；Settings 顯示 4 個 secrets。

### Phase 4：Actions workflow + 手動觸發驗證

改動：
1. 新增 `requirements-actions.txt`：只含 `requests>=2.31`、`yfinance>=0.2.40`（不裝 fastapi/uvicorn/pytest；不動本機 `requirements.txt`）。
2. 新增 `.github/workflows/snapshot.yml`（骨架如下；**GitHub Actions 不支援 YAML anchor，env 區塊要在兩個 wrangler step 各寫一份**）：
   ```yaml
   name: snapshot
   on:
     schedule:
       - cron: '7,27,47 1-5 * * 1-5'   # light（台北 09:07-13:47 每 20 分）
       - cron: '10 0 * * 1-5'          # full（台北 08:10 早晨重建）
       - cron: '40 13 * * 1-5'         # full（台北 21:40 盤後籌碼齊）
     workflow_dispatch:
       inputs:
         mode:
           type: choice
           options: [full, light]
           default: full
   concurrency:
     group: snapshot
     cancel-in-progress: false
   jobs:
     snapshot:
       runs-on: ubuntu-latest
       env:
         FINMIND_TOKEN: ${{ secrets.FINMIND_TOKEN }}
         CF_PAGES_PROJECT: ${{ secrets.CF_PAGES_PROJECT }}
       steps:
         - uses: actions/checkout@v4
         - uses: actions/setup-python@v5
           with:
             python-version: '3.12'
             cache: pip
             cache-dependency-path: requirements-actions.txt
         - run: pip install -r requirements-actions.txt
         - id: mode
           # schedule 事件用 github.event.schedule 與 cron 字串精確比對（必須與 on.schedule 逐字一致）
           run: |
             if [ "${{ github.event_name }}" = "workflow_dispatch" ]; then
               echo "mode=${{ inputs.mode }}" >> "$GITHUB_OUTPUT"
             elif [ "${{ github.event.schedule }}" = "7,27,47 1-5 * * 1-5" ]; then
               echo "mode=light" >> "$GITHUB_OUTPUT"
             else
               echo "mode=full" >> "$GITHUB_OUTPUT"
             fi
         - run: python snapshot.py --mode "${{ steps.mode.outputs.mode }}" --out dist --base-url "https://${CF_PAGES_PROJECT}.pages.dev"
         - uses: actions/setup-node@v4
           with:
             node-version: 20
         - name: create pages project (idempotent)
           run: npx wrangler@4 pages project create "$CF_PAGES_PROJECT" --production-branch=master 2>/dev/null || true
           env:
             CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}
             CLOUDFLARE_ACCOUNT_ID: ${{ secrets.CLOUDFLARE_ACCOUNT_ID }}
         - name: deploy
           run: npx wrangler@4 pages deploy dist --project-name "$CF_PAGES_PROJECT" --branch=master
           env:
             CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}
             CLOUDFLARE_ACCOUNT_ID: ${{ secrets.CLOUDFLARE_ACCOUNT_ID }}
   ```

驗收：
1. GitHub → Actions → snapshot → Run workflow（mode=full）→ 綠燈。
2. 手機瀏覽器開 `https://<專案名>.pages.dev` → 決策面板完整、有資料時間、無新增/刪除鈕。
3. `curl.exe -sI https://<專案名>.pages.dev/ | findstr /i x-robots` → 看到 `noindex`。
4. 再手動觸發 mode=light → 綠燈；頁面報價時間更新、分析時間不變。
5. 檢視 full 那次的 log：無 402/429（FinMind 額度內）——但 `tick_tier_available()` 探測在免費 token 下收到一次付費層拒絕屬**預期行為**（現行程式靜默吞掉；若 snapshot.py 有加 HTTP 層 log 看到單次 4xx 不算失敗）；若 yfinance 分鐘資料在 Actions IP 被限流，確認有降級 log 且分析仍產出（風險 §6-3）。

### Phase 5：排程實跑驗證 + PWA + 文件

1. 下一個交易日確認：Actions 歷史中盤中每 ~20 分有 light 綠燈；台北 08:10 與 21:40 有 full 綠燈；21:40 之後頁面籌碼分反映當日法人/融資券。
2. 手機「加入主畫面」（Safari/Chrome），確認以 standalone 模式開啟（manifest 已存在）。
3. `README.md` 補「手機雲端版」章節：架構圖、排程表（§3）、secrets 清單、如何用 GitHub 手機 App 改 watchlist.json（commit 後等下一次排程或手動 Run workflow）、故障排查（Actions 紅燈看哪裡）。

驗收：README 更新已 commit；使用者手機主畫面一鍵開啟且「報價時間」在最近一次排程視窗內。

## 3. 排程表與額度計算

| cron（UTC） | 台北時間 | 模式 | 次數 |
|---|---|---|---|
| `7,27,47 1-5 * * 1-5` | 09:07–13:47 每 20 分（週一至五） | light | 15 次/交易日 |
| `10 0 * * 1-5` | 08:10 | full（早晨重建，兼前晚失敗補救） | 1 次/日 |
| `40 13 * * 1-5` | 21:40 | full（盤後：FinMind 法人 20:00、融資券/外資持股/借券 21:00 入庫後） | 1 次/日 |

分鐘數刻意避開整點/整半（GitHub cron 尖峰延遲可達 15 分以上）；13:47 那班涵蓋 13:30 收盤。

**GitHub Actions 額度（private repo Free 2000 分鐘/月，ubuntu 1x 計費、無條件進位）**：
- light ≈ 2 分/次（checkout+pip cache ~40s、16 次 MIS ~10s、wrangler ~40s）× 15 × 22 平日 ≈ 660 分
- full ≈ 5 分/次（97 次 FinMind 循序 + 16 檔 yfinance 分鐘資料 2-3 分、部署 1 分）× 2 × 22 ≈ 220 分
- 合計 ≈ **880 分/月（額度 44%）**。逼近上限時把 light 改 30 分一班（`7,37 1-5 * * 1-5`，10 班/日）→ 2×10×22＋220 ≈ 660 分。

**FinMind 額度（免費 token 600 次/hr）**：
- full：16 檔 ×（日線+PER+法人+融資券+外資持股+借券）= 96 次 + tick 探測 1 次 = **97 次/run**，單一小時內遠低於 600
- light：**0 次**（TWSE MIS 為公開端點）
- 每日 FinMind 總量 = 2 × 97 = 194 次；雲端與本機 PC 使用互不干擾（各自 IP/token 時窗，同 token 合計亦僅 291 次/hr 峰值以下）

## 4. Secrets 與安全

- 4 個 GitHub Actions secrets 見 Phase 3 表格；`.env` 已在 `.gitignore`，永不 commit。
- Cloudflare API token 權限最小化（僅 Pages Edit）。
- watchlist 只存在於 private repo 與難猜 URL 的 data.json；頁面純讀、無任何寫入端點。
- 每次部署另有 `<hash>.<專案名>.pages.dev` 預覽 URL，屬同一隱私模型（知道 URL 即可看，使用者已接受）。

## 5. 需要使用者自己做的事（彙總）

即 Phase 3 的 1-6 步（建 repo、push、Cloudflare 帳號/Account ID/API token、隨機專案名、4 個 secrets），加上 Phase 4 驗收後在手機加入主畫面。其餘全部由執行者完成。

## 6. 風險與假設（執行時對照）

| # | 項目 | 說明 | 信心 |
|---|---|---|---|
| 1 | FinMind 資料入庫時間 | 官方文件：法人 20:00、融資券/外資持股/借券 21:00（週一至五）→ 21:40 full 可全拿。這是 FinMind 入庫時間（比交易所原始公布晚），我們的資料源就是 FinMind，以它為準 | 高（官方文件） |
| 2 | GitHub cron 延遲 | 尖峰可延遲 15 分+，盤中頻率是 best-effort；已用 7/27/47 分避峰 | 高 |
| 3 | yfinance 在 Actions 資料中心 IP 可能被限流 | 影響 5 分 K（OF/VP 精度）；`analyze()` 已支援 minute=None 降級（日線 VP、無 OF），分數仍產出。Phase 4 實測確認 | 中 |
| 4 | TWSE MIS 對海外 IP | 一般可存取但無 SLA；失敗自動降 yfinance，再失敗保留上次收盤價（時間戳誠實顯示舊） | 中 |
| 5 | Cloudflare「500 builds/月」是否計 Direct Upload | 文件僅寫 Git 整合建置；即使計入，374 次/月 < 500 | 中高（雙保險） |
| 6 | 排程 60 天不活動可能被 GitHub 停用 | 文件主要針對公開 repo，私有 repo 行為**低信心**；使用者平時會 commit watchlist，實務難觸發；GitHub 停用前會寄信 | 低信心標註 |
| 7 | 台股休市日（非週末的國定假日） | 照跑，浪費少量分鐘、資料無變化，接受不維護休市表 | 高 |
| 7a | `ZoneInfo("Asia/Taipei")` 在 Windows 本機依賴 pip 的 tzdata 套件 | 現行 venv 已有（pandas 連帶安裝，實測通過）；Actions ubuntu 有系統 tzdata 不受影響。若未來重建 venv 失敗，`pip install tzdata` 即解 | 高（已實測） |
| 8 | 執行者回報格式 | 遵守專案守則三態：已驗證（附方式與結果）／已完成但未驗證／未完成（附卡點） | — |

## 7. 與既有架構的關係

- 本機 FastAPI 版**完全保留**：PC 上開發、改 watchlist、即時互動照舊；雲端版是它的唯讀快照。
- `pipeline.py` 抽取後，本機 API 與雲端 snapshot 共用同一份邏輯，之後改分析規則兩邊自動同步。
- 前端同一份 `index.html` 靠執行期偵測 `data.json` 決定模式，不維護兩份前端。

## 8. 查證來源（2026-07-14）

- Cloudflare Pages Direct Upload：https://developers.cloudflare.com/pages/get-started/direct-upload/
- Cloudflare Pages Limits：https://developers.cloudflare.com/pages/platform/limits/
- GitHub Pages 私有 repo 限制：https://github.com/orgs/community/discussions/22817 、https://docs.github.com/get-started/learning-about-github/githubs-products
- FinMind 籌碼面資料更新時間：https://finmind.github.io/tutor/TaiwanMarket/Chip/
