# 盤勢（多頭/空頭/盤整）分策略研究

2026-09-06，`scripts/report_regime_factor_ic.py` 實際跑出的結果與結論。方法論、數字、限制、建議都在這份文件，是 `docs/adr/0004-regime-adaptive-strategy.md` 決策的依據，也是 Phase C 接進 `RANKING_WEIGHTS`/`STRATEGY_PARAMS` 時要遵守的證據邊界。

## 方法論

- **盤勢判斷**：`src/market_regime.py`，大盤（TAIEX）收盤價 vs MA20 vs MA60 排列 + MA20 斜率，三態多頭/空頭/盤整，細節見該模組 docstring。
- **樣本區間**：`data_sources.get_index_daily("TAIEX")`，FinMind 來源，2021-11-03 ~ 2026-09-04（5年，1234 根K棒，扣掉前 59 天暖機後可分類 1175 天）。
- **分桶方式**：
  - 因子 IC 分桶：`factor_eval.evaluate_factors_by_regime()`，把 `finlab_factors` 全市場快取（`cache/factor_run/*.json`，1721 檔）的月度再平衡日期，用「as of」方式對應到 TAIEX 當時的**原始（未防抖）**盤勢分類，各桶各自跑既有 `evaluate_factors()`。
  - 訊號勝率分桶：`backtest.signal_win_rates_by_regime()`，對 watchlist 26 檔中已匯入 `data/backtest.db`（實際查出來的路徑是 `src/data/backtest.db`，見下方「順便發現的問題」）的 23 檔個股，逐檔算 `an.BACKTEST_SIGNALS` 各訊號在各盤勢下的勝率，再依樣本數加權池化成一組全市場統計（見 `scripts/report_regime_factor_ic.py`的 `_pool_signal_stats()`，加權平均在數學上等於把所有個股的交易攤平成一個列表算平均，不是近似）。23 檔中有 1 檔（2330）因 `adj_factor` 只算過一部分被 `_daily_from_db()` 主動跳過（見 `run_backtest.py._adjusted_bars()` 的既有保護邏輯），實際進池化統計的是 22 檔。
- **防抖 `confirm_days`**：用預設 3 天跑 `effective_regime_series()` 只是為了對照原始分類跳動頻率（55 次原始切換 → 3 天確認後剩 23 次，約每月一次），Phase B 的統計本身用**原始**分類，見 `src/market_regime.py` 文件字串說明的取捨。`confirm_days=3` 這個初始值沒有理由再調整。

## 因子 IC 分盤勢結果（RANKING_WEIGHTS 候選的依據）

`h=20`（本專案的標準持有期）：

| 盤勢 | 樣本再平衡日數 | momentum ICIR | revenue ICIR | roe ICIR |
|---|---|---|---|---|
| 多頭 | 21 | -0.069 | +0.113 | **+0.379** |
| 空頭 | 2 | -0.329 | -0.340 | +2.257 |
| 盤整 | 22 | -0.134 | +0.060 | **+0.674** |

完整 h=5/20/60 數字見 `scripts/report_regime_factor_ic.py` 的輸出（跑一次即可重現，未寫死存檔）。

### 結論：**證據不足以現在套用分盤勢的 RANKING_WEIGHTS**

- **空頭桶 n=2**（h=20 只剩 n=2 個再平衡日）——這是「兩個資料點」，不是統計意義上的樣本，roe ICIR=+2.257 完全是雜訊，不能當成任何依據。本專案的 walk-forward/PBO/DSR 框架（`optimize.py`）之所以存在，就是為了防止把這種雜訊當訊號套用；套用一組只靠 2 個資料點推出來的權重，違背了 `docs/adr/0002-auto-apply-ranking-weights.md` 一貫的紀律。
- **多頭/盤整桶 n=21/22**，比空頭好但仍薄——本專案既有的 walk-forward 搜尋通常會切多個 fold 交叉驗證，21-22 個月度樣本點分不出更細的 fold。
- 三個桶有一個共同、值得記錄但不能拿來馬上動手的觀察：**momentum 在這個樣本窗格的 ICIR 在三種盤勢都是負的，roe 都是正的且相對最穩定**——如果這個模式在更長歷史下仍然成立，代表「多頭該偏動能」這個直覺（一般金融教科書的說法）在本專案的資料下不成立，可能是這 5 年台股由 revenue/roe 驅動的個股（如台積電系列）漲幅太集中，動能因子在月頻率上偏向均值回歸而非延續。**這只是一個假說，不是結論**，需要更長樣本或更高頻率的再平衡才能驗證。

**建議**：`meta.active_ranking_weights_by_regime` 暫不提案套用（Phase C 的 `scripts/propose_regime_weights.py`/`apply_regime_weights_candidate.py` 機制照樣建好，但不主動執行 propose）。等 `cache/factor_run` 累積更長歷史（尤其是需要更多空頭月份）後再重跑這份報告，才有資格產生候選。

## 訊號勝率分盤勢結果（STRATEGY_PARAMS 候選的依據）

樣本數遠大於因子 IC 側（池化 22 檔個股的逐日訊號，`h=20` 每個訊號 n 從 52 到 1631 不等，視訊號本身出現頻率而定），可信度足夠支撐方向性判斷。挑幾個關鍵訊號、`h=20`：

