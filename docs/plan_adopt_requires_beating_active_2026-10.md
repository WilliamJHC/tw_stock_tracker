# 計畫：自動套用／rollback 必須「勝者平均報酬高於現行權重」

2026-10-09。**以下「要解決什麼」到「驗收標準」是施工前寫的計畫（原文保留）；施工結果見文末「施工紀錄」。**

## 要解決什麼

排程的權重最佳化（`scripts/run_ranking_optimize.py`，`optimize.yml` 每月 2、16 日台北 06:00）現在決定「換不換權重」只看 `evaluate_decision()` 的三條規則：高原寬度（勝者贏了夠多折）、PBO、勝者自己的 DSR。**沒有任何一條要求勝者的樣本外平均報酬比現行權重（`active`）高。**

所以會出現這種情況：勝者贏的折數最多、DSR 也過，但把各折報酬平均，它比現行權重低，系統照樣自動換上去（`adopt_suggested`）或退回舊權重（`rollback_to_previous`）。實例：週／雙週頻對照（`docs/rebalance_frequency_report.md`），高原勝者雙週頻 DSR 0.9669 會過 0.95，但 4 折平均 94.41% < 月頻（現況）107.74%。那次是手動腳本，沒有真的換；排程不會有人看。

注意這個例子只是**類比**：排程路徑上的候選只有 `active`／`previous`／`ic_suggested` 三個，差別在權重，不在 `rebalance_freq`，所以雙週頻的情境不會在排程裡原樣重現；它說明的是「DSR 過了、平均較低」這種結構在現行閘門下會被放行。

這個缺口是舊閘門就有的，不是 2026-10-04 改 DSR 對象造成的；但改 DSR 對象後，勝者自己的 DSR 更容易過關（不再借 point_best 的），缺口更值得補。

## 為什麼選「勝者 > active」，不選「勝者 = point_best」

你上一輪選了選項 2。理由備忘：

- 想防的是「換成比現在更差的」，不是「一定要換成平均最高的那個」。
- 平均報酬容易被單一 fold 主導（雙週頻例子裡月頻最後一折是 264.78%），要求勝者同時是 point_best，等於把採用與否交給最後一折的雜訊。
- 勝者贏過 active、但輸給另一個候選時，換成勝者並不吃虧；選項 3 會多擋這種情況。

## 要做什麼（開工後的步驟）

1. **`src/optimize.py` 的 `resolve_ranking_weights_action()`** 新增必填參數 `candidate_scores`（`sweep_ranking_weights_walk_forward()` 結果裡已有的 `result["candidate_scores"]`，各候選樣本外平均報酬，可能含 None）。**判斷順序固定如下，且只在走到該分支時才用 key 查值**（生產環境 `active` 必存在，`previous` 只在 `has_previous` 為 True 時存在，`ic_suggested` 可能不存在，先查 key 會 KeyError）：
   1. `accept` 為 False → `keep`（不變）。
   2. `winner == "previous" and has_previous` → 取 `candidate_scores.get("previous")` 與 `.get("active")`，前者嚴格大於後者才 `rollback_to_previous`，否則 `keep`。理由：`resolve_ranking_weights_action()` 自己的 docstring（`src/optimize.py` 第 250–252 行）把 rollback 的前提寫成「舊設定在新的樣本外已證明比現行好」，現在才真的檢查這個前提。
   3. `winner == "ic_suggested"` → 同樣取 `ic_suggested` 與 `active` 的平均，嚴格大於（不加邊際值）才 `adopt_suggested`，否則 `keep`。
   4. 其他（含勝者是 `active`）→ `keep`（不變）。
   - 任一邊的平均是 None → `keep`。原則：沒有證據證明比現行好，就不換。這和 `evaluate_decision()`「算不出來的規則不否決」方向相反，是刻意的：否決規則缺證據時放行是避免誤殺，採用動作缺證據時放行則是自動改動線上設定。實際上勝者的平均不可能是 None（fold 贏家必須有非 None 分數，`src/optimize.py` 第 703–710 行），只有 `active` 的平均可能是 None（例如全程無交易）；這時 `active` 實際表現等於 0%，勝者再好也會被擋成 `keep`。這與 `_pbo_and_dsr()` 把 None 當 0% 的做法不一致，記在這裡並用測試釘住，不順手改。
   - 用**必填**參數而非預設 None，避免未來的呼叫端漏傳而靜默略過檢查。現有 5 個測試（`tests/test_optimize.py` 第 166–193 行）需補傳 `candidate_scores`（`keeps_when_active_wins`、`no_rollback_without_a_real_previous_candidate` 補傳的 dict 可以不含 `previous`／`ic_suggested`，上面的判斷順序保證不會 KeyError）。
