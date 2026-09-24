# 候選因子 IC 評估 ＋ 動能 formation window 對照實驗報告

2026-09-22。回應 `docs/factor_timeframe_optimization_directions.md` §2.1、§3.1 的建議，用 `cache/factor_run/`（1821 檔、173 個月頻 rebalance 日，2012-05-02 ~ 2026-09-01）跑出的實測結果。零新資料成本、零 pipeline 改動——`RANKING_WEIGHTS`、`finlab_factors.MOMENTUM_DAYS` 都未變動，本文件只是證據，不是決策。

## §2.1：三個 Phase 6b 候選因子的 rank-IC

跑法：`python scripts/report_factor_ic.py`（既有腳本，此前從未執行過）。

```
factor                                   h=1                               h=5                              h=20                              h=60
momentum     -0.025  t= -2.39  icir=-0.194  n=151   -0.018  t= -1.58  icir=-0.128  n=151   -0.019  t= -1.71  icir=-0.139  n=150   +0.008  t= +0.82  icir=+0.067  n=148
revenue      +0.013  t= +2.16  icir=+0.176  n=151   +0.038  t= +5.61  icir=+0.457  n=151   +0.019  t= +2.81  icir=+0.229  n=150   +0.034  t= +4.66  icir=+0.383  n=148
roe          +0.021  t= +2.81  icir=+0.228  n=151   +0.021  t= +2.58  icir=+0.210  n=151   +0.027  t= +2.90  icir=+0.237  n=150   +0.014  t= +1.47  icir=+0.121  n=148

候選因子（Phase 6b，先評估、不保證採用）：
gross_margin_trend (候選)        +0.004  t= +0.50  icir=+0.066  n=58   +0.007  t= +0.83  icir=+0.109  n=58   +0.017  t= +1.73  icir=+0.229  n=57   +0.018  t= +1.56  icir=+0.210  n=55
operating_margin_trend (候選)    -0.001  t= -0.13  icir=-0.018  n=58   +0.010  t= +1.14  icir=+0.150  n=58   +0.014  t= +1.31  icir=+0.174  n=57   +0.004  t= +0.38  icir=+0.052  n=55
value/pbr (候選)                 +0.023  t= +1.42  icir=+0.184  n=59   +0.039  t= +2.29  icir=+0.298  n=59   +0.044  t= +2.40  icir=+0.316  n=58   +0.068  t= +3.88  icir=+0.518  n=56
```

**結論**：

- **value/pbr 通過顯著性門檻，且訊號隨 horizon 拉長而增強**：h=20 (t=+2.40)、h=60 (t=+3.88, icir=+0.518) 都顯著為正，icir 甚至比現有三因子任何一個在對應 horizon 都高。這是三個候選裡唯一一個「跟現有三因子把關標準（t 顯著、icir 為正）一致」的因子，且補上了 `RANKING_WEIGHTS` 目前完全缺席的價值象限（§2.1 原文假說得到證實）。**值得走 ADR 0003 人工審核流程**，列入下一步候選。
- **gross_margin_trend／operating_margin_trend 都沒有通過顯著性門檻**：最好的一格是 gross_margin_trend h=20 的 t=+1.73（傳統雙尾 5% 門檻需要 |t|≳2），且樣本數只有 n=55~58（現有三因子 n=148~151 的三分之一左右，財報資料覆蓋率較低）。**不建議進入候選審核**——不是「因子無效」的定論，是「目前證據不夠、且樣本太小」，若未來財報快取覆蓋率提高可重新評估，但不急。

## §3.1：動能 formation window 對照實驗

跑法：新增 `scripts/report_momentum_window_ic.py`（評估用途，未修改 `finlab_factors.MOMENTUM_DAYS` 或 `composite_scores()`），對照三組窗口：現行 60 日、短期 20 日、近似 12-1 動能的「120 日、跳過最近 21 日」。

