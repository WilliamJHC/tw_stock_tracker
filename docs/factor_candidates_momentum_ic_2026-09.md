# 候選因子與動能視窗 IC 評估（全市場快取重跑 + PBR walk-forward）

2026-09-29，`scripts/report_factor_candidates_and_momentum.py` 用完整快取實跑，再對 PBR 走 ADR 0003 的 `run_strategy_search.py --pbr` walk-forward + DSR/PBO。這是 `docs/factor_timeframe_optimization_directions.md` §2.1（Phase 6b 候選因子）與 §3.1（動能 formation window）兩個「立即」項目的執行結果。**評估報告，不是採用決策**——沒有任何一項在這裡被採用。

> 修訂紀錄：本文初版用的是 209 檔系統抽樣，結論是「PBR 值得追蹤、可接進 ADR 0003」。完整快取（1821 檔）重跑後 PBR 單因子 IC 變顯著，但進 walk-forward 的組合層檢定**沒過**（§4）。§1-3 已改用完整重跑數字，初版的抽樣數字不再引用。

## 樣本與限制（先看這段）

- **樣本**：`cache/factor_run/` 扣掉 `skip:true` 後 **1821 檔**、173 個月度再平衡日（2012-05 ~ 2026-09）；「2021-12 起」子樣本 58 個再平衡日。純讀快取、不打網路（`cache/` 被 gitignore，重現要在有這份快取的 checkout 跑）。
- **候選因子資料覆蓋只有 2021-10 起**（PBR，1821 檔中 1613 檔從 2021-10 起）／毛利率、營益率趨勢同樣只有近幾年（各 57 / 55 個再平衡日）：n≈55-58 個再平衡日，且 h=60 的報酬視窗與月頻再平衡**重疊約 3 倍，t 值被高估**（自相關），h=60 的 t 只能當方向參考。
- 多重比較：7 種動能視窗 × 2 個 horizon × 2 個區間，`|t|≈2` 附近的結果不應單獨當證據。
- 報酬是未含息的收盤價報酬；未做產業中立；生存者偏差同 `regime_strategy_research.md`／`regime_switch_vs_0050.md` 已知限制。

## 1. Phase 6b 候選因子（rank-IC，橫截面 Spearman，月頻）

| 因子 | h=20 | h=60 | n（再平衡日） | 判讀 |
|---|---|---|---|---|
| 毛利率年增趨勢 | IC +0.017, t +1.73 | IC +0.018, t +1.56 | 57 / 55 | 無顯著訊號 |
| 營益率年增趨勢 | IC +0.014, t +1.31 | IC +0.004, t +0.38 | 57 / 55 | 無顯著訊號 |
| 價值（−PBR） | IC +0.044, t +2.40, ICIR +0.32 | IC +0.068, t +3.88, ICIR +0.52 | 58 / 56 | **單因子 IC 顯著**；但組合層 walk-forward 未通過（§4） |

同期（2021-12 起）基準對照：roe h=20 ICIR +0.27、h=60 +0.26；revenue h=60 ICIR +0.27；momentum h=20 ICIR −0.08。

**結論**：兩個 margin trend 依 lighthouse Phase 6b 既定規則（t-stat 未達顯著即排除）**維持排除**。PBR 的單因子 IC 是三個候選裡唯一顯著的（h=60 的 t 受重疊高估，方向參考），但單因子 IC 顯著**不等於**混進 top-N 選股組合會賺得更多，見 §4。

## 2. 動能 formation window（各視窗單因子 rank-IC）

| 視窗 | h=20（全期） | h=60（全期） |
|---|---|---|
| mom5 | −0.007 (t −0.91) | −0.001 (t −0.17) |
| mom20 | **−0.030 (t −3.06)** | +0.002 (t +0.21) |
| mom60（現行 `MOMENTUM_DAYS`） | −0.013 (t −1.24) | +0.016 (t +1.84) |
| mom120 | −0.006 (t −0.59) | +0.018 (t +1.89) |
| mom60 跳過最近 21 根 | +0.017 (t +1.76) | **+0.024 (t +2.98)** |
| mom120 跳過最近 21 根 | +0.012 (t +1.17) | **+0.028 (t +2.94)** |
| mom231 跳過 21（≈12-1） | +0.003 (t +0.27) | −0.013 (t −1.35) |

