# 分層回測報告：月頻選股 ＋ breakout_strategy 進場濾網（§3.2）

2026-09-24。回應 `docs/factor_timeframe_optimization_directions.md` §3.2：月頻多因子排名選出 top-N 後，現況是「選到就在當天收盤買進」，這份報告測試「改成等 `breakout_strategy.py` 的技術＋籌碼訊號確認才進場」，選股邏輯本身完全不變，只測進場時機。

## 資料範圍

`cache/factor_run/`（全市場因子快取）的 `bars` 只有收盤價/成交量，沒有 `technical_signal()` 需要的完整 OHLC；`chip_signal()` 需要的逐日法人買賣超原始資料也從沒被快取過（§2.2 只存了聚合後的 `inst_net_5d`）。把範圍限縮到「回測期間（2012-05-02~2026-09-14，全部 173 個月頻 rebalance 日）曾經真的被選進 top-20 的股票」，結果是 **616 檔**（vs 全市場 1821 檔）。`scripts/fetch_breakout_layer_data.py` 補抓這 616 檔的完整 OHLC＋逐日法人資料到 `cache/breakout_layer/`，616 檔全部成功、零失敗。

## 方法

`scripts/report_breakout_entry_walkforward.py` 新增 `simulate_with_entry_gate()`：只有「真正新進場」的部位（原本沒持有、這次被選進 top-N）會被擋住等確認；已持有、這次續留 top-N 的部位照現況邏輯立即再平衡，不重新閘門。被擋住的候選股票**沒有強制過期**——只要還留在 top-N 就持續每日檢查，直到訊號確認或被下一次 rebalance 踢出候選名單為止（踢出就永遠不買這一輪）。

兩個刻意的簡化，寫在腳本 docstring：

1. **略過融資閘門**——`chip_signal()` 的 `margin_window` 永遠傳空（沒有另外抓融資資料），`chip_signal()` 本身的邏輯允許這麼做（缺融資資料不擋單，`margin_ok` 預設 True），所以這裡測的是「三大法人合力（≥2/3 淨買超）＋技術突破」的部分融合，不是 `entry_signal()` 在單股情境下的完整三閘門融合。
2. **成交價用調整後收盤，不用未調整開盤**——`breakout_strategy.py` 自己的既定慣例（`simulate_breakout()` docstring）是訊號日 D 確認、D+1 開盤成交（因為 `technical_signal(...,i)` 要用到當天收盤價/成交量/高低價，只有收盤後才確定），這份報告保留了這個 D/D+1 反 look-ahead 的時序邊界，但 D+1 當天用的成交價是跟現況比較基準同一條調整後收盤價序列，不是另外抓一條未調整開盤價——避免為了一個未採用的候選再多建一條調整後開盤價序列。

用跟 §3.3 同樣的 purged walk-forward + DSR/PBO 檢驗（`--folds 4`，anchored，purge_days=60），比較兩個候選：baseline（現況）vs gated（等確認）。

**2026-09-25 更正**：`/code-review` 抓到一個同日洗價交易的 bug——如果某檔股票在 D-1 日訊號確認、排進「D 日用當天價格成交」的佇列，而 D 日恰好又是月頻 rebalance 日、這檔股票的複合分數剛好掉出 top-N，原本的邏輯會先在 D 日買進（執行昨天確認的訂單）、緊接著又被當天的 rebalance 賣出邏輯賣掉——等於白繳一趟買賣手續費/滑價，部位淨額卻是零。已修正：rebalance 日當天，先確認新的 target_codes，取消任何即將被剔除股票的排隊訂單，再執行成交。下面的數字是修正後重跑的結果；修正前的版本 4 折平均報酬是 24.44%（vs 修正後 24.48%）、DSR=0.3906（vs 0.3880）——影響很小（全歷史範圍只有 3 筆洗價交易被消除），**結論方向完全不變**。

## 結果

```
fold 0: test=2015-03-06..2018-01-09   baseline=71.61   gated=21.91
fold 1: test=2018-01-10..2020-11-26   baseline=17.76   gated=27.57
fold 2: test=2020-11-27..2023-10-19   baseline=76.80   gated=12.81
fold 3: test=2023-10-20..2026-09-14   baseline=264.78  gated=35.62

OOS mean return%（4 折平均）：baseline=107.74   gated=24.48
per-fold winner counts: {baseline: 3, gated: 1}（75% plateau）
PBO=0.33（門檻 0.5，通過）
DSR（winner=baseline，n_trials=8, n_obs=4）=0.39（門檻 0.95，差很多，不是邊緣案例）
```

全歷史（2012-05-02~2026-09-14，非 walk-forward，僅供參考量級）：baseline 總報酬 1210.6%（CAGR 19.6%，MDD -52.8%，Sharpe 0.87）vs gated 總報酬 137.6%（CAGR 6.2%，MDD -13.2%，Sharpe 0.88）——gated 版本 428 筆買入交易裡只有 243 筆是真的等到訊號確認才買，其餘 185 筆是既有持股續留 top-N 時的再平衡（不受閘門影響）。

## 判斷

**明確不建議採用**——這次不是像 value/pbr、accrual_quality、rebalance_freq 那樣「差一點點沒過門檻」，DSR=0.39 遠低於 0.95，方向也不含糊：4 折裡 3 折 baseline 完勝，gated 版本平均報酬只有 baseline 的約 1/4。

機制很直觀：等訊號確認會讓很多被多因子排名選中的候選**永遠等不到技術突破+籌碼合力同時成立**，資金因此閒置在現金裡（或錯過那些不靠短線爆量突破、而是靠基本面/動能穩定上漲的候選）——這跟前面 `lot_size=1000` 整張限制下資金閒置拖累報酬（`docs/regime_switch_vs_0050.md`）是同一類機制：**選股訊號的價值來自「選到」這件事本身，用短線技術面再加一道濾網去卡「進場時機」，反而把選股訊號的效益過濾掉了大半**。gated 版本的最大回撤明顯較小（-13.2% vs -52.8%），Sharpe 幾乎打平（0.88 vs 0.87），所以它不是「爛策略」——它比較接近一個低曝險、保守的版本，只是**絕對報酬**這個本專案既定的比較口徑（`docs/regime_switch_vs_0050.md`「只看總報酬，MDD 附帶參考」）下，明顯劣於現況。

**結論：維持現況（選到就買），不採用進場濾門。** 這也連帶回答了 §3.2 原文的問題：「月頻決定買哪些、週頻/日頻決定什麼時候買」這個分層構想，在「用 breakout_strategy 當濾網」這個具體實作方式下不成立；如果之後想再試這個方向，換一個不會讓大量候選永遠不成交的濾網設計（例如给一個固定的最長等待窗、逾期就照現況買，而不是無限期等待）可能是更合理的下一步，但不在本次範圍內延伸。

## 重現方式

```
python scripts/fetch_breakout_layer_data.py          # 補資料，616 檔，約 1-2 小時（FinMind 速率限制）
python scripts/report_breakout_entry_walkforward.py --folds 4
```
