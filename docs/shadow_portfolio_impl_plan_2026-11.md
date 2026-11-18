# 前瞻影子組合（A 線）實作計畫（2026-11-16）

> 規格：`docs/shadow_portfolio_spec_2026-11.md`（定稿，A1–A9 全部經使用者確認）。本計畫只講「怎麼做、先後順序、驗收」，不重述規則；條號（A5b-3、A5d-1 等）都指規格。
> 狀態：**計畫，未開工**。等使用者看過並明確說開工。

## 1. 目標與限制

- 目標：2026-11-30 前完成鎖定（鎖定 commit 已 push ＋ tag `shadow-lock-v1`），2026-12-01 起跑。
- 趕不上：依規格 A3，起始日順延到 2027-01 第 1 個交易日，不回填。**寧可順延，不帶著未測完的程式鎖定**——鎖定後規格歧義＝作廢重來。
- 紀律（A5）：鎖定前**不得對 S2、S3 跑歷史回測**。所有新程式只用合成資料的單元測試驗證；端到端演練（階段 5）只驗「跑得動、帳對得上」，不看也不輸出績效比較。
- 不新增依賴：抓取用 `requests`（已在 `requirements-actions.txt` 的話沿用；否則用標準庫 `urllib`，開工第一步確認）。
- push 與打 tag 都要使用者明確同意（CLAUDE.md 第 12 條）。

## 2. 檔案配置

| 檔案 | 內容 | 規格條號 |
|---|---|---|
| `src/shadow_market_data.py` | TWSE／TPEx 全市場日收盤與除權息抓取、schema 檢查、休市日曆 | A9、A5b-1、A5b-4、A5d-6 |
| `src/shadow_rules.py` | 純函式：換倉日／起始日／判定日、主產業清洗與上限選股、同分排序、成本、S2 營收完成判定 | A1、A3、A5b-5、A5b-11、A5c-4～7、A5d-4 |
| `src/shadow_portfolio.py` | ledger 狀態機：每日計值、換倉、凍結／下市、缺資料計數、補記更正序列 | A2、A5、A5b-6、A5b-8、A5c-3、A5c-5、A5d-1、A5d-5、A5d-7 |
| `scripts/run_shadow_portfolio.py` | 每日進入點：讀 ledger → 抓資料 → 更新 → 寫出；`--dry-run`、`--replay` | A5b-3、A5c-8、A5d-2 |
| `scripts/build_shadow_lock_snapshot.py` | 鎖定時產生宇宙清單與主產業快照 | A1、A5d-6 |
| `shadow/universe_2026-11.json`、`shadow/industries_snapshot_2026-11.json` | 鎖定快照（進 repo，不放 gitignore 的 `cache/`） | A5d-6 |
| `.github/workflows/shadow_portfolio.yml` | 每日 job、推 `shadow-ledger`、失敗告警、週報 | A6-5、A6-6、A5b-12 |
| `tests/test_shadow_market_data.py`、`test_shadow_rules.py`、`test_shadow_portfolio.py`、`test_run_shadow_portfolio.py` | 單元測試（合成資料＋存檔的端點回應 fixture） | A6-7、A6-10 |

純函式（`shadow_rules`）與有狀態的 ledger（`shadow_portfolio`）、有網路的抓取（`shadow_market_data`）分開，三者可以平行開發、各自測試。

**與規格 A6-5 的一個差異（要你點頭）**：規格寫「接進 `daily_recommendations.yml`」。我建議改成**獨立的 workflow 檔**，同樣週一至五 07:00。理由：
- `daily_recommendations.yml` 與 `optimize.yml` 共用 `backtest-db-state` concurrency group，影子組合不讀寫 `backtest.db`，沒必要排在那條隊伍後面。
- 規格 A6-6.5 要求「前一步失敗時影子 job 仍要跑」，獨立檔天然滿足，不用 `if: always()`。
- 推 `shadow-ledger` 需要 `contents: write`，獨立檔可以把這個權限只給它。