**觀察**：
1. 純 20 日動能在 h=20 顯著為負（t −3.06），是明確的短期反轉訊號；現行 60 日動能在 h=20 也為負（t −1.24），與 `regime_strategy_research.md`「三種盤勢 ICIR 皆負」方向一致。
2. 把最近 21 根排除後，h=20 的符號從負翻正，h=60 的 t 升到約 3，支持「現行視窗被最近一個月的短期反轉污染」這個假說（初版抽樣的結論方向相同，但只有 120 跳 21 顯著、且 t 值較高）。
3. 60 跳 21 與 120 跳 21 在 h=60 幾乎一樣好（t +2.98 vs +2.94）；初版抽樣裡 120 跳 21 明顯領先的說法**在完整樣本不成立**。傳統 12-1（231 根）依然不成立（h=60 −0.013）。
4. **2021-12 起的子樣本這個優勢不再顯著**：mom60_skip21 h=60 只剩 +0.013（t +0.73），mom120_skip21 h=60 +0.007（t +0.38）；h=20 的 mom60_skip21 仍為正（+0.017，t +0.81）但不顯著，mom20 仍為負（−0.031，t −1.68）。符號大多還在、顯著性沒了；子樣本 n=55-57 檢定力低，不能斷定是效應消失還是樣本太短。

## 3. 綜合分數 rank-IC（權重僅為示意，非提案）

| 設定 | h=20（全期） | h=60（全期） | h=60（2021-12 起） |
|---|---|---|---|
| 現行 rev .4 / roe .3 / mom60 .3 | +0.016 (ICIR +0.14) | +0.033 (ICIR +0.31) | +0.037 (ICIR +0.35) |
| 動能換成 120 跳 21 | +0.028 (ICIR +0.25) | +0.037 (ICIR +0.32) | +0.032 (ICIR +0.30) |
| 動能換成 60 跳 21 | +0.031 (ICIR +0.29) | +0.036 (ICIR +0.34) | +0.036 (ICIR +0.34) |
| 拿掉動能 rev .55 / roe .45 | +0.030 (ICIR +0.30) | +0.034 (ICIR +0.32) | +0.038 (ICIR +0.40) |
| 現行 + PBR .2 | — | — | +0.068 (ICIR +0.68) |
| 動能換 120 跳 21 + PBR .2 | — | — | +0.062 (ICIR +0.60) |

（PBR 資料 2021-10 才開始，全期欄位不適用。）

**觀察**：
- 換動能視窗對 h=60 全期綜合 IC 的增益很小（ICIR +0.31 → +0.32 ~ +0.34），h=20 的改善較明顯（+0.14 → +0.25 ~ +0.29）；近期子樣本反而略差。
- 加 PBR 的近期 IC 最高（h=60 ICIR +0.68），但 n=55、重疊、且是六組裡挑出來的，**有事後挑選風險**——而且 §4 的組合層檢定沒有支持它。

## 4. PBR 的 ADR 0003 walk-forward（`run_strategy_search.py --pbr --dry-run`）

設定：baseline（`RANKING_WEIGHTS` rev .4 / roe .3 / mom .3）對 with_pbr（rev .3 / roe .2 / mom .2 / value_pbr .3，權重比照既有 `with_ma_cross`，是首猜不是搜尋出來的）。日曆限縮到 PBR 有資料的 2021-10-04 起（`value_pbr` 在此之前對每檔都是 None，會清空可選宇宙；推測這些 fold 各候選會同分，而 `sweep_ranking_weights_walk_forward()` 的 per-fold argmax 是嚴格大於、先列者勝，同分時會記成 baseline 勝——這一段未實跑驗證，是限縮日曆的理由，不是實測結果）。train=2021-10-04..2026-01-13，held-out test=2026-03-16..2026-09-14（未使用，因為 train 階段就沒過）。4 個 purged anchored fold，各 fold 樣本外報酬：

