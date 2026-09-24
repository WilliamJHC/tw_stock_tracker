# 選股策略優化方向研究：因子擴充與時間架構分層

2026-09-22。這是一份**研究方向清單**，不是決策記錄——沒有任何一項在本文件裡被「採用」，每一項的落地都必須先過專案既有的驗證關卡（見第 4 節）。目的是回答「除了現有 revenue/roe/momentum 三因子、單一月頻 rebalance 之外，還有哪些站得住腳的方向可以研究」，依據是通讀 `CONTEXT.md`、`README.md`、`docs/adr/*`、`docs/regime_strategy_research.md`、`docs/regime_switch_vs_0050.md`、`docs/https-finlab-...-lighthouse.md`，以及 `finlab_factors.py`/`factor_eval.py`/`market_regime.py`/`optimize.py`/`backtest.py`/`portfolio_backtest.py`/`breakout_strategy.py`/`pipeline.py`/`data_sources.py`/`analysis.py` 的實際程式碼後的結果。

## 核心結論（先講重點）

三個方向，優先序由高到低：

1. **有一個已經寫好但從未評估過的洞先填**：`finlab_factors.py` 裡 `gross_margin_trend_series()`／`operating_margin_trend_series()`／`value_series()`（PBR）三個候選因子，連同 `factor_eval.evaluate_candidate_factor()` 這個現成的評估函式，都是 Phase 6b 就寫好的（見 `docs/.../lighthouse.md:270-283`），但全專案文件（`docs/` 全文搜尋）找不到任何一份報告記錄過它們的實際 rank-IC 結果——跟 `regime_strategy_research.md` 有實跑報告的規格完全不對稱。這是零新資料成本、幾小時工作量、應該最先做的事。
2. **因子面：籌碼流（法人買賣超）目前只活在個股即時決策面板，從未進過全市場排名引擎**；再加上應計盈餘品質（accrual quality）這個台股少見的品質因子，是兩個資料已在手（或只差幾個既有 dataset 的欄位）、值得走一次完整 `evaluate_candidate_factor → walk-forward → DSR/PBO` 流程的新候選。
3. **時間架構面：真正該做的不是「新增一個時間週期」，是重新檢視動能因子本身的 formation window**——`regime_strategy_research.md` 發現 momentum 在三種盤勢的 ICIR 都是負的，而 `MOMENTUM_DAYS=60` 這個視窗涵蓋了最近一個月（學界普遍認為這段最容易被短期反轉污染）。這極可能是根因，值得用專案自己現成的 `evaluate_factors()` 對比不同視窗，而不是急著丟掉動能因子或引入新的時間框架。另外，月頻選股（`portfolio_backtest.py`）與日頻個股評分（`analysis.py`）、盤中監控（`monitor.py`）三層時間架構目前彼此獨立、互不餵資料，這裡有一個具體、低風險的整合實驗可做。

以下逐項展開，每項都標了資料成本、工程量級、該走哪一條既有治理軌道。

---

## 1. 現況盤點（為什麼是這三個方向）

