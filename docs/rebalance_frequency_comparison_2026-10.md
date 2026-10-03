# 換倉頻率對照：月頻 / 雙月頻 / 季頻

2026-10-01，`scripts/run_rebalance_comparison.py`。這是**評估報告，不是採用決策**：沒有任何東西被套用，也沒有改現行月頻。起點是 `docs/factor_candidates_momentum_ic_2026-09.md` §3 的觀察——現行三因子綜合分數在 h=60 的 ICIR（+0.31）高於 h=20（+0.14），暗示持有期拉長可能不輸月頻、還能降換手成本。

## 設計（跑之前就固定，腳本檔頭有同一份文字）

- 3 個候選：月頻（每 1 個月）、雙月頻（每 2 個月）、季頻（每 3 個月）；**權重相同**，只有 `portfolio_backtest` 的 `rebalance_every_months` 不同。不試其他間隔。
- 月份格以**日曆年**對齊：雙月 = 1/3/5/7/9/11 月，季頻 = 1/4/7/10 月，換倉日取當月第一個交易日；不隨 walk-forward 切片起點移動。
- 資料：`train_test_split_by_calendar()` 的 train 段（2012-05-02 ~ 2026-01-13，1821 檔）；最後 6 個月（2026-03-16 起）保留為 held-out，**這次沒有碰**。4 個 purged anchored fold，與 `run_strategy_search.py` 同一套引擎；成本用引擎內建（來回 0.6% + 單邊滑價 0.1%）。
- 權重：程式預設 `RANKING_WEIGHTS`（revenue .4 / roe .3 / momentum .3）。雲端的 `active_ranking_weights` 於 2026-10-02 核實為「從未被寫入，用的就是這組預設」（推論而非直接讀值：該 key 只有 `optimize.yml` 採用/rollback 與 `apply_strategy_candidate.yml` 會寫；`optimize.yml` 的三次執行〔9/2、9/16、10/2〕都因雲端 `cache/factor_run` 還原失敗、載入 0 檔，在寫入前就結束；`apply_strategy_candidate.yml` 與 `apply_regime_weights_candidate.yml` 從未被執行過），所以基準成立。
- 事先寫死的採用規則：非月頻的 X 要同時滿足 ①X 是 point_best（平均樣本外報酬最高）②X 的平均高於月頻 ③X 也是 per-fold 勝出的多數 ④`evaluate_decision()` 通過（DSR ≥ 0.95、PBO ≤ 0.5，以及 plateau 門檻：多數勝者的 fold 勝出比例 ≥ max(20%, 1/候選數)）。否則維持月頻。
- 診斷（**不進規則**）：`--offset N` 把月份格位移 N 個月，看結果對起始月份有多敏感。

## 結果

主要跑（offset 0）各 fold 樣本外報酬 %：

| fold | 月頻 | 雙月頻 | 季頻 |
|---|---|---|---|
| 0 | +64.4 | +62.7 | +74.1 |
| 1 | +24.6 | +62.5 | +42.5 |
| 2 | +60.0 | +60.5 | +47.2 |
| 3 | +137.9 | +141.3 | +157.3 |
| 平均 | +71.7 | +81.8 | +80.3 |

- point_best = 雙月頻；PBO 0.33；DSR 0.968；引擎 `decision.accept = True`。
- per-fold 勝出：季頻、雙月頻、雙月頻、季頻（各 2 次，打平）。引擎在打平時取先出現者（季頻），所以規則的第③條不成立 → **規則判定：維持月頻**。這是一個靠第③條才擋下來的邊緣結果，不是明顯的否決；但我不事後改規則。
- DSR 是引擎對 point_best 算的（`optimize.py` 的 `sweep_ranking_weights_walk_forward()` 呼叫 `_pbo_and_dsr(..., point_best_name, ...)`；該函式 docstring 寫成「被選中的候選」，與實際不一致，以實際程式為準），不是「想採用的那一組」自己的。這個說法同時記在 `docs/factor_candidates_momentum_ic_2026-09.md` §4。
- **【2026-10-04 更新】** 以上 DSR 是**改動前**的算法。此後閘門 DSR 改為對多數勝者計算（結果有 `dsr_of` 欄位，見 `docs/plan_dsr_target_plateau_winner_2026-10.md`）。用上表四捨五入後的逐 fold 分數反算：舊算法對雙月頻得 0.968（與上面一致，PBO 0.33），新算法對多數勝者（季頻）約得 0.947，略低於 0.95。因為輸入已四捨五入，這個 0.947 只是近似，要確切值得重跑 `scripts/run_rebalance_comparison.py`（**未重跑**）。**規則判定不受影響**：採用規則要求 X 同時是 point_best 與多數勝者，這個比賽兩者不同（雙月頻 vs 季頻），第①／③條不成立，仍是維持月頻。
- 雙月頻的優勢集中在單一 fold：fold 1 月頻只有 +24.6、雙月頻 +62.5。雙月頻對月頻的平均差約 10.1 個百分點，fold 1 一個就貢獻約 9.5（+37.9 / 4）；拿掉 fold 1，雙月頻平均 88.2 對月頻 87.4（幾乎一樣）。
- 季頻不是這個形態：拿掉 fold 1 仍領先（92.9 對 87.4），fold 0、fold 3 各領先 9.7、19.4，但 fold 2 落後 12.8，且 fold 1 同樣有 +17.9。季頻領先在 4 個 fold 中有 3 個，變動很大。