| fold（test 區段） | baseline | with_pbr |
|---|---|---|
| 1（2022-08 ~ 2023-06） | +47.3% | +10.3% |
| 2（2023-06 ~ 2024-04） | +49.5% | +46.9% |
| 3（2024-05 ~ 2025-03） | −8.9% | −7.6% |
| 4（2025-03 ~ 2026-01） | +55.1% | +4.4% |
| 平均 | **+35.8%** | +13.5% |

- with_pbr 只贏 fold 3（差 1.2 個百分點），其餘 3 個 fold 都輸；per-fold argmax 為 baseline 3 次、with_pbr 1 次（plateau 0.75）。
- PBO 0.33；閘門的 **DSR 0.91 < 0.95，未過**（`decision.accept=False`），不產生待審候選、不發 Telegram。**這個 DSR 不是 with_pbr 的**：`sweep_ranking_weights_walk_forward()` 是對 `point_best`（平均樣本外報酬最高者）算 DSR（`optimize.py` 的 `_pbo_and_dsr(..., point_best_name, ...)`），這次 point_best 是 baseline，所以 0.91 是 baseline 的、扣掉 n_trials 後的顯著性；with_pbr 自己的 DSR 沒有被計算。不採用的依據是：平均報酬（+13.5% 對 +35.8%）與逐 fold 紀錄（只贏 1 個 fold）都不如 baseline，閘門數字只是被擋下的機制。（註：該函式 docstring 寫成「被選中的候選」，與實際傳入 `point_best_name` 不一致，這裡以實際程式為準。）
- 兩個候選的平均樣本外報酬都低於母體買進持有的 +55.8%。
- 單因子 IC 顯著卻在組合層變差，原因**沒有驗證**。可能的解釋（純猜測）：價值股在這段期間逆風、top-20 的集中持股放大差異、0.3 的權重動到了原本有效的營收/ROE 訊號。
- **這個否定結論的檢定力不高**：只有 4 個 fold、train 區間約 4.3 年（樣本外 fold 合計 2022-08 ~ 2026-01，約 3.4 年）、n_trials=8；換權重（例如 0.1-0.15）可能得到不同結果，但每多試一組就要再付 DSR 的試驗次數懲罰，不應用同一批資料調到過為止。

重現：`python scripts/run_strategy_search.py --pbr --dry-run`（`--dry-run` 不寫 meta、不發 Telegram）。註：baseline 用程式預設 `RANKING_WEIGHTS`。本機 `src/data/backtest.db`（2026-09-21 最後寫入）的 meta 從未寫過 `active_ranking_weights`，所以本機一律 fallback 到預設；雲端 GitHub Actions cache 裡那份的實際值在本機取不到。**2026-10-02 已核實為預設 0.4/0.3/0.3（推論而非直接讀值）**：該 key 只有 `optimize.yml`（採用／rollback）與 `apply_strategy_candidate.yml` 會寫；`optimize.yml` 的三次執行（9/2、9/16、10/2）都因雲端 `cache/factor_run` 還原失敗、載入 0 檔，在寫入前就結束；`apply_strategy_candidate.yml` 與 `apply_regime_weights_candidate.yml` 從未被執行過。所以 baseline 用 0.4/0.3/0.3 成立，本節各 walk-forward 的比較基準沒有偏差。（順帶發現、未處理：雲端 `optimize.yml` 目前空跑，見該日的 Actions 紀錄。）

## 5. 後續兩輪 walk-forward（2026-09-30，同一支 `run_strategy_search.py`，皆 `--dry-run`）

### 5.1 PBR 加第二個權重（`--pbr`，3 候選、n_trials=12）

`with_pbr_15`（rev .35 / roe .25 / mom .25 / value_pbr .15）與 §4 的 `with_pbr`（.3）事先一起固定、一起計入 n_trials，沒有看完結果再調。

| fold | baseline | with_pbr (.3) | with_pbr_15 |
|---|---|---|---|
| 1 | +47.3% | +10.3% | +51.2% |
| 2 | +49.5% | +46.9% | +69.9% |
| 3 | −8.9% | −7.6% | −13.8% |
| 4 | +55.1% | +4.4% | +24.4% |
| 平均 | **+35.8%** | +13.5% | +32.9% |

