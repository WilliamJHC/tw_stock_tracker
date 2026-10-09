# rebalance 頻率對照實驗報告（§3.3）

2026-09-24。回應 `docs/factor_timeframe_optimization_directions.md` §3.3：`portfolio_backtest.py` 的月頻 rebalance 是沿用 FinLab 原文章慣例，這個專案自己從沒驗證過是不是最好的頻率。這份報告用專案既有的 purged walk-forward + DSR/PBO 門檔，對照月頻（現況）／雙週頻／週頻三種 rebalance 頻率。

## 串接

`src/portfolio_backtest.py` 的 `_rebalance_dates(calendar)` 原本寫死「每個自然月的第一個交易日」。擴充為 `_rebalance_dates(calendar, freq="monthly")`，新增 `freq="weekly"`（ISO 曆週第一個交易日）與 `freq="biweekly"`（每兩個 ISO 曆週一組，用 `week // 2` 分組，是行事曆的確定性函式，不是滾動固定天數的窗口，才不會因為假日調整而位移）。`simulate()` 讀 `params.get("rebalance_freq", "monthly")`，預設值不變，既有呼叫端行為完全不受影響。`PORTFOLIO_PARAMS` 新增 `"rebalance_freq": "monthly"` 這個 key 供文件化，同樣是預設值、不影響現況。單元測試見 `tests/test_portfolio_backtest.py`（`_rebalance_dates` 的三種 freq 各自的分組行為 + `simulate()` 接受這個新 param 不出錯）。

新增 `scripts/report_rebalance_freq_walkforward.py`（結構比照 `optimize.sweep_ranking_weights_walk_forward()`，但換成對 `params["rebalance_freq"]` 這個候選維度做同樣的 purged walk-forward + DSR/PBO 檢驗，理由見腳本 docstring——沒有把 `optimize.py` 既有的 sweep 函式改成通用版，因為那個函式是 `run_ranking_optimize.py`／`run_strategy_search.py` 兩條正式治理管線在用的正式基礎設施，為了一個未採用的候選維度加大它的介面範圍，跟 `report_value_walkforward.py` docstring 已經講過的「不為了一個實驗做 production pipeline 手術」是同一個理由）。

## 結果（`--folds 4`，anchored，purge_days=60）

```
fold 0: test=2015-03-06..2018-01-09   monthly=71.61   biweekly=87.25   weekly=61.54
fold 1: test=2018-01-10..2020-11-26   monthly=17.76   biweekly=27.11   weekly=23.40
fold 2: test=2020-11-27..2023-10-19   monthly=76.80   biweekly=68.69   weekly=87.46
fold 3: test=2023-10-20..2026-09-14   monthly=264.78  biweekly=194.58  weekly=196.96

OOS mean return%（4 折平均）：monthly=107.74   biweekly=94.41   weekly=92.34
per-fold winner counts: {biweekly: 2, weekly: 1, monthly: 1}（無穩定 plateau，50% 換手）
PBO=0.17（門檻 0.5，通過）
DSR（winner=monthly，n_trials=12, n_obs=4）=0.9459（門檻 0.95，差 0.0041 沒過）
```

`--folds 2`（粗略對照，順手記錄）：月頻在兩折都贏（fold0: 165.97 vs 165.19/145.94；fold1: 270.81 vs 216.82/256.02），DSR=0.75——折數越粗，月頻優勢看起來越乾淨，但可用折數太少同樣讓 DSR 過不了門檻，跟 `--folds 4` 的結論方向一致（月頻不輸），只是統計力都不夠正式核准。

## 判斷

**現階段不建議改變 rebalance 頻率**：

- 月頻（現況）在 4 折平均報酬上是三者最高的（107.7% vs 94.4%/92.3%），符合直覺——rebalance 越頻繁，`fee_pct`/`slippage_pct` 交易成本拖累越大，這個成本在等權重、每次都要對齊 `target_value_each` 的換手機制下會隨頻率近乎線性疊加。
- 但 per-fold 沒有穩定贏家（4 折裡月頻只贏 1 折），DSR 差一點點沒過 0.95 門檻——這跟 §2.3（accrual_quality）、§2.1（value/pbr）遇到的同一個結構性限制一樣：**折數不夠多，這套統計工具沒辦法正式排除「均值差異是運氣」**，不是「月頻沒有優勢」的證據。
- **不需要等資料累積**（跟 value/pbr、accrual_quality 不同，這裡沒有歷史長度限制——rebalance 頻率可以用全部 173 個 rebalance 日重跑，folds 數受限於 `purged_calendar_walk_forward_splits()` 的折法本身，不是資料覆蓋率）。若要拿到更多折數的統計力，可以直接調高 `--folds`（例如 6-8 折）重跑，但折數越多、每折測試窗越短，OOS 樣本雜訊也會上升，這是一般 walk-forward 折數選擇的固有 trade-off，不是這個實驗特有的限制。
- 結論方向支持現況（月頻），沒有證據建議改成週頻或雙週頻——**維持月頻**，不提案變更 `PORTFOLIO_PARAMS["rebalance_freq"]` 的預設值。

## 重現方式

```
python scripts/report_rebalance_freq_walkforward.py --folds 4
python scripts/report_rebalance_freq_walkforward.py --folds 2   # 粗略對照
```
