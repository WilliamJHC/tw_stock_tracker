# 多因子下一輪：參數預先登記（pre-registration）

2026-10-14 起草。**狀態：草案，尚未鎖定。** 鎖定條件見 §8；鎖定之前不得跑本文件的比賽。鎖定之後候選、權重、閘門不改；要改就另開一份登記、n_trials 重算。

來源：`docs/factor_candidates_high250_2026-10.md` §5.4——「等到有新資料再以 pre-registered 設計重跑，候選至少含 baseline／no_momentum／high250，閘門以現行換倉日為準；在此之前不要在同一段訓練資料上再加候選或調整權重」。同一種格式的姊妹文件：`docs/pullback_backtest_preregistration_2026-10.md`。

## 1. 要回答的問題

只有一個：**用 high250（收盤 ÷ 近 250 根最高收盤）取代 60 日動能的 0.3 權重，在沒被看過的新資料上，是否仍優於現行權重，且不只是「拿掉動能」的效果？**

不回答：其他新因子（應計品質、低波動等）、換倉頻率、sector-neutral。

## 2. 固定的東西

### 2.1 候選（共 3 個，權重事先定死，不調）

設 `A` = 執行當下的 `active_ranking_weights`（沒被寫過時為程式預設 revenue 0.4／roe 0.3／momentum 0.3）。

| 候選 | 權重 |
|---|---|
| `baseline` | `A` 原樣 |
| `no_momentum` | `A` 去掉 momentum（其餘權重不重新縮放，同 `report_high250_followups.py` 的 `_with_no_momentum()`） |
| `high250` | `A` 的 momentum 權重改給 high250（`build_high250_candidates()` 的 `high250`） |

- **不含** `mom_high250_half`：上一輪它輸給 high250（平均 +91.9% vs +103.2%），不再列為候選。
- 若 10/16 之後雲端 `optimize.yml` 第一次真的跑完並自動換了權重，`A` 會變；候選照上面規則重新推導，**鎖定時把當時 `A` 的實際數字填在 §8**。若 `A` 已含 high250，`build_high250_candidates()` 會直接報錯，此登記作廢。

### 2.2 資料與切分

- 資料截止 D0 = **2026-09-14**（本機快取最後一日；雲端播種完成後以雲端快取為準，鎖定時填入）。上一輪的訓練資料（2012-05-02 ~ 2026-01-13）已被看過，上一輪的 held-out（2026-03-16 起）沒被 high250 候選使用過。
- **執行門檻**：快取最後一日 ≥ **2027-03-15**。屆時 `train_test_split_by_calendar()` 預設 `test_months=6` 切出的 test 段 ≈ 2026-09-15 ~ 2027-03-14，正好是 D0 之後的新資料；訓練段（涵蓋上一輪 held-out 的大部分；test 段前有 60 天 purge 間隔，最後約 2 個月不在訓練段）給 walk-forward 用。
- 未達門檻不跑，由 §2.3 第 4 點在程式裡強制（`--dry-run` 也一樣，因為 dry-run 通過閘門時會算 test 段）。
- 換倉日：月內第 1 個交易日（現行引擎）。成本：引擎內建，不另調。
- 流動性、250 根歷史過濾：同上一輪（所有候選共用 `ff.used_extra_factors()` 的聯集過濾）。

### 2.3 需要的程式改動（鎖定前完成、不得動到評估邏輯）

`run_strategy_search.py` 目前沒有「baseline／no_momentum／high250」這組候選（`no_momentum` 只存在 `report_high250_followups.py`），且現有流程只要 `decision.accept` 且勝者≠baseline 就會算 test 段、寫 `meta.pending_strategy_candidate`、發 Telegram，test 段也只比「勝者 vs baseline」。所以新增模式（暫名 `--round2`）要做到下列四件事，**除此之外不動評估邏輯**，並補單元測試：

1. 組出 §2.1 的三個候選。
2. **先判標準 1～3，三項全過才計算 test 段**；任一不過就結案——不算 test 段、不印 test 數字、不寫 `meta.pending_strategy_candidate`、不發通知（只印訓練段結果）。
3. test 段額外模擬 `no_momentum`，供標準 4 比較。
4. **執行門檻硬編進程式**：快取最後一日 < 2027-03-15 就拒絕執行（含 `--dry-run`），不靠自律。

## 3. 事後加入的東西（必須揭露）

