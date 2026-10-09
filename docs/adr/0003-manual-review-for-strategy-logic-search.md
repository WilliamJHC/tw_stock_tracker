# 0003. 策略邏輯搜尋改為人工審核後套用，共用既有 meta key 並靠一次性保護避免衝突

## 狀態

已接受（2026-09-04，/grill-me 收斂）

## 背景

ADR 0002 決定 `RANKING_WEIGHTS`（revenue/roe/momentum 三個既有因子的權重）候選比較「不設人工審核關卡，靠自動 rollback 補償」。這次要新增的是不同性質的東西：兩個系統裡還沒有的**條件型態**——均線交叉（`ma_cross`）、量價背離（`vol_price_div`）——作為新候選因子邏輯，用同一套 `src/optimize.py` purged walk-forward + PBO/DSR 框架比較（`finlab_factors.factor_ranks()`/`composite_scores()` 擴充 `extra_factors` 參數後才能餵進 `sweep_ranking_weights_walk_forward()`），排程跑在 `scripts/run_strategy_search.py`。

第一個要決定的問題跟 ADR 0002 當初一樣：候選通過 DSR/PBO/高原寬度門檻之後，誰按下「採用」？

## 決策

**設人工審核關卡，不自動套用。** `scripts/run_strategy_search.py` 跑完整評估（train 段 purged walk-forward 搜尋 + test 段一次性驗證）後，只寫入 `meta.pending_strategy_candidate` 並發一則 Telegram 摘要，實際套用交給人工另外執行的 `scripts/apply_strategy_candidate.py`（`--reject` 可以捨棄待審候選）。

**`apply_strategy_candidate.py` 必須透過 `.github/workflows/apply_strategy_candidate.yml`（`workflow_dispatch`）執行，不能在本機終端機直接跑。** 這是 2026-09-05 第二輪 `/code-review` 抓到的缺口：正式生效、`run_ranking_optimize.py`/`run_daily_recommendations.py` 實際讀寫的 `backtest.db`，只活在 GitHub Actions 的 `backtest-db-state` cache 裡，不是一個本機連得到的伺服器或共用檔案。`db.connect()` 沒給路徑時預設開一個本機的獨立 sqlite 檔——在自己電腦上跑這支腳本，寫的是一份跟排程無關的全新資料庫，`已套用候選 ...` 會照常印出來、看起來成功了，但套用結果永遠不會被任何排程讀到，而且沒有任何錯誤訊息。新增的 workflow 用同一套 `backtest-db-cache` composite action 還原/寫回同一份 cache lineage，讓「人工核准」這個動作真的能落到排程實際在用的那份資料庫上。

**兩條排程管線（既有的 RANKING_WEIGHTS 自動優化、這次新增的策略邏輯搜尋）共用同一組 `meta.active_ranking_weights`/`previous_ranking_weights` key**，而不是切成兩組獨立 key，並新增 `meta.active_weights_source`（`"strategy_search"` 或 `"ranking_optimize"`）協調兩者：

- `scripts/apply_strategy_candidate.py` 套用時把 `active_weights_source` 設成 `"strategy_search"`。
- `scripts/run_ranking_optimize.py` 的 `build_candidates()` 用 `src/optimize.py` 的純函式 `previous_candidate_eligible(active_weights_source, has_previous)` 決定該輪要不要把 `previous_ranking_weights` 納入候選——`active_weights_source == "strategy_search"` 時該輪跳過（`ic_suggested` 照常比較，沒有被關掉）。
- `scripts/run_ranking_optimize.py` 的 `apply_action()` 每次跑完這個週期就無條件把 `active_weights_source` 寫回 `"ranking_optimize"`（`adopt_suggested`/`rollback_to_previous`/`keep` 三種結果都一樣），解除保護——只擋這一輪，不會因為連續幾輪都是 `keep` 就一直卡著不解除。

## 為什麼這樣分界（人工審核 vs. ADR 0002 的自動套用）

ADR 0002 的理由是「DSR/PBO 已經是把主觀判斷規則化的機制，人在裡面扮演的角色是『執行結論』，把執行結論自動化不算拿掉框架真正依賴的判斷力」——但那個結論成立的前提是**因子本身的訊號早就被驗證過**：Phase 4 的 rank-IC/ICIR 顯著性檢定已經確認 revenue/roe/momentum 三個因子有真訊號，`RANKING_WEIGHTS` 的搜尋空間只是這三個數字的比例，沒有額外需要人工判斷的自由度。

`ma_cross`/`vol_price_div` 不是這種情況：這是全新、從未在這個系統裡跑過的條件型態，沒有對應的 Phase 4 式事前驗證。DSR/PBO/高原寬度門檻能防的是「訓練集雜訊被誤認成訊號」，防不了「這個邏輯本身的公式假設有沒有問題」（例如量價背離公式的分段設計本身就是人工判斷過的產物，不是搜尋出來的）。全新邏輯的風險等級不同，值得人工看過 train/test 兩段報告再決定，這點原則上跟 ADR 0002 並不衝突——ADR 0002 的自動套用/rollback 決策本身完全不變（不重新開放討論），這次只是把同一套風險判斷邏輯用在不同的搜尋空間上做出不同的分界。

