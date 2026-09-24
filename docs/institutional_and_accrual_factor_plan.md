# §2.2 法人買賣超 + §2.3 應計盈餘品質因子 — 串接計畫

2026-09-22。回應 `docs/factor_timeframe_optimization_directions.md` §2.2/§2.3，接續 `docs/factor_ic_candidate_and_momentum_window_report.md` 已做的 §2.1/§3.1 零成本實驗。前置的 API 小規模探索已完成（見對話紀錄，用 2330 在 free tier 不帶 token 直接打通），本檔只規劃「怎麼串」，不包含實作——照這個專案的既有規矩，計畫寫完就停，等使用者看過再決定要不要開工、要不要調整順序。

## 現況與探索結論

- 資料源：FinMind，跟 repo 現有 revenue/roe/pbr 抓取用同一個 `data_sources._finmind(dataset, data_id, start_date, end_date)`，free tier（`data_id` 查單一股票）即可，不需要升級付費方案。
- §2.2 探索結果：`TaiwanStockInstitutionalInvestorsBuySell`，每天每個法人類別（`Foreign_Investor`／`Investment_Trust`／`Dealer_self`／`Dealer_Hedging`／`Foreign_Dealer_Self`）各一列 `buy`/`sell`（股數），2330 抓 2026-08-01 起有 180 列，資料乾淨。
- §2.3 探索結果：三個財報 dataset 都命中所需欄位——
  - `TaiwanStockBalanceSheet`：`CurrentAssets`、`CurrentLiabilities`、`CashAndCashEquivalents`（101 個 type，`TotalAssets` 應該在後段，**尚未逐一確認完整列表**，是開工前第一步要補的）
  - `TaiwanStockCashFlowsStatement`：`NetCashInflowFromOperatingActivities`（營運現金流）
  - `TaiwanStockFinancialStatements`：`IncomeAfterTaxes`（稅後淨利）

## §2.2：法人買賣超因子

### 因子定義（待人工確認，這裡先給一個具體提案）

`inst_net_5d`：近 5 個交易日三大法人（外資+投信+自營商合計，不分自營商避險/自行）淨買賣超股數，除以近 5 日均量做正規化（避免大小型股買賣超股數量級差異主導排名），同一 rank-IC 評估方式（`factor_eval.evaluate_candidate_factor()`）。

比照 §2.1 gross_margin_trend/value 候選因子的既有模式，不是唯一可能定義——法人買賣超常見替代做法還有「連續買超天數」「外資持股比例變動」，這裡先選最簡單、最貼近 momentum 式動能訊號的版本，若 rank-IC 顯著再考慮是否測試替代定義。

### 串接步驟

1. `src/data_sources.py` 新增 `get_institutional_flow(code, days, cache=True)`：呼叫 `_finmind("TaiwanStockInstitutionalInvestorsBuySell", code, start_date=...)`，回傳 `[{date, foreign_net, trust_net, dealer_net}, ...]`（升冪）——同 `get_per_pbr()` 的既有 shape 慣例。
2. `src/finlab_factors.py` 新增 `institutional_flow_series(flow_rows)`：算 `inst_net_5d`（滾動 5 日淨買賣超/均量），輸出 `[{"usable_date", "inst_net_5d": float}, ...]`——同 `value_series()`/`gross_margin_trend_series()` 的既有 shape 慣例，供 `_as_of()` 查。**法人買賣超資料是 T 日收盤後才公布**，這裡的 `usable_date` 要設成 T+1（比照 revenue/roe 的公告延遲處理慣例，避免用到還沒公布的資料算進當天排名——這步是本因子最容易犯的 look-ahead 錯誤，要特別小心）。
3. `scripts/run_factor_backtest.py` 的 `fetch_universe_data()` 依既有 Phase 6b（per_pbr_series）backfill 模式，加一段 `institutional_flow_series` 的抓取＋快取欄位（`universe_data[code]["institutional_flow_series"]`）。
4. `scripts/report_factor_ic.py` 加一行候選因子評估：`("institutional_flow (候選)", ("institutional_flow_series", "inst_net_5d"))`，沿用既有 `evaluate_candidate_factor()`。
5. 跑 `run_factor_backtest.py` 補抓全市場快取（1821 檔 × 1 個新 dataset request，量體跟當初補 per_pbr_series 差不多），再跑 `report_factor_ic.py` 看 rank-IC 是否顯著。

