# Phase 0 Spike 筆記

執行日期：2026-07-20（週一）18:19-18:50 台北時間（收盤後，非交易時段）

## Binance WebSocket smoke test — 已完成

- 腳本：`scripts/spike_binance_ws_smoke_test.py`；完整 log：`docs/spike_ws_log.txt`
- 訂閱：`btcusdt`/`ethusdt`/`solusdt` 的 `aggTrade` + `depth20@1000ms`（combined stream）
- 跑滿 30 分鐘（18:19:53-18:50:02），總計 30,762 則訊息，**0 次斷線/重連**
- 結論：連線非常穩定，`realtime/binance_source.py` 目前的退避參數（初始 1 秒、上限 30 秒、倍增、成功後重置）沒有機會被真實觸發驗證，僅有 `next_backoff()` 純函式的單元測試覆蓋計算邏輯本身。若之後在正式運行中觀察到頻繁斷線，回頭調整退避參數即可，本次 smoke test 沒有理由現在就改。

## MIS 台股輪詢壓測 — 延後，待下一交易日盤中補測

- 現在（2026-07-20 週一 18:19 台北時間）已收盤（盤中為 09:00-13:30），無法做「watchlist 檔數 × 5 秒間隔連續 1 小時」的真實壓測
- 已用單次真實請求驗證：MIS 支援多檔用 `|` 分隔一次請求（例：`ex_ch=tse_2330.tw|tse_2317.tw|otc_6469.tw` 一次回傳 3 檔），故決定採計畫書建議的保守預設值：**整批一次請求、批次間隔 10 秒**（即單檔更新頻率同為 ≥10 秒），已寫進 `realtime/mis_source.py` 並在程式碼註解註明「待下一交易日盤中補測校正」
- 待補測項目：連續 1 小時輪詢下的成功率、是否觸發限流/擋 IP、10 秒間隔是否可再調快
- 下一個交易日（週二 2026-07-21，若非國定假日）09:00-13:30 期間可執行 `scripts/spike_binance_ws_smoke_test.py` 的姊妹腳本（尚未寫，屬於待辦，不在本次 Phase 0-4 範圍內，需要時再補一支類似的 MIS 壓測腳本）