- `no_momentum` 是上一輪看過結果之後才加的對照（high250 報告 §4.2）；high250 這個想法本身來自創意（3443）的漏選診斷與 §2.2 的規則檢驗，兩者都在上一輪的訓練資料上看到。所以：**訓練段的 walk-forward、DSR、PBO 對這個想法不是乾淨的樣本外**，它們只決定「有沒有資格進 test 段」；真正乾淨的證據只有 D0 之後的 test 段。
- 上一輪已為此想法付出的試驗次數（3 個候選 × 4 個 fold、再加 4 個換倉相位與 1 個對照）沒有累積進這一輪的 DSR；本輪 n_trials 只算本輪的 3 個候選 × fold 數（由引擎計算，不手填）。這是低估，報告時要寫出來。
- 本輪沒有其他事後調整；10/16 雲端 `optimize.yml` 若真的執行，它掃描的是 revenue／roe／momentum 三因子權重網格，不含 high250，但會讀同一份快取。

## 4. 成功標準（全部成立才算通過；通過也只是產生「待審候選」）

| # | 標準 | 判定 |
|---|---|---|
| 1 | 訓練段閘門（沿用 `optimize.py`，不放寬） | 高原勝者為 `high250`；DSR（對高原勝者自己算）≥ 0.95；PBO ≤ 0.5；高原寬度 ≥ 0.2 |
| 2 | 贏過現行 | `high250` 訓練段 fold 平均樣本外報酬**嚴格高於** `baseline`（ADR 0002 修訂的「須贏 active」規則） |
| 3 | 不只是拿掉動能 | `high250` 訓練段 fold 平均樣本外報酬**嚴格高於** `no_momentum` |
| 4 | test 段（只用一次） | test 段累計報酬（`_test_window_summary()` 的總報酬）：`high250` > `baseline`，且 `high250` ≥ `no_momentum` |

- test 段只有約 6 次換倉，勝負可能只差一次換倉：**標準 4 是 sanity check，不是證據**，這點要寫進結果報告。
- 標準 3 用「嚴格高於」、標準 4 用「不低於」是刻意的不對稱：訓練段是篩選閘門，要嚴；test 段 6 次換倉太少，不讓平手或單次換倉的雜訊否決整輪。
- 標準 1 要求高原勝者是 `high250`。若勝者是 `baseline` 或 `no_momentum` → 結案（見 §5），不是只看 `high250`。
- 標準 1 的 DSR 上一輪是 0.923（現行換倉日），差 0.027 沒過；本輪重新計算，不沿用。

## 5. 結果怎麼處置

- **全部通過**：依 ADR 0003 產生 `meta.pending_strategy_candidate`，**人工審核後才套用**，不自動換權重。審核時要同時看這份登記與結果。
- **標準 1～3 任一不過（含高原勝者是 `baseline` 或 `no_momentum`）**：結案。`high250` 與 `no_momentum` 都不採用，現行權重不變；test 段不揭露。**不得**在同一段資料上加候選、調權重、換換倉相位再跑。
- **標準 1～3 過、4 不過**：不產生待審候選，結果照實記錄；test 段此時已用掉，要再試需等更多新資料並開新登記。
- 不論結果，在 `docs/` 補一份結果記錄（格式同 `research_round_closeout_2026-09.md`）。

## 6. 已知限制

1. 樣本外只有 6 個月、約 6 次換倉，標準 4 的檢定力很低（見 §4）。
2. 現有名單（生存者偏差）、未含息、無產業中立，同上一輪。
3. 沒做多重檢定校正（見 §3 對 n_trials 的說明）。
4. 若執行時 `A` 已被自動換過，結論只對那組 `A` 成立。

## 7. 執行

```powershell
# 從專案根目錄；--round2 待實作（§2.3）。先 --dry-run 只能在 §2.2 的執行門檻達成之後
.venv\Scripts\python scripts\run_strategy_search.py --round2 --dry-run
```

## 8. 鎖定

鎖定 = 下列都完成，並在一個 commit 內補上空白處：

- [ ] 使用者已確認或修改 §2.1 候選、§2.2 執行門檻（2027-03-15）與 §4 閘門
- [ ] §2.3 的 `--round2` 模式已實作並通過測試
- [ ] 鎖定時的 `A`（active_ranking_weights）：______；資料截止 D0：______
- [ ] 鎖定時 `src/optimize.py` 的 `PBO_MAX`、`DSR_MIN_SIGNIFICANT`、`PLATEAU_WIDTH_MIN_FRAC` 為 0.5／0.95／0.2（若不同，以鎖定當下為準並填入：______）

鎖定日期：______