這只改部署形式，不改任何規則；但它偏離了已確認的字面，所以列出來。

## 3. 階段與相依

```
階段 0（半天）  開工前確認
階段 1（平行，約 3 天）
   ├─ 1a 抓取層        shadow_market_data.py
   ├─ 1b 規則純函式    shadow_rules.py
   └─ 1c ledger 資料格式定稿（文件＋fixture）
階段 2（約 3 天）  ledger 狀態機  ← 依賴 1b、1c
階段 3（約 2 天）  每日進入點＋重放  ← 依賴 1a、2
階段 4（約 1.5 天）workflow＋shadow-ledger 分支＋告警  ← 依賴 3
階段 5（約 2 天）  雲端演練（dry-run，不看績效）  ← 依賴 4，需要 push
階段 6（半天）    鎖定  ← 需要 push 與 tag
```

合計約 12 個工作天的量，日曆上只有 14 天（11/17–11/30）。階段 1 的三條平行做才塞得進去；任何一個階段卡住 2 天以上，就會觸發順延到 1 月。

### 階段 0：開工前確認（半天）
1. `requirements-actions.txt` 有沒有 `requests`；決定抓取用 `requests` 還是 `urllib`。
2. 讀雲端 `update_fundamentals` 最近一次 run 的結果，量 A5d-4 的營收完成覆蓋率（11/15 那次 run 的 log；需要登入 GitHub 才看得到，若拿不到就由階段 5 的 dry-run 量）。
3. TWSE 休市日表的端點與格式（A5b-4）。**這個還沒驗證過**，是階段 1a 的第一件事；取不到時的備案規格已寫（A5d-7）。
4. 0050 含息價的每日更新路徑：既有快取來源是 Yahoo `0050.TW`、停在 10/19（A5c-10）。

### 階段 1a：抓取層 `src/shadow_market_data.py`
- `fetch_twse_close(date)`、`fetch_tpex_close(date)`：回傳 `{代號: {"o","h","l","c","v"}}`，只留 4 位數代號；無成交（`--`／`----`）回傳「無收盤」。
- `fetch_twse_exright(start, end)`、`fetch_tpex_exright(start, end)`：回傳 `{(日期, 代號): {"pre_close","ref_price"}}`。
- `adj_factor_for_day(...)`：因子＝參考價 ÷ 除權息前收盤；上櫃備援走 Yahoo，只對當日行情標「除息／除權」的代號；都失敗回傳 `adj_missing`（A5d-6）。
- `trading_calendar(year)`：TWSE 休市日表。
- schema 檢查：表的辨識、欄位數、列數下限（上市 ≥ 900、上櫃 ≥ 700 檔 4 位數，低於就當缺資料）、回應日期必須等於請求日期（TPEx 舊端點會靜默回傳最新一天，這條擋住同類錯誤）。
- timeout 40 秒以上（Actions 上 TPEx 日收盤實測 6–12 秒），請求間隔 3 秒，明確指定 utf-8。
- 測試：把 A9 驗證時的真實回應存成 fixture（`tests/fixtures/shadow/`），測解析、除權息日（`X`／「除息」）、無成交、日期不符、列數不足。**不在單元測試裡打網路**。