相位診斷（不進規則；各組都是同一批資料上多看一眼，不是新樣本）：

| offset | 月頻平均 | 雙月頻平均 | 季頻平均 | 引擎決策 |
|---|---|---|---|---|
| 0（主要跑） | +71.7 | +81.8 | +80.3 | accept |
| 1 | +71.7 | +73.0 | +72.9 | 否決：DSR 0.90 < 0.95 |
| 2 | +71.7 | +81.8（與 offset 0 同一格，雙月的 offset 2 ≡ 0） | +75.1 | 否決：PBO 0.67 > 0.5 |

- 雙月頻的平均對相位敏感：+81.8（1/3/5… 月）對 +73.0（2/4/6… 月）。季頻三個相位平均 80.3 / 72.9 / 75.1，都不低於月頻，但各 fold 差異很大（例如 offset 1 的季頻 fold 2 只有 +7.6、fold 3 卻有 +212.6）。
- 全 train 的 CAGR 也不單調：月頻 16.4%；雙月頻 19.4%／15.1%；季頻 17.0%／14.4%／18.2%（相位 0／1／2）。

換手與成本（引擎已計入報酬，這裡只是描述；offset 0）：

| | 交易筆數 | 估計成本拖累（每年） |
|---|---|---|
| 月頻 | 4157 | 約 4.7% |
| 雙月頻 | 2278 | 約 2.9% |
| 季頻 | 1633 | 約 2.3% |

## 結論

- **現行月頻維持不變。** 依事先規則為維持。相位診斷下，雙月頻與季頻在三個相位的平均報酬都沒有低於月頻（最小差距 offset 1 只有 +1.2 ~ +1.3），但差距小、不穩定，引擎在 offset 1、2 都否決。
- 能說的是「不輸月頻」的一面：成本降低是結構性的（換手隨間隔拉長而降，各相位一致），報酬沒有因此變差。不能說的是「更好」：雙月頻的優勢主要來自單一 fold（fold 1）且隨相位大幅變動（+81.8 對 +73.0）；季頻的領先不依賴單一 fold，但逐 fold 起伏很大、隨相位變動（80.3／72.9／75.1）；4 個 fold 下檢定力低。
- 若有人想繼續：這份結果不構成改變換倉頻率的理由；真要推進，應先決定怎麼處理相位依賴（例如把多個相位當同一個候選的重複，而不是各算一次試驗），再用 held-out 的 2026-03 起區段做一次性檢查——那一步這次**沒做**，需要另外點頭。

## 限制與沒做的事

- 只有 4 個 fold；PBR／skip-21 那一輪的限制同樣適用（未含息收盤價、未做產業中立、生存者偏差）。
- 順帶發現（與本結論無關，未處理）：雲端 `optimize.yml` 目前是空跑，因為 `update-universe-bars-state-*` 快取不存在、`cache/factor_run` 載入 0 檔；這代表雲端排程的權重最佳化實際上從未運作過。
- 沒有做：held-out 區段檢查、不同權重下的對照、換倉間隔以外的執行參數（top_n 等）。
- 沒有接進任何排程或 `apply_*` 流程。引擎新增的是 `rebalance_every_months` / `rebalance_offset_months` 兩個參數（預設 1 / 0，行為不變）與 `sweep_ranking_weights_walk_forward(candidate_params=...)`。

## 重現

```
python scripts/run_rebalance_comparison.py            # 主要跑
python scripts/run_rebalance_comparison.py --offset 1 # 診斷
python scripts/run_rebalance_comparison.py --offset 2
```
需要 `cache/factor_run/`（gitignored，要在有這份快取的 checkout 跑）。可用 `--weights '{"revenue":..., ...}'` 換成雲端實際權重。
