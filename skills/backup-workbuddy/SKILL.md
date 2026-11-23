---
name: backup-workbuddy
description: 备份、迁移、恢复 WorkBuddy 桌面版（国际版 .workbuddy-ai ↔ 国内版 .workbuddy）的会话数据。用于：定期状态留底、跨版本搬迁会话、侧栏会话数量不对/点开空白、记忆不生效、会话正文 jsonl 定位、db 表导入与用户 ID 改写。
---

# WorkBuddy 数据备份 / 迁移 / 恢复

WorkBuddy 桌面版把「会话元数据 + 正文」拆成两处存：**SQLite 库**（列表、标题、分组）和
**每会话一个 jsonl**（正文）。备份、迁移、排障都必须同时看这两处，只搬一边就会出现
「列表有、点开空白」或「文件在、列表没有」。

## 0. 先摸清现场（无条件第一步）

```bash
python scripts/inspect_workbuddy.py          # 只读，不写任何东西
python scripts/inspect_workbuddy.py --root "C:/Users/me/.workbuddy-ai"
```

它会打印：数据根、用户 ID、device_id、各表行数、**每条会话的 jsonl 是否真的存在**、
各工作区记忆目录状态。任何判断先看这份报告，别猜。

关键位置（`<root>` = 数据根，Windows 用反斜杠）：