### 階段 1b：規則純函式 `src/shadow_rules.py`
- 日期：`first_trading_day(calendar, year, month)`、`first_trading_day_on_or_after(calendar, year, month, day=12)`、`start_date(lock_date, calendar)`（A5b-9）、`judgment_date(start, calendar)`（A3：加 12 個月後該月第 1 個交易日的前一個交易日）、`restart_date(trigger_date, calendar)`（A5b-8）。
- 排名：`rank_top(scores, n)` 以 `(−分數, 代號)` 排序，不用 `ff.top_n()`（A5b-11）。
- 主產業：`primary_industry(industries, universe)`——別名合併、非產業標籤剔除、「其他」精確比對、成員數以聯集計、平手取 code point 最小（A5b-5、A5c-7）。
- 上限選股：`select_with_cap(ranked, primary, cap=4, n=20, frozen=...)`，凍結檔先佔名額與產業額度（A5d-5）。
- 成本：`buy_cost(value)`、`sell_cost(value)`，買 0.0314%、賣 0.3314%、各加滑價 0.1%（A5c-6）。
- S2 完成判定：`revenue_refresh_complete(universe_data, today)`，`revenue` 蓋章或 `revenue_attempt` ≥ 當月 11 日，≥ 90%（A5d-4）；20 日強制、逢非交易日順延（A5c-4）。
- 測試重點（每條都是規格裡吵過的邊界）：
  - 起始日 12/01 → 判定日 2027-11-30；起始日 2027-01-04 → 判定日是 2027-12 最後一個交易日；鎖定日恰在 12/01 → 起始日順延到 1 月。
  - 農曆年假、月第 1 個交易日不是 1 號。
  - 別名兩種寫法同時掛的代號、只有「創新板股票」的代號、「其他電子業」不被當成「其他」。
  - 單一產業集中到要往下補很多名、同分。

### 階段 1c：ledger 資料格式（先定稿再寫狀態機）
`shadow-ledger` 分支的目錄（只追加）：

```
ledger/<候選>/daily.jsonl        每日一列：日期、淨值、日報酬、持股市值、現金、狀態旗標、supersedes
ledger/<候選>/rebalances.jsonl   每次換倉：名義日、成交日、前 200 名（分數、各因子百分位、主產業）、成交明細、成本
ledger/benchmark_0050.jsonl      0050 每日含息價與日報酬
prices/<YYYY-MM-DD>.json         當日全市場收盤（上市＋上櫃）
adj/<YYYY-MM-DD>.json            當日除權息因子
meta/state.json                  缺資料計數、順延紀錄、口徑受損旗標、S2 營收覆蓋率與達標日
meta/lock.json                   鎖定 commit、宇宙與產業快照的雜湊
```

- 持股以市值（元）記，不記「股數 × 最新 `c_adj`」（A5b-1）。
- 更正序列（A5d-1）：補記列帶 `supersedes: [原列的日期]`；讀取時同一天取最新的一列，舊列不刪。
- 產出：一份格式說明＋手寫的 fixture，階段 2 的測試直接用。

### 階段 2：ledger 狀態機 `src/shadow_portfolio.py`
一個純函式核心：`step(state, day_inputs) -> (new_state, rows_to_append)`，不做 I/O，所以能用合成資料完整測試。
- 每日計值：持股市值 × (當日收盤 ÷ 前日收盤 × 調整因子)；寫入後凍結。
- 換倉：T 日 07:00 以 T−1 資料算目標並存檔，T 收盤成交，T+1 記帳（A5d-3）；調差額、只對交易差額計成本（A5b-10）。
- 凍結與下市：無收盤第 1 天起凍結，連續 20 個交易日出場（只扣賣出腿）；全市場缺資料日暫停計數（A5c-5、A5d-5）。
- 缺資料：全市場層級累計、單次順延計數、口徑受損旗標、連續 20 日作廢（A5、A5b-8）。
- 補抓與更正序列：重試到 T 之後第 3 個交易日（A5d-1）。
- 起始建倉成本計入起始日淨值；0050 起始不扣（A5c-1）。
- 判定日：取收盤市值；若順延的換倉恰落在判定日，取換倉前市值（A5d-7）。
- 測試：A6-7 與 A6-10 列的每一項各至少一個案例；另加兩個不變量測試——(1) 任何一天「持股市值總和＋現金＝淨值」；(2) 同一組輸入重跑兩次，輸出逐位元相同（重放的前提）。