```
window                                                         h=1                               h=5                              h=20                              h=60
60d (現行 MOMENTUM_DAYS)             -0.022  t= -2.30  icir=-0.176  n=170   -0.011  t= -1.08  icir=-0.083  n=170   -0.013  t= -1.24  icir=-0.095  n=169   +0.016  t= +1.84  icir=+0.143  n=167
20d (短期動能)                         -0.032  t= -3.14  icir=-0.239  n=172   -0.023  t= -2.31  icir=-0.176  n=172   -0.030  t= -3.06  icir=-0.234  n=171   +0.002  t= +0.21  icir=+0.016  n=169
120d-skip-21 (近似 12-1 動能)          -0.001  t= -0.10  icir=-0.008  n=166   +0.003  t= +0.31  icir=+0.024  n=166   +0.012  t= +1.17  icir=+0.091  n=165   +0.028  t= +2.94  icir=+0.230  n=163
```

**結論**：

- **§3.1 原文假說得到支持——「跳過最近一個月」在台股全市場樣本上是對的方向**：
  - 現行 60 日窗（涵蓋最近一個月）在短 horizon（h=1/5/20）ICIR 全負，其中 h=1 顯著（t=-2.30）。
  - 20 日短期動能（更貼近最近一個月）**訊號更差**：h=1/5/20 全部顯著為負（t=-3.14/-2.31/-3.06），沒有一格轉正。這排除了「短期動能本身有效、只是被 60 日窗稀釋」的假說——短期動能在台股就是負的。
  - 跳過最近 21 個交易日的 120 日窗（近似傳統 12-1 動能）**幾乎完全消除了短 horizon 的負訊號**（h=1/5/20 都接近零、不顯著），且**在 h=60 轉為顯著正值**（t=+2.94, icir=+0.230）——三組窗口裡唯一一組長 horizon ICIR 顯著為正的。
- 這證實「momentum ICIR 三盤勢皆負」的根因高度可能就是 formation window 涵蓋了容易被短期反轉污染的最近一個月，而非動能因子本身在台股不成立。傳統 12-1 動能的「跳過最近一個月」規則，至少在這個資料集上是對的（§3.1 原文提到的 Alpha Architect 反例在此未複現，可能是產業層級 vs 個股層級樣本的差異，不在本報告範圍內深究）。
- **治理判斷（依 §3.1 原文的灰色地帶指引）**：這是「既有已驗證因子的參數調整」而非新因子類別，證據已經到位（h=60 t=+2.94 顯著）。**建議下一步**：用 `evaluate_factors_by_regime()` 對 120d-skip-21 窗口重跑一次三盤勢分桶（呼應 `regime_strategy_research.md` 的既有格式），確認三種盤勢下 ICIR 是否都轉正或至少不再全負，再決定是否正式走 ADR 0003 候選審核把 `MOMENTUM_DAYS`/momentum 計算方式換成這組窗口。本報告本身不觸發自動套用。

## §3.1 追加：動能 formation window 的三盤勢分桶重跑

跑法：新增 `scripts/report_momentum_window_regime_ic.py`，用 `market_regime.regime_series(TAIEX)` 對 173 個 rebalance 日分桶（同 `docs/regime_strategy_research.md` 的既有分類方式），三組動能窗口分別在 bull/bear/consolidation 各自重跑。

