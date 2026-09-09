# 計畫：資料正確性與管線可靠度修復（稽核報告查證後）

狀態：**2026-09-09 已開工**。以下四項主張已逐一對照目前程式碼與 `cache/factor_run/`、`cache/factor_run_long/` 實際檔案內容核實。目前進度：
- 優先序 1：**全量作業已跑完並驗證**。2149 檔快取中 428 檔為 skip（下市/無資料），可用 1721 檔裡 c_adj 覆蓋 1709 檔（99.3%），per_pbr_series 覆蓋 1721 檔（100%）。剩 12 檔 c_adj 缺失，經查是 yfinance 對這些代號回傳「possibly delisted / no data」，屬真實資料源缺口（非程式邏輯問題），best-effort 設計下會在下次執行再嘗試。
- 優先序 2：根因已查明（YoY/TTM lead-in 結構性損耗，非資料源限制）；程式修法（拆 `fundamentals_days` 欄位）已實作並對 3 檔樣本實跑驗證通過（usable 序列起點成功提前到對齊 bars 起點）。**全量套用作業已跑完並驗證**：1721 檔可用代號中 1675 檔（97.3%）`fundamentals_days` 已補到 2250（=1800+450 lead-in），抽驗 2330 確認 `revenue_series[0].usable_date`（2021-08-10）已提前到 bars 起點（2021-08-16）之前。剩 46 檔仍卡在 0，log 明確記錄為「lead-in 重抓後 revenue/roe 序列仍不足」，屬真實資料不足（非程式邏輯錯誤），best-effort 設計下會在下次執行再嘗試。
- 優先序 3：便宜驗證已做（用部分遷移中的快取），**確認兩支腳本的排序都會被 lot_size 翻轉**，`run_ranking_optimize.py` 甚至連自動套用的 accept/reject 決策都會翻盤，比原本推測更嚴重。**待辦：用現在已全量遷移完成的乾淨快取重跑，取得最終數字**（見下方待辦）。
- 優先序 4：`scripts/update_universe_bars.py` 的 sanity floor 已實作並實跑驗證通過（正常路徑 + 模擬空快取路徑皆確認），已 commit（`da1a6df`）。

commit 紀錄：`da1a6df`（優先序 4）→ `cb21e54`（優先序 2 程式修法）→ `5aad426`（本文件）→ `f47d8ad`（fresh-context read-back 修正）→ `d930ce4`（優先序 1 全量結果 + 啟動優先序 2）。優先序 1 的全量背景作業（PID 25620，2026-09-09 09:32 啟動）已於同日約 15:34 結束，exit code 0，無 Python 例外，輸出在工作階段 scratchpad 下的 `full_backfill_run.log`（2651 行）。優先序 2 的全量套用作業（`python scripts/run_factor_backtest.py --universe all --days 1800`，2026-09-09 15:38 啟動，輸出在同目錄 `full_fundamentals_leadin_run.log`，2447 行）已於 2026-09-10 跑完，exit code 0，無 Python 例外；跑完後的排名法多因子回測摘要（universe=all）：`total_return_pct=20.83`、`cagr_pct=3.85`、`sharpe=0.60`、`max_drawdown_pct=-11.86`、trades=267（1721/2149 檔通過流動性+基本面篩選）——這組數字比優先序 1 剛跑完、優先序 2 尚未套用時的版本（total_return 17.23% / sharpe 0.49 / trades 187）全面改善，符合「基本面覆蓋起點提前→更早開始交易→樣本數增加」的預期方向。

## 查證結論總覽

| # | 主張 | 查證結果 |
|---|---|---|
| 1 | c_adj 全面缺失且靜默降級 | VERIFIED-TRUE |
| 2 | 基本面資料覆蓋起點過晚（拖累回測起點） | VERIFIED-TRUE |
| 3 | run_strategy_search.py 比較可能失真（整張捨入扭曲） | PARTIALLY-TRUE——且原報告有一個關鍵誤判：run_ranking_optimize.py 並未倖免 |
| 4 | update_universe_bars.py 空清單靜默成功 | VERIFIED-TRUE |