2. **`scripts/run_ranking_optimize.py` 的 `main()`**：呼叫時傳入 `result["candidate_scores"]`；Telegram 訊息多一行 `candidate_scores=...`，讓被擋下時看得出原因（action 仍是 `keep`、摘要仍是「維持現況」，不新增 action 種類，`apply_action()` 不用改）。
3. **`vs_buy_hold` 改成以勝者為對象**（`sweep_ranking_weights_walk_forward()` 裡 `chosen_name` 與 `chosen_oos_mean_return_pct` 從 `point_best_name` 改成 `winner`）。現在唯一的使用處是 Telegram 訊息（`run_ranking_optimize.py` 第 229 行）；`run_optimize.py` 用的是 `sweep_param_walk_forward()` 的另一個 `vs_buy_hold`，不動。這樣單次決策裡的 DSR、高原、`vs_buy_hold` 描述的是同一個候選。`point_best` 欄位仍保留（純資訊）。這一步與步驟 1 獨立，你不要的話可以略過。
4. **測試**（`tests/test_optimize.py`；步驟 1 的測試是純函式測試，不需要假的 `simulate`；步驟 3 的測試才需要）：
   - 勝者 `ic_suggested` 通過閘門且平均 > active → `adopt_suggested`。
   - 同上但平均 ≤ active（含剛好相等）→ `keep`。
   - 勝者 `previous` 通過閘門且平均 > active → `rollback_to_previous`；平均 ≤ active → `keep`。
   - 任一邊平均為 None → `keep`。
   - 勝者 `active` 或 `accept` 為 False → `keep`（不變）。
   - `active` 的平均為 None、勝者很好 → `keep`（釘住上面記的不一致行為）。
   - 步驟 3 若做：沿用前一輪 `test_ranking_sweep_dsr_is_computed_for_plateau_winner_not_point_best` 的腳本化假 `simulate`（`_sweep_with_scripted_fold_returns`，`tests/test_optimize.py` 第 558–581 行），加斷言 `vs_buy_hold["chosen_name"] == winner`。
   - 反向驗證：把新條件暫時拿掉，「平均 ≤ active → keep」的測試應該失敗。
   - 完整 pytest 全過（目前 556 passed）。
5. **歷史影響評估**（純計算，不寫 DB、不發 Telegram）：這些比賽都**不是**走排程的 `resolve_ranking_weights_action()`，所以這一步回答的是「若套用同一條規則（勝者平均 > 對照組平均）會怎樣」，不是「歷史上有東西被擋」。各比賽實際走的路徑與已知數字（來源已在文件中，開工時重新核對）：
   - 週／雙週頻（`scripts/report_rebalance_freq_walkforward.py`，複製的 sweep）：勝者雙週頻 94.41% < 月頻 107.74%，會被新規則擋下（原本 DSR 過）。
   - 月／雙月／季頻（`scripts/run_rebalance_comparison.py`，自己呼叫 sweep 並有自己的採用規則）：勝者季頻平均約 80.3%，**高於**月頻 71.7%（`docs/rebalance_frequency_comparison_2026-10.md`），新規則不會擋它；它是被 DSR 0.9475 擋下的。
   - PBR 兩組、skip-21（`run_strategy_search.py` 的 `--pbr`／`--skip21`）：skip-21 勝者 mom120_skip21 平均 67.6% < baseline 71.4%，新規則會擋，但本來就被 DSR 0.47 擋下；PBR 另一組勝者 with_pbr_15 的平均低於 baseline（+32.9% vs +35.8%），同理。
   - 結論預期：新規則只會多擋、不會多放，所以不會有任何過去的「不採用」被翻成「採用」。表格不要預設每場都會被新規則擋，要逐場列實際數字。