### 階段 3：每日進入點 `scripts/run_shadow_portfolio.py`
- 流程：讀 `shadow-ledger` 現況 → 判斷要處理哪些交易日（含漏跑補抓）→ 抓價與除權息 → 讀 `cache/factor_run`（歷史底與營收／ROE）→ 呼叫 `ff.composite_scores()` → `step()` → 寫出新增列。
- `--dry-run`：算但不寫。`--replay --out <分支>`：只用 ledger 存的輸入重算，寫到 `shadow-ledger-replay-N`（A5c-8、A5d-2）。
- 排名用的 `c_adj` 連鎖：週更新寫入的優先，每日補抓的 bar 以當日因子往前接（A5b-1）。
- 測試：用 fixture 目錄模擬一段 30 個交易日（含一次換倉、一天缺資料、一檔除息、一檔停牌），驗最終 ledger 與手算一致；重放結果與原 ledger 一致。

### 階段 4：workflow
- `.github/workflows/shadow_portfolio.yml`：cron `0 23 * * 0-4`（台北週一至五 07:00）＋`workflow_dispatch`（`dry_run`）。
- `permissions: contents: write`；checkout `main`（程式）與 `shadow-ledger`（資料）兩份；restore `cache/factor_run`（唯讀，沿用 `update-universe-bars-state-` 血統）。
- 失敗時發 Telegram（A5 第 72 行）；每週一次摘要（各候選 vs 0050 的累計報酬、Sharpe、MDD），標「影子組合・未驗證・不是買賣訊號」。
- **起跑日前不寫正式 ledger**：程式讀 `meta/lock.json` 的起始日，早於起始日只允許 `dry_run`，避免演練資料混進正式分支。
- keepalive：查證「推 `shadow-ledger` 是否算 repo 活動」（A5b-12）；不算就加月度 keepalive。

### 階段 5：雲端演練（需要 push；2026-11-18 依使用者決定改寫）
原計畫用 `dry_run` 連跑 3 天，有三個問題：(1) 快照檔與 `meta/lock.json` 不存在時 workflow 會 `exit 0` 假綠；(2) dry-run 不寫檔也不 commit，驗不了跨日 state、推送權限與重放；(3) 起始日前的非 dry-run 會被 workflow 整個跳過。改成：
1. **前置（需 push 同意）**：push `main`（含 F3／F4 分支、快照腳本、`shadow_lock_snapshot.yml`）。
2. **演練快照**：手動 dispatch `shadow-lock-snapshot`（分支名如 `shadow-snapshot-rehearsal`），fetch 該分支，把 `shadow/` 兩個檔 commit 進 `main`；用 `build_shadow_lock_snapshot.py --write-lock` 產生演練用 `meta/lock.json`（`start_date` 設為演練起始日，例如 2026-11-19），放進本地建立的孤兒分支 `shadow-ledger-rehearsal` 並 push。
3. **演練**：`workflow_dispatch`，`dry_run=false`、`ledger_branch=shadow-ledger-rehearsal`，連 3 個交易日各一次（11/19、11/20、11/23）。
4. **判讀不能只看綠燈**：每次要確認 log 有 `booked` 字樣、`shadow-ledger-rehearsal` 有新 commit。
5. **驗**：端點在 Actions 上穩定（TPEx 慢，A5f-5：量缺資料日頻率）、`factor_run` 快取讀得到、S2 營收覆蓋率（11/19 起 ≥12 日，可提早看 90% 門檻）、執行時間、推 `shadow-ledger-rehearsal` 的權限、對演練分支跑 `--replay --out` 與原帳一致、Telegram 告警送達。
6. **3 天碰不到換倉**（月初、≥12 日都不在窗內）：換倉、順延、補抓重跑靠單元測試；演練起始日當天會跑到起始建倉。
7. 演練分支與演練快照在鎖定前清掉（鎖定 commit 內的快照以鎖定日重新產生的為準）。
- **演練不輸出任何「候選 vs 0050」的比較數字**（A5 紀律）；只看帳對不對、流程通不通。

