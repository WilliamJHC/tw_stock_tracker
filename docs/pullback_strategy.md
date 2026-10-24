# 強勢股回調買法（第二種選股方式）

2026-10-13 新增。多因子排名法（`finlab_factors.composite_scores()`，月頻、全市場 top-N）以外的另一種方式：**挑已經漲很強、正在整理的股票，等它回到支撐區、分鐘線轉強才進場，先定停損再算股數，3R/5R 分批出場。** 兩者並列，互不影響：多因子那條的權重、自動套用、回測流程都沒有動。

> **2026-10-24 收尾：排程與盤中輪詢已停用，程式與測試保留。** 每日掃描 step 在 `daily_recommendations.yml` 設 `if: false`；`monitor.py` 預設不再載入回調清單（要開設 `.env` 的 `PULLBACK_MONITOR=1`）；網頁警語與 Telegram 摘要都標明「回測未通過」。`meta.pullback_watchlist` 與 data.json 的 `pullback` 停在最後一次掃描的內容，不會更新。下面 §3 的排程與 §4 的盤中觸發說明描述的是停用前的設計。

> **回測未通過：平均 net_r = −0.045。** 2026-10-19 依預先登記（`docs/pullback_backtest_preregistration_2026-10.md`）用日線代理回測，去重後 4,005 筆，平均 net_r −0.045（95% 區間 [−0.142, +0.049]；通過門檻為 ≥ +0.10 且下界 > 0），2012–2019 與 2020 之後兩半都是負的；§4 五項只過樣本量與「勝過控制組」，結論為**不通過**。完整數字見 `docs/pullback_backtest_result_2026-10.md`。這份結果只針對日線代理，**盤中 5 分 K 觸發仍未驗證**；它是照一套公開描述的交易流程實作出來的篩選＋計畫工具，不是像多因子那樣通過 walk-forward／DSR 檢驗的策略。畫面（`static/index.html`）與 Telegram 摘要（`run_pullback_scan.py` 的 `summary_text()`）自 2026-10-24 起都標示「日線代理回測未通過」。

## 1. 方法 → 程式對照

| 原始規則 | 實作（`src/pullback_strategy.py`） | 參數（`PULLBACK_PARAMS`） |
|---|---|---|
| 選股器按近 1/3/6 個月漲幅排序 | `strength_screen()`：每檔以自己最新一根 K 為準，算 21/63/126 根報酬（用 `c_adj` 還原價） | `return_windows` |
| 優先看漲幅 >30%、排名前 20%–30% | 任一窗格「漲幅 >30% **且** 百分位 ≥ 前 30%」即入選；排名只在通過流動性的股票之間算 | `min_return_pct=30`、`top_rank_pct=0.30` |
| 剔除成交稀少的股票 | 20 日平均成交金額（原始收盤×量）> 5,000 萬。刻意比多因子的 1,000 萬嚴：進場看 1/5 分 K，需要盤中流動性 | `min_turnover` |
| 熱門題材、同板塊多檔一起走強 | `theme_strength()`：用 FinMind `TaiwanStockInfo` 的產業別（`data_sources.get_industries()`），標出同族群有幾檔一起入選。**只顯示、不過濾** | — |
| 先看周線再看日線的整理 | `find_base()`：近 60 根的最高點＝階段高點，之後至少 5 根才算「整理」；回檔深度 >30% 視為走壞 | `base_lookback`、`min_base_bars`、`max_base_depth_pct` |
| 回調低點逐漸抬高 | 整理區內最後兩個 swing low 遞增（不足兩個時：後半段低點 > 前半段） | `pivot_span=2` |
| 上下波動越來越小 | 最近 5 根平均振幅 ÷ 整理前半段平均振幅 ≤ 0.85 | `contraction_ratio_max` |
| 重新站上走平或向上的 9、21 日 EMA | 收盤 ≥ EMA21、近 5 根有收在 EMA9 上，且兩條 EMA 5 日變化 ≥ −0.5% | `ema_flat_tol_pct` |
| 高低點都下降、壓在下彎均線下 → 不符合 | `consolidation_checks()["downtrend"]`，直接排除 | — |
| 標出 9/21/50 EMA、前期高低點、未補缺口、AVWAP | `support_levels()`：EMA 三條、以**整理區階段高點**為錨的 AVWAP、之後被收盤站上的前高（舊壓變支撐）、整理區內的前低、未補的向上跳空缺口（取缺口上緣） | `level_lookback=120`、`max_level_distance_pct=12` |
| AVWAP、21 日 EMA、前高落在相近位置 → 重點盯 | `build_zone()`：相距 1.5% 內的價位合成一個區；優先選「AVWAP/EMA21/前高至少兩個重疊」中最高的那個區，其次任兩種價位重疊 | `cluster_pct` |
| 站上重要位置後的第一次回測 | `retest_state()`：最近一次收盤由下往上穿越支撐區上緣後、20 根內還沒回測過 → `first_retest` | `retest_lookback` |
| 股價接近支撐時切分鐘圖 | `zone_status()`：收盤距支撐上緣 ≤3% 或當日低點碰到 → `near_support`；跌破下緣 1% → `broken`；其餘 `extended` | `near_support_pct`、`break_tolerance_pct` |
| 下探後回升，突破前一根 K 線高點 | `intraday_trigger()`：分鐘 K 先碰到支撐區（下探），在最低那根之後，某根突破**前一根高點 + 1 檔** → `triggered` | `intraday_bar_minutes=5`、`touch_tolerance_pct` |
| 一路跌穿支撐沒有回升 → 繼續等 | 最新分鐘收盤跌破支撐區下緣 1% → `broken`，不發進場訊號 | — |
| 停損參考當天低點、突破 K 或前一根 K 低點 | 觸發時給兩個停損：`stop`＝min(觸發 K 低, 前一根低) − 1 檔；`day_low_stop`＝當日低 − 1 檔 | — |
| 多數停損距離 2.5%–3% 以內 | `stop_ok`：停損距離 ≤ 3%，超過就在卡片與警報標 ⚠ | `max_stop_pct` |
| 單筆風險約 0.5%；股數＝可虧金額 ÷ 每股風險 | `position_size()`：例 10,000 元、買 20、停損 19.5 → 100 股、投入 2,000（20%）。上限為帳戶本身（不融資）；可選整張 | `risk_pct=0.5`、`lot_size=1000` |
| 3R、5R 附近賣一部分 | `r_targets()`：價位對齊台股升降單位；另算**扣成本後的 R**（`net_r`） | `r_targets=(3,5)` |
| 剩餘部位看 9 日 EMA；急拉遠離均線可減碼 | `management_signal()`：已達 3R/5R、跌破 EMA9、收盤 > EMA9 + 3×ATR 各給建議文字 | `extension_atr_mult` |