---

## 優先序 1：c_adj 靜默降級（資料正確性最上游，其他分析都建立在這之上）

**現況**：`src/finlab_factors.py:273-277` 的 `_close()` 邏輯是「`c_adj` 有就用、沒有就退回原始 `c`」，不記錄警告、不拋錯。抽驗 `cache/factor_run/2330.json`、`2317.json`、`5315.json` 三檔，bars 的 key 都只有 `date`/`c`/`v`，0 筆有 `c_adj`。`docs/regime_switch_vs_0050.md:133,143` 專案自己也記錄過同一個數字（全庫 2148 檔皆無 `c_adj`）。動能因子權重 0.3（`src/finlab_factors.py:44`）確認屬實，除權息日會產生假跌幅，汙染 rank-IC 評估、`portfolio_backtest`、以及 `regime_switch_vs_0050.md` 報告的全部數字。

**查證時的新發現（報告未提）**：
- `scripts/run_factor_backtest.py` 的 `fetch_universe_data()`（271-280 行）已經內建「快取缺 c_adj 就自動補算並回寫」的邏輯。也就是說架構上不是做不到，是 `cache/factor_run/*.json` 這批檔案自從加了這個補算邏輯後就沒有被重新跑過整批。
- Repo 裡另外躺著一個 `cache/factor_run_long/` 目錄，結構相同、歷史回溯到 2012 年、100% 有 `c_adj`，但全 repo 搜尋不到任何程式碼引用它（像是被遺棄的一次性實驗輸出）。這批資料能不能直接拿來用、或只是結構恰好相同但內容已過期，需要先確認再決定要不要參考它，不要假設可以直接掛上。

**建議動作（尚未執行）**：
1. 確認 `fetch_universe_data()` 的自動補算邏輯本身正確（先用一兩檔手動跑一次、人工核對填出來的 `c_adj` 合理），再決定是重新整批跑 `run_factor_backtest.py` 全量補齊 `cache/factor_run/`。**已確認 `scripts/build_adj_factors.py` 不是同一套機制**：它是寫入資料庫（`corporate_actions` 表 + `daily_bars.adj_factor` 欄位，屬於 `docs/archive/backtest_plan.md` Phase 1b 的舊架構），不會寫 `cache/factor_run/*.json` 裡的 `c_adj` 欄位——不要直接照原報告字面去跑它，補 `cache/factor_run/` 要走 `fetch_universe_data()`/`run_factor_backtest.py` 這條路徑。
2. 加一個 coverage guard：載入 universe 時算 `c_adj` 覆蓋率，寫進報告 metadata，低於門檻要警示或 fail——原則同 `CONTEXT.md` 裡「降級原因要在賦值當下決定，不要留給消費端事後推敲」那條。
3. `cache/factor_run_long/` 先查清楚用途與新鮮度再決定去留，不要順手刪除也不要順手掛上生產路徑。

---

## 優先序 2：基本面覆蓋起點過晚（直接決定 regime_switch_vs_0050.md 的結論是否定案）

**現況**：三檔快取 `revenue_series` 均從 2022-10-10 開始、`roe_series` 均從 2022-08-29 開始（僅 17 季），`bars` 從 2021-08-16（2330/2317）或 2021-09-15（5315，與另兩檔起點不同）開始。因果鏈已確認：`src/finlab_factors.py:429-466` 的 `factor_ranks()` 要求 momentum/yoy/roe 三者同時非 None，`src/portfolio_backtest.py:98-104` 的 `_rebalance_dates()` 抓每月第一個交易日再平衡，兩者疊加後把第一筆交易死死卡在 2022-11-01——不是流動性篩選、也不是動能暖機期（兩者用 2021-08-16 起算最晚 2021 年底就滿足），是 revenue/roe 快取覆蓋率不足造成的。`docs/regime_switch_vs_0050.md:24,137,141` 已自行記錄「0050 全期基準 363.4% 被墊高、對規則不利」，但原文用「懷疑」「原因待查」，未下定論——本次查證確認了因果鏈，不再是待查。