```
-- bull (sample_dates=24) --
  window                                    h=1                    h=5                   h=20                  h=60
  60d (現行)             -0.059  t=-1.96  icir=-0.400  n=24   -0.078  t=-2.68  icir=-0.547  n=24   -0.020  t=-0.58  icir=-0.120  n=23   +0.037  t=+1.02  icir=+0.217  n=22
  20d                   -0.045  t=-1.82  icir=-0.371  n=24   -0.053  t=-2.80  icir=-0.572  n=24   +0.005  t=+0.21  icir=+0.043  n=23   +0.029  t=+1.09  icir=+0.233  n=22
  120d-skip-21          -0.039  t=-1.47  icir=-0.301  n=24   -0.071  t=-2.40  icir=-0.489  n=24   -0.026  t=-0.79  icir=-0.164  n=23   +0.033  t=+1.00  icir=+0.214  n=22

-- bear (sample_dates=6) --
  60d (現行)             -0.140  t=-4.36  icir=-1.778  n=6   -0.069  t=-0.89  icir=-0.365  n=6   -0.115  t=-2.01  icir=-0.822  n=6   -0.064  t=-0.69  icir=-0.309  n=5
  20d                   -0.254  t=-4.23  icir=-1.726  n=6   -0.079  t=-0.76  icir=-0.312  n=6   -0.190  t=-2.04  icir=-0.831  n=6   -0.023  t=-0.35  icir=-0.158  n=5
  120d-skip-21          +0.067  t=+0.80  icir=+0.326  n=6   +0.040  t=+0.62  icir=+0.252  n=6   +0.059  t=+0.57  icir=+0.235  n=6   -0.043  t=-0.56  icir=-0.250  n=5

-- consolidation (sample_dates=28) --
  60d (現行)             +0.011  t=+0.43  icir=+0.081  n=28   +0.025  t=+1.09  icir=+0.205  n=28   +0.016  t=+0.67  icir=+0.126  n=28   +0.009  t=+0.44  icir=+0.084  n=28
  20d                   -0.005  t=-0.21  icir=-0.040  n=28   -0.025  t=-1.10  icir=-0.207  n=28   -0.026  t=-1.15  icir=-0.218  n=28   -0.005  t=-0.29  icir=-0.055  n=28
  120d-skip-21          -0.004  t=-0.16  icir=-0.031  n=28   +0.014  t=+0.55  icir=+0.104  n=28   +0.008  t=+0.35  icir=+0.066  n=28   -0.004  t=-0.19  icir=-0.036  n=28
```

**這一步把上面「h=60 顯著轉正（t=+2.94, n=163）」的結論打了折扣，必須誠實記錄**：

- 三桶合計只有 24+6+28=58 個 rebalance 日有明確盤勢分類，遠少於全樣本的 173 個（同 `regime_strategy_research.md` 已知的限制——TAIEX 分類需要足夠歷史 K 棒，樣本前段與過渡期不可分類）。換句話說，pooled 版本的 h=60 顯著正值，**有約三分之二的樣本來自「未分類」區間**，不能簡單套用「三種盤勢都改善」的敘述。
- 在**已分類**的三桶裡，120d-skip-21 沒有一桶在 h=60 顯著為正（bull t=+1.00、bear t=-0.56、consolidation t=-0.19，全不顯著）。bull 桶在 h=5/h=20 甚至還是負的（h=5 t=-2.40，比 60d 的 t=-2.68 略好但仍顯著負）。
- 唯一乾淨的正面訊號在 **bear 桶的短 horizon**：h=1 從 60d 的 t=-4.36（強烈顯著負）變成 120d-skip-21 的 t=+0.80（不顯著，但至少不再是強烈負訊號），20d 窗口在 bear 桶則是三者最差（h=1 t=-4.23）。但 bear 桶 n=6，`regime_strategy_research.md` 已經把空頭樣本標記為「不可用」等級的小樣本，這裡的 t/icir 數字本身也不該當作定論。

**結論修正**：§3.1 原本的「pooled h=60 顯著正值」結論**成立但範圍有限**——它主要反映的是尚未分類的樣本期間（多半是早期歷史），而非「在每種已知盤勢下都成立」的訊號。這不否定 120d-skip-21 優於 60d／20d 的相對排序（三桶橫向比較，120d-skip-21 多數情況下不比另外兩者差，bear 桶短 horizon 明顯較好），但**不足以支撐「motion window 換成 120d-skip-21 就能解決三盤勢動能訊號皆負」這個更強的宣稱**。要拿到更乾淨的答案，需要等 `cache/factor_run` 累積更多空頭月份樣本（`docs/factor_timeframe_optimization_directions.md` §3.4 已指出這是「等時間、不是等工程」），現在的 n=6 不足以下定論。

