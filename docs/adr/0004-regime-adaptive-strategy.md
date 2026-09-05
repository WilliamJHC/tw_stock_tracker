# 0004. 盤勢（多頭/空頭/盤整）分策略：定義基準、範圍界線、兩套不同的治理方式

## 狀態

已接受（2026-09-06，/grill-me 收斂）

## 背景

使用者想把大盤盤勢分成多頭/空頭/盤整，研究並實作各盤勢下該用的策略。專案裡完全沒有大盤層級的市場狀態判斷（`analysis.py`/`breakout_strategy.py` 的「均線多頭/空頭排列」都是**個股**均線排列，非大盤；`data_sources.py` 沒有任何抓取加權指數的函式）——這是全新功能，經過一輪 `/grill-me` 把範圍談清楚，見下方決策。

## 決策

**1. 盤勢定義：大盤（TAIEX）MA 排列 + 斜率，三態多頭/空頭/盤整。**

`src/market_regime.py`：`close > MA20 > MA60` 且 MA20 斜率 > 門檻 → 多頭；`close < MA20 < MA60` 且斜率 < -門檻 → 空頭；其餘 → 盤整。延續 `analysis.py` 既有「均線多頭/空頭排列」的個股判斷語彙，只是搬到大盤層級並多一個「盤整」的中間態。

**2. 範圍：只調整 (a) 選股因子權重、(c) 個股回測警示層參數；不做曝險/現金控制，不動個股即時決策面板。**

- (a) `finlab_factors.RANKING_WEIGHTS` 依盤勢調整（`composite_scores(weights=...)` 本來就支援 override，最自然的注入點）。
- (c) `backtest.STRATEGY_PARAMS` 依盤勢調整（`simulate()` 新增 `regime_by_date` 參數）。
- **不做**曝險/現金控制：`portfolio_backtest.py` 本來就是「滿倉買 top N、等權重」，沒有現金部位的概念，引入這個是完全不同量級的工程。
- **不動**個股即時決策面板（`analysis.py` 的「準備進場/減碼警示/今日關注」）：範圍卡在「排名權重 + 回測進出場參數」兩個既有機制，個股頁面 UI 改版牽動 `SCORE_WEIGHTS`/`compute_attention()` 的閾值語意，是另一塊工程。

**3. 治理分兩套，依風險高低——這是本 ADR 的核心決策。**

- **`RANKING_WEIGHTS` 分盤勢：人工審核，比照 ADR 0003。** 這是即時自動化的 alpha 引擎，`scripts/propose_regime_weights.py` 只產生候選（寫 `meta.pending_regime_weights_candidate`），`scripts/apply_regime_weights_candidate.py`（`workflow_dispatch`-only）人工核准/拒絕。
- **`STRATEGY_PARAMS` 分盤勢：不建審核流程，沿用它現有的「敘述統計後寫死進 code」模式。** `src/backtest.py` 的 `STRATEGY_PARAMS`（`:64-69`）本來就是「一次性 walk-forward 調校後寫死在常數裡、附註解說明依據」的做法，不是像 `RANKING_WEIGHTS` 那樣有 meta 存值＋即時審核的系統。`STRATEGY_PARAMS_BY_REGIME` 依同樣模式，用 `signal_win_rates_by_regime()` 的敘述統計驗證後直接寫進常數（見 `docs/regime_strategy_research.md`），不需要另一套 `pending_*`/`apply_*` 機制。

**4. 防抖（連續 N 天同一原始分類才算切換）是純函式，不用 `meta` 持久化狀態。**

`market_regime.effective_regime_series(raw_series, confirm_days)` 每次呼叫都從頭掃過整段原始分類序列重算，不記住「昨天算到哪」。`scripts/run_daily_recommendations.py`（今天要用哪組排名權重）和 `src/run_backtest.py`（歷史上每一天曾經是什麼盤勢，回放時直接用未防抖的原始 `regime_series()`——見決策 5）各自對同一段 TAIEX 歷史重算，結果保證一致，不需要一份共享狀態去同步。

**5. Phase B 的因子/勝率統計分桶用原始（未防抖）分類，防抖只用在「正式上線後，今天套用哪組」這件事上。**

`evaluate_factors_by_regime()`/`signal_win_rates_by_regime()` 都吃 `raw_regime_series`（`market_regime.regime_series()`）而非防抖後的序列——分桶統計要的是樣本粒度乾淨，防抖是操作面「不要天天切換」的考量，兩者目的不同，混在一起會讓統計結果依賴一個跟「這天真實盤勢是什麼」無關的緩衝參數。

## 為什麼這樣分界

`RANKING_WEIGHTS` 走人工審核而 `STRATEGY_PARAMS` 不用，理由跟 ADR 0003 分辨「既有已驗證因子的權重調整」vs.「全新未驗證邏輯」是同一套判斷邏輯，只是這次的分界線畫在「這組參數本來就有沒有審核機制」而非「因子新不新」：`RANKING_WEIGHTS` 是即時讀 `meta`、每天自動生效的排名系統，任何權重改動當天就影響全市場推薦；`STRATEGY_PARAMS` 一直以來就是改 code、跑 walk-forward 驗證、commit 進版本控制，本來就要經過 PR 這道人的關卡，重複建一套 `pending_*`/`apply_*` meta 機制只是把同一件事再做一次，屬於過度工程化。

