# 0003. 多因子選股管線從純內部研究升級為對外服務的一部分

## 狀態

已接受（2026-08-24，`docs/dev_plan_recommendations.md` grill-me 收斂）

## 背景

`docs/adr/0002-auto-apply-ranking-weights.md` 於 2026-08-24 定案時，背景段與「考慮過但拒絕的方案」都明確假設 Phase 6 這條多因子選股管線是純內部研究工具：「這條管線目前又是純內部研究管線（見 lighthouse.md Phase 6 範圍界定，`portfolio_backtest.py` 沒有被 `app.py` 引用過）」。

`docs/https-finlab-finance-blog-python-get-tai-memoized-lighthouse.md` 第 321 行「明確不做」表格更直接地拒絕過這個方向：

| 項目 | 理由 | 替代 |
|---|---|---|
| Phase 6 順便幫多因子引擎補選股清單出口 | `portfolio_backtest.py` 至今沒被 `app.py` 引用過一次，範圍會擴大到 UI/報告設計，是規模不同的另一個決定 | 維持純內部研究管線，出口留給未來單獨評估 |

`docs/dev_plan_recommendations.md`（v1.0，2026-08-24，同一天透過 `/grill-me` 收斂）就是這句「留給未來單獨評估」所指的那次評估：把 `finlab_factors.py` 既有的全市場選股結果（`composite_scores()` + `active_ranking_weights`，Phase 6d 自動套用的同一條管線）接進 `app.py`／`snapshot.py`，讓使用者在首頁看到、並可一鍵加入追蹤清單。Phase 0-3 的實作（見「影響」）已經把這個決定落地，兩處假設從這個時間點起不再成立，需要一份新記錄取代，而不是放著讓後人以為還是內部研究管線。

## 決策

接受選股結果進入 UI，範圍限定於**唯讀展示 top 5 ＋ 加入追蹤**：

- `GET /api/recommendations`（即時模式）與 `snapshot.py` `run_full()` 烘進 `data.json` 的 `recommendations` 欄位（靜態模式）唯讀輸出全市場 top-5 推薦卡片：代碼、名稱、即時報價、`target_price`（ADR 0001）、三因子細項（營收年增/ROE/動能 rank）、是否已追蹤。
- 使用者手動按「加入追蹤」才會呼叫既有的 `POST /api/watchlist/{code}` 寫入 `watchlist.json`；不自動下單、不自動調整追蹤清單、不做自動停損停利、不寫任何交易紀錄。
- 資料新鮮度誠實揭露：Phase 0 的每週增量更新（`cache/factor_run/*.json`）與 Phase 1 的每日重排名各自把 `as_of`（資料基準日）跟 `generated_at`（排程實際執行時間）分開記，UI 只顯示 `as_of`，不讓使用者誤以為是當下重新計算的即時排名。

## 為什麼現在可以

- 這是一次獨立的 `/grill-me` 決策過程（`docs/dev_plan_recommendations.md` 開頭：「版本：v1.0（2026-08-24，grill-me 收斂）」，第 3 節「範圍決策摘要」逐條列出十項決定），不是在做其他 Phase 時順手夾帶擴權。`docs/adr/0002` 拒絕「自動套用要不要人工審核」以外的擴權時警惕的正是「趁機夾帶」；這次是先把範圍決策攤開問清楚、再動工，性質不同。
- `lighthouse.md:321` 當時拒絕的具體理由是「範圍會擴大到 UI/報告設計，是規模不同的另一個決定」——不是永久禁止，是判定「現在」不是討論這件事的時機，要留給獨立評估。`docs/dev_plan_recommendations.md` 就是那次獨立評估，而且做完後確實產生了一整組新檔案（Phase 0-3，見「影響」），印證了當初「規模不同、值得獨立處理」的判斷是對的，不是憑空推翻。
- Phase 0／Phase 1 建立的「新鮮度誠實標示」機制（`as_of` 與 `generated_at` 分開記，UI 只顯示前者）解決了把研究管線端上 UI 最大的隱患——假裝這是即時資料。資料流的誠實揭露先做好，才把它端上使用者看得到的頁面，這是「唯讀展示」被判定現在安全的前提，不是繞過這個問題。

