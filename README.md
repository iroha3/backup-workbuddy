# backup-workbuddy

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)

备份、迁移、恢复 **WorkBuddy 桌面版**（国际版 `~/.workbuddy-ai` ↔ 国内版 `~/.workbuddy`）的会话数据。

WorkBuddy 把会话拆成两处存：**SQLite 库**（列表/标题/分组/用量）和 **每会话一个 `jsonl`**（正文）。备份或迁移时只搬一边，结果就是「列表有、点开空白」或者「文件都在、列表没有」——这类问题靠猜是猜不出来的。这个 skill 把路径规则、表结构、症状对照表和两个能直接跑的脚本一起带上，让 Agent 接手就能定位。

## 装

仓库是一个 [Agent Skill](https://agentskills.io)（`skills/backup-workbuddy/`）。

```text
/plugin marketplace add iroha3/backup-workbuddy     # Claude Code
/plugin install backup-workbuddy@backup-workbuddy

pi install git:github.com/iroha3/backup-workbuddy    # pi
```

手动装：把 `skills/backup-workbuddy/` 放进 `~/.agents/skills/` 或 pi 设置里的 `skills` 数组。

## 用

装上之后直接说人话就行：

- 「给我备份一下 WorkBuddy 的会话数据」
- 「我换了 WorkBuddy 版本，把旧会话迁过去」
- 「WorkBuddy 侧栏少了 7 个会话，文件还在，为什么」

Agent 会先跑体检脚本，再决定动作。也可以自己跑：

```bash
# 只读体检：数据根、用户ID、各表行数、每条会话的 jsonl 是否真的在位、记忆目录状态
python skills/backup-workbuddy/scripts/inspect_workbuddy.py

# 状态留底：打包成可还原的 zip（app 开着也能跑，db 走 sqlite 在线备份接口）
python skills/backup-workbuddy/scripts/workbuddy_snapshot.py
python skills/backup-workbuddy/scripts/workbuddy_snapshot.py --list      # 只看会打包什么
python skills/backup-workbuddy/scripts/workbuddy_snapshot.py --keep 10   # 保留份数
```

留底产物：`~/WorkBuddy-Backups/workbuddy-state-<时间戳>.zip`，实测 27 MB 原始数据 → 5.9 MB、0.8 秒，
内含 `workbuddy.db`、`projects/`（全部 jsonl）、记忆、登录密钥和各工作区 `.workbuddy/memory`，
外带 `MANIFEST.json` 记录各表行数以便核对。自动排除 `plugins/`、`binaries/`、Chromium 缓存、
威胁库等可重装内容。

## 包里有什么

| 文件 | 作用 |
|---|---|
| `SKILL.md` | 主指令：现场勘查 → 备份 → 迁移 → 症状对照 → 纪律 |
| `scripts/inspect_workbuddy.py` | 只读体检，按路径规则重算每条会话的落点并核对文件 |
| `scripts/workbuddy_snapshot.py` | 可还原快照（db 用在线备份接口，热备份安全） |
| `references/migration-playbook.md` | 数据布局、两版差异表、目录名算法、SQL 级迁移配方、真实案例数字 |
| `references/app-internals.md` | 规则说不清时，怎么从 `app.asar` 里把路径逻辑挖出来 |

## 两个容易踩的点（文档里都有）

1. **两个版本的数据根不同**（`.workbuddy-ai` → `.workbuddy`），工作区里的配置目录名也跟着变。
   按原路径照搬 = 记忆静默失效。
2. **侧栏条数不对 ≠ 文件丢了**。会话带 `space_id` 而 `spaces`/`space_folders` 表没同步时，
   它们会被当成孤儿过滤掉；补表立刻恢复，跟 jsonl 毫无关系。

## 纪律

- 先快照，再动手；快照失败就不要继续。
- 不整库覆盖 `workbuddy.db`，按表导入并改写用户 ID。
- 不把 `.workbuddy/` 当缓存删；不擅自清 `deleted_at`。
- 每一步都给出可核对的数字（行数、文件数、MD5），而不是「应该好了」。

## 开发

```bash
python tools/check-skill.py      # frontmatter / 清单 / 目录名一致性
```

改了 `SKILL.md` 或脚本记得 bump `package.json` 与 `.claude-plugin/plugin.json` 的版本号。

## License

MIT © iroha3
