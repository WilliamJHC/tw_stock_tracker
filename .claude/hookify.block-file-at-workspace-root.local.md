---
name: block-file-at-workspace-root
enabled: true
event: file
action: block
conditions:
  - field: file_path
    operator: regex_match
    pattern: ^[A-Za-z]:[\\/]Users[\\/]william\.chen[\\/]claude[\\/][^\\/]+$
---

🚫 **這個檔案會直接落在工作區根目錄**

CLAUDE.md 第 7 條：`C:\Users\william.chen\claude\` 只放專案資料夾與 `.claude\`，不放任何檔案、不建 venv、不跑 python。

請改成：
- 屬於某個專案的東西 → 寫進 `report_generator\` 或 `stock_tracker\` 底下對應的 `src/`、`tests/`、`scripts/`、`docs/`
- 臨時腳本、查詢檔、中間產物 → 寫進 `$CLAUDE_JOB_DIR/tmp`，不要留在工作區
