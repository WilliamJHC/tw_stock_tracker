# 影子組合 ledger 資料格式（階段 1c，2026-11-17）

規格：`docs/shadow_portfolio_spec_2026-11.md`；計畫：`docs/shadow_portfolio_impl_plan_2026-11.md`。本檔定義 `shadow-ledger` 分支的檔案格式與狀態機 `step()` 的輸入輸出，是階段 2 的介面，**開工後改格式要回報**。

## 1. 通則
- 分支 `shadow-ledger`，**只追加**：已寫入的列不改、不刪（A5b-3、A5c-3）。檔案為 JSON Lines（一列一個 JSON 物件，UTF-8，鍵順序固定、數字不用科學記號），好 diff、好重放。
- 日期一律 `YYYY-MM-DD`（台北時間的交易日）；金額單位元（float，ledger 內保留到小數 4 位）。
- 候選代號：`S0_baseline`、`S1_high250`、`S2_high250_revday`、`S3_high250_cap`。
- 持股一律以**市值（元）**記，不記「股數 × 最新 c_adj」（A5b-1）。
- 更正序列（A5d-1）：補抓成功後追加整段列，帶 `"supersedes": "<原列的 written_at>"`；讀取時同一個 `date` 取 `written_at` 最新的一列，舊列保留。

## 2. 目錄
```
ledger/<候選>/daily.jsonl        每日淨值與持股
ledger/<候選>/rebalances.jsonl   每次換倉（排名輸入快照＋成交）
ledger/benchmark_0050.jsonl      0050 每日含息價與日報酬
prices/<YYYY-MM-DD>.json         當日全市場收盤（上市＋上櫃）
adj/<YYYY-MM-DD>.json            當日除權息因子
meta/state.json                  缺資料計數、順延紀錄、旗標、S2 覆蓋率
meta/lock.json                   鎖定資訊（起始日、判定日、快照雜湊）
```

## 3. 各檔欄位

### 3.1 `ledger/<候選>/daily.jsonl`
```json
{"date":"2026-12-01","written_at":"2026-12-02T07:03:11+08:00","nav":999869.1234,"ret":-0.0001308766,
 "cash":0.0,"holdings":{"2330":50000.0,"2317":50000.0},
 "frozen":["xxxx"],"flags":[],"supersedes":null}
```
- `ret`：當日報酬＝nav ÷ 前一日 nav − 1；起始日 `ret`＝nav ÷ 1,000,000 − 1（含建倉成本，A5c-1）。**寫入後凍結**（A5b-1）。
- `holdings`：當日收盤後各持股市值；`frozen`：無收盤而凍結的代號；`flags` 取值 `missing_day`（全市場缺資料）、`adj_missing:<code>`、`delisted_exit:<code>`、`rebalance_deferred`、`late_fill`（補記）。

### 3.2 `ledger/<候選>/rebalances.jsonl`
```json
{"nominal_date":"2026-12-01","signal_asof":"2026-11-30","fill_date":"2026-12-01","written_at":"...",
 "ranking":[{"code":"2330","score":0.912,"ranks":{"revenue":0.9,"roe":0.95,"high250":0.93},"industry":"半導體業"}],
 "targets":["2330","..."],"orders":{"buys":{"2330":50000.0},"sells":{}},"cost":131.2,
 "s2_coverage":null,"supersedes":null}
```
- `ranking`：前 **200 名**，含分數與各因子百分位與主產業（A5c-8、A5d-2）；`s2_coverage` 只有 S2 填：`{"ratio":0.93,"complete_day":"2026-12-14","forced":false}`。
- 重放只用這裡與 `prices/`、`adj/` 存下的輸入。

### 3.3 `ledger/benchmark_0050.jsonl`
`{"date":"...","adj_close":..,"ret":..,"source":"yahoo","written_at":"..."}`；起始日 `ret`＝0（A5c-1）。

### 3.4 `prices/<date>.json`
`{"date":"...","twse":{"2330":{"o":..,"h":..,"l":..,"c":..,"v":..,"status":"ok"}},"tpex":{...},"fetched_at":"..."}`；`status` 與 `src/shadow_market_data.py` 一致（`ok`／`no_close`／`exright`）。無法取得整日資料時**不寫檔**，缺資料記在 `meta/state.json`。

### 3.4b `adj/<date>.json`
`{"date":"...","factors":{"1102":{"factor":0.9339,"source":"twse"}},"fetched_at":"..."}`；`source` 為 `twse`／`tpex`／`yahoo`／`adj_missing`（此時 `factor` 為 null）。只記有事件或標記的代號。

