# 計畫：權重掃描的 DSR 改為對「高原勝者」計算

2026-10-04。**這是計畫，不是施工紀錄：尚未動任何程式碼，等你說「開工」才做。**

## 要解決什麼

`src/optimize.py` 的 `sweep_ranking_weights_walk_forward()`（第 719–721 行）把 `point_best_name`（樣本外**平均報酬最高**者）傳給 `_pbo_and_dsr()` 算 DSR；但 `run_ranking_optimize.py`／`run_strategy_search.py` 實際採用的是 `plateau["most_common"]`（逐 fold 勝出**次數最多**者，見 `resolve_ranking_weights_action()` docstring）。兩者可以不同，所以：

- 閘門 `evaluate_decision()` 的三條規則裡，高原寬度描述的是高原勝者、PBO 與候選無關、**DSR 卻描述 point_best**。
- 高原勝者與 point_best 不同時，「通過閘門」並不代表被採用的那個候選自己通過了 DSR。
- 同一個閘門驅動 `optimize.yml` 自動套用（ADR 0002，無人工審核），所以這不只是說明文字問題。

文字層面已在 `0d6a7de` 更正（docstring 與註解）；本計畫處理的是語意本身。

## 為什麼要在雲端第一次真的跑 optimize 之前決定

雲端 `optimize.yml` 至今三次都在載入 0 檔時提早結束（快取血統從未播種，見 `closeout` 未結事項 1）。播種完成後，下一次排程會第一次真的算出結果並可能自動套用。排程是 `0 22 1,15 * *`（UTC），換成台北時間是每月 **2、16 日 06:00**（與已發生的 9/2、9/16、10/2 吻合；`optimize.yml` 內「1、15 號台北 06:00」的註解寫錯了，不在本計畫範圍），**下一次是台北 2026-10-16 06:00**，GitHub 排程另可能延遲。播種本身還沒 push、還沒執行，所以不一定趕得上，但閘門語意應該在「第一次真的會自動套用」之前定案。

## 要做什麼（開工後的步驟）

1. **改一行**：`sweep_ranking_weights_walk_forward()` 的 `_pbo_and_dsr(..., point_best_name, ...)` 改傳 `winner`（`plateau["most_common"]`，第 714 行已算好、此時必不為 None，因為 `usable_folds > 0` 時 `argmax_counts` 非空）。`sweep_param_walk_forward()` 傳的是離高原中心最近的候選值（無高原時退回 point_best 值），不動。另一個邊界：`evaluate_decision()` 對 `dsr_value is None` 不否決，而 `metrics.sharpe_ratio` 在零變異時回 None；若高原勝者各 fold 報酬恆為 0，改傳 winner 後 DSR 會是 None 並靜默放行。步驟 4 加一個測試釘住這個行為（先確認現況，再與你決定要不要改成保守否決，不順手改）。
2. **`vs_buy_hold` 不動**：它的 `chosen_name` 仍是 point_best，是「平均報酬最高者對買進持有」的獨立指標，不屬於閘門；是否一併改成另案。
3. **更新兩處文字**：`_pbo_and_dsr()` docstring 與第 719 行註解（現在寫「DSR 描述 point_best」，改成兩個呼叫端一致）。
4. **測試**（`tests/test_optimize.py`；現有 `mock.patch("optimize.pb.simulate", side_effect=spy)` 的手法可沿用，但要製造情境比直接沿用複雜：假的 `simulate` 必須回傳 `{"equity_curve": [{"date", "equity"}, ...]}`，且在各 fold 的 test 區間內至少有 2 個點，所以得先用 `opt.purged_calendar_walk_forward_splits` 算出 fold 邊界，或依呼叫順序（每個 fold 依候選順序）腳本化）：
   - 新增：造出「point_best ≠ 高原勝者」（一個候選贏 3 fold 但每次小贏、另一個贏 1 fold 但大贏拉高平均；2 個候選時 chance_threshold 為 0.5，3/4 足夠），斷言 `result["dsr"]` 等於對高原勝者單獨呼叫 `_pbo_and_dsr` 的值、且不等於對 point_best 的值。
   - 新增：兩者相同時 `dsr` 與改動前相同（迴歸保護）。
   - 新增：勝者報酬零變異時 `dsr` 為 None（釘住現況，見上面的邊界）。
   - 若採用 `dsr_of` 欄位，`test_sweep_ranking_weights_walk_forward_shape_and_trial_accounting` 的 key 集合斷言要同步改。
   - 既有測試必須全過（上次實跑為 524 passed）。