## 考慮過但拒絕的方案

**改用 `finlab_strategy.py` 的 finlab 版 `ranking_position()`。**
`docs/dev_plan_recommendations.md`「已查證事實」表已確認官方選股邏輯是 `finlab_factors.py` 的 `composite_scores()`（Phase 6d 自動套用 `active_ranking_weights` 生效），不是 `finlab_strategy.py`。後者需要額外的 finlab 登入依賴，也拉入 pandas/numpy，與專案 stdlib-only 政策衝突，沒有理由為了端上 UI 換一條血統不同、需要額外依賴的選股邏輯。

**只從使用者已追蹤的股票子集裡挑推薦，不做全市場排名。**
`docs/dev_plan_recommendations.md` 範圍決策第 1 條明確排除。子集版推薦違背 `finlab_factors.py` 全市場排名的設計初衷，也失去「幫使用者發現原本沒在追蹤的股票」這個產品價值——若只從既有追蹤清單裡選，等於只是把現有表格重新排序一次，不是真正的選股功能。

**放棄每週增量、改成每日對全市場重新全量抓取一次。**
`lighthouse.md` 第 322 行「明確不做」已經拒絕過同一件事：Phase 4 實測全市場全量抓取約 19-22 小時，`stock_tracker` 是 private repo，GitHub Actions 免費額度撐不住每月重複這個量級，遑論每日。本次沒有出現新事實推翻這個結論，維持 Phase 0 的「每週增量 + `as_of` 誠實標示」設計，不重新開放討論。

**後端多回傳一個「現價是否已越過目標價」的 `valid: bool` 欄位。**
`docs/adr/0001-target-price-ownership.md` 已經拒絕過：判斷「是否突破」只需要比較同一份 payload 裡已有的 `target_price.level` 與現價，不需要後端才知道的額外領域知識。Phase 2／Phase 3 的推薦卡片與追蹤清單標籤都沿用這個決定，沒有重新開放討論。

## 影響

Phase 0-3 實際新增/修改的檔案（`git diff --stat ca40a3e..a73b63d`）：

- **Phase 0（全市場日線增量更新，每週）**：新增 `scripts/update_universe_bars.py`、`.github/workflows/update_universe_bars_part1.yml`、`.github/workflows/update_universe_bars_part2.yml`；修改 `src/finlab_factors.py`（抽出 `factor_ranks()`）、`.github/workflows/optimize.yml`（拆分 cache 血統，見該檔案內註解）。
- **Phase 1（每日推薦排名批次腳本）**：新增 `scripts/run_daily_recommendations.py`、`.github/workflows/daily_recommendations.yml`。
- **Phase 2（後端 API／snapshot 串接 + `target_price` 實作 ADR 0001）**：修改 `src/analysis.py`（新增 `target_price_for()` 與 `analyze()` 的 `target_price` 欄位）、`src/app.py`（新增 `GET /api/recommendations`）、`src/pipeline.py`（新增 `build_recommendations()`，`app.py`／`snapshot.py` 共用）、`src/snapshot.py`（`run_full()`／`run_light()` 烘入並刷新 `recommendations`）；對應測試 `tests/test_analysis.py`、`tests/test_app.py`（新檔）、`tests/test_pipeline.py`、`tests/test_snapshot.py`。
- **Phase 3（前端 UI 改版）**：修改 `static/index.html`——移除「今日決策面板」，新增「推薦股票」卡片區塊與追蹤清單表格的「標籤」欄。

本 ADR（Phase 4）：新增 `docs/adr/0003-recommendation-pipeline-user-facing-scope.md`（本檔），記錄上述範圍升級本身；不含程式碼變更。

`docs/adr/0002` 與 `docs/https-finlab-finance-blog-python-get-tai-memoized-lighthouse.md` 兩份文件的背景段/表格描述的「純內部研究管線」現況，從本 ADR 接受之日起不再準確，後續讀者應以本 ADR 為準；兩份文件本身不回頭修改（保留當時決策的歷史脈絡，符合本專案「已知落差不要假裝已解決」的既有作法）。