### 3.5 `meta/state.json`（覆寫式，但每次寫入都是一個 git commit，歷史即紀錄）
```json
{"schema":1,"missing_days":["2026-12-08"],"missing_total":1,"consecutive_missing":0,
 "deferrals":[{"candidate":"S1_high250","nominal_date":"2027-01-04","executed":"2027-01-06","trading_days":2}],
 "impaired":false,"void":null,"last_processed":"2026-12-08",
 "pending_retry":[{"date":"2026-12-08","kind":"close","until":"2026-12-11"}],
 "s2":{"2026-12":{"complete_day":"2026-12-14","ratio":0.93,"forced":false}}}
```
- `impaired`：單次順延 > 5 個交易日或累計缺資料 > 10 日（A5）；`void`：連續缺 20 個交易日觸發時寫入 `{"trigger_date":..,"new_start":..}`。

### 3.6 `meta/lock.json`
`{"lock_commit":"","lock_tag":"shadow-lock-v1","lock_date":"","start_date":"2026-12-01","judgment_date":"2027-11-30","universe_sha256":"","industries_sha256":"","calendar_source":""}`；程式在 `today < start_date` 時只允許 `--dry-run`（計畫階段 4）。

## 4. `step()` 狀態機介面（階段 2）
純函式，不做 I/O：
```
step(state: dict, inputs: dict) -> (new_state: dict, rows: list[dict])
```
- `state`：各候選的 `holdings`（市值）、`cash`、`nav`、`frozen_days`（代號→連續無收盤天數）、`pending_rebalance`、`meta`（對應 3.5）。
- `inputs`（單一交易日）：`date`、`prices`（當日全市場收盤，或 `None`＝缺資料）、`prev_prices`、`adj_factors`、`ranking_by_candidate`（若為換倉日，由進入點用 `ff.composite_scores()` 算好；`step` 不碰因子）、`benchmark_close`、`calendar`、`s2_complete`。
- `rows`：本次要追加的列，每列帶目標檔名（`daily`／`rebalances`／`benchmark`／`meta`）。
- 同樣的 `state` 與 `inputs` 必得相同輸出（無隨機、無時鐘；`written_at` 由呼叫端注入）。

## 5. 不變量（測試必須驗）
1. 每日 `sum(holdings.values()) + cash == nav`（容許 1e-6 浮點誤差）。
2. 同一組輸入重跑兩次，輸出逐位元相同。
3. 重放（只用 ledger 存的輸入）與原 ledger 的 `nav`、`ret` 一致。
4. `daily.jsonl` 同一 `date` 有多列時，最新一列的 `supersedes` 指向被取代的列。

## 6. 實作後更新（2026-11-17；以 `src/shadow_portfolio.py` 的模組 docstring 為準）
階段 2 實作與審查後，上面各節有以下差異，**實作為準**：
1. `step` 的 `inputs` 欄位名：`adj`（不是 `adj_factors`）、`rankings`（不是 `ranking_by_candidate`）、`s2_status`（`{"complete","ratio"}`，每天都要給，缺資料日也要）、`benchmark_close`；沒有 `prev_prices`（用 state 內的 `last_close`）；選填 `benchmark_source`。
2. `state` 頂層有 `frozen_days`、`last_close`、`acc_factor`（三者跨候選共用，以代號為鍵），每候選有 `holdings`／`cash`／`nav`／`rets`／`pending_rebalance`。這些**不在 ledger 列裡**，所以重建 state 靠 `snapshots/<日期>.json`（處理完該日後的完整 state，階段 3 新增，全部保留）或從起始日重放。
3. `rebalances` 列新增 `kind`（`start`／`rebalance`）、`frozen`、`flags`（如 `rebalance_skipped_all_frozen`）；`benchmark` 列新增 `flags`（`carry_forward`、`missing_day`）與 `supersedes`；daily flags 新增 `s2_status_missing`、`rebalance_skipped_all_frozen`、`adj_missing:<code>`。
4. `mark_superseded(rows, index)` 的 `index` 由 `supersede_index(original_rows)` 產生，鍵為「檔案＋日期（rebalances 用 `nominal_date`）」；舊的「`{date: written_at}`」用法已移除。
5. 階段 3 新增的目錄：`snapshots/`、`rankings/<日期>.json`（當日 07:00 算好存檔的各候選排名與 `asof`）、`s2/<日期>.json`。
6. `state.meta` 新增 `status`（`not_started`／`active`／`void`／`ended`）、`start_date`、`start_wait`、`start_history`、`judgment_date`、`impaired_reasons`、`judgment`。
7. 順延換倉的排名與缺資料計數（A5f，2026-11-18）：`pending_rebalance` 可帶 `ranking`（`asof`／`ranked`／`detail`），在名義日當天存入、成交後隨 `pending_rebalance` 清除；`rebalance_due` 不再回報已存排名的候選。daily flags 新增 `ranking_missing`（待辦換倉、當天有資料但沒有任何排名可用）。