- **全市場多因子排名**（`finlab_factors.composite_scores()`）：`RANKING_WEIGHTS = {"revenue": 0.4, "roe": 0.3, "momentum": 0.3}`，月頻 rebalance，`RANKING_TOP_N=20`。這是唯一被驗證過「跟大盤盤勢無關、長期有選股訊號」的引擎（`docs/regime_switch_vs_0050.md` 的空頭分桶年化超額 +27.6%~+58.8%）。
- **個股即時決策**（`analysis.py`）：`SCORE_WEIGHTS`（技術，9 個分項，含 5 日相對強度 `rsRanking`）＋ `CHIP_WEIGHTS`（籌碼，法人/融資/外資/借券，4 個分項）獨立並列，不進 `composite_scores()`。
- **單股回測擇時**（`backtest.py`）：已經誠實承認結構性輸給買進持有（`docs/README.md` 第 253 行「四項已知限制」第 4 點），定位是進出場警示，不是 alpha 引擎。
- **盤中監控**（`monitor.py`/`intraday.py`/`alerts.py`）：秒級 CVD/VWAP/ORB，寫進 `cache/intraday_*.json`，只餵回 `pipeline.py` 的當日快照，不進任何回測或排名。
- **已有但未啟用的候選因子**：`gross_margin_trend_series()`／`operating_margin_trend_series()`／`value_series()`（PBR）、`ma_cross_signal()`／`volume_price_divergence()`——後兩者已經走完 ADR 0003 的人工審核機制（有 `EXTRA_FACTOR_FUNCS`、`used_extra_factors()`、`run_strategy_search.py`/`apply_strategy_candidate.py` 的完整管線），前三者則連評估都還沒做。
- **盤勢分桶研究已有的結論**（`docs/regime_strategy_research.md`）：momentum ICIR 三種盤勢都負、roe ICIR 都正且最穩定；空頭因子 IC 樣本 n=2，不可用；`STRATEGY_PARAMS_BY_REGIME` 目前只有空頭有覆寫（`exit_composite`/`max_hold_days`）。`RANKING_WEIGHTS_BY_REGIME` 因樣本不足**尚無候選**，不是刻意不做。
- **盤勢切換資金配置已被證偽**（`docs/regime_switch_vs_0050.md`）：任何「盤勢決定抱策略還是抱 0050」的規則，三項採用門檻全部沒過，結論是繼續抱 0050、不做資金配置層級的擇時。**這條路已經走過、有明確反例，不建議重新開放討論**，本文件所有建議都限定在「選股因子」與「進出場時機」範圍內，不涉及曝險/現金控制（與 ADR 0004 的既有範圍界線一致）。

---

## 2. 因子面建議

### 2.1 立即可做：把 Phase 6b 已寫好的三個候選因子跑一次 IC 評估（零新資料成本）

`gross_margin_trend_series()`（毛利率年增趨勢）、`operating_margin_trend_series()`（營益率年增趨勢）都吃 `get_financials()` 已經在抓的欄位（`revenue`/`gross_profit`/`operating_income`，零額外 request）；`value_series()`（PBR，取負號後方向一致）吃 `get_per_pbr()`。三者都已經是 `factor_eval.evaluate_candidate_factor()` 的設計目標（該函式的 docstring 明確點名這三個）。

**建議動作**：寫一個對照 `scripts/report_regime_factor_ic.py` 規格的小腳本（或擴充既有的 `scripts/report_factor_ic.py`，若存在），對這三個候選跑 `evaluate_candidate_factor(universe_data, series_key, value_key)`，horizon 1/5/20/60 都跑，產出跟 `regime_strategy_research.md` 同等級的報告。IC 的 t-stat 沒過顯著性門檻的直接排除，跟現有三因子受到的把關標準一致（`docs/.../lighthouse.md:214`「這條規則跟 Phase 4 對既有三因子做的把關一致，不因為是『新』因子就放寬」）。

品質因子（毛利率/營益率趨勢）在學理上跟現有的 ROE 同屬「獲利品質」象限但角度不同（ROE 是水準值，margin trend 是變化率），過去 IC 研究對「邊際改善」類因子的解釋力常常不輸「水準值」因子，值得驗證是否與 ROE 有增量訊號而非重複。PBR value 因子則補上目前完全缺席的「價值」象限——`RANKING_WEIGHTS` 三個因子（成長/獲利/動能）裡沒有一個是價值類，在半導體/PCB 這種資本密集、景氣循環股權重高的 watchlist／全市場組成下，價值因子與動能/成長因子的相關性通常較低，若 IC 顯著，加入後對組合的分散化效益可能大於單看 IC 數字本身。

**治理軌道**：這三個因子雖然函式已寫好，但從未被評估、從未進 `RANKING_WEIGHTS`，屬於 ADR 0003 定義的「全新未驗證邏輯」，需要走 `run_strategy_search.py` 人工審核流程，不能套 ADR 0002 的自動套用（那個只適用於「既有已驗證因子的權重比例調整」）。

### 2.2 中優先：把籌碼面（法人買賣超）從「個股即時分數」升級為「全市場月頻因子」

