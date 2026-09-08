# 0005. STRATEGY_PARAMS 與 PORTFOLIO_PARAMS 的三層覆寫邏輯，backtest.py 與 portfolio_backtest.py 各自實作，不合併

## 狀態

已接受（決策本身定案於 2026-09-07 的 `7e9ea37`；本文件為 2026-09-08 `/improve-codebase-architecture` 架構調查補記，把當時只活在 commit message 裡的推理固定成查得到的紀錄）

## 背景

`src/backtest.py` 的 `_resolve_params(explicit_override, regime, by_regime)`（376-407 行）與 `src/portfolio_backtest.py` 的 `_resolve_invest_pct(explicit_override, regime, by_regime)`（126-150 行），實作的是同一套三層覆寫慣例：

```
模組級預設 → *_PARAMS_BY_REGIME[regime]（該盤勢的既有覆寫） → by_regime[regime]（呼叫端額外覆寫）
```

兩者的命名、參數順序、docstring 措辭都刻意保持一致——`portfolio_backtest.py:132` 的 docstring 直接寫「as backtest._resolve_params()'s by_regime, scoped to just this one knob」。這個重複在 `0c1b8ca` 引入 `portfolio_backtest.py` 那份覆寫邏輯時就已存在，`/code-review` 在同一個 commit 上抓到，並在下一個 commit `7e9ea37` 的訊息裡明確記錄「考慮過合併、決定不做」。

`2026-09-08` 的 `/improve-codebase-architecture` 架構調查重新掃到同一段重複（因為兩個檔案都在最近的盤勢功能開發中頻繁改動），本文件把 `7e9ea37` 當時的推理固定下來，避免下一輪調查再花時間重新論證一次同樣的結論。

## 決策

**不合併成共用函式，兩份三層覆寫邏輯維持獨立實作。**

## 為什麼不划算

兩者管的 key 範圍本質不同：

- `backtest._resolve_params()` 覆寫**整份** `params` dict（`STRATEGY_PARAMS` 的所有 key，如 `fee_pct`/`allow_entry`/`exit_composite`/`max_hold_days` 等）。
- `portfolio_backtest._resolve_invest_pct()` 刻意**只管 `invest_pct` 一個 key**——這是 `0c1b8ca` 的設計決定：`PORTFOLIO_PARAMS_BY_REGIME` 目前只需要覆寫這一個知識點，沒有理由讓呼叫端可以透過它動到 `PORTFOLIO_PARAMS` 的其他 key。

合併成一個共用函式，必須讓它同時支援「整份取代」與「單一 key 覆寫」兩種形狀，等於新增一層「這次是要換整包還是只換一格」的通用抽象——為了省下兩份加起來不到 30 行、彼此已經用一致命名與 docstring 顯式標註對應關係的程式碼，換一個新的通用介面要學、要維護，不划算。

## 考慮過但拒絕的方案

**抽出 `_resolve_layered_override(base, by_regime_map, regime, override, keys=None)`，`keys=None` 時整份覆寫、給定 `keys` 時只覆寫該子集。** `7e9ea37` 考慮過的做法。拒絕理由同上：這個 `keys` 參數本身就是專門為了遷就這兩個呼叫端形狀不同而加的新自由度，加了它之後，兩個呼叫端反而都要多讀一層「這次傳的是 None 還是一個 list」才看得懂自己在覆寫什麼，可讀性不會比現在兩份各自明確寫死「整份」或「一個 key」更好。

**把 `portfolio_backtest._resolve_invest_pct()` 也改成覆寫整份 `PORTFOLIO_PARAMS`，跟 `backtest._resolve_params()` 同形狀，才能真的共用同一個函式。** 拒絕理由：擴大 `PORTFOLIO_PARAMS_BY_REGIME` 目前刻意收斂的範圍（只准覆寫 `invest_pct`），是為了合併這次修正去承擔的額外風險面——未來如果真的需要覆寫 `invest_pct` 以外的 key，屆時再依實際需求擴大範圍，而不是現在為了消重複去預先開放。

## 影響

- 無程式碼變更，純文件記錄。
- 未來的架構調查（含 `/improve-codebase-architecture`）掃到 `backtest._resolve_params()`/`portfolio_backtest._resolve_invest_pct()` 這組重複時，應視為已決策事項，不需要重新列為候選——除非 `PORTFOLIO_PARAMS_BY_REGIME` 未來真的需要覆寫超過 `invest_pct` 一個 key，此時上述「範圍不同」的理由不再成立，值得重新評估。
