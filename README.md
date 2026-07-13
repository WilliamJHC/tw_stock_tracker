# 台股追蹤器 v2（本地 Python 版）

前身是單檔 HTML 版（`legacy/taiwan_stock_tracker.html`，保留於此當參考）。
v2 改為 Python 後端＋網頁 UI：資料抓取與指標計算全部在 Python，瀏覽器只負責顯示。

## 功能

- 追蹤清單（`watchlist.json` 持久化）、即時報價、本益比
- K 線圖＋ MA5/20/60 ＋支撐壓力
- **Volume Profile**：POC／Value Area（70%）／HVN･LVN，直方圖疊在 K 線旁
- **Order Flow**：內外盤 delta 與累積 delta
  - FinMind 付費級 token → 逐筆 tick 精確計算
  - 免費級 token → 自動降級為 yfinance 分鐘 K 的 tick-rule 推估（UI 會標示）
- 量價四象限、20 日趨勢推估區間、綜合技術評分、訊號回測勝率
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