## §2.1 追加：value/pbr 候選因子的正式 walk-forward + DSR/PBO 檢驗

跑法：新增 `scripts/report_value_walkforward.py`。不重用 `portfolio_backtest.simulate()`（其 `extra_factors` 掛勾是 `fn(bars, idx)`，適合 ma_cross/vol_price_div 這種純 K 棒因子，但 value/pbr 要按日期查 `value_series`，硬塞會變成改動 production pipeline 去測一個還沒決定要不要採用的候選——不在本次範圍），改為沿用 §2.1 pooled 測試同一套 rank-IC 方法，但改成套 `src/optimize.py` 既有的 `purged_calendar_walk_forward_splits()`（purge_days=60，anchored）切出 out-of-sample 折，每折算 `composite_scores` 對 h=60 forward return 的 mean rank-IC，再丟進 `metrics.pbo()`/`deflated_sharpe_ratio()`——跟 `sweep_ranking_weights_walk_forward()`/`sweep_param_walk_forward()` 用來檢驗 RANKING_WEIGHTS 候選、strategy-logic-search 候選的同一套統計門檻（`PBO_MAX=0.5`、`DSR_MIN_SIGNIFICANT=0.95`）。

比較的三組權重：現行 `RANKING_WEIGHTS`（無 value）／加入 value 15%（等比例稀釋 revenue/roe/momentum）／加入 value 30%。

**重要限制先講**：`value_series`（`get_per_pbr()`）在快取裡最早只回溯到 **2021-10-04**（vs. revenue/roe/momentum 從 2012 年就有資料），所以 `--folds 4`（沿用前面§3.1 用的粗折）時，2015~2020 年那兩折的 value 因子樣本數是 0，只剩 2 折可用；改用 `--folds 10` 把折切細，2021 年以前的 6 折仍是 0，但 2021 之後能切出 4 折可用。以下用 `--folds 10` 的結果（可用折數較多，統計power較好）：

```
fold 6: test=2021-06-07..2022-09-20 (11 檔)   baseline ic=-0.0140   value15%=+0.0289   value30%=+0.0893
fold 7: test=2022-09-21..2024-01-11 (16 檔)   baseline ic=+0.0408   value15%=+0.0523   value30%=+0.0648
fold 8: test=2024-01-12..2025-05-15 (16 檔)   baseline ic=+0.0413   value15%=+0.0576   value30%=+0.0726
fold 9: test=2025-05-16..2026-09-14 (13 檔)   baseline ic=+0.0623   value15%=+0.0899   value30%=+0.1165

usable_folds=4
OOS mean IC: baseline=0.0326／value15%=0.0572／value30%=0.0858
per-fold winner: value30% 贏 4/4 折（100%）
PBO=1.00（over 4 blocks）
DSR（winner=value30%, n_trials=30, n_obs=4）=0.82
```

`--folds 4`（粗折，only 2 usable）：OOS mean IC baseline=0.032／value15%=0.056／value30%=0.083，value30% 2/2 折全勝，PBO=0.0，DSR=0.71。

**結論：兩種折法都未通過 `src/optimize.py` 既有的 overfitting 門檻**：