`CHIP_WEIGHTS`／`get_institutional()` 目前只餵 `analysis.py` 的 `chip_analysis()`，用途是「今天這檔股票的籌碼是偏多還是偏空」，時間窗是 5 日（`chip_analysis()` 用 5 日持有窗，`breakout_strategy.py` 用更短的 3 日窗）。這批資料從未以「月頻、全市場排名因子」的形態餵進 `composite_scores()`。

法人買超的資訊含量與現有三因子（營收/ROE/動能）機制不同——法人買賣超反映的是「有更多資訊的參與者當下在做什麼」，跟財報端的基本面因子（落後於財報揭露日）、價格端的動能因子（落後於價格本身）是不同的資訊來源，理論上與現有三因子的相關性較低，是值得測試增量價值的候選。具體做法：仿造 `revenue_yoy_series()`/`roe_series()` 的形狀，寫一個 `institutional_net_persistence_series(institutional_rows)`，用月度視窗算「過去 N 個交易日法人（或僅外資）淨買超金額 / N 日均量」這類已在 `CHIP_THRESHOLDS["inst_net_full"]` 用過的正規化方式，`usable_date` 用資料本身的公告時間（法人買賣超是隔日公布，沒有 `REVENUE_LAG_DAYS` 這種級別的揭露延遲問題，比照 `value_series()` 的「當日可用」處理）。

**資料成本**：零——`get_institutional()` 已經在抓；但要注意全市場規模下每檔多一次 `TaiwanStockInstitutionalInvestorsBuySell` request，會疊加進 Phase 4 已經很吃緊的抓取時數預算（`docs/.../lighthouse.md` 的 ~22 小時表），需要跟 6a/6c 的排程週期一起評估，不是純函式層面零成本。

**治理軌道**：全新因子邏輯，走 ADR 0003 人工審核，先 `evaluate_candidate_factor` 拿 IC 證據再進 `run_strategy_search.py`。

### 2.3 中優先：應計盈餘品質（accrual quality）——現有 balance sheet dataset 多抓幾個欄位即可

這是 Sloan (1996) 的經典異常：把財報淨利拆成「現金流部分」與「應計（accrual）部分」，應計占比越高的公司（獲利越是靠會計估計撐出來而非實際現金流入），後續報酬統計上越差——機制是投資人對「漂亮的財報數字」定價過度樂觀，沒有區分現金流品質。這個異常在 2002 年後於美股樣本有明顯減弱（近期文獻對其穩健性有分歧），但**台股很少人專門測過**，加上這個專案 `get_financials()` 已經在抓 `TaiwanStockBalanceSheet`（只取了 `Equity` 這個 type），同一個 dataset 底下應該還有 `CurrentAssets`/`CurrentLiabilities`/`CashAndCashEquivalents` 等其他 `type` 值（跟現有 `income_rows` 抓 `Revenue`/`GrossProfit`/`OperatingIncome` 用的是同一個「同一個 request、多抓幾個 type」手法，`data_sources.py:915-970` 的 docstring 已經示範過這個零額外 request 的模式），資料抓取成本極低。

Sloan 原始的 balance-sheet 法：`accruals = (ΔCurrentAssets − ΔCash) − (ΔCurrentLiabilities − ΔShortTermDebt) − Depreciation`，除以總資產標準化，取負號後跟其他因子同方向（低應計比較「好」）。**需要先確認 FinMind 的 `TaiwanStockBalanceSheet` 對這幾個 type 的覆蓋率**（尤其是短期借款這個 type 名稱在 FinMind 底下怎麼標，需要先查一次 API 回傳的 `type` 清單，不能假設一定有），這是動工前第一步要做的資料可行性檢查，不是直接假設可行。

**治理軌道**：全新因子邏輯，ADR 0003 人工審核。

### 2.4 低優先、先做可行性調查：董監持股與股票質押比率（公司治理風險因子）

