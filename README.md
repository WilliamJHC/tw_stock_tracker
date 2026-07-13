# 台股追蹤器 v2（本地 Python 版）

前身是單檔 HTML 版（`legacy/taiwan_stock_tracker.html`，保留於此當參考）。
v2 改為 Python 後端＋網頁 UI：資料抓取與指標計算全部在 Python，瀏覽器只負責顯示。

## 功能

- **今日決策面板**：開首頁最上方，按行動優先級排序告訴你今天該看哪幾檔、為什麼、建議動作（見下方「決策面板與籌碼分」）
- 追蹤清單（`watchlist.json` 持久化）、即時報價、本益比
- K 線圖＋ MA5/20/60 ＋支撐壓力
- **Volume Profile**：POC／Value Area（70%）／HVN･LVN，直方圖疊在 K 線旁
- **Order Flow**：內外盤 delta 與累積 delta
  - FinMind 付費級 token → 逐筆 tick 精確計算
  - 免費級 token → 自動降級為 yfinance 分鐘 K 的 tick-rule 推估（UI 會標示）
- 量價四象限、20 日趨勢推估區間、綜合技術評分、訊號回測勝率
- **籌碼面子分**：法人買賣超／融資融券／外資持股比／借券賣出餘額，與技術分並列顯示，不揉進同一個數字
- 資料來源優先度：**FinMind → yfinance → TWSE**（每檔股票顯示實際來源）

## 安裝與啟動

```powershell
cd C:\Users\william.chen\claude\stock_tracker
python -m venv .venv                       # 第一次
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m uvicorn app:app --port 8932
```

瀏覽器開 http://localhost:8932

日常使用直接雙擊 `start_server.bat` 即可（會顯示手機用的網址並以區網模式啟動）。

## 手機存取（同一 Wi-Fi）

1. 電腦雙擊 `start_server.bat` 啟動，畫面會顯示 `手機（同一Wi-Fi）請開 http://192.168.x.x:8932`
2. 手機連同一個 Wi-Fi，瀏覽器開那個網址
3. 想要 App 體驗：手機瀏覽器選單 →「加入主畫面」，會以全螢幕獨立視窗開啟

**第一次使用前，需要開防火牆（一次性設定，要系統管理員權限）：**

用「系統管理員身分」開 PowerShell，執行以下兩行：

```powershell
# 1. 把家用 Wi-Fi 從 Public 改為 Private（比在 Public 網路開 port 安全；公司/公共 Wi-Fi 不要改）
Set-NetConnectionProfile -Name "chenfamily" -NetworkCategory Private

# 2. 開放 8932 port（只對 Private 網路生效；日後移除：netsh advfirewall firewall delete rule name="stock-tracker-8932"）
netsh advfirewall firewall add rule name="stock-tracker-8932" dir=in action=allow protocol=TCP localport=8932 profile=private
```

**手機連不上時，依序檢查：**
1. 手機和電腦是否真的在同一個 Wi-Fi（訪客網路算不同網路）
2. 上面的防火牆規則是否已加、Wi-Fi profile 是否為 Private（`Get-NetConnectionProfile` 查看）
3. 路由器是否開了「AP 隔離／訪客隔離」（公司 Wi-Fi 常見，會擋裝置互連，只能請 IT 開或換網路）
4. 電腦 IP 是否變了（DHCP 會換 IP，重跑 `start_server.bat` 看最新網址）

**安全註記**：此 App 無帳號驗證，同網段任何人知道網址就能看到追蹤清單並增刪。家用網路通常無妨；若在意，之後可以加簡單的 token 驗證。

**注意**：`start_server.bat` 與 Claude preview（launch.json）都用 8932 port，不要同時啟動。

## 決策面板與籌碼分

**技術分**（`-100~100`）：MA 位置/排列、量價四象限、20日趨勢、Volume Profile 位置、Order Flow，權重見 `analysis.py` 的 `SCORE_WEIGHTS`。

**籌碼分**（`-100~100`，獨立子分，不併入技術分）：法人買賣超（權重40）／融資融券（20）／外資持股比（20）／借券賣出餘額（20），任一項缺資料時按可用權重歸一，四項全缺則整體為 `null`（明細卡會標示「N/4 項資料」）。權重常數在 `analysis.py` 的 `CHIP_WEIGHTS`。

**行動優先級**（決策面板排序依據）：

```
綜合分 = 0.6 × 技術分 + 0.4 × 籌碼分（籌碼分缺資料時 = 技術分）
優先級 = min(100, round(0.6 × |綜合分| + 事件加分))
```

事件加分：today 突破/跌破 MA60（各+15）、OF 量價背離（+15）、爆量>2倍均量（+10）、OF 或外資連續同向天數 ≥3（+10）、籌碼任一組件訊號強烈（+10）、價格在 Value Area 外（+5）。

行動判斷（由上而下取第一個命中）：綜合分≥40 且優先級≥50 →「準備進場」；綜合分≤-40 且優先級≥50 →「減碼警示」；優先級≥50 →「今日關注」；優先級≥25 →「觀察」；否則「無變化」。

**可調參數位置**（皆為產品判斷的合理預設，非唯一最優解）：
- `analysis.py` 的 `SCORE_BANDS`／`SCORE_LABELS`：技術分與籌碼分的多空標籤分界（前端經 `/api/config` 同步取用，改這裡前端會自動跟著變）
- `analysis.py` 的 `CHIP_WEIGHTS`：四個籌碼組件的權重
- `analysis.py` 的 `ACTION_THRESHOLDS`：行動優先級的 40/50/25 門檻
- `analysis.py` 的 `compute_attention()` 函式內：各事件加分的分值與各組件滿分刻度（法人 10%、融資 5%、外資持股 1pp、借券 5%）寫在 `chip_analysis()` 內

## 設定

- `.env`：`FINMIND_TOKEN=你的token`（https://finmindtrade.com 免費註冊後在會員頁取得）。
  沒填也能跑（FinMind 匿名額度較低或自動退到 yfinance），填了升到 600 次/hr。
  填完後用這行驗證 token 有生效、且能正常打 API：
  ```powershell
  .venv\Scripts\python -c "import data_sources as ds; print(ds.FINMIND_TOKEN[:8] or 'NO TOKEN'); print(len(ds._finmind('TaiwanStockPrice','2330','2026-07-01')), 'rows')"
  ```
- `watchlist.json`：追蹤清單，可直接編輯，格式 `[{"code": "2330", "name": "台積電"}, ...]`。
  上櫃股票直接填代碼即可（FinMind/TWSE 原生支援，yfinance 備援會自動嘗試 .TWO）。
- `cache/`：日線等資料的當日快取，可隨時整個刪掉。

## 免責

技術評分、訊號、回測勝率與推估區間均為歷史統計整理，屬機率傾向而非預測，不構成投資建議。
