# 選股因子研究（2026-09 這一輪）收尾記錄

2026-10-01。這份是**結案與否決記錄**，不是新研究。起點是 `docs/factor_timeframe_optimization_directions.md` 的「立即」項目：評估 Phase 6b 候選因子、檢視動能 formation window，再把有訊號的接進 ADR 0003 的審核流程。**這一輪沒有任何新因子被採用，現行 `revenue .4 / roe .3 / momentum(60 日) .3` 維持不變。**

## 結果總表

| 候選 | 結論 | 主要依據 | 數字來源 |
|---|---|---|---|
| 毛利率年增趨勢 | 排除 | h=20 IC +0.017（t +1.73）、h=60 IC +0.018（t +1.56），未達顯著，依 lighthouse Phase 6b 規則排除 | 雲端完整快取重跑（1821 檔） |
| 營益率年增趨勢 | 排除 | h=20 IC +0.014（t +1.31）、h=60 IC +0.004（t +0.38） | 同上 |
| 價值（−PBR） | 不採用 | 單因子顯著（h=60 IC +0.068、t +3.88，重疊折減後約 2.2），但 walk-forward 組合層輸給基準：平均樣本外報酬 +13.5%（權重 .3）／+32.9%（另一組權重 .15）vs 基準 +35.8%；.3 權重只贏 1 個 fold、.15 贏 2 個 fold，平均都低於基準。閘門 DSR 0.91／0.92（皆 < 0.95）是 point_best（兩次都是基準）的數字，**不是 PBR 候選自己的**；不採用的依據是平均報酬與逐 fold 紀錄 | .3 權重為雲端實跑；另一組權重與兩個 DSR 為本機回報，未在雲端重現（細節見因子文件 §4、§5.1） |
| 動能跳過最近 21 根（60／120 跳 21） | 不採用 | h=60 單因子 t 約 +3（全期），但綜合分數 ICIR 僅 +0.31 → +0.32 ~ +0.34；2021-12 起子樣本不顯著；walk-forward 中 mom120_skip21 對基準 3 勝 1 負但平均仍低於基準（+67.6% 對 +71.4%），閘門 DSR 0.76 < 0.95 是 point_best（基準）的數字，不是 mom120_skip21 自己的 | 單因子與綜合 IC 為雲端實跑；walk-forward 為本機回報（因子文件 §5.2） |
| 法人買賣超 | 不採用（證據不足，**未做 walk-forward**） | 完整 1821 檔單因子 IC：h=20 為負（全期 t −2.01，2021-12 起不顯著）、h=60 為正（全期 t +3.02，重疊折減後約 1.7，2021-12 起 t +1.29 不顯著），兩個持有期符號相反；沒做增量 IC、相關性與 walk-forward | 本機評估，未在雲端重現（因子文件 §6） |

> **2026-10-04 註**：表中的「閘門 DSR」是**改動前**的算法（對 point_best 計算）。此後權重掃描的閘門 DSR 改為對高原勝者計算（`docs/plan_dsr_target_plateau_winner_2026-10.md`）。用本機快取重跑後，PBR 另一組權重的勝者 with_pbr_15 自己的 DSR 是 0.86、skip-21 的勝者 mom120_skip21 是 0.47，都仍 < 0.95，兩項「不採用」結論不變；.3 權重那次勝者與 point_best 同為基準，DSR 不變。

## 這一輪學到的

1. **單因子 IC 顯著不等於組合層會賺更多。** PBR 是最清楚的例子：單因子 t +3.88，放進 top-20 加權（權重 .3）卻在 4 個 fold 裡輸 3 個（權重 .15 為輸 2 個，平均仍低於基準）。原因沒有驗證，不要寫成已知結論。
2. **抽樣結論會被完整樣本翻轉。** 209 檔抽樣時 PBR 看起來「值得追蹤」、120 跳 21 動能「明顯最好」；完整 1821 檔下前者變顯著但組合層失敗，後者與 60 跳 21 沒有差別。以後候選因子的第一步直接用完整快取。
3. **「現行動能被最近一個月短期反轉污染」這個假說有支持**（mom20 h=20 t −3.06；跳過 21 根後 h=20 符號翻正），但改善幅度太小、近期子樣本消失，不足以支持更換定義。
4. 現行動能在 h=20 為負、拿掉動能的綜合 IC 不比現行差（h=60 ICIR +0.32 vs +0.31），與 `regime_strategy_research.md` 一致，但這同樣只是 IC 層證據，沒有走 walk-forward，**不構成移除動能的理由**。

