# stock_tracker 手機獨立查看 — 計畫書撰寫計畫

## Context

使用者要求手機能**完全獨立**查看台股追蹤器（現況：只能在家中同網段、PC 開機時看 FastAPI 頁面）。本次交付物是一份計畫書檔案，不做實作——使用者同意後撰寫計畫書即停止，轉交 Sonnet 5 (medium) 執行。

使用者已確認（AskUserQuestion）：完全獨立於 PC（雲端排程＋靜態頁）、盤中每 15-30 分＋盤後更新、難猜網址即可（不需密碼）、手機只讀。

重要現況（Plan agent 掃描確認）：**前一份 docs/optimization_plan.md 已全部執行完成**（git repo 已建、Phase 0-5 commits 在案、watchlist 現為 16 檔、決策面板與籌碼分已存在），本計畫直接以其為基礎，無前置未完成的分支情境。

## 交付動作

1. 將完整實作計畫寫成計畫書：`C:\Users\william.chen\claude\stock_tracker\docs\mobile_snapshot_plan.md`
2. 新建大文件 → 派 fresh-context agent read-back 驗證（可執行性、路徑/指令真實性、內部矛盾），修正後複核
3. 回報三態後停止，不做任何實作

## 計畫書內容（已由 Plan agent 設計完成，外部事實已查證附來源）

### 定案架構
GitHub **private** repo（程式＋watchlist 不公開）＋ GitHub Actions cron（產快照）＋ **Cloudflare Pages Direct Upload**（wrangler 直傳 dist/，不連 repo）。理由：GitHub Pages 免費版不支援 private repo（會曝光 watchlist）；Cloudflare Pages 免費無限頻寬、Direct Upload 不受 500 builds/月限制（即使被計入，374 次/月也在內）。隱私＝隨機專案名 `https://tw-<隨機12碼>.pages.dev` ＋ `_headers` X-Robots-Tag noindex ＋ meta robots（不加 robots.txt Disallow——會擋掉 noindex 的讀取）。

### 資料流
Actions runner → `snapshot.py --mode full`（16 檔完整 pipeline，97 次 FinMind）或 `--mode light`（下載線上 data.json 為基底＋16 次 TWSE MIS 報價合併，0 次 FinMind）→ 組 dist/（index.html + data.json + _headers）→ wrangler 部署。light 的基底來源＝目前線上部署的 data.json（部署即持久層，比 actions/cache 可靠；取不到自動 fallback full）。

### Phases
- **Phase 0** 抽共用管線：app.py 的 api_analysis 主體抽成 `pipeline.py: build_stock_payload(code)`（不 import fastapi），app.py 改薄包裝（SourceError→404、ValueError→422）；commit 未入庫的 watchlist.json。驗收：pytest 全過＋pipeline 直呼 2330＋curl 本機 API 欄位不變
- **Phase 1** snapshot.py full 模式＋前端雙模式：輸出 data.json（version/mode/full_generated_at/quotes_updated_at/config/watchlist/stocks，單檔失敗記 error 繼續，失敗過半 exit 1；時間戳用 ZoneInfo("Asia/Taipei")）；index.html 開頭偵測 data.json（200→靜態模式：隱藏增刪 UI、顯示「分析 MM/DD HH:mm・報價 HH:mm」；404→走現有 API）；.gitignore 加 dist/。驗收：本機產 dist 用 http.server 目測＋uvicorn 版行為不變
- **Phase 2** light 模式：合併邏輯抽純函式 `merge_light(base, quotes, now_iso)`（只覆寫 quote 與 quotes_updated_at）＋ tests/test_snapshot.py。驗收：pytest＋本機 full→light 檢查時間戳與分數不變
- **Phase 3** 雲端開通（使用者手動）：建 private repo、push（分支 master）、Cloudflare 帳號/API token（權限僅 Pages Edit）/Account ID、隨機專案名、GitHub 四個 secrets（FINMIND_TOKEN / CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID / CF_PAGES_PROJECT——專案名放 secret 避免進 log）
- **Phase 4** Actions workflow：`.github/workflows/snapshot.yml`——cron `7,27,47 1-5 * * 1-5`（light，台北 09:07-13:47 每 20 分）、`10 0 * * 1-5`（full 早晨 08:10 補救）、`40 13 * * 1-5`（full 盤後 21:40，FinMind 法人 20:00/融資券外資借券 21:00 入庫後）；workflow_dispatch 可手動選模式；mode 判斷用 `github.event.schedule` 比對 cron 字串；`requirements-actions.txt`（requests+yfinance）；wrangler pages project create（首跑自建、冪等吞錯）+ deploy。驗收：手動觸發 full/light 綠燈、手機開頁、curl 驗 X-Robots-Tag
- **Phase 5** 排程實跑驗證（下一交易日檢查 Actions 歷史與籌碼分更新）＋手機 PWA 加入主畫面＋README 補章節（含 GitHub App 改 watchlist 流程）

### 額度計算（寫入計畫書）
- GitHub Actions（private 免費 2000 分/月）：light ~2 分 ×15×22 ≈ 660 分＋full ~5 分 ×2×22 ≈ 220 分 ≈ **880 分/月（44%）**；逼近時 light 改 30 分一次
- FinMind：full 97 次/次 ×2/日＝194 次/日，遠低於 600 次/hr；light 0 次

### 風險與假設（附信心度）
FinMind 入庫時間（高，官方文件）、GitHub cron 延遲 best-effort（高，已避峰）、yfinance 在 Actions IP 可能限流→minute=None 降級路徑已存在（中）、TWSE MIS 海外 IP（中，有 yfinance 降級）、60 天不活動排程停用（低信心標註）、休市日照跑（接受）

## Verification

- 計畫書寫完後：fresh-context read-back agent 驗證（只給檔案路徑，問「照這份計畫執行會在哪一步卡住？引用的檔案/函式/指令是否真實存在？」，特別抽查：pipeline 抽取對 app.py 現行結構的假設、workflow YAML 語法、cron 與台北時間換算、額度計算）
- 發現問題→修→複核
- 本 session 不做實作