**根因已進一步查明（2026-09-09 補查）**：直接用 `days=1800` 呼叫 `ds.get_monthly_revenue('2330')`/`ds.get_financials('2330')`（`src/data_sources.py:871,914`），原始資料其實從 2021-10-01／2021-12-31 開始，只比 `bars` 晚 2-4 個月，**不是**上游資料源供應不到更早資料。「usable 序列」看起來從 2022-08~10 才開始，是 `src/finlab_factors.py:90-135` 的 `revenue_yoy_series()`（需要往前比對 12 個月）與 `src/finlab_factors.py:140-168` 的 `roe_series()`（TTM 需要 4 季）本身結構性的 lead-in 損耗，不是快取或資料源問題。**已用 `days=2500` 實測驗證**：2330 的原始 revenue/financials 資料可回溯到 2019-11／2019-12，算出的 usable 序列起點也跟著提前到 2020-11~12，早於 bars 現在的起點——證實 FinMind 能供應足夠早的資料，原本「資料源本身可能供應不到」的懸念可以排除。

**修法時遇到的卡點（已解決，選用「拆兩個 days 欄位」方案）**：原本 `ds.get_monthly_revenue(code, days=days, ...)`/`ds.get_financials(code, days=days, ...)` 用的是跟 bars 完全相同的 `days`，直覺的修法是改成 `days + 一個 lead-in 天數`；但 `_load_code_cache()`（`scripts/run_factor_backtest.py:100-112`）原本用單一 `"days"` 欄位同時當作 bars 和 revenue/roe 的快取失效判準，cache hit 分支完全不會重新抓 revenue_series/roe_series，只補 c_adj／per_pbr_series——只改抓取時傳的 days 值、不動快取結構的話，下次照樣用 `--days 1800` 跑會判定 cache hit，新 lead-in 邏輯永遠碰不到已存在的 2148 檔快取。因此改採「拆兩個獨立欄位」的方案，已實作：

**已實作並驗證（2026-09-09，「拆兩個 days 欄位」方案）**：
- `scripts/run_factor_backtest.py` 新增 `FUNDAMENTALS_LEAD_IN_DAYS = 450` 常數；`fetch_universe_data()` 內部算出 `fundamentals_days = days + FUNDAMENTALS_LEAD_IN_DAYS`，revenue/financials 呼叫（302、305 行 `ds.get_monthly_revenue`/`ds.get_financials`）一律改用這個較寬的天數抓取。
- 快取多存一個獨立欄位 `fundamentals_days`（`days` 欄位維持原本語意，只管 bars/整包快取是否要整組重抓）。cache-hit 分支新增一段（299-319 行）：`cached.get("fundamentals_days", 0) < fundamentals_days` 時，重抓 revenue/financials、重算 `revenue_series`/`roe_series`/margin-trend 序列，只在新結果非空時才覆寫（best-effort，跟既有 c_adj 補算——271-280 行——同一套「失敗就保留原值、下次再試」邏輯，不會因為一次網路失敗就把已有的好資料蓋掉）。
- 全新代碼（cache miss）的首抓路徑也一併改成用 `fundamentals_days` 抓，並在存檔時一起記錄該欄位。
- `update_universe_bars.py` 完全不讀寫 `days`/`fundamentals_days`，只動 `bars`，不受影響。