**空頭 `RANKING_WEIGHTS` 目前沒有候選，是證據不足，不是決策排除。** `docs/regime_strategy_research.md` 的 Phase B 實測：空頭桶在 h=20 只有 2-3 個再平衡日，`propose_regime_weights.py` 的樣本數門檻（`DEFAULT_MIN_SAMPLE_DATES`，預設 20）會自動跳過它，不是這份 ADR 刻意不做空頭——等 `cache/factor_run` 累積更多空頭月份的歷史後，重跑 `propose_regime_weights.py` 就有機會產生候選。這個門檻寫在 code 裡（`propose_regime_weights.build_candidate()`），不是只寫在文件的 prose 裡，好處是未來樣本變多時會自動重新評估，不需要有人記得這個限制。

## 考慮過但拒絕的方案

**盤勢判斷改用 ADX 或波動度/布林通道寬度。** ADX 專案裡完全沒有既有實作，等於要新引入一整套技術指標依賴；波動度類指標適合抓「盤整」但抓不出方向，等於要疊兩套規則。MA 排列延續專案既有語彙、不用新增依賴，之後若實證發現分類效果不好，再考慮混入這些指標。

**空頭曝險改用降低持股檔數/引入現金部位。** 使用者已確認排除——這是系統目前完全沒有的能力（`portfolio_backtest.py` 一直是滿倉、等權重），要嘛新增一個資金配置維度，要嘛只是在既有滿倉框架裡挑「相對最不糟」的股票；後者是本 ADR 實際採用的範圍，前者列為明確不做，需要時再單獨評估。

**`STRATEGY_PARAMS_BY_REGIME` 也走跟 `RANKING_WEIGHTS` 一樣的完整 purged walk-forward + PBO/DSR 框架。** 使用者已確認拒絕：那套框架是為了防止「在大量參數組合裡硬選出來的最優解」過擬合，而這裡只是 3 個盤勢 x 少數幾個既有參數的直接調整，過擬合風險本來就低很多，上完整框架是額外工程量換不到對應的風險控制效益。

**盤勢防抖狀態存進 `meta`，讓兩個消費端（排名權重、回測參數）共讀同一份「目前盤勢」。** 查證後發現不需要：`src/backtest.py` 的 `STRATEGY_PARAMS` 本來就是批次工具（`run_backtest.py` 定期跑歷史回放，不是每次網頁請求即時重算），回放歷史時本來就要用「當時」的盤勢而非「今天」的盤勢，跟 `run_daily_recommendations.py` 要的「今天生效哪組」是不同的查詢——用一個純函式各自對同一段 TAIEX 資料重算，比維護一份跨天同步的共享狀態更簡單，也不會有兩邊狀態不同步的風險。

## 影響

- `src/data_sources.py`：新增 `get_index_daily(index_id="TAIEX", days=1825, cache=True)`（FinMind → yfinance 兩層降級，索引無 splits/dividends 故無 `adjusted` 欄位）；抽出共用的 `_finmind_price_rows_to_hist()`（`get_daily()`/`get_index_daily()` 共用）。
- `src/market_regime.py`（新檔）：`classify_regime()`/`raw_regime_series()`/`regime_series()`/`effective_regime_series()`/`current_regime()`，純函式，見決策 1/4。
- `src/factor_eval.py`：新增 `evaluate_factors_by_regime()`（分桶包一層既有 `evaluate_factors()`，不重寫評估邏輯）、`_regime_as_of()`（rebalance 日期對應盤勢的「as of」查詢）、`REGIMES` 常數。
- `src/analysis.py`：`backtest_signal()` 新增可選 `allowed_indices` 參數（`None` 時行為完全不變），供分盤勢的勝率統計重用同一個迴圈。
- `src/backtest.py`：新增 `STRATEGY_PARAMS_BY_REGIME`（目前只有 `bear` 有覆寫，見決策 3）、`signal_win_rates_by_regime()`、`_resolve_params()`；`simulate()` 新增可選 `regime_by_date` 參數（`None` 時行為完全不變，即決策 4/5 的「不用 meta 狀態」）。
- `src/run_backtest.py`：`run_replay_and_simulate()` 新增 `_taiex_regime_by_date()`（整批共用一次 TAIEX 抓取，FinMind/yfinance 都失敗時回傳 `None` 優雅降級），`simulate()` 呼叫改傳 `regime_by_date`；`strategy_runs.params_json`/`meta.params_json` 改存 `{"base":..., "by_regime":...}`（原本是扁平的 `STRATEGY_PARAMS`，因為現在有分盤勢版本需要一併留痕）。
- `src/db.py`：新增 `active_ranking_weights_by_regime`、`pending_regime_weights_candidate` 兩個 `meta` key，通用 key-value 表，不需要 schema migration。
- 新增 `scripts/report_regime_factor_ic.py`（研究報告，因子 IC + 訊號勝率分盤勢）、`scripts/propose_regime_weights.py`（產生 `RANKING_WEIGHTS` 候選，含樣本數門檻）、`scripts/apply_regime_weights_candidate.py`（人工核准/`--reject`，須透過 `.github/workflows/apply_regime_weights_candidate.yml` 執行，理由與 `apply_strategy_candidate.py` 相同——本機 `db.connect()` 開的是無關的本機 sqlite 檔）。
- `scripts/run_daily_recommendations.py`：`build_recommendations()` 新增 `_current_market_regime()`（`get_index_daily()` + `market_regime.current_regime()`，失敗時回傳 `None`），讀 `meta.active_ranking_weights_by_regime` 決定要不要覆寫 `weights`（未核准前 fallback 回現有 `active_ranking_weights`，行為不變）；輸出 payload 新增 `market_regime` 欄位。
- `CONTEXT.md`：新增「盤勢／市場狀態（market regime）」詞條，界定與既有「均線多頭排列」（個股層級）的差異。