## 2. 台股化的調整

- **升降單位**：所有支撐、停損、目標都用 `round_to_tick()` 對齊（10 元以下 0.01、50 元以下 0.05 … 1000 元以上 5）。停損向下取、觸發價向上取。
- **交易成本**：原方法的 R「均未計交易成本」。台股來回成本約 0.585%（手續費 0.1425%×2＋證交稅 0.3%），在 1–3% 的停損下等於吃掉 0.2–0.6R，所以另外顯示 `net_r`。券商折扣用 `fee_discount` 調。
- **漲跌停 10%**：觸發停損不保證成交在停損價（跳空、跌停鎖死），卡片與警報都提醒。
- **零股／整張**：畫面上的股數是零股精度，括號內顯示張數；`position_size(odd_lot=False)` 可改成整張。
- **還原價**：支撐、EMA、AVWAP 都在「今天的價格尺度」上算（`c_adj` 還原），所以數字可以直接拿來下單。

## 3. 資料流

```
cache/factor_run/*.json（收盤＋量，每週更新）
   └─ scripts/run_pullback_scan.py
        第一階段（不連網）：strength_screen → close_only_prefilter → 依強度排序，最多 120 檔
        第二階段：data_sources.get_daily()（每檔 1 個 FinMind 請求，與 UI 共用當日快取）
                  → 用快取的 c_adj/c 還原 OHLC → evaluate_setup()
   └─ meta.pullback_watchlist（backtest.db，與 meta.daily_recommendations 同一個 DB）
        ├─ GET /api/pullback（app.py，即時模式）
        ├─ data.json 的 "pullback"（snapshot.py，靜態雲端版；light 模式只刷新報價）
        └─ monitor.py：盤中把這些代碼一起輪詢，pullback_alerts() 發 Telegram
```

**為什麼要兩階段**：因子快取只有收盤價與成交量，算不出振幅、缺口、AVWAP；但全市場重抓 OHLC 太貴（見 ADR 0003「明確不做」）。所以先用快取篩到 ≤120 檔，再對這些抓新鮮日線。