- `--folds 10`：PBO=1.00 > 0.5（REJECT），DSR=0.82 < 0.95（REJECT）——雖然 value30% 候選在每一折的 raw mean-IC 都贏，但 CSCV（PBO）用 in-sample Sharpe 選出的「贏家」在 out-of-sample half 排名不穩定，這正是折數太少（只有 4 折，每折只有 1 個資料點餵進 Sharpe 計算）時 CSCV 本身就預期會出現的雜訊，不是候選真的變糟。
- `--folds 4`：PBO=0.0（可用折數只有 2，此時 C(4,2)=6 組合的統計力更弱，PBO 幾乎沒有鑑別力），DSR=0.71 仍未達 0.95 門檻。
- 兩種折法一致的地方：**DSR 都不夠**——不管怎麼切，只有 2~4 個可用折，`deflated_sharpe_ratio()` 對折數少會給很重的懲罰（分母是 `n_obs-1`），這不是「候選因子沒訊號」，而是「樣本折數不夠多，沒辦法用這套統計工具證明它不是運氣」。
- 跟 §2.1 pooled 版本（h=60 t=+3.88 顯著）不矛盾，但更保守：pooled test 用了全部 56~59 個 rebalance 日一次性算一個 IC 序列的 t 檢定；這裡改成分折驗證「排除掉最容易overfit的 in-sample 挑選過程」後還站不站得住——OOS 上 value30% 確實每折都贏（4/4），方向一致，但折數太少（受限於 value_series 只有 ~5 年歷史）讓 DSR/PBO 這兩道「防止自欺」的門檻沒辦法給出「不是運氣」的正式背書。

**治理判斷**：**現階段不建議正式提案把 value/pbr 排進 `RANKING_WEIGHTS`**——不是因為訊號方向錯（OOS 每折都贏，跟 pooled 結論同向），而是樣本折數不足以讓 DSR/PBO 這兩道既有門檻通過，貿然採用等於繞過了這個專案自己訂的 overfitting 防線。比較合理的下一步是**等 `value_series` 歷史累積更長**（目前約 5 年，再等 1~2 年可以切出 6~8 個可用折）再重跑同一支 `report_value_walkforward.py`，而不是現在就用 pooled IC 一個數字去核准。

## §2.3：應計盈餘品質（accrual_quality）候選因子

跑法：`docs/institutional_and_accrual_factor_plan.md` §2.3 串接完成（`data_sources.get_financials()` 擴充 `total_assets`/`operating_cash_flow`，`finlab_factors.accrual_quality_series()`，全市場 1821 檔重抓，`accrual_quality_series` 非空 983 檔），跑 `python scripts/report_factor_ic.py`：

```
accrual_quality accrual_ratio (候選)   -0.029  t= -1.72  icir=-0.239  n=52   -0.036  t= -2.92  icir=-0.405  n=52   -0.034  t= -3.83  icir=-0.536  n=51   -0.043  t= -4.78  icir=-0.683  n=49
```

（h=1/h=5/h=20/h=60 依序對應上面四組數字，n 是可用 rebalance 日數——`accrual_quality_series` 覆蓋期最早只到 2022-05-30，比 value/pbr 的 2021-10-04 還晚，所以 n 比其他候選略小。）

**pooled 結果是這一輪所有候選裡訊號最強的**：h=5/h=20/h=60 都顯著（|t|>2），h=60 的 t=-4.78、icir=-0.683 甚至強於 value/pbr 的 t=+3.88。**方向是負**——`accrual_ratio`（本季稅後淨利減營運現金流、除以總資產）越高，未來報酬越差，跟 Sloan (1996) accrual anomaly 的經典方向一致（應計比重越高＝財報數字越靠會計估計撐出來、現金流品質越差）。`accrual_quality_series()` 的實作刻意不先翻轉符號（見函式 docstring），這裡實測出負號，之後若要排進 `composite_scores()`，排名分數要用 `-accrual_ratio`（值越低越好，同 `value_series()` 對 `-pbr` 的既有處理方式）。

### §2.3 追加：accrual 候選的正式 walk-forward + DSR/PBO 檢驗

跑法：新增 `scripts/report_accrual_walkforward.py`（結構同 `report_value_walkforward.py`，`--folds 10`/`--folds 4` 都跑）：