### 階段 6：鎖定
1. 鎖定日再跑一次 `shadow-lock-snapshot`（新分支名），fetch 後把兩個快照檔放進鎖定 commit（宇宙以雲端快取為準，A5f-6）。
2. 全部測試通過；派 fresh-context agent 對「程式是否符合規格」做一次審查，並把程式內約 41 處 `# ASSUMPTION:` 逐條歸類成「進規格」或「無兩種合理解讀」。
3. 使用者同意後 push `main`、打 tag `shadow-lock-v1` 並 push。
4. 另開一個只改 A7 的 commit，填鎖定 commit、鎖定日、快照日期、起始日、判定日（A5b-3）。
5. 建立 `shadow-ledger` 分支與 `meta/lock.json`（`build_shadow_lock_snapshot.py --write-lock`，`start_date` 2026-12-01）。**硬限制：UTC 11/30 23:00（台北 12/1 07:00）排程前完成**；GitHub 排程常遲到，建議最晚 11/27（週五）鎖定。
6. keepalive 查證（A5b-12）：先查 GitHub 文件；不確定就加月度 keepalive。

## 4. 派工方式

- 階段 1a、1b 互不相依、介面已在上面寫死，可以各派一個實作 subagent 平行做（model 依 `.claude/playbook/delegation.md`）；1c 我自己做，因為它是階段 2 的介面。
- 階段 2 是全案最容易出錯的地方（狀態機＋更正序列），不拆、不平行；做完派 fresh-context agent 審查，審查的依據是規格條號，不是我的實作說明。
- 每個階段結束回報三態（已驗證／已完成未驗證／未完成），附測試輸出。

## 5. 風險

| 風險 | 影響 | 對策 |
|---|---|---|
| 14 天做不完 | 起始日順延到 2027-01 | 11/24 做一次進度檢查：階段 2 沒完成就直接決定順延，不硬趕 |
| TWSE 休市日表端點沒驗證過 | 交易日曆是所有日期規則的基礎 | 階段 1a 第一件事；備案已在 A5d-7 |
| S2 營收完成覆蓋率量不到 | S2 可能每月都拖到 20 日 | 階段 0 嘗試讀 log；否則階段 5 dry-run 量。量出來若長期低於 90%，**鎖定前**要回頭跟你談門檻 |
| 端點格式改版 | 當天變缺資料日 | schema 檢查＋告警；連續 20 日才作廢，有緩衝 |
| 更正序列實作複雜 | bug 集中處 | 不變量測試＋重放一致性測試 |
| `cache/factor_run` 快取被淘汰或週更新失敗 | 排名拿不到歷史底 | job 開頭檢查快取檔數（沿用 `MIN_CACHED_CODES` 的做法），不足就告警並當缺資料日 |

## 6. 使用者決定（2026-11-17）

1. 獨立 workflow 檔（不接進 `daily_recommendations.yml`）。
2. 本地 commit 等雲端演練（階段 5）時跟程式一起 push。
3. **11/24 進度檢查**：階段 2（ledger 狀態機）若未完成，直接順延起始日到 2027-01 第 1 個交易日，不硬趕。
4. 開工。

## 7. 使用者決定（2026-11-18，全部照建議）

1. 影子組合狀態機 F3／F4：順延換倉用名義日存下的排名、T−1 缺資料不重複計並加 `ranking_missing` 旗標（規格 A5f-1～3）。
2. 規格 A5e 九項全部確認（含第 6 項 Yahoo 備援收窄）；兩條字面讀法：重試窗含當天、任一市場缺即整日缺資料（A5f-4、5）。
3. 宇宙以雲端 Actions 快取 `factor_run` 為準（A5f-6）；快照腳本與 `shadow_lock_snapshot.yml` 提前到階段 5 之前。
4. 演練改為非 dry-run 寫到 `shadow-ledger-rehearsal`（見階段 5）。
5. 時程：11/19、11/20、11/23 演練；11/24 檢查點；最晚 11/27 鎖定。