台股市場對「董監事股票質押比率」偏高的公司有長期關注——質押比率高代表大股東把股票拿去融資，一旦股價重挫容易觸發追繳／斷頭賣壓，形成自我強化的下跌螺旋，學術與實務文獻（如中山管理評論〈董監事股權質押與公司治理之研究〉）都把這個指標當作治理風險與潛在崩盤風險的預警信號。公開資訊觀測站（`mopsov.twse.com.tw`）有「董監事持股餘額明細資料」可查，但**目前看不到 FinMind 有直接對應的 dataset**（`data_sources.py` 現有的 `get_` 函式清單裡沒有董監持股/設質相關項目）——這點在動工前必須先花 1-2 小時查證 FinMind 的付費/免費層是否涵蓋，若沒有，要嘛放棄、要嘛評估另開一個資料來源（成本與 Fugle/finlab 這類「新資料源該不該接」的既有討論同一量級，見 `docs/.../lighthouse.md` 的「明確不做」表），不建議在資料可行性不明的狀態下排進工程排期。列為低優先，等前面幾項跑出結果、有餘裕再評估。

### 2.5 更長期、暫不建議現在做：低波動 / sector-neutral 排名

低波動異常（低波動股票的風險調整後報酬historically優於高波動股票，違背 CAPM 的高風險高報酬假設）與產業中立化排名（避免半導體/電子供應鏈權重股系統性主導 ROE/動能排名）都是成熟的研究方向，但都需要更大的全市場樣本與更細的分層驗證，且與現有「先擴大 universe、再做最佳化」的既定順序（`docs/.../lighthouse.md`「已確認的決策」）不衝突但不急迫。放進候選池，不建議這次投入工程時間。

---

## 3. 時間架構面建議

### 3.1 重新檢視動能因子的 formation window——這可能是「動能 ICIR 三盤勢皆負」的根因

`regime_strategy_research.md` 的發現：momentum 在多頭/空頭/盤整三種盤勢的 ICIR 都是負的，文件本身也誠實標注「這只是一個假說」。但有一個更基本的技術細節值得先排除：`finlab_factors.MOMENTUM_DAYS = 60`，也就是動能因子用的是「過去 60 個交易日（近 3 個月）」的報酬，**這個視窗完整包含了最近一個月**。

金融學界對動能因子的經典構造（Jegadeesh & Titman）是「12 個月形成期、跳過最近 1 個月」（俗稱 12-1 動能），理由是最近一個月常帶有短期反轉效應，直接用進動能公式會稀釋甚至反轉訊號方向。**但這個「跳過最近一個月」的傳統理由本身，在近年研究中也有爭議**——查證後發現至少有研究（Alpha Architect 對 1975-2024 產業層級資料的重新檢視）發現「上個月報酬強的股票，接下來的動能表現反而更強」，跟傳統的「應該跳過以避免反轉」的假設方向相反。也就是說，**學界對「該不該跳過最近一個月」本身沒有定論**，不能直接照搬某一個版本的公式當作正確答案。

這正好是這個專案一直以來的方法論——不抄公式、用自己的資料驗證（`docs/regime_strategy_research.md`「這裡是本專案自己的資料證實，不是引用外部說法」）。**具體建議**：用 `factor_eval.factor_ic_series()`／`evaluate_factors()` 現成的框架，對照測試至少三組動能 formation window——現有的 60 日、更短的 20 日（純短期動能，可能受惠於短期延續而非被反轉污染）、以及 120 日跳過最近 21 日（傳統 12-1 動能的 bar 數近似版本）——各自算一次 IC/ICIR/衰減曲線，直接比較哪一組在台股全市場樣本上訊號最乾淨。這個實驗不需要新增資料、不需要改動 pipeline，只是把 `momentum_pct(bars, idx, days=...)` 的 `days` 參數換幾個值重跑評估，是這次所有建議裡**工程成本最低、驗證意義最大**的一項。

**治理軌道**：這屬於「既有已驗證因子（動能）的參數調整」還是「全新邏輯」，是個灰色地帶——比較保守的做法是仍走 ADR 0003 的評估流程（先 `evaluate_factors` 出 IC 報告），但不一定需要整套 `run_strategy_search.py`/人工核准機制，因為改變的只是動能這一個因子的計算方式，不是新增一個因子類別。建議先出報告、視結果決定要不要正式走候選審核。