## 這份結論的檢定力限制

- 候選因子（PBR、margin 趨勢）資料只有 2021-10 起，walk-forward 只有 4 個 fold、樣本外合計約 3.4 年；否定結論的檢定力不高。
- h=60 的報酬視窗與月頻再平衡約 3 倍重疊，IC 的 t 值因此被高估（粗估約 √3 ≈ 1.7 倍，不是 3 倍；例：PBR t +3.88 折減後約 2.2），h=60 的 t 只能當方向參考。
- 報酬為未含息收盤價報酬、未做產業中立、有生存者偏差（同 `regime_switch_vs_0050.md` 已知限制）。
- 若要重試 PBR：等歷史變長、或先弄清楚單因子與組合層為何背離，**不要在現有 4 個 fold 上繼續調權重**（每多試一組都要付 DSR 的試驗次數懲罰）。

## 未結事項

1. **雲端 `active_ranking_weights`：已核實（2026-10-02，推論，非直接讀值）為程式預設 0.4/0.3/0.3，從未被寫過。** 依據：該 key 只有 `optimize.yml` 的採用／rollback 與 `apply_strategy_candidate.yml` 會寫；`optimize.yml` 三次執行（9/2、9/16、10/2）都因雲端 `cache/factor_run` 還原失敗、載入 0 檔而在寫入前結束，兩個 `apply_*` workflow 從未執行過。所以 walk-forward 的 baseline 成立。
   - 順帶發現：雲端 `update-universe-bars-state-*` 快取血統從未被建立，`optimize`／`strategy_search`／`daily_recommendations` 都在空快取下空跑，`update_universe_bars_part1/2` 失敗。補救是 `worktree-seed-factor-run` 分支的 `seed_factor_run.yml`（尚未 push、尚未在雲端執行）。播種完成後，雙週 `optimize.yml` 第一次真的跑起來時可能自動套用新權重。
2. **分支 `worktree-pbr-factor-adr0003`（未 push）要不要併回主線。** 該分支比本地 `main` 多 8 個 commit（`main` 已含 PBR 接入的前三個 commit），乾跑合併無衝突、可快轉。`EXTRA_FACTOR_FUNCS` 註冊 PBR／skip-21 不會讓已否決的候選進入排程搜尋：`used_extra_factors()` 只收權重有引用到的 extra factor，排程的候選池（`build_candidates()`）是固定的，`strategy_search.yml` 也沒帶 `--pbr`／`--skip21`（那兩個只是手動模式）。是否併入由你決定；本地 `main` 本身也領先 `origin/main` 3 個 commit，同樣尚未 push。
3. **PBR 資料過期問題（29 檔超過 30 天）**：因 PBR 不採用，暫不需處理；日後重啟 PBR 才要定義。
4. **文件同步**：本地 `main` 上的 `docs/factor_candidates_momentum_ic_2026-09.md`（888f3e2 版）沒有 §5（後續兩輪 walk-forward）與 §6（法人買賣超），且寫著 skip-21「尚未進 walk-forward」；分支版已補上，合併後以分支版為準。

## 尚未開始的方向（沒有被否決，只是這一輪沒做）

- 月頻選股 + `breakout_strategy` 進場濾網（方向清單 §3.2）。
- ~~rebalance 頻率對照（§3.3）~~ 已在另一個分支 `worktree-rebalance-freq` 做完（未 push、未併）：依事先寫死的規則維持月頻，詳見該分支的 `docs/rebalance_frequency_comparison_2026-10.md`。
- 應計品質（accrual）因子；董監事質押比（FinMind 查不到對應 dataset，可行性待確認）。