**排程**：`.github/workflows/daily_recommendations.yml` 的第二個 step，台北週一到週五 07:00（開盤前）。`continue-on-error: true`，掃描失敗不會讓多因子那一步的結果存不進快取。

## 4. 怎麼用

```powershell
# 全市場掃描（需要 cache/factor_run；--dry-run 只印不寫）
.venv\Scripts\python scripts\run_pullback_scan.py --dry-run

# 只看追蹤清單或指定代碼（不寫 meta，不覆蓋排程結果）
.venv\Scripts\python scripts\run_pullback_scan.py --universe watchlist
.venv\Scripts\python scripts\run_pullback_scan.py --codes 2344,3081

# 盤中觸發：直接跑 monitor，會自動讀最新的 meta.pullback_watchlist
.venv\Scripts\python src\monitor.py
```

- **網頁**：首頁「推薦股票」下方新增「強勢回調（第二種選股法）」面板。右上角填帳戶規模與風險 %，卡片會算出股數；這兩個值只存在你自己的瀏覽器（localStorage），不會進 `data.json` 或雲端。
- **Telegram**：每天 07:00 一則清單摘要；開著 `monitor.py` 時，每檔每天最多三則：進入支撐區、觸發（含進場／停損／3R／5R，設了 `.env` 的 `PULLBACK_ACCOUNT_SIZE` 會附股數）、跌穿不進場。
- **本機 monitor 讀的是本機的 `src/data/backtest.db`**：雲端排程的結果存在 Actions 快取裡，不會同步回本機（跟 `meta.daily_recommendations` 一樣）。要在本機用盤中觸發，開盤前先在本機跑一次 `run_pullback_scan.py`（不加 `--dry-run`）；本機的 `cache/factor_run` 若很久沒更新，第一階段的強勢股名單會跟著舊。
- 盤中觸發用 5 分 K（MIS 每 30 秒輪詢一次，1 分 K 太粗）。要換 1 或 15 分，改 `PULLBACK_PARAMS["intraday_bar_minutes"]`。

## 5. 已知限制

1. **回測未通過（日線代理）。** 2026-10-19 平均 net_r = −0.045，未達預先登記的成功標準（見頂部說明與 `docs/pullback_backtest_result_2026-10.md`）。所有門檻（30%、前 30%、0.85 收斂比、1.5% 聚集、3% 接近）仍是照原方法的描述或合理預設、沒有調過；盤中觸發的績效仍不知道。
2. **還原只到因子快取的日期。** 快取最後一天之後若有除權息，那之前幾根 K 不會被還原。超過 ±11% 的跳動（分割、減資）會被偵測並**略過該檔**（例：6669 2026-09-01 的分割，實測時被擋下）；一般現金股利（2–5%）偵測不到，會讓支撐位小幅偏移，直到下次週更新。
3. **第一階段的報酬用週更新的快取**，最多落後 1–2 週。第二階段的日線是當天的，所以支撐區與狀態是新的，只有「誰算強勢股」可能晚一點反映。
4. **盤中觸發是估算級**：MIS 輪詢的成交價組出來的分鐘 K，不是逐筆，高低點可能漏掉瞬間價。觸發後請在券商軟體確認。
5. **只做多。** 空頭盤勢下這套方法的入選檔數自然會變少；目前沒有接 `market_regime`。
6. **進出場管理只給建議**（`management_signal()`），不記錄部位——ADR 0003 的「不寫任何交易紀錄」仍然有效。

## 6. 下一步：怎麼驗證

依本專案的規矩（`docs/research_round_closeout_2026-09.md`、high250 報告 §5），應先**預先登記**再回測，避免事後挑參數：

> 登記文件：`docs/pullback_backtest_preregistration_2026-10.md`（2026-10-19 鎖定並已執行，結果 `docs/pullback_backtest_result_2026-10.md`：不通過）。依登記 §5，不得調參數或換出場規則後當成同一份登記重跑；想再試要另開登記並累計試驗次數。下面三點是起草時的大綱，**日線代理的停損、出場與成功標準以登記文件為準**。

1. 因子快取補上 o/h/l（`daily_to_bars()` 目前只存 c/v），否則歷史上算不出振幅、缺口、AVWAP。
2. 事件研究：以日線代理盤中觸發（隔日「突破前一日高點」進場、停損＝前一日低點），固定本文件第 1 節的參數，統計 R 倍數分布、勝率、期望值（扣成本），並與同期 0050、多因子 top-N 比較。
3. 參數只在樣本外確認後才調；每多試一組都要算進 DSR 的試驗次數。