### 3.2 讓三層既有時間架構真正互相餵資料，而不是各自獨立

目前月頻選股（`portfolio_backtest.py`）、日頻個股評分（`analysis.py` 的 `SCORE_WEIGHTS`）、盤中監控（`monitor.py`）三層時間架構都已經存在，但彼此不互通：月頻排名選出 top-N 後是無腦等權重全部買進（`portfolio_backtest.py` 的既有邏輯），沒有用日頻或盤中訊號做「選到之後、什麼時候真的進場」的濾網。

`breakout_strategy.py` 已經做好一套獨立的「技術突破＋籌碼合力」雙確認進場邏輯（`entry_signal()` 要求 `tech["confirmed"]` 與 `chip["confirmed"]` 同時成立），目前是完全獨立的引擎，沒有跟多因子排名引擎接起來。**具體建議**：對月頻排名選出的 top-N 候選，測試「不是選到當下的下一根 K 棒開盤就買，而是等 `breakout_strategy.technical_signal()`／`chip_signal()` 確認後才進場」這個組合策略的回測表現——這不是新建系統，是把兩個已經各自驗證過、獨立存在的引擎做一次分層測試（月頻決定「買哪些」、週頻/日頻決定「什麼時候買」），用 `optimize.py` 既有的 walk-forward 框架驗證這個組合是否優於「選到就買」的現況，而不是優於買進持有（後者這個比較 `regime_switch_vs_0050.md` 已經做過，這裡問的是不同的問題：同樣的選股結果，進場時機優化能不能提升執行效率）。

另外，`analysis.py` 的 `rs_ranking()`（5 日相對強度）目前只在 `build_watchlist_payloads()` 的 ~28 檔追蹤清單內部排名，樣本太小，訊號意義有限。如果把同一個邏輯搬進全市場 `universe_data`（Phase 4 已經有全市場日線），5 日相對強度在幾千檔的橫截面裡可能是一個有意義的短期動能候選因子，可以跟 3.1 的 formation window 實驗一起測（20 日以下的超短動能，本質上跟 RS ranking 是同一類訊號）。

### 3.3 rebalance 頻率本身從未被系統性測試過

`portfolio_backtest.py` 的月頻 rebalance 沿用的是 FinLab 原文章的設計慣例，不是這個專案自己驗證出來的結果。`optimize.py` 已經有 `purged_calendar_walk_forward_splits()`／`train_test_split_by_calendar()` 這類以行事曆為單位切分的工具（本來是為策略搜尋的 train/test 切分而寫），理論上可以直接拿來測試「週頻 / 雙週頻 / 月頻」三種 rebalance calendar 對 `portfolio_backtest.simulate()` 總報酬與換手成本的影響——尤其如果 3.1 的短天期動能候選因子被驗證有效，月頻可能太慢，捕捉不到訊號在衰減前的窗口。這是用現有工具回答新問題，不需要新框架，但需要先確認 `_rebalance_dates()` 這個私有函式是否容易換成非月頻的行事曆（如果目前寫死月頻邊界，需要一點重構）。

### 3.4 盤勢層級的時間架構——目前已驗證的部分要維持現狀，不要重新打開

`STRATEGY_PARAMS_BY_REGIME` 目前只有空頭有覆寫（`exit_composite`/`max_hold_days`），多頭/盤整刻意不動，理由是樣本沒有清楚到值得偏離現狀。`RANKING_WEIGHTS_BY_REGIME` 因為空頭因子 IC 樣本只有 n=2 個再平衡日，完全不可用，目前沒有候選——這不是本文件能解決的問題，需要 `cache/factor_run` 累積更長歷史（尤其更多空頭月份）後才有資格重新評估，是等時間、不是等工程。資金配置層級的盤勢切換規則（抱策略 vs 抱 0050）已經被 `regime_switch_vs_0050.md` 證偽，不建議重新開放討論。

---

## 4. 落地順序與治理軌道