## 為什麼共用 key + 一次性保護，而不是切成兩組獨立 key

兩條管線切成兩組完全獨立的 key（例如 `strategy_active_ranking_weights` 另開一組）表面上更乾淨、不會互相干擾，但有一個實質缺點：`scripts/run_ranking_optimize.py` 之後每次比較的「active」基準會跟真正上線生效的設定脫節——如果人工核准了一個策略搜尋候選，但 RANKING_WEIGHTS 管線仍然只認自己那組 key 裡的「active」，它會繼續拿一個已經不是真正上線設定的基準去做比較與 rollback 判斷，這條管線的自動化就失去意義。

共用同一組 key 保證「不管哪條管線改的，`active_ranking_weights` 永遠只有一個生效版本」，唯一要解決的副作用是本文件開頭那個未經設計的衝突：`build_candidates()` 原本「只要 `previous` 存在就納入候選」的判斷，會讓 RANKING_WEIGHTS 管線在人工核准後的下一輪把它當成候選、贏了就 `rollback_to_previous`，直接推翻人工決定。`active_weights_source` + `previous_candidate_eligible()` 這個一次性保護（只擋 `previous` 進候選池一輪，`ic_suggested` 照常評估，且保護會在 RANKING_WEIGHTS 管線跑完這一輪就無條件解除，不看這輪結果是 `keep` 還是真的換手）用最小改動解決這個副作用，不需要引入第二組 key、不需要改 `resolve_ranking_weights_action()`/門檻本身（2026-10-09 註：該函式後來多了 `candidate_scores` 參數與「勝者平均須高於 active」一條規則，見 ADR 0002 修訂記錄；與這裡的協調邏輯無關）。

## 考慮過但拒絕的方案

**兩組完全獨立的 meta key（`active_ranking_weights` 給 RANKING_WEIGHTS 管線，另開一組給策略搜尋）。** 見上一節——拒絕理由是會讓 RANKING_WEIGHTS 管線的比較基準跟真正上線的設定脫節。

**策略搜尋也採 ADR 0002 式自動套用+rollback。** 使用者已確認拒絕：全新未驗證邏輯的風險值得人工看過，這點跟 RANKING_WEIGHTS 純粹調整既有已驗證因子的權重比例不同。

**用一把全域鎖（例如一個 boolean flag）完全關掉 RANKING_WEIGHTS 管線的 rollback，直到人工再次介入解除。** 比一次性保護更保守，但如果沒人記得手動解除，RANKING_WEIGHTS 管線的自動化就會被永久卡住，且需要額外的人工步驟才能恢復——`previous_candidate_eligible()` 的一次性設計（保護在 RANKING_WEIGHTS 管線跑完下一輪就自動解除，不用等它真的換手）不需要這個額外步驟。

## 影響