**實跑驗證結果**（對 2330/2317/5315 三檔跑 `fetch_universe_data()`）：三檔的 `revenue_series[0].usable_date` 都從 2022-10-10 提前到 **2021-08-10**，`roe_series[0].usable_date` 都從 2022-08-29 提前到 **2021-08-29**（`roe_series` 長度也從 17 季增為 21 季），兩者都已經對齊或早於 `bars[0].date`（2330/2317 為 2021-08-16，5315 為 2021-09-15）——原本卡在 2022-11-01 的第一筆交易日，理論上可以往前推約 14 個月。重跑同一批代碼第二次確認冪等（`fundamentals_days` 已達標的代碼不會重複發請求）。**尚未驗證全新（cache miss）代碼走的首抓路徑**——邏輯是同一段程式碼、同樣的 `fundamentals_days` 參數，結構上跟已驗證的 cache-hit 路徑一致，但沒有另外找一支真正全新的代碼實跑，屬於「已完成但未做這一分支的獨立驗證」。

**已完成**：優先序 1、2 兩輪全量作業（`python scripts/run_factor_backtest.py --universe all --days 1800`）都已跑完並驗證，見文件開頭狀態列與上方「全量套用」段落。97.3% 的可用代號 `fundamentals_days` 已補到位，剩 46 檔為真實資料不足的殘餘案例。

**已完成（2026-09-10）**：用預設參數重跑 `python scripts/report_vs_benchmark.py`（乾淨快取，`--lot-size 0` 預設值）。**整體結論不變：仍是「沒有任何候選×規則三項門檻同時通過，繼續抱 0050」**，跟 `docs/regime_switch_vs_0050.md` 現有版本記錄的結論一致。但個別門檻的通過情形有變化——原文件記錄全部候選×規則都是「✗✗✗」三項全不過；這次重跑後，`baseline_top10/rule_a`、`baseline_top20/rule_a` 的留出測試期門檻轉為通過（`x x V`），`baseline_top20/rule_b` 的滾動勝率門檻轉為通過（`x V x`）——基本面覆蓋起點提前確實讓部分候選的樣本外表現改善，但沒有任何一組三項同時過關，整體採用建議不受影響。`docs/regime_switch_vs_0050.md` 本身的完整報告（含逐月數字、敏感度分析等）尚未依這次結果重新產生/改寫——是否要整份重新產生是後續任務，這裡先記錄「結論不變、個別門檻有改善」這個查證結果，原始輸出存在 scratchpad `report_vs_benchmark_out.json`。

---

## 優先序 3：run_strategy_search.py／run_ranking_optimize.py 的整張捨入扭曲——**兩支腳本都受影響，不是只有前者**

**現況**：
- `scripts/run_strategy_search.py:164-166,220-221` 呼叫 `opt.sweep_ranking_weights_walk_forward()` 與 `pb.simulate()` 時，`params` 只帶 `ranking_weights`/`extra_factors`，未覆寫 `initial_capital`、`lot_size`。`src/portfolio_backtest.py:54-58,193-195` 確認未覆寫的鍵吃預設值：`initial_capital=1_000_000`、`lot_size=1000`。`docs/regime_switch_vs_0050.md:21,105` 已記錄這組設定下多數個股連一張都買不起，數字被整張捨入嚴重扭曲（`lot_size=1000` 下 baseline_top20 全期僅 4.9%，`lot_size=0` 則是 252.2%）。
- **原報告主張「`run_ranking_optimize.py` 走 rank-IC、不呼叫 `pb.simulate()`，不受影響」——查證後不成立。** `scripts/run_ranking_optimize.py:195` 一樣呼叫 `opt.sweep_ranking_weights_walk_forward()`，其內部（`src/optimize.py:671`）最終仍是 `pb.simulate(sliced, params=p, ...)`，同樣沒有覆寫 `initial_capital`/`lot_size`。該腳本雖然第一步用 rank-IC（`fe.suggest_weights()`）挑候選權重，但第三步（purged walk-forward 比較）一樣會被整張捨入扭曲。

**便宜驗證已完成，結果確認排序會翻轉（2026-09-09，用目前部分遷移中的快取跑，見下方警示）**：直接呼叫兩支腳本自己的 `build_candidates()` 產生真實候選組合，對 `opt.sweep_ranking_weights_walk_forward()` 帶 `sim_params={"lot_size": ...}` 各跑一次 `lot_size=1000`（現行預設）與 `lot_size=0`：