| 内容 | 路径 |
|---|---|
| 数据根 | `%USERPROFILE%\.workbuddy`（国内版）/ `.workbuddy-ai`（国际版）；可用 `WORKBUDDY_CONFIG_DIR` 覆盖 |
| 元数据库 | `<root>\workbuddy.db`（配套 `-wal` / `-shm`，三件套要一起动） |
| 会话正文 | `<root>\projects\<压缩cwd>\<sessionId>.jsonl` |
| 全局记忆 | `<root>\memory\<用户ID>_memory.md` |
| 人设/长期记忆 | `<root>\user-<用户ID>-personal\{MEMORY,SOUL,IDENTITY,USER,BOOTSTRAP}.md` |
| 工作区记忆 | `<工作区>\.workbuddy\memory\YYYY-MM-DD.md` + `MEMORY.md` |
| 登录/密钥 | `<root>\keyblob`、`device-id*`、`login-device-nonce`、`local_storage\`、`storage\`、`settings.json` |
| 纯缓存（可丢） | `<root>\{plugins,binaries,cache,logs,traces,connectors-marketplace-sources,external-packages}\`、`app\session\`、`app\CodeCache\`、`security\threat-database\` |

用户 ID 怎么找：目录名 `user-<UID>-personal`、文件名 `memory/<UID>_memory.md`，或
`select distinct user_id from sessions`。

## 1. 备份/留底（默认动作）

```bash
python scripts/workbuddy_snapshot.py            # -> ~/WorkBuddy-Backups/workbuddy-state-<时间戳>.zip
python scripts/workbuddy_snapshot.py --list     # 先看会打包什么、多大
python scripts/workbuddy_snapshot.py --keep 10  # 保留份数
```

要点：

- db 必须用 **sqlite3 在线备份接口**（`con.backup(out)`）复制，App 运行时直接 `copy` 可能拿到半截事务。
- 排除 `plugins\`(百 MB 级)、`binaries\`、`app\session\`（Chromium 缓存）、`security\threat-database\`。
- 各工作区的 `.workbuddy\memory\*.md` 要单独去抓（它们不在数据根下）。
- 打完必须自检：`zipfile.testzip()` + 条目数 + db 行数写进 `MANIFEST.json`。

**动任何数据前**都必须先做这份快照，并要求用户退出 App 与托盘进程（托盘进程会重写 db）。

## 2. 跨版本迁移（国际版 → 国内版，已验证可复现）

两版差异只有三处：**数据根名**（`.workbuddy-ai` → `.workbuddy`）、**用户 ID**、
**工作区配置目录名**（`.workbuddy-ai` → `.workbuddy`）。jsonl 格式与 db schema 通常一致，
先用第 0 步确认（比较 `__workbuddy_drizzle_migrations` 行数与 `PRAGMA table_info(sessions)`）。

步骤：

1. **先备份新装状态**（保登录）：`settings.json`、`workbuddy.db(+wal/shm)`、`keyblob`、
   `device-id*`、`login-device-nonce`、`local_storage\`、`storage\`、`memory\`、
   `user-<UID>-personal\`、`app\data`、`app\memory`。漏了 `keyblob` 就得重新登录。
2. **正文**：把 `raw/projects/` 原样放到 `<新根>\projects\`。
3. **元数据**：按表 `insert or replace` 导入（**不要整库覆盖**）：
   `sessions`、`workspaces`、`session_usage`、`spaces`、`space_folders`；
   **不要动** `__workbuddy_drizzle_migrations`、`migration_meta`、`local_skills`、`skill_sync_meta`。
   导入时改写 `user_id` → 新 ID，`spaces.device_id` → 本机 `<root>\device-id` 的内容。
   导入前先 `copy workbuddy.db workbuddy.db.bak-before-import`。
4. **记忆三处**：全局 `memory\<新UID>_memory.md`（**要改名**）、`user-<新UID>-personal\`、
   各工作区 `<工作区>\.workbuddy\memory\`（旧版的 `.workbuddy-ai\memory` 新版不认）。
5. **`spaces` 的 `create_folder_path`** 指向的目录要重建（空目录即可），否则打开 space 报错。
6. 启动验证：侧栏条数 == `MANIFEST` 里的 `sessions_visible`；打开最长的那条会话看正文；
   在会话里问「你的每日记忆文件绝对路径」应回 `<工作区>\.workbuddy\memory\YYYY-MM-DD.md`。

细节、踩坑与真实案例数字见 `references/migration-playbook.md`。

## 3. 症状 → 病因（排障速查）

| 症状 | 先查这里 |
|---|---|
| 侧栏条数少于 db 里 `deleted_at is null` 的行数 | 这些会话的 `space_id` 是否指向**不存在的 `spaces` 行**（孤儿被过滤）。查 `spaces` / `space_folders`，不是查文件 |
| 列表有、点开空白 | `<root>\projects\<压缩cwd>\<sessionId>.jsonl` 是否存在；缺就说明当初只导了 db 没搬正文 |
| 会话在、但新对话接不上历史 | jsonl 放在别的目录名下 → 会新建目录分叉。用 `inspect` 脚本核对每条会话的落点 |
| 记忆问答答不上来 | 记忆是否放进了 `.workbuddy\memory`（不是 `.workbuddy-ai`）；全局记忆文件名是否含**新**用户 ID |
| 打开 space 报错 | `spaces.create_folder_path` 目录是否被删 |
| 少了几条会话但文件都在 | `deleted_at` 有值（用户/旧版删过），保持原样，别手动清零 |

**目录名规则**（`normalizeWorkspacePathBase` + `compressWorkspacePathName`）：

```js
name = realpath(cwd).replace(/[/\\:]/g, "-").replace(/^-+/, "").replace(/-+$/, "").replace(/-+/g, "-")
if (Buffer.byteLength(name) > 255) name = name.slice(0,180 字节) + "-" + djb2Base36(原名)
// NTFS 大小写不敏感：c- 与 C- 是同一个目录；读取时还会同时尝试 canonical 与 legacy-raw 两种键
```

## 4. 兜底（db 坏了但 jsonl 还在）

```bash
python <backup>/export_conversations.py "<root>/projects"    # 导出 Markdown，仅可读不可还原
```

## 5. 硬性纪律

- **先快照，再动手**；快照失败就不要继续。
- 不整库覆盖 db；只按表导入 + 改写 ID。
- 不把 `.workbuddy\` 当缓存删（里面是项目数据/记忆）。
- 不擅自清 `deleted_at`，不擅自重建用户 ID。
- 每一步都给出可核对的数字（行数、文件数、MD5），别只说"应该好了"。
