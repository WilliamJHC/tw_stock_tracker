# 0007. 強勢股回調買法作為獨立的第二種選股方式

## 狀態

已接受（2026-10-13）

2026-10-24 註記：日線代理回測未通過（`docs/pullback_backtest_result_2026-10.md`），排程與盤中輪詢已停用，程式保留；本 ADR 的架構決定（獨立於多因子）仍然成立，但這條選股方式目前不啟用。

## 背景

使用者要求把一套「強勢股回調買法」（篩近 1/3/6 月強勢股 → 整理形態 → 支撐區 → 分鐘線觸發 → 先定停損再算股數 → 3R/5R 分批）設為**除了多因子以外的另一種方式**。專案現有兩個選股／擇時引擎：

- 多因子排名（`finlab_factors.py`，月頻全市場 top-N，權重由 `optimize.yml` 自動套用，ADR 0002/0003）
- 短線突破（`breakout_strategy.py`，2026-09-01 決定「新策略完全獨立、不擴充既有引擎」）

## 決策

1. 新增 `src/pullback_strategy.py`，與多因子、`analysis.analyze()`、`breakout_strategy.py` **完全獨立**，沿用 2026-09-01 的「新策略獨立」原則。
2. 結果寫進 `meta.pullback_watchlist`，與 `meta.daily_recommendations` 並列；UI 是推薦股票下方的獨立面板，不合併排序、不混進多因子分數。
3. **不接 `optimize.yml`、不自動調參**。所有門檻寫死在 `PULLBACK_PARAMS`，改動走人工。
4. 範圍比照 ADR 0003：唯讀展示＋加入追蹤＋Telegram 提醒；不下單、不記錄部位。帳戶規模只存在使用者瀏覽器或本機 `.env`，不進雲端。
5. 全市場只用既有的收盤價快取做第一階段篩選，第二階段最多 120 檔抓新鮮日線——不違反 ADR 0003「不每日全量重抓全市場」。

## 考慮過但拒絕的方案

**把回調條件做成多因子的一個新因子（例如「整理收斂度」）丟進 `EXTRA_FACTOR_FUNCS`。** 這套方法的核心是「等價格回到某個價位、盤中轉強才進場」，是事件觸發、不是月頻排名；硬塞成因子會失去支撐區與觸發這兩個關鍵步驟，而且每多一個候選因子都要付 DSR 試驗次數的代價（`docs/research_round_closeout_2026-09.md`）。

**擴充 `breakout_strategy.py`。** 兩者進場邏輯相反（突破追價 vs. 回測支撐），共用參數表只會讓兩邊都難調。

**先回測再上線。** 因子快取沒有 o/h/l，歷史上算不出振幅、缺口與 AVWAP，要先改快取格式才能回測，工程量與這次不同級。決定先上線為「標示未驗證的篩選＋計畫工具」，回測列為下一步（`docs/pullback_strategy.md` §6）。

## 影響

- 新增：`src/pullback_strategy.py`、`scripts/run_pullback_scan.py`、`docs/pullback_strategy.md`、本 ADR、`tests/test_pullback_strategy.py`、`tests/test_run_pullback_scan.py`。
- 修改：`src/data_sources.py`（`get_industries()`）、`src/pipeline.py`（`build_pullback_watchlist()`、`load_pullback_plans()`）、`src/app.py`（`GET /api/pullback`）、`src/snapshot.py`（full/light 都帶 `pullback`）、`src/monitor.py`（`pullback_alerts()`，只在不帶參數執行時自動載入）、`static/index.html`（新面板）、`.github/workflows/daily_recommendations.yml`（第二個 step）、`.env.example`、README、CONTEXT.md。
- 多因子管線、`optimize.yml`、`active_ranking_weights` 都沒有變。
