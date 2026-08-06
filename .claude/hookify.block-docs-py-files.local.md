---
name: block-docs-py-files
enabled: true
event: file
action: block
conditions:
  - field: file_path
    operator: regex_match
    pattern: (^|[\\/])docs[\\/].+\.py$
---

🚫 **`docs/` 底下不放 `.py`**

CLAUDE.md 第 9 條：`docs/` 只放 `.md`／`.txt`（設計文件、計畫書、spike 紀錄）。

- 一次性驗證／spike／維運腳本 → `scripts/`
- 應用程式碼 → `src/`