`with_pbr_15` 贏 2 個 fold（fold 2 贏 20 個百分點），但 fold 4 輸 31 個百分點、平均仍低於 baseline；PBO 0.17，閘門的 **DSR 0.92 < 0.95，未過**。這個 DSR 同樣是 `point_best` 的（見 §4 說明）：這次 point_best 仍是 baseline（+35.8% 對 +32.9%），並非 with_pbr_15 的 DSR；per-fold 勝出最多的 `plateau` 勝者是 with_pbr_15（2 次），因 DSR 閘門擋下而未成為待審候選。PBR 的組合層結論不變：目前沒有證據它值得取代現行權重（平均報酬仍低於 baseline）。這個否定的檢定力同 §4 的限制（4 個 fold、約 4.3 年）。

### 5.2 skip-21 動能（`--skip21`，全期日曆 2012-05 ~ 2026-01，3 候選、n_trials=12）

只把動能換成跳過最近 21 根的窗口，其餘權重不變：`mom60_skip21`、`mom120_skip21` 各佔原動能的 0.3。

| fold | baseline | mom60_skip21 | mom120_skip21 |
|---|---|---|---|
| 1 | +64.4% | +53.4% | +73.2% |
| 2 | +24.6% | +37.4% | +30.7% |
| 3 | +59.0% | +45.4% | +6.4% |
| 4 | +137.7% | +88.6% | +160.2% |
| 平均 | **+71.4%** | +56.2% | +67.6% |

`mom120_skip21` 對 baseline 是 3 勝 1 負（fold 1、2、4 贏；fold 4 贏 22.6 個百分點），但輸的 fold 3 輸了 52.6 個百分點，平均仍低於 baseline（+67.6% 對 +71.4%）；三個候選一起比時，各 fold 的最佳者是 mom120_skip21 兩次、mom60_skip21 一次、baseline 一次。PBO 0.17，閘門的 **DSR 0.76 < 0.95，未過**——這是 `point_best`（baseline，平均 +71.4%）的 DSR，不是 mom120_skip21 的（見 §4 說明）；per-fold 勝出最多的 plateau 勝者是 mom120_skip21（2 次），因閘門未過而未成為待審候選。§2-3 的單因子／綜合 IC 增益很小，這裡組合層也沒有轉成穩定的樣本外優勢，與該增益「小」的判讀一致。train 區間 2012-05 ~ 2026-01（約 13.7 年），比 PBR 那次的約 4.3 年長得多，各 fold 也更長。

### 5.3 沒做的：PBR 當過濾條件

`factor_ranks()` 本來就是「先過濾再排名」：任何 extra factor 回傳 None 的股票會被剔除，所以「PBR 超過門檻就回傳 None、權重 0」的因子函式理論上能當過濾條件，不必動 `portfolio_backtest.py`。**但目前的 walk-forward 用不了它**：`optimize.sweep_ranking_weights_walk_forward()` 把 extra_factors 的聯集套用到**每個**候選（含 baseline，見該函式對 `p["extra_factors"]` 的設定），gate 會連 baseline 一起過濾，無法比較「有過濾 vs 沒過濾」。要比較得先讓 sweep 支援每個候選各自的 extra_factors，這會動 `optimize.py` 的核心比較邏輯，且再增加一組要付 DSR 懲罰的試驗；在 §5.1 已顯示 PBR 混進排名沒有樣本外優勢的前提下，這次沒做。（這裡的可行性判斷只讀了程式，沒有實作驗證。）

## 6. 法人買賣超因子（`directions.md` §2.2，2026-09-30 完整 1821 檔評估）

因子：外資＋投信近 20 個交易日淨買超股數 ÷ 同期成交量（`finlab_factors.institutional_net_ratio_series()`；自營商排除；`usable_date` 用下一個交易日，避免盤後才公布的前視偏誤）。資料由 `scripts/fetch_institutional_series.py` 抓（FinMind 每檔一次請求，2012-05 起完整歷史），`scripts/report_institutional_ic.py` 評估。

