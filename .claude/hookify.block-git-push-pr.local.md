---
name: block-git-push-pr
enabled: true
event: bash
action: block
pattern: git\s+push|gh\s+pr\s+create
---

🚫 **不自行 push／開 PR**

CLAUDE.md 第 12 條：`git commit` 可以自己做，但不自行 `push`、不開 PR。要推上遠端一律先問，得到明確同意才做（這條也蓋掉背景 job 預設「做完就 commit 並 push」的行為）。

已完成的變更請把分支名與 commit hash 回報給使用者，由使用者自己決定要不要推。
