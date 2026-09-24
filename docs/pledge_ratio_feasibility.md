# 董監質押比率因子（§2.4）：資料可行性查證

2026-09-24。回應 `docs/factor_timeframe_optimization_directions.md` §2.4 的建議——這份文件只回答「這個資料到底拿不拿得到、用什麼方式拿」，**不設計因子公式、不寫串接步驟**：查證結果是這條路目前走不通全市場歷史回測，所以還沒到需要設計因子的階段。

## 結論

**FinMind 免費層與付費層都沒有董監事持股／質押的 dataset**——查過 FinMind 官方 dataset 目錄，「籌碼面」類別最接近的 `TaiwanStockShareholding` 是外資持股，跟董監事無關。

**證交所有官方開放資料 CSV**（`mopsfin.twse.com.tw/opendata/t187ap11_L.csv`／`_O.csv`，上市／上櫃），免登入、免爬蟲，欄位直接含「設質股數」「設質股數佔持股比例」，資料乾淨，取得成本低。**但這個端點只回傳當月快照、不支援歷史月份查詢**（實測對 URL 加 `year=113&month=1` 參數，回傳內容仍是當月最新資料）——這是本查證發現的關鍵限制：能拿到「現在」的斷面，拿不到「回測需要的歷史序列」。

要拿到可回測的歷史，唯二可行路徑都要花錢：**TEJ 台灣經濟新報**（Company DB「上市（櫃）公司董監事持股資料(月)」，回溯至 1991 年，付費機構訂閱＋API）或 **CMoney 數據貓頭鷹**（「公開發行月董監持股與設質明細」，付費 API）——這兩項只查到搜尋摘要層級，沒有實際打開它們的 API 文件核對定價/欄位/更新頻率，如果之後真的要走這條路，需要先申請試用帳號或直接讀官方 API 文件核實。

## 逐項證據

1. **FinMind**：`https://finmind.github.io/llms-full.txt`（完整 dataset 目錄）與 `https://finmind.github.io/tutor/TaiwanMarket/Chip/`（籌碼面 25 個 dataset 清單）都搜過「董監事持股」「股票質押」「內部人持股」「insider」關鍵字，無對應項目。
2. **公開資訊觀測站 MOPS**：
   - 人工查詢頁 `mopsov.twse.com.tw/mops/web/stapap1` 只有網頁表單，無 API/CSV 匯出（教學文 `https://blog.ezbow.tw/2022/09/09/directors-supervisors-shareholding-pledging-of-shares` 只描述手動點選路徑）。
   - 官方開放資料 CSV（經 `https://data.gov.tw/dataset/22811` 連到）：`https://mopsfin.twse.com.tw/opendata/t187ap11_L.csv`（上市）／`t187ap11_O.csv`（上櫃），實測可直接下載，欄位含 `出表日期,資料年月,公司代號,公司名稱,職稱,姓名,選任時持股,目前持股,設質股數,設質股數佔持股比例,內部人關係人目前持股合計,內部人關係人設質股數,內部人關係人設質比例`；實測當下資料年月為 11508（2026-08），確認是活資料、每月更新，但**加歷史查詢參數無效，只回傳當月快照**。
   - 另有窄範圍資料集 `https://data.gov.tw/dataset/10574`（金管會銀行局，只收錄質押比例 >50% 的個案，CSV：`https://www.banking.gov.tw/webdowndoc?file=/stat/itopendata/banking6.csv`），同樣只有最新一期、無歷史回溯證據。
3. **第三方付費源**（僅 WebSearch 摘要層級，未逐頁核實）：TEJ（`https://api.tej.com.tw/databases.html`，Company DB「TH2」，1991 年起）、CMoney（`http://www.cmoney.com.tw/edu/data-list-01.html`，付費 API 金鑰）。

## 判斷

**現階段不建議投入工程時間**——不是因為因子構想沒有價值，而是免費資料源沒有歷史回溯能力（只能拿到「現在」，沒辦法算過去的 rank-IC，這個專案的評估方法論（`evaluate_candidate_factor()`）需要歷史序列才能跑），要嘛自己從今天開始按月存快照（等於用時間換歷史深度，這條路徑跟 `docs/factor_timeframe_optimization_directions.md` §3.4 對空頭樣本說的「等時間、不是等工程」是同一類限制，但更極端——現在存快照要等好幾年才有像樣的樣本數），要嘛付費訂閱 TEJ/CMoney（需要使用者決定要不要花這筆錢，不是工程判斷能決定的事）。

列為候選池裡的「付費資料源決策」項目，跟 `docs/https-finlab-finance-blog-python-get-tai-memoized-lighthouse.md` 裡「Fugle/finlab 這類新資料源該不該接」的既有討論同一量級——需要使用者對是否付費做決定，本查證不代為決定。