**樣本**：1821 檔全部抓完（先抽樣 228 檔、再補齊其餘 1593 檔，皆 0 失敗），173 個再平衡日（2012-05 ~ 2026-09）。基準三因子的數字與 §1 全市場重跑一致（例：revenue h=60 IC +0.034、t +4.66），可以直接對照。

| 區間 | h=20 | h=60 |
|---|---|---|
| 全期（n=171/169） | IC −0.011, t −2.01, ICIR −0.15 | IC +0.015, t +3.02, ICIR +0.23 |
| 2021-12 前（n=114） | IC −0.014, t −2.06, ICIR −0.19 | IC +0.017, t +2.80, ICIR +0.26 |
| 2021-12 起（n=57/55） | IC −0.006, t −0.61, ICIR −0.08 | IC +0.012, t +1.29, ICIR +0.17 |

同期基準對照（全期 h=60 ICIR）：revenue +0.38、roe +0.12、momentum +0.07；（2021-12 起）revenue +0.27、roe +0.26。

**與先前 1/8 抽樣（228 檔）的差異**：抽樣時 h=20 全期 IC −0.009、t −0.93，且前後段符號翻轉（−0.023 → +0.017）；完整資料下 h=20 是**三個區間符號一致的小幅負值**（全期 t −2.01，2021-12 起不顯著），抽樣那次的「翻轉」應是抽樣雜訊（合理解釋，沒有另外檢驗）。h=60 抽樣為 IC +0.024、t +2.77，完整資料下 +0.015、t +3.02，方向一致、幅度較小。

**判讀**：
- **h=20 為負**：過去 20 日外資＋投信淨買超佔量越高，接下來一個月報酬略差（可能是追高後的短期反轉，這只是猜測，沒有驗證）；但 |IC| 只有 0.011，且 2021-12 起 t 只有 −0.61，不顯著。
- **h=60 為正**：三個區間都是正的，全期 t +3.02；但 60 日視窗與月頻再平衡**約 3 倍重疊**，t 粗估折減約 √3 後實質 t≈1.7，2021-12 起 t +1.29 也不顯著，且 ICIR +0.23 明顯低於 revenue 的 +0.38。
- 同一個因子在兩個持有期給出**相反符號**，不是乾淨的單向選股訊號；用在排名上得選定一個 horizon，而 h=60 這一格的證據只是邊緣。
- 沒有做：加進現行綜合分數後的增量 IC、與 revenue/roe/momentum 的相關性、walk-forward。單因子證據不足以支持進 ADR 0003 的候選審核。

## 建議的下一步（依專案既有治理流程）

1. **PBR 目前不採用**（§4、§5.1，兩個權重都沒過）。若要重試，等 PBR 歷史變長（每多一年就多一個有效 fold），或先想清楚為什麼單因子 IC 顯著而組合變差；不要在現有 4 個 fold 上繼續調權重。過濾條件的接入方式要改引擎，見 §5.3。
2. **skip-21 動能目前不採用**（§5.2：平均樣本外報酬低於 baseline，閘門 DSR 0.76 為 point_best 即 baseline 的數字）。現行 60 日動能維持不變。
3. **法人買賣超暫不進 walk-forward**（§6）。若要繼續，先做便宜的離線增量檢驗：加進現行綜合分數後 IC 有沒有提升、與 revenue/roe/momentum 的相關性；沒有明顯增量就停。資料已在 `cache/institutional_series/`（1821 檔），不需要再打 API。
4. §3.2 的「月頻選股 + `breakout_strategy` 進場濾網」與 §3.3 rebalance 頻率對照，需要價格資料以外的 chip 快取與 `portfolio_backtest` 整合，尚未開始。

## 重現方式

```
python scripts/report_factor_candidates_and_momentum.py                      # 預設 --since 2021-12-01, horizons 5,20,60
python scripts/report_factor_candidates_and_momentum.py --horizons 20,60
python scripts/run_strategy_search.py --pbr --dry-run                        # §4 的 walk-forward
```