### 已知風險/未定事項（2026-09-22 開工時已處理）

- ~~法人買賣超資料的公告延遲慣例需要跟現有 revenue/roe 對齊~~：實測 2330 發現當日盤中/盤後即可查到當天資料，公布時點不夠明確，因此 `institutional_flow_series()` 保守採用 **T+1**（滾動窗最後一天的下一個交易日）當 usable_date，不直接用 `date` 當天（避免 look-ahead，見函式 docstring）。
- ~~未分市場（上市/上櫃/興櫃）法人分類可能有欄位差異~~：加測 6488（上櫃）欄位與 2330（上市）一致，無需特殊處理。
- 因子定義：`inst_net_5d` = 近 5 日三大法人合計淨買賣超股數 / 近 5 日均量，已實作在 `src/finlab_factors.py`。
- 串接已完成：`data_sources.get_institutional()`（既有函式，不需新增）→ `finlab_factors.institutional_flow_series()` → `run_factor_backtest.py` 的 `institutional_flow_series` 快取欄位 backfill → `report_factor_ic.py` 候選因子評估行。單元測試見 `tests/test_finlab_factors.py`。
- 全市場快取回填（1821 檔，institutional_flow_series backfill，約 2026-09-22 17:33-20:56 跑完）+ `report_factor_ic.py` 已跑，結果如下。

### 結果（2026-09-22，`report_factor_ic.py`，n=56-59）

| horizon | mean IC | t | ICIR |
|---|---|---|---|
| h=1 | -0.009 | -0.83 | -0.108 |
| h=5 | -0.015 | -1.48 | -0.192 |
| h=20 | -0.010 | -1.07 | -0.141 |
| h=60 | -0.003 | -0.30 | -0.040 |

**結論：`inst_net_5d` 在所有 horizon 都不顯著**（`|t|` 全部 < 2，比對此專案既有的顯著性門檻），甚至方向偏負（跟 momentum 因子在短天期的負向類似，不是 revenue/roe/value 那種穩定正向訊號）。跟同一輪測試的 value/pbr（h=60 時 t=+3.88, icir=+0.518，見 `docs/factor_ic_candidate_and_momentum_window_report.md` §2.1）對比明顯較弱。

**判斷：不採用**——`inst_net_5d`（近 5 日三大法人淨買賣超/均量）這個具體定義沒有可用的預測力，不進入候選因子清單，也不需要再走 §2.1 追加那種正式 walk-forward + DSR/PBO 檢驗（第一關 rank-IC 顯著性就沒過，見「共同事項」的兩關流程）。若未來想重新嘗試法人買賣超這個方向，建議先換定義（例如「連續買超天數」「外資持股比例變動」，見上面「因子定義」段落列出的替代方案），而不是重跑同一個定義期待不同結果。

## §2.3：應計盈餘品質因子

### 因子定義（待人工確認）

`accrual_ratio`：Sloan (1996) 精神的簡化版——`(IncomeAfterTaxes - NetCashInflowFromOperatingActivities) / TotalAssets`（用當季 TotalAssets 當分母；若要更貼近原始 Sloan 公式，分母可改用期初期末總資產均值，這裡先用簡化版本，之後如果顯著再考慮加精度）。比 revenue/roe 現有 quarterly 頻率一致，用 `_sane_announce_date`/`FINANCIALS_LAG_DAYS` 同一套公告延遲慣例（三個財報 dataset 應該共用同一份 `financials_rows`/`announce_date` 欄位，需要開工時確認三者的 `date`/`origin_name` 是否可以用同一個 `data_id` 一次抓齊，或要三次個別 request）。

### 串接步驟

