# 台股追蹤器 v2.1：手機支援（同網段存取＋完整功能 RWD）

## Context（背景與目標）

v2（2026-07-08 完成並驗證）是本地 FastAPI 應用：`C:\Users\william.chen\claude\stock_tracker\`，目前只綁 localhost:8932、UI 是桌面版面（表格 min-width 1080px 橫向捲動）。

使用者需求：**手機上也能看**。問答確認（2026-07-08）：
- 場景：**同一 Wi-Fi 就好**——手機瀏覽 `http://<電腦IP>:8932`，不做雲端部署、不裝 tunnel
- 深度：**完整功能**——卡片式盤面、K 線面板、觸控十字線、新增/刪除/排序都要能用

後端不需要動（API 與計算與裝置無關），工作集中在：伺服器綁定與防火牆、前端 RWD、觸控互動。

## 改動清單

### 1. 區網存取（啟動層，不改 app.py）

- 新增 `start_server.bat`：一鍵啟動 `uvicorn app:app --host 0.0.0.0 --port 8932`，啟動前自動印出電腦的區網 IP（從 `ipconfig` 取），顯示「手機請開 http://192.168.x.x:8932」
- **Windows 防火牆**：新增 inbound 規則開放 TCP 8932（規則名 `stock-tracker-8932`，方便日後刪除）。實作時先查電腦目前網路的 profile（Domain/Private/Public）再決定套用範圍，不無腦全開
- `.claude/launch.json`（preview 用）維持 localhost 不動；README 註明兩者不要同時起（同 port 衝突）
- README 新增「手機存取」一節：步驟、AP isolation 疑難排解（公司 Wi-Fi 常擋裝置互連，連不上時第一個查這個）、安全註記（**無帳號驗證，同網段的人知道 IP 就能看與改追蹤清單**；在意的話之後可加簡單 token）

### 2. 手機版面（static/index.html，CSS 為主）

Breakpoint `max-width: 768px`，桌面版面完全不動：

- **表格 → 卡片**：CSS 把每列 `<tr>` 轉成卡片（display:grid + td data-label 標籤）；卡片結構——首行「代碼＋名稱＋來源 tag｜現價＋漲跌 badge」，次行 2×3 指標格（評分、OF Δ5日、VP位置、60MA、本益比、成交量），renderTable() 為每個 td 加 `data-label` 屬性供 CSS 顯示欄名
- **展開面板**：`.detail-grid` 已有 980px 斷點轉單欄（沿用）；訊號區與量化摘要字級微調
- **輸入防縮放**：input font-size 提到 16px（iOS Safari focus 自動放大的唯一解法）、加 `-webkit-tap-highlight-color`
- **觸控目標**：展開鈕與刪除鈕點擊區 ≥44px；刪除一律加 `confirm()` 確認（手機誤觸防呆，桌面也一致）
- stats-grid、add-bar、filter 列既有 flex/grid 已可自適應，只微調間距

### 3. 手機版 K 線圖（buildChartSVG 加 mobile 模式）

現有 SVG viewBox 960×476 直接縮到手機 ~360px 寬會讓 10px 標註字縮成 ~4px 不可讀。改法：

- `buildChartSVG(code, mobile)`：mobile 時 viewBox 改 ~480 寬、只畫**近 60 根** K 棒（`CHART_BARS` 後端照送 120，前端切片）、字級與 K 棒寬按比例放大、Y 軸標籤精簡（3 條網格）、VP 直方圖照畫
- 以 `matchMedia('(max-width:768px)')` 判定；斷點變化時（轉橫屏）重繪已展開面板
- **觸控十字線**：overlay rect 加 `touchstart`/`touchmove` handler，共用 klineMove 的定位邏輯（touches[0].clientX），點一下放十字線並顯示該日 OHLCV；`touch-action: pan-y` 保留垂直捲動

### 4. 加入主畫面（PWA-lite，小成本大體感）

- `static/manifest.json`（name、theme_color、display: standalone）＋ index.html `<link rel="manifest">` 與 apple-touch-icon meta；icon 用單一 SVG/PNG 簡單產一張
- 不做 service worker（看盤資料即時，離線快取無意義）

## 不改的東西

- app.py、data_sources.py、analysis.py 零改動（後端與裝置無關）
- 桌面版面與互動維持現狀（mobile CSS 全部包在 media query 內）
- 不做雲端部署、不裝 tunnel、不加登入（使用者未選；README 註記風險）

## 驗證計畫

1. **後端無變化**：pytest 13 測仍全過（防守性確認）
2. **手機版面**：preview_resize mobile（375×812）——卡片版面無橫向捲動、展開面板單欄、SVG 圖存在且標註字有效大小 ≥9px（用 getBoundingClientRect 實測，不用截圖猜）、新增/刪除/排序流程實跑
3. **桌面回歸**：preview_resize desktop——版面與 v2 一致（表格、面板、hover 都重測一輪）
4. **觸控**：preview_eval 派發 TouchEvent 驗證十字線與 OHLCV 資訊列會動
5. **區網綁定**：以 0.0.0.0 起 server，從本機對自己的區網 IP `curl http://<LAN-IP>:8932/api/watchlist` 驗證綁定與防火牆規則生效（`netsh advfirewall firewall show rule` 確認規則存在）
6. **真手機**：我無法操作實體手機——交付後請使用者用手機實測，README 附疑難排解清單（AP isolation、防火牆 profile、IP 變動）

## 假設（若證偽會回報）

- 路由器允許裝置互連（家用通常可；公司 Wi-Fi 若有 AP isolation 則手機連不上，屬環境限制非程式問題，README 有寫）
- 電腦區網 IP 可能因 DHCP 變動——bat 每次啟動重新偵測並顯示，不寫死
- 防火牆規則需要管理員權限——bat 不自動加規則（避免每次 UAC），改為一次性手動指令寫在 README，實作時我會直接執行一次（若權限不足則列入回報請使用者手動跑）
