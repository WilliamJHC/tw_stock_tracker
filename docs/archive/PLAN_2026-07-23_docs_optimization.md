# stock_tracker 優化計畫

## Context（為什麼做這件事）

使用者要求「檢討 stock_tracker 所有文件並提出優化計畫」。此專案是一套**個人用**的台股決策追蹤系統（Python 3.12 + FastAPI + Vanilla JS 前端），已部署到 Cloudflare Pages（GitHub Actions 排程產靜態快照，手機 PWA 可看），並有 Telegram 盤中警報。

**現況基準（已實跑驗證，非引用宣告）**：
- `tests/` **163 個測試全過**（`.venv/Scripts/python.exe -m pytest tests/ -q`，0.71s）。專案地基健康，記憶檔裡舊的「60+/72」已過時。
- 核心 11 個 Python 模組、realtime/ 適配器層、10 個測試檔、9 份文件。

檢討由三個 Explore agent 分頭盤點（結構／核心程式碼／部署與文件），主對話再對高危宣稱逐一實檔驗證。以下計畫**只列已驗證或標明信心度的項目**，並依「這是個人工具、使用者真正產出是投資決策而非程式碼」的原則排序——優先清理與正確性，不做過度工程。

**使用者已定案（2026-07-23）**：範圍＝全做（Tier A+B+C+文件）；A1 共用函式＝新建 `env_utils.py`；殘渣＝驗收腳本移入 `tests/`、spike 日誌與 `watchlist.json` 提交。

## 執行順序建議

一次一階段、每階段結束跑測試守住 163 綠，順序：**A（去重清理）→ B1（先驗證是否真有 bug）→ B2/B3 → C → 文件 D**。B1 若重現不出 bug 就不動碼、據實回報。最後才做涉及版控的提交（殘渣清理＋watchlist）。

---

## 分層優化項目

### Tier A — 清理與去重（低風險、高價值，建議先做）

**A1. 合併重複的 .env 讀取邏輯**（✅ 已實檔確認重複）
- `data_sources.py:25-45` `_load_token()` 與 `alerts.py:151-173` `_load_env(key)` 是幾乎相同的逐行解析＋剝引號邏輯；`alerts.py` docstring 自己註明「same convention as data_sources.py:25-45」。
- 做法（已定案）：**新建 `env_utils.py`**，內含單一 `load_env_value(key)`（含 env var 優先、`.env` 逐行解析、剝引號、`OSError` 吞掉回空字串）。`data_sources._load_token()` 改為 `load_env_value("FINMIND_TOKEN")`；`alerts._load_env` 刪除、改呼叫 `load_env_value`。注意 `BASE_DIR` 常數來源要一致（`env_utils` 自行推導專案根，或由呼叫端傳入）。
- 驗收：`pytest tests/` 仍 163 過；若有測試直接測 `_load_env`/`_load_token`，同步更新。

**A2. 合併重複的 `_cache_file()`**（✅ 已實檔確認逐字相同）
- `intraday_feedback.py:41-42` 與 `monitor.py:176-177` 完全相同（都是 `os.path.join(ds.CACHE_DIR, f"intraday_{code}_{date_str}.json")`）。
- 做法：兩者都已 `import data_sources as ds`，把此 helper 放 `data_sources.py`（如 `intraday_cache_file(code, date_str)`），兩處匯入使用，避免循環匯入。
- 驗收：`pytest tests/test_monitor.py tests/test_intraday_feedback.py` 通過。

**A3. 版控殘渣清理**（✅ 已確認 git 狀態）
- `watchlist.json` 已改未提交（16–20 檔清單）——決定要不要提交（見「待決問題」）。
- `docs/spike_phase3_verify_log.txt`（259 行 Telegram 警報日誌）、`docs/verify_phase3_binance_alert.py`（72 行 Phase 3 驗收腳本）為未追蹤檔，且 `.gitignore` 未排除 `docs/`。
- 做法（已定案）：`verify_phase3_binance_alert.py` **移入 `tests/`** 並改寫成 pytest 形式（讀懂它現在測什麼、轉成 `test_...` 斷言，納入 163→N 的測試集）；`spike_phase3_verify_log.txt` 與 `watchlist.json` **提交進版控**。提交為不可逆對外動作，放在所有程式碼改動驗證通過後最後執行，commit message 用繁中。