1. 開工前先補一步小探索：查完整 101 個 `TaiwanStockBalanceSheet` type 值確認 `TotalAssets`（或等價欄位）的確切字串，以及三個 dataset 的 `date` 欄位是否對得上同一個財報期（例如都用 `2026-08-14` 代表 Q2 財報，還是各自独立公告日）。
2. `src/data_sources.py` 新增 `get_accrual_inputs(code, days, cache=True)`：整合三個 dataset 的 request（`get_financials()` 現有函式可能已經抓了其中一部分，需要先讀 `get_financials()` 現況確認是否能擴充複用，而不是重寫一次三個 dataset 的抓取邏輯）。
3. `src/finlab_factors.py` 新增 `accrual_quality_series(...)`：同 `gross_margin_trend_series()` 的既有 shape 慣例，輸出 `[{"usable_date", "accrual_ratio": float}, ...]`。
4. `scripts/run_factor_backtest.py`／`scripts/report_factor_ic.py` 同 §2.2 步驟 3-5 的模式加掛。

### 已知風險/未定事項（2026-09-23 開工後更新）

- ~~三個財報 dataset 分開 request~~：實作結果比預期省——`total_assets`（`TaiwanStockBalanceSheet` 的 `TotalAssets` type）搭 `equity_rows` 既有 request 免費拿到，只有 `operating_cash_flow`（`TaiwanStockCashFlowsStatement`）是真正新增的第 3 個 request，`get_financials()` 從 2 個 request 變 3 個（不是原本估的可能要 4 個）。全市場 1821 檔重抓已完成。
- ~~`TotalAssets` 欄位字串未確認~~：已用 2330 實測確認型別字串是 `TotalAssets`，且與 `NetCashInflowFromOperatingActivities`（`TaiwanStockCashFlowsStatement`）的 `date` 對得上同一個財報期。全市場 983/1821 檔算出非空 `accrual_quality_series`（其餘因缺 `operating_cash_flow`/`total_assets` 被跳過，屬預期內的資料覆蓋率限制，非 bug）。
- accrual 公式沿用簡化版（分母用當季 `TotalAssets`，非期初期末均值）——結果見 `docs/factor_ic_candidate_and_momentum_window_report.md` §2.3，暫不需要加精度：pooled IC 已經顯著，卡關的是 walk-forward 折數不夠，不是公式精度問題。

## 共同事項

- 兩個因子都走跟 §2.1 三個候選因子（gross_margin_trend/operating_margin_trend/value）一樣的路：**先用 `evaluate_candidate_factor()` 做 rank-IC 顯著性檢驗，顯著才考慮進一步走 §2.1 追加那種正式 walk-forward + DSR/PBO 檢驗，不顯著就記錄下來、不進候選**。全程不改動 `RANKING_WEIGHTS`/`composite_scores()` 預設行為。
- 兩個因子都需要先補抓全市場快取（`cache/factor_run/`），跟當初 Phase 6b 三個候選因子的做法一樣，抓取時間跟 API 用量是主要成本，不是程式碼複雜度。

## 建議順序（2026-09-24 更新）

1. ~~§2.2（法人買賣超）：資料形狀已經確認乾淨、複雜度低，建議先做。~~ 已做完，結果不顯著（見上方「結果」），不進候選。
2. ~~§2.3（應計盈餘品質）~~ 已做完，見 `docs/factor_ic_candidate_and_momentum_window_report.md` §2.3——pooled rank-IC 是四個已測候選因子裡最強的（h=60 t=-4.78），方向為負（高應計比＝未來報酬更差，符合 Sloan accrual anomaly），但正式 walk-forward + DSR/PBO 檢驗（`scripts/report_accrual_walkforward.py`）的 DSR 差一點沒過門檻（0.9477 vs 0.95）。**現階段不採用**，但標記為四個候選裡最接近通過、資料累積後優先重跑的對象。

---

§2.2、§2.3 都已做完並記錄結論（皆不採用，§2.3 保留「資料累積後重跑」的路）。`docs/factor_timeframe_optimization_directions.md` §2.4（董監質押比率）另有獨立的可行性查證，見 `docs/pledge_ratio_feasibility.md`。