| 訊號 | 多頭 win_rate / avg_ret (n) | 空頭 win_rate / avg_ret (n) | 盤整 win_rate / avg_ret (n) |
|---|---|---|---|
| 均線多頭排列 | 58.6% / +6.51% (1344) | 56.2% / +1.33% (73) | 54.0% / +3.79% (1149) |
| 跌破MA60（破線續跌） | 54.4% / +4.39% (169) | **48.1% / -0.31%** (52) | 59.3% / +3.88% (398) |
| 爆量長紅（反彈訊號） | 57.9% / +9.97% (337) | **70.6% / +9.02%** (68) | 55.9% / +5.17% (331) |

### 結論：空頭盤勢下，「順勢做空/追跌」類訊號明顯轉弱，「反轉」類訊號明顯轉強

- **跌破MA60在空頭幾乎打平**（48.1% 勝率、avg_ret 幾乎是 0），是六個盤勢-訊號組合裡唯一一個 avg_ret 為負的——代表「趨勢延續」假設在空頭盤勢下對這個訊號不成立，個股一旦破線，在空頭市場裡續跌的機率沒有比隨機好。
- **爆量長紅在空頭是三個盤勢裡表現最好的一組**（70.6% 勝率，avg_ret +9.02%，僅次於多頭市場少數幾個訊號）——空頭market裡的反彈訊號比多頭/盤整都更可靠，符合「超跌反彈」的市場常識，但這裡是本專案自己的資料證實，不是引用外部說法。
- 多頭、盤整兩桶彼此的差異遠比空頭桶小（大部分訊號的 win_rate 落在 52-59% 之間），沒有清楚到值得為這兩者分別調整參數的地步。

**建議**（寫進 `src/backtest.py` 的 `STRATEGY_PARAMS_BY_REGIME`，見 Phase C）：
- **空頭**：`max_hold_days` 從 120 縮到 60（既然趨勢延續類訊號在空頭不可靠，拉長持有期只是讓虧損部位有更多時間繼續惡化）；`exit_composite` 從 -40 收緊到 -25（更早停損，因為破線續跌的期望值接近 0，沒有理由等到 -40 分才出場）。`atr_mult` 維持 100（不動）——目前的資料只驗證了「持有多久」「多早出場」這兩個維度，沒有針對 ATR 停損本身重新評估（那本身就是 `backtest.py` 現有註解點名要重做的「真正的移動停損」，超出這次的範圍）。
- **多頭、盤整**：維持現有 `STRATEGY_PARAMS` 不變——資料沒有清楚到值得偏離現狀的程度，硬要為每個盤勢都生一組不同參數，只是在雜訊上調整（呼應本專案一貫「不追逐單點最優，維持在高原中心」的立場）。

## 已知限制

1. **空頭因子 IC 樣本（n=2）完全不可用**，見上方——這是本研究最大的限制，直接決定了 Phase C 的範圍（只接 STRATEGY_PARAMS，不接 RANKING_WEIGHTS-by-regime 的實際候選）。
2. **盤勢定義本身是主觀的一組門檻**（`MA_SHORT=20`/`MA_MID=60`/`SLOPE_LOOKBACK=10`/`SLOPE_THRESHOLD=0.01`），換一組門檻，三態的邊界跟樣本分佈都會變。這次沒有做門檻敏感度分析（例如換成 ADX 或不同 MA 天數會不會得到差不多的分類），留給未來需要時再做。
3. **這 5 年的空頭段（2021-2022）在樣本裡偏短**——TAIEX 這幾年多數時間處於多頭或盤整，空頭天數（125/1175，約 11%）本來就少，不是分析方法的問題，是市場本身這段期間的特性。
4. **訊號勝率池化把 22 檔個股當成同質樣本**——沒有按產業、市值分層，一支占比重的權值股（如台積電系列，若在樣本裡反覆觸發某個訊號）可能主導某個桶的統計，這次沒有做逐檔貢獻度檢查。

## 順便發現的問題（不在本次範圍內，記錄供之後處理）

`src/db.py` 的 `BASE_DIR = os.path.dirname(os.path.abspath(__file__))`（只有一層 `dirname`），`DB_PATH` 因此指向 `src/data/backtest.db`，寫法跟 `data_sources.py`/`CACHE_DIR` 等其他模組用的 `dirname(dirname(...))` 兩層寫法不一致，第一眼看像 bug。**查證後確認是有意為之，不是問題**：`.github/actions/backtest-db-cache/action.yml` 明確把 `src/data/backtest.db` 這個路徑寫死在 GitHub Actions cache 設定裡（`path: src/data/backtest.db`），`apply_strategy_candidate.yml` 等四個 workflow 共用的正是這個 cache lineage——`db.py` 的路徑跟正式環境的 CI 設定是一致的。唯一美中不足的是 `CLAUDE.md`「資料...放專案根目錄」這條慣例沒有把這個例外寫進去，本機開發者（包含這次）第一次查會誤以為是 bug；建議之後找機會把這個例外補進 `CLAUDE.md`，不需要動程式碼。