6. **文件同步**：
   - ADR 0002 修訂記錄補一段（新增「勝者平均報酬須高於 active」，說明理由與 rollback 的對應；DSR／PBO／高原門檻本身不變）。
   - ADR 0003（檔名 `0003-manual-review-for-strategy-logic-search.md`）第 35、49、53 行寫「`resolve_ranking_weights_action()` 不變」，加註 2026-10 起多一個參數與一條規則。
   - `docs/research_round_closeout_2026-09.md` 的「未結事項」新增一項（標已完成）說明這條新規則；週／雙週頻報告 2026-10-09 補註那段加一句「若走排程路徑，這條新規則也會擋下它」。
   - `docs/factor_timeframe_optimization_directions.md` 的進度註記視需要補一行。
   - 程式內說明文字：`src/optimize.py` 的 `resolve_ranking_weights_action()` docstring（第 223–261 行，keep／rollback／adopt 條件）、`sweep_ranking_weights_walk_forward()` 的回傳說明（第 644–653 行）與第 736 行註解（若做步驟 3）、`src/factor_eval.py` 第 266–269 行（`suggest_weights` docstring，「贏得 walk-forward 且通過門檻後才採用」）、`scripts/run_ranking_optimize.py` 第 27–29 行模組 docstring 步驟 4。
7. **commit 到本地分支**（繁體中文 message），回報分支名與 hash；不 push、不開 PR。

## 範圍外（不做，除非你要）

- **`scripts/run_strategy_search.py`**（策略搜尋，勝者非 baseline 才產生待審候選）：它的決策是 `not decision["accept"] or winner is None or winner == "baseline"`，沒經過 `resolve_ranking_weights_action()`，且最後由人看 train／test 對照後才套用（ADR 0003）。你說的是「自動套用閘門」，所以預設不改。代價：策略搜尋仍可能把一個平均報酬低於 baseline 的勝者送來待審。要一併改的話是額外幾行（`winner` 的平均須 > baseline 的），請告訴我。
- `evaluate_decision()` 本身、`sweep_param_walk_forward()`、`optimize.yml`、`.2f` DSR 顯示問題、零變異勝者 DSR=None 靜默放行：都不動（前兩項是前一輪已記的待辦）。
- 不加「最小改善幅度」邊際值：加了就多一個要調的設定值，而 4 折的雜訊遠大於任何合理邊際；現在只擋「更差或持平」。

## 取捨與已知風險

- **只防「換成更差的」，不防「換成其實沒更好的」。** 勝者平均只高一點點、其實是雜訊時，這條規則會放行，這時仍要靠 DSR／PBO／高原三條把關。
- **更保守。** 之後的排程更可能 `keep`。對 10/16 沒影響（雲端快取是空的，仍會空跑）；播種完成後才有作用。
- **平均仍被單一 fold 主導。** 用的是和既有 `candidate_scores`／`point_best` 同一種平均，沒有解決這個性質，只是在換設定這個動作前多一道不能更差的檢查。
- **n_folds=4 的檢定力不變。**

## 需要你決定的事（我的建議放前面）

1. **`vs_buy_hold` 要不要一起改成勝者**（步驟 3）：建議一起改，成本極小，單次決策的證據才對得上同一個候選。
2. **策略搜尋要不要也加同樣的條件**：建議先不加（範圍外，且有人工審核）；若你希望兩條管線一致再加。
3. **平均為 None 時的行為**：建議 `keep`（沒證據就不換）。
4. **順序**：建議做完這份、再 push 合併後的 `main`、再播種。若你想先 push／播種，至少要在播種完成後的第一次排程前做完，因為那是第一次可能真的自動套用。

## 驗收標準

- 新增測試通過、反向驗證有效、完整 pytest 全過。
- 步驟 5 的對照表產出，明確寫出「有／沒有任何過去結論被改變」。
- 文件同步後，repo 內不再有「`resolve_ranking_weights_action()` 不變」或「自動套用只看三條規則」而未加註的說法（以 grep `resolve_ranking_weights_action` 交叉核對）。

## 施工紀錄（2026-10-09，本地，未 push）

六個決定都照建議：步驟 3（`vs_buy_hold` 改勝者）一併做；策略搜尋不動；None → `keep`；rollback 同規則；嚴格大於；先做這份。