- `run_strategy_search.py` 的候選（baseline/with_ma_cross/with_vol_price_div/with_both）：`lot_size=1000` 下 winner=`with_both`（candidate_scores 全是負值：baseline -1.63、with_both 2.49）；`lot_size=0` 下 winner 翻成 `baseline`（分數全變正值且量級完全不同：baseline 20.8、with_both 15.7）。兩種 lot_size 下 `decision["accept"]` 都是 False，這次剛好都沒有觸發自動產生候選，但排序本身已經翻轉，score 正負號都不一樣。
- `run_ranking_optimize.py` 的候選（active/ic_suggested）：`lot_size=1000` 下 winner=`active`（維持現況），`decision["accept"]=False`；`lot_size=0` 下 winner 翻成 `ic_suggested`，且 **`decision["accept"]` 從 False 翻成 True**。這支腳本按 ADR 0002 設計是「accept 就自動套用」，代表同一組真實候選在兩種 lot_size 下，一個會維持現況、另一個會直接自動改動產線的 `RANKING_WEIGHTS`——不只排序不同，連「要不要自動改參數」這個決策本身都會被 lot_size 這個跟策略優劣無關的實作細節翻盤。這比原報告與本文件先前的推測都更嚴重。

**最終數字（2026-09-10，優先序 1、2 全量作業都跑完、快取已穩定後重驗）**：用同樣手法（同一批 scratchpad 腳本）重跑一次，這次讀到的是 1721 檔全部已補完 c_adj/per_pbr（優先序 1）且 97.3% 已補完 fundamentals lead-in（優先序 2）的乾淨快取：

- `run_strategy_search.py` 的候選：`lot_size=1000` 下 winner=`with_both`（point_best 同為 `with_both`，candidate_scores：baseline 6.20、with_both 6.86）；`lot_size=0` 下 winner 翻成 `baseline`（point_best 同為 `baseline`，candidate_scores 全面改觀：baseline 26.9、with_both 21.5）。**排序翻轉的定性結論在乾淨快取下依然成立**，兩種 lot_size 下 `decision["accept"]` 都是 False。
- `run_ranking_optimize.py` 的候選：`lot_size=1000` 下 winner（plateau most_common）=`ic_suggested`、但 point_best=`active`（candidate_scores：active 5.21、ic_suggested 4.01）；`lot_size=0` 下 winner 與 point_best 一致收斂為 `ic_suggested`（candidate_scores：active 45.4、ic_suggested 51.4）。**這次兩種 lot_size 下 `decision["accept"]` 都是 False，跟優先序 1 剛跑完時（用部分遷移中的快取）觀察到的「accept 從 False 翻成 True」不同**——用乾淨快取重驗後，accept 決策本身這次沒有翻盤，但 winner 與 point_best 是否一致依然會隨 lot_size 改變（point_best 從 active 變成與 winner 一致的 ic_suggested）。這說明先前「accept 會翻盤」的說法對當時那組（遷移中、不完整的）快取成立，但不是在所有快取狀態下都必然重現——**排序/評分對 lot_size 敏感**這個核心結論不變，但「連 accept 都會自動翻盤」不宜當成放諸四海皆準的定論，需視當下候選權重與快取狀態而定。