5. **歷史結果影響評估**（用本機 1821 檔快取、`--dry-run`，不寫 DB、不發 Telegram）：把 PBR（兩組權重）與 skip-21（60／120）那幾次 walk-forward 重跑，列出改動前後的「高原勝者、point_best、DSR、decision」。目的是回答「過去的『不採用』結論會不會被這個改動翻轉」。**這一步有真實的翻轉風險**，依 `docs/factor_candidates_momentum_ic_2026-09.md` 的記錄：
   - 首次 PBR（`with_pbr`）：勝者與 point_best 都是 baseline，預期不變。
   - PBR 第二組權重（§5.1）：point_best 是 baseline，高原勝者是 `with_pbr_15`（贏 2 fold）；原本只被 DSR 0.92（baseline 的）擋下。
   - skip-21（§5.2）：point_best 是 baseline，高原勝者是 `mom120_skip21`（贏 2 fold）；原本只被 DSR 0.76（baseline 的）擋下。
   - 後兩次的勝者平均報酬都低於 baseline、各有大幅輸掉的 fold，直覺上勝者自己的 DSR 不太會更高，但這是**推測，不是驗證**。若重跑結果出現「閘門通過」，要先停下來跟你討論，不自行改任何採用結論（ADR 0003 規定這類候選本來就要人工審核）。
6. **文件同步**：ADR 0002 補一段修訂記錄（說明閘門 DSR 對象的變更與理由；ADR 0002 的自動套用／rollback 決策本身不變）；`docs/adr/0003-manual-review-for-strategy-logic-search.md` 第 58 行（注意 `docs/adr/` 下有兩個 0003 開頭的檔，要寫全檔名）、`docs/factor_candidates_momentum_ic_2026-09.md` §4／§5 與第 140 行（「建議的下一步」章節也有 point_best 說法）、`docs/research_round_closeout_2026-09.md` 第 11、12 行裡「DSR 是 point_best 的」那些句子，加註「2026-10-xx 起閘門 DSR 改為對高原勝者；本表數字為改動前的算法」，**不改寫歷史數字**。`docs/archive/dev_plan_data_integrity.md` 是歷史觀察紀錄（第 74、82、127、128 行有 winner／point_best 的說法，不是對 DSR 對象的描述），不動，驗收 grep 時排除。
7. **commit 到本地分支**（繁體中文 message），回報分支名與 hash；不 push、不開 PR。

## 取捨與已知風險（請看過再決定）

- **行為改變不是單向的**：高原勝者的 DSR 可能比 point_best 的高也可能低（DSR 用的是 Sharpe，不是平均報酬；勝者平均報酬較低但波動可能也較低）。所以不能預先說這個改動「更嚴」或「更鬆」，只能說「驗證的對象與採用的對象一致」。
- **哪些情境才會被影響**：只有高原勝者**不是**會讓 action 恆為 keep 的那個候選時，DSR 對象才影響行動。
  - 權重排程（`run_ranking_optimize.py`）：勝者是 `ic_suggested`（可能自動套用）或 `previous`（可能 rollback）。例：point_best 是 `active`、`ic_suggested` 贏高原投票時，改動前 `ic_suggested` 可借 `active` 的 DSR 過關，改動後用它自己的。勝者是 `active` 時，不論 accept 與否 action 都是 keep，不受影響。
  - 策略搜尋（`run_strategy_search.py`）：勝者是非 baseline 的候選時（會產生待審候選）；勝者是 baseline 則不受影響。這條路徑上才真的出現過被 DSR 擋下的紀錄（上面步驟 5 的兩次）。
  - 這個情境不是假設：`docs/archive/dev_plan_data_integrity.md` 第 74、82 行記錄過 winner=`ic_suggested`、point_best=`active` 的一次（當時 accept 為 False）。
- **樣本很小**：walk-forward 預設 4 個 fold，DSR 的 `n_obs` 是 usable fold 數（可能小於 4），數字本身很粗糙；本改動只修正「對象」，不改善檢定力。
- **n_trials 沒變**：每個 fold 都累加（含被排除的 fold），改動不影響懲罰項。

## 需要你決定的事（我的建議放前面）

1. **是否採用選項 2**（本計畫）：建議採用。替代方案是維持現狀、只靠已寫好的 docstring 說明；代價是自動套用的閘門仍有「驗證對象 ≠ 採用對象」的缺口。
2. **要不要在結果裡加一個欄位標明 DSR 針對誰**（例如 `dsr_of`）：建議加（一個欄位，順便讓 `test_..._shape_and_trial_accounting` 的 key 集合多一項）。這次的混淆就是因為結果裡看不出 DSR 是誰的。不加也可以，改動更小。
3. **順序**：建議先併 `worktree-pbo-dsr-docstring`（文字）→ 再做本計畫 → 之後才 push 與播種。若你想先播種，也請至少在 10/16 排程前完成本計畫。

## 驗收標準

- 新增測試通過、既有測試全過（完整 pytest）。
- 第 5 步的前後對照表產出，並明確寫出「有／沒有任何過去結論被翻轉」。
- 文件同步後，repo 內不再有「閘門 DSR 是 point_best 的」而未加註的說法（以 grep `point_best` 與 `DSR` 交叉核對，`docs/archive/` 排除）。