**改了什麼**
- `src/optimize.py`：`resolve_ranking_weights_action(decision, winner, has_previous, candidate_scores)`，`candidate_scores` 必填；新增小函式 `_mean_beats()`（兩邊都非 None 且嚴格大於才為真）；只在走到 `previous`／`ic_suggested` 分支時才查 key。`vs_buy_hold` 的 `chosen_name`／`chosen_oos_mean_return_pct` 改用 `winner`。docstring 與註解同步。
- `scripts/run_ranking_optimize.py`：傳入 `result["candidate_scores"]`；Telegram 訊息多一行 `candidate_scores=...`；模組 docstring 步驟 4 補一句。
- `src/factor_eval.py`：`suggest_weights` docstring 同步。
- 文件：ADR 0002 修訂記錄、ADR 0003 三處「不變」加註、收尾記錄「未結事項」第 5 項、週／雙週頻報告補註。`factor_timeframe_optimization_directions.md` 沒有需要更新的進度註記，未動。

**驗證（已驗證）**
- `resolve_ranking_weights_action` 的測試由 5 個擴為 10 個（既有 5 個補傳 `candidate_scores`；新增 keep ≤、keep 相等、rollback 平均不夠、任一邊 None、active 為 None），另新增 1 個 `vs_buy_hold` 測試。完整 pytest：562 passed（之前 556，淨增 6）。
- 反向驗證：把 `_mean_beats()` 的條件拿掉（改成恆為真），5 個「應 keep」測試失敗；把 `vs_buy_hold` 改回 point_best，`test_ranking_sweep_vs_buy_hold_describes_the_plateau_winner` 失敗。兩個變更都還原後重跑通過。

**歷史影響（若把同一條規則「勝者平均 > 對照組平均」套用到過去各場比賽；純對照文件上已記錄的數字，沒有重跑回測、沒寫 DB、沒發 Telegram）**

| 比賽 | 高原勝者 | 勝者平均 | 對照組平均 | 新規則 | 原本結論與擋下者 |
|---|---|---|---|---|---|
| 週／雙週／月頻（`report_rebalance_freq_walkforward.py`） | 雙週頻（贏 2 折） | 94.41% | 月頻 107.74% | 擋下 | 維持月頻（本來靠平均較低與無穩定 plateau 判斷；高原勝者 DSR 0.9669 其實過了） |
| 月／雙月／季頻（`run_rebalance_comparison.py`） | 季頻（打平取先出現者） | 80.3% | 月頻 71.7% | **不擋** | 維持月頻，由 DSR 0.9475 < 0.95 擋下 |
| 動能跳過 21 根（`--skip21`） | mom120_skip21 | 67.6% | baseline 71.4% | 擋下 | 不採用，本來就被高原勝者 DSR 0.47 擋下 |
| PBR 另一組權重（`--pbr`） | with_pbr_15 | +32.9% | baseline +35.8% | 擋下 | 不採用，本來就被 DSR 擋下（高原勝者 0.86） |
| PBR 權重 .3 | baseline（贏家與 point_best 同為基準） | — | — | 不適用 | 不採用（基準勝出，無候選要換） |

**結論：沒有任何過去的「不採用」被翻成「採用」。** 新規則在 5 場裡擋下 3 場（其中 2 場本來就被 DSR 擋下）、不擋 1 場（季頻，本來由 DSR 擋下）、1 場不適用。應計品質（DSR 0.9477）與進場濾網（DSR 0.39）兩場沒有重新核對勝者平均，不在表內；兩者本來都被 DSR 擋下，新規則只會多擋、不會多放，結論不可能翻轉。

**注意**：週／雙週頻那場是類比，不是排程路徑上的候選差異（排程候選差在權重，不在 `rebalance_freq`），見上文「要解決什麼」。

**未動**（同計畫「範圍外」）：`evaluate_decision()`、`sweep_param_walk_forward()`、`optimize.yml`（含第 28 行註解日期錯誤）、`.2f` DSR 顯示問題、零變異勝者 DSR=None 靜默放行、策略搜尋。

**已知限制**：只防「換成更差或持平的」，不防「換成其實沒更好、只是雜訊略高的」；平均仍被單一 fold 主導；4 折檢定力不變；10/16 排程不受影響（雲端快取是空的，仍空跑）。
