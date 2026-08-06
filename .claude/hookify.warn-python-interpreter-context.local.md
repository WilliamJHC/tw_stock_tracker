---
name: warn-python-interpreter-context
enabled: true
event: bash
action: warn
pattern: (?<![\\/.\w])(python3?(\.exe)?|pip3?)\b
---

⚠️ **直接呼叫 python/pip，沒有指名哪個專案的直譯器**

CLAUDE.md 第 7–8 條：
- 工作區根目錄不跑 python（要先 `cd` 進 `report_generator\` 或 `stock_tracker\`）
- venv 只在專案根目錄建，一個專案一個；跑 python 前用該專案自己的直譯器（例如 `.venv\Scripts\python.exe`），不要借用另一個專案的 venv

繼續前確認：現在的 cwd 是哪個專案？用的是不是那個專案自己的 `.venv`？

（2026-07-29 曾因為借錯專案的 venv、又跨 Python 次版本就地升級，裝了東西卻一 import 就死，整個 venv 得刪掉重建）