- `src/finlab_factors.py`：新增 `ma_cross_signal()`/`volume_price_divergence()`/`EXTRA_FACTOR_FUNCS`；`factor_ranks()`/`composite_scores()` 新增可選的 `extra_factors` 參數（預設 `None`，行為與改動前完全一致）。
- `src/portfolio_backtest.py`：`PORTFOLIO_PARAMS`/`simulate()` 新增 `extra_factors` 透傳。
- `src/optimize.py`：新增 `train_test_split_by_calendar()`（策略搜尋的 train/test 切分）、`previous_candidate_eligible()`（本文件的協調邏輯）；`sweep_ranking_weights_walk_forward()` 新增 `extra_factors` 參數。`resolve_ranking_weights_action()`/`evaluate_decision()`（ADR 0002 的門檻本身）不變（2026-10-09 註：`resolve_ranking_weights_action()` 之後新增 `candidate_scores` 參數與「勝者平均須高於 active」規則，見 ADR 0002 修訂記錄；`evaluate_decision()` 仍不變）。
- `src/db.py`：新增 `active_weights_source`、`pending_strategy_candidate` 兩個 `meta` key，通用 key-value 表，不需要 schema migration。
- 新增 `scripts/run_strategy_search.py`（排程用，只產生報告）、`scripts/apply_strategy_candidate.py`（人工執行套用/`--reject`，須透過下面的 workflow 執行）、`.github/workflows/strategy_search.yml`（雙週排程，20:00 UTC，跟 `optimize.yml`(22:00)/`daily_recommendations.yml`(23:00) 都錯開，共用 `backtest-db-state` concurrency group）、`.github/workflows/apply_strategy_candidate.yml`（`workflow_dispatch` 專用，人工到 Actions 頁面手動觸發，同樣共用 `backtest-db-state`）。
- `scripts/run_daily_recommendations.py`：`factor_ranks()`/`composite_scores()` 呼叫補上 `extra_factors=ff.EXTRA_FACTOR_FUNCS`，否則套用後的 `ma_cross`/`vol_price_div` 權重會靜默失效（`composite_scores()` 對沒被算過的 key 直接跳過，不報錯）。
- `scripts/run_ranking_optimize.py`：`build_candidates()` 讀 `active_weights_source`、改用 `previous_candidate_eligible()` 判斷要不要納入 `previous`；`apply_action()` 每輪跑完無條件把來源寫回 `"ranking_optimize"`；`sweep_ranking_weights_walk_forward()` 呼叫改用 `finlab_factors.used_extra_factors()`（只在候選真的用到新因子時才啟用，見下一條）。ADR 0002 記載的 DSR/PBO/高原門檻、`resolve_ranking_weights_action()` 本身不變（2026-10-09 註：此後多了「勝者平均須高於 active」一條，見 ADR 0002 修訂記錄）。
- 2026-09-29 追加（PBR 價值因子接入，`docs/factor_candidates_momentum_ic_2026-09.md` §4）：
  - `src/finlab_factors.py`：新增 `value_pbr_factor(entry, date)` 並登記進 `EXTRA_FACTOR_FUNCS`。PBR 是 point-in-time series 而非 bars 衍生值，所以 `factor_ranks()` 對帶 `needs_entry = True` 的因子改呼叫 `fn(universe_data entry, date)`；既有 `fn(bars, idx)` 因子與其測試合約不變。
  - `src/optimize.py`：`_slice_universe_by_date()` 現在會把 `value_series` 原樣帶進切片（只在來源有此鍵時），否則切片後 PBR 對每檔都是 None。
  - `scripts/run_strategy_search.py`：新增 `--pbr` 旗標，只比較 `baseline` 與 `with_pbr` 兩個候選，並把日曆限縮到 PBR 有資料的 2021-10 之後。刻意不併入預設的四候選：`used_extra_factors()` 給 sweep 的是各候選額外因子的聯集，而 `value_pbr` 在 2021-10 前對每檔都是 None，併入會讓 `ma_cross`/`vol_price_div` 的 pre-2021 歷史也被清空。不帶旗標時行為與追加前完全一致，`strategy_search.yml` 排程不受影響。
  - 首次結果：`--pbr --dry-run` 的 baseline 勝（平均樣本外 +35.8% 對 +13.5%），閘門 DSR 0.91 未過 0.95（註：sweep 的 DSR 是對 `point_best`——平均樣本外報酬最高者——計算的，這次是 baseline，並非 `with_pbr` 自己的 DSR；見 `docs/factor_candidates_momentum_ic_2026-09.md` §4），未產生待審候選（`--dry-run` 也不寫 `meta.pending_strategy_candidate`），所以沒有可供 `apply_strategy_candidate.py` 套用的 `value_pbr` 候選。（2026-10-04 補註：上述 DSR 的對象是改動前的算法；此後閘門 DSR 改為對高原勝者計算，這次勝者與 point_best 同為 baseline，數字不變。見 `docs/plan_dsr_target_plateau_winner_2026-10.md`。）
  - 正式端路徑驗證（2026-09-29，本機快取、`report_factor_ic.load_cached_universe()` 與 `run_daily_recommendations.py` 同一個載入函式）：權重含 `value_pbr` 時 `factor_ranks()` 仍排出 1028 檔（與 baseline 相同，可選宇宙沒有縮小），`factor_ranks` 帶有 `value_pbr` 鍵，前五名與 baseline 不同。
  - **套用前仍要確認的風險**：`update_universe_bars.py` 每週只重寫 `bars`，不更新 `per_pbr_series`，而 `_as_of()` 取最後一筆、不管多舊。本機快取當天 PBR 中位數只落後 6 天，但有 29 檔超過 30 天（最舊 1608 天）；雲端 `cache/factor_run` 的 PBR 序列有多新沒有驗證。真要套用前，要先決定 PBR 過期怎麼處理（週更、或超過 N 天視為 None）。
  - baseline 權重：`run_strategy_search.py` 本機跑時讀不到雲端的 `meta.active_ranking_weights`，fallback 為預設 0.4/0.3/0.3。核實方法：看 GitHub Actions 最近一次 `optimize.yml`（`run_ranking_optimize.py`）的 log 或 Telegram 訊息——`apply_action()` 的摘要行會印 `維持現況 active={...}`（或剛採用的 `ic_suggested`／rollback 的 `previous`），即目前雲端的 `active_ranking_weights`。**2026-10-02 結果**：最近三次 `optimize.yml`（9/2、9/16、10/2）都沒有這一行——雲端 `cache/factor_run` 還原失敗（`Cache not found for input keys: update-universe-bars-state-`），`run_ranking_optimize.py` 載入 0 檔就在連資料庫之前結束，沒有評估也沒有寫入；且 `apply_strategy_candidate.yml`、`apply_regime_weights_candidate.yml` 從未被執行過。該 key 沒有其他寫入路徑，故推論雲端 `active_ranking_weights` 從未被寫過、使用預設 0.4/0.3/0.3（推論，非直接讀值）。
- `src/finlab_factors.py`：新增 `used_extra_factors()`，避免 `run_daily_recommendations.py`／`run_ranking_optimize.py` 在候選權重根本沒用到新因子時，也被新因子「算不出來就排除」的篩選悄悄縮小可用股票池；`REQUIRED_FACTOR_KEYS` 供 `apply_strategy_candidate.py` 套用前驗證權重字典完整性。