**結論與建議動作**：
1. ~~等優先序 1、2 的兩輪全量作業都跑完、快取穩定後，重跑取得最終數字~~ — 已完成，見上方最終數字。
2. `run_strategy_search.py` 與 `run_ranking_optimize.py` 產出的任何既有結論（包括已經寫進 ADR 的審核輸入，例如 `docs/adr/0003-manual-review-for-strategy-logic-search.md`——repo 裡還有另一份同編號但主題不同的 `docs/adr/0003-recommendation-pipeline-user-facing-scope.md`，注意不要對錯檔案）都要標記為「需要用修正後的參數重跑」，不要直接沿用舊結論。
3. 應該修的不是「拿哪個 lot_size 當標準」這種一次性調參，而是 `src/portfolio_backtest.py` 的 `PORTFOLIO_PARAMS` 預設 `initial_capital`/`lot_size` 組合本身——目前的 100 萬本金＋千股一張，會讓大多數個股連一張都買不起，這是資金規模設定跟台股股價分布不匹配的問題，兩支腳本目前都沒有覆寫這兩個鍵，才會全部繼承這組不合理的預設。要不要調整預設值本身（例如提高 `initial_capital` 或允許零股）是取捨判斷，留給你決定，這裡不擅自改。

---

## 優先序 4：update_universe_bars.py 空清單靜默成功

**現況**：`scripts/update_universe_bars.py:81-83` 的 `_cached_codes()` 用 `glob.glob(os.path.join(rfb.RUN_CACHE_DIR, "*.json"))` 決定要更新哪些代碼，完全依賴本地快取檔案是否存在。`main()`（188-229 行）通篇沒有任何空清單檢查；`codes == []` 時迴圈跑 0 次、`counts` 全 0，仍照常送出 Telegram 摘要並以 exit code 0 結束。CI 端一旦 GitHub Actions cache miss 或被驅逐，這條路徑就會被觸發，下游 `update_universe_bars` → `run_ranking_optimize` / `daily_recommendations` / `run_strategy_search` 全部讀到空 universe 但 workflow 顯示綠燈。

**建議動作**：
加一個 sanity floor：`len(codes)` 低於預期下限（例如低於 universe 應有檔數的某個比例）就 `sys.exit(1)` 並發 Telegram 告警。改動量很小（幾行），但擋掉的是整條管線靜默空轉的風險。

**已實作並驗證（2026-09-09）**：`scripts/update_universe_bars.py` 加了 `MIN_CACHED_CODES = 1500` 常數與對應檢查（`_cached_codes()` 回傳數低於門檻就印訊息、送 Telegram、`sys.exit(1)`，且在花掉 TWSE 額度前就短路）。實跑驗證：`--dry-run --limit 2` 正常路徑照跑無誤；monkeypatch `_cached_codes()` 回傳空列表模擬 cache miss，確認會印中止訊息並以 exit code 1 結束。已 commit（`da1a6df`）。

---

## 本次查證的假設與限制

- 最初的四項主張查證階段是唯讀的：讀程式碼、讀 JSON 快取內容、讀既有文件，沒有執行任何腳本、沒有連網、沒有改動任何檔案。使用者「開工」後才開始的優先序 1/2/4 修復與驗證動作有真的跑腳本、連網（FinMind/TWSE/Yahoo）、寫入 `cache/factor_run/*.json`、以及 3 個 git commit，細節見各優先序章節。
- 抽驗樣本限定 2330、2317、5315 三檔，未逐一核對全庫 2148 檔（但 `docs/regime_switch_vs_0050.md` 裡的既有記錄與抽驗結果一致，交叉印證了全庫層級的說法）。
- `cache/factor_run_long/` 的資料新鮮度與可用性尚未查證，只確認了「存在、有 c_adj、無程式碼引用」這三件事。
- 第三優先（盤勢分桶歸因，選股 alpha vs 再平衡成本拆解）與低優先（MIS 輪詢壓測）維持原報告的判斷，本次未展開查證——這兩項本身就是「下一步該做什麼」的研究提案，不是可查證的既存事實主張。

## 建議處理順序

優先序 1（c_adj）→ 優先序 2（基本面起點）→ 優先序 3（整張捨入驗證，兩支腳本都測）→ 優先序 4（sanity floor，改動最小可隨時插入）。1、2 會動到 `cache/factor_run/` 全量重跑，動作最大也最該先做，因為後面所有分析都建立在這份快取之上。