這個專案已經有完整的三層驗證紀律：**IC/ICIR 顯著性檢定 → purged walk-forward + DSR/PBO/高原寬度門檻 → 依風險分兩軌治理**（ADR 0002 既有已驗證因子權重調整走自動套用+rollback；ADR 0003 全新邏輯走人工審核）。本文件所有建議都必須走這條路，不建議為了求快繞過。建議順序：

**立即（零成本，數小時）**
1. ~~§2.1：把 `gross_margin_trend`/`operating_margin_trend`/`value(PBR)` 三個已寫好的候選跑一次 `evaluate_candidate_factor` 報告。~~ 已完成，見 `docs/factor_ic_candidate_and_momentum_window_report.md`——value/pbr pooled 顯著但正式 walk-forward 的 DSR 未過門檻，不採用。
2. ~~§3.1：動能 formation window（20/60/120-skip-21 日）對照實驗，用 `evaluate_factors()` 現成框架。~~ 已完成，見同一份報告——120d-skip-21 三盤勢分桶後結論保守，不建議換 `MOMENTUM_DAYS`。

**短期（資料已在手或只差既有 dataset 的欄位，數天工程量級）**
3. ~~§2.2：法人買賣超持續性因子，`evaluate_candidate_factor` 驗證。~~ 已完成，見 `docs/institutional_and_accrual_factor_plan.md`——`inst_net_5d` 各 horizon 皆不顯著，不採用。
4. ~~§2.3：應計盈餘品質因子——先花半天查 FinMind `TaiwanStockBalanceSheet` 的 type 覆蓋率，可行再動工。~~ 已完成，見 `docs/factor_ic_candidate_and_momentum_window_report.md` §2.3——pooled IC 是目前所有候選裡最強的一個，但正式 walk-forward 的 DSR 差 0.0023 沒過門檻，暫不採用，標記為資料累積後優先重跑對象。

**中期（需要新整合，約一週工程量級）**
5. §3.2：多因子排名 top-N ＋ `breakout_strategy.py` 進場濾網的分層回測。尚未執行。
6. ~~§3.3：rebalance 頻率（週/雙週/月）對照實驗。~~ 已完成，見 `docs/rebalance_frequency_report.md`——月頻（現況）4 折平均報酬最高，但 DSR 差 0.0041 沒過門檻，維持月頻，不提案變更。

**長期／先做可行性調查**
7. ~~§2.4：董監質押比率治理風險因子——先查資料來源可行性，未必值得投入。~~ 可行性查證已完成，見 `docs/pledge_ratio_feasibility.md`——FinMind 沒有這個 dataset，證交所開放資料只有當月快照、無歷史回溯，能回測的歷史資料只能靠付費源（TEJ/CMoney），現階段不建議投入工程時間。
8. §2.5：低波動因子、sector-neutral 排名——放進候選池，暫不排工程排期。尚未執行（文件本身建議暫不做）。

每一項通過 IC 顯著性檢定後，視風險等級走 ADR 0002 或 ADR 0003 的既有審核路徑，套用門檻（DSR ≥ 0.95、PBO ≤ 0.5、高原寬度 ≥ 搜尋範圍 20%）維持不變，不為了讓新因子過關而放寬。

---

## Sources（時間架構部分引用的外部研究）

- [Momentum: what do we know 30 years after Jegadeesh and Titman's seminal paper?](https://link.springer.com/article/10.1007/s11408-022-00417-8)
- [The Skip-Month Mystery: What Last Month's Returns Are Really Telling You — Alpha Architect](https://alphaarchitect.com/skip-month-mystery/)
- [Short-term momentum (almost) everywhere — ScienceDirect](https://www.sciencedirect.com/science/article/pii/S1042443119300976)
- [Accrual Anomaly — Quantpedia](https://quantpedia.com/strategies/accrual-anomaly)
- [Low volatility anomaly — Robeco](https://www.robeco.com/en-us/glossary/quantitative-investing/low-volatility-anomaly)
- [董監事股權質押與公司治理之研究 — 中山管理評論](https://mgtr.cm.nsysu.edu.tw/fulljournal_process.php?id=106&pno=2010108&p=299)
- [公開資訊觀測站：董監事持股餘額明細資料](https://mopsov.twse.com.tw/mops/web/stapap1)