```
--folds 10（usable_folds=4）
  OOS mean IC: baseline=0.0405／accrual15%=0.0506／accrual30%=0.0497
  per-fold winner: accrual30% 贏 3/4 折（75%）
  PBO=0.50（等於門檻，未超過，不算 REJECT 那一項）
  DSR（winner=accrual30%, n_trials=30, n_obs=4）=0.9477

--folds 4（usable_folds=2）
  OOS mean IC: baseline=0.0478／accrual15%=0.0581／accrual30%=0.0590
  per-fold winner：accrual30%/accrual15% 各贏 1/2 折（50%）
  PBO=0.0
  DSR（winner=accrual30%, n_trials=12, n_obs=2）=0.8151
```

**結論：兩種折法都未通過 DSR 門檻（0.95），`--folds 10` 只差一點點（0.9477 vs 0.95）**：

- `--folds 10`：PBO=0.50 剛好卡在門檻上（`PBO_MAX=0.5`，判定式是 `>` 不是 `≥`，所以這項沒有觸發 REJECT），但 DSR=0.9477 差 0.0023 沒過——是這幾輪候選因子檢驗裡最接近通過的一次。
- `--folds 4`：可用折數只剩 2，DSR 掉到 0.82，跟 value/pbr 那組（可用折數同樣受限於候選因子歷史較短）呈現同樣的模式。
- 跟 §2.1 的 value/pbr 遇到同一個結構性限制：`accrual_quality_series` 依賴的 `TaiwanStockCashFlowsStatement` 覆蓋期只有約 4 年（2022-05-30 起），折數切不細，`deflated_sharpe_ratio()` 對折數少的懲罰很重——這不是「候選因子沒訊號」（pooled IC 是這輪最強的、OOS 每折方向也一致偏正），而是「樣本折數不夠，沒辦法用這套統計工具正式排除運氣」。

**治理判斷**：**現階段不建議正式提案把 accrual_quality 排進 `RANKING_WEIGHTS`**——理由與 value/pbr 相同（訊號方向站得住腳，但 DSR 差一點沒過），但這是四個已測候選因子裡**最接近通過門檻**的一個，值得標記為「資料累積後優先重跑」的第一候選。合理的下一步是等 `TaiwanStockCashFlowsStatement` 歷史再累積（多幾季就可能多切出 1-2 個可用折，讓 DSR 越過 0.95），到時候直接重跑 `report_accrual_walkforward.py`，不需要重新設計。

## 下一步（未在本次執行範圍內，留給後續）

1. ~~§2.2（法人買賣超）~~ 已完成，見 `docs/institutional_and_accrual_factor_plan.md`——`inst_net_5d` 各 horizon 都不顯著，不採用。
2. ~~§2.3（應計盈餘品質）~~ 已完成，見上一節——pooled IC 顯著且是這輪最強，但正式 walk-forward 的 DSR 差一點沒過（0.9477 vs 0.95 門檻），暫不提案採用，標記為資料累積後優先重跑對象。
3. ~~120d-skip-21 動能窗口的三盤勢分桶重跑~~ 已完成，見上一節——結論比預期保守，不建議僅憑本報告就正式提案更換 `MOMENTUM_DAYS`。
4. ~~value/pbr 因子的正式 walk-forward + DSR/PBO 檢驗~~ 已完成，見上一節——OOS 方向一致但可用折數太少（value_series 僅約 5 年歷史），DSR/PBO 門檻未通過，建議等資料累積更長再重跑，暫不提案正式採用。
5. 若要對「未分類樣本」（占 173 個 rebalance 日中的多數）做進一步歸因，可以另開一份報告單獨看這批日期落在哪個時間區間、是否集中在 2012-2015 年 TAIEX 歷史不足分類的早期樣本——不在本次範圍內。
6. `docs/factor_timeframe_optimization_directions.md` §2.4（董監質押比率）：資料可行性尚未查證，見該文件與本次新增的 `docs/pledge_ratio_feasibility.md`。