### Tier B — 韌性與正確性（中價值）

**B1. 籌碼分融資組件的零除風險**（⚠️ 低信心，**須先寫重現測試確認 bug 是否真存在**再修）
- 來源：核心分析 agent 指 `analysis.py:600-610` 對「融資餘額整列全 0」的 ETF/無融資標的可能觸發 ZeroDivisionError。**尚未實跑重現**。
- 做法：先用 0050 ETF 或構造全 0 margin 的 fixture 寫最小重現測試；紅了才修，並補防守（不足 6 筆或全 0 視為缺資料）；綠了再驗一次。若無法重現，回報「疑慮不成立」不動碼。

**B2. FinMind API 韌性與資料層級可觀測性**（中信心）
- `data_sources.py` 三層降級（FinMind→yfinance→TWSE）無 retry；且 token 無效時靜默降級到免費資料，使用者不知拿到的是差等級資料。
- 做法：`_finmind()` 上層加 exponential backoff（2–3 次）；在 `/api/config` 或快照 metadata 標示 `data_tier: premium|free`，前端顯示。**範圍偏大，建議與使用者確認是否納入本輪。**

**B3. 前端 data.json 載入失敗的 fallback**（中信心）
- `static/index.html:331` 有 try/catch 但無 fallback UI；快照異常時手機白屏無提示。
- 做法：加「資料載入失敗／顯示上次更新時間」的提示框。

### Tier C — 可維護性（依使用者意願，可延後）

**C1. 集中散落的常數**：`analysis.py:29-39`（權重、SCORE_BANDS）、籌碼門檻 `0.10/0.05/0.30`（`analysis.py:584,600,607,627`）、`pipeline.py:32-33`、`monitor.py:54-55`、`alerts.py:23-36` 硬編。集中到 analysis.py 頂部或 `thresholds.py`，方便回測調參。**注意：屬「能簡單就別加戲」的邊界，若使用者不常調參則價值有限。**

**C2. 補測試缺口**：snapshot.py light-mode 增量更新（生產每 20 分跑但無測試）、籌碼 getter 單位轉換（融資張×1000 vs 借券股不轉）。

### 文件面（使用者原始要求核心：「檢討所有文件」）

**D1. 計畫書執行狀態標註**：`docs/optimization_plan.md`、`mobile_snapshot_plan.md` 已執行完（Phase 0-5 / 0-4），`dev_plan_intraday.md` 進行中（Phase 4-5 待）。建議在各計畫書頂部加一行「狀態：已完成／進行中（到 Phase N）」，或把已完成者移到 `docs/archive/`，避免下個 session 誤把舊計畫當待辦。
**D2. spike 臨時檔歸位**：同 A3，`spike_*.txt` / `verify_*.py` / `spike_*_smoke_test.py` 屬實驗殘留，統一分類（tests/ 或 archive/ 或刪）。

---

## 驗證方式（每階段結束都要跑）

- **程式碼改動**：`cd C:\Users\william.chen\claude\stock_tracker && .venv/Scripts/python.exe -m pytest tests/ -q`，須維持 163 過（或改動涉及新測試時 ≥163）。跑 Python 遇中文亂碼加 `PYTHONIOENCODING=utf-8`。
- **B1 修 bug**：先紅（重現）後綠（修完），親眼看輸出。
- **前端 B3**：依環境備忘，Claude Preview 封鎖外部網路——先用 Python 抓真實 data.json 存 fixture、頁面裡 stub fetch 再測。
- **文件大改（D1 若移檔）**：派 fresh-context agent read-back，確認引用路徑仍存在。
- 回報一律三態：已驗證（附方式與結果）／已完成但未驗證／未完成（附卡點）。
