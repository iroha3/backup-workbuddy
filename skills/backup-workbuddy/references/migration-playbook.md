# WorkBuddy 数据布局与迁移手册（实战版）

> 来源：2026-11-23 一次真实的「国际版 5.11.4 → 国内版 5.11.3」迁移，18 条会话全部恢复。
> 下面的数字都是实测值，用来对照校验；路径以 本机为例，换机器只改 `%USERPROFILE%`。

## 1. 数据布局

```
<root>\
├─ workbuddy.db (+ -wal, -shm)      ← 元数据：会话列表/标题/分组/用量
├─ projects\
│   └─ <压缩后的cwd>\
│       ├─ <sessionId>.jsonl            ← 会话正文（核心）
│       ├─ <sessionId>.file-rollback.ndjson
│       ├─ tool-results\                ← 工具输出原文
│       └─ subagents\                   ← 子 agent 的独立会话
├─ memory\<用户ID>_memory.md         ← 全局记忆
├─ user-<用户ID>-personal\           ← MEMORY/SOUL/IDENTITY/USER/BOOTSTRAP.md
├─ blobs\<xx>\<sha>.<ext>            ← 会话引用的图片
├─ settings.json / keyblob / device-id* / login-device-nonce
├─ local_storage\ / storage\ / security\<用户ID>\   ← 登录态、本地存储
├─ sessions\  workspace\sessions\    ← 新版才有（空目录正常）
├─ 可重装/缓存：plugins\ binaries\ cache\ logs\ traces\
│                connectors-marketplace-sources\ external-packages%
│                app\session\ app\CodeCache\ app\playbook-covers\
│                security\threat-database\
└─ 工作区本体不在 root 下：<USERPROFILE>\WorkBuddy\<时间戳>\ （国内版）
                          <USERPROFILE>\WorkBuddy AI\<时间戳>\ （国际版）
   每个工作区里有 .workbuddy\memory\{YYYY-MM-DD.md, MEMORY.md}
```

## 2. 两版差异表（国际版 vs 国内版）

| 维度 | 国际版（5.11.4） | 国内版（5.11.3） |
|---|---|---|
| 数据根 | `~\.workbuddy-ai` | `~\.workbuddy` |
| 工作区配置目录 | `<工作区>\.workbuddy-ai` | `<工作区>\.workbuddy` |
| 工作区默认根 | `%USERPROFILE%\WorkBuddy AI\` | `%USERPROFILE%\WorkBuddy\` |
| 每日记忆 | `.workbuddy-ai\memory\YYYY-MM-DD.md` | `.workbuddy\memory\YYYY-MM-DD.md` |
| 项目长期记忆 | 无 | `.workbuddy\memory\MEMORY.md`（3000 字上限） |
| 正文存储 | `projects\<压缩cwd>\<id>.jsonl` | 同左，无变化 |
| db schema | 19 表 / 24 个 drizzle 迁移 | 一致 |
| 用户 ID | 每台登录各一个 | 同 |
| 覆盖配置根 | — | `WORKBUDDY_CONFIG_DIR` / `CODEBUDDY_CONFIG_DIR` |

**判断两版是否可以直搬**：比较 `select count(*) from __workbuddy_drizzle_migrations`
和 `PRAGMA table_info(sessions)` 的列数（本次两侧都是 24 / 42 列）。

## 3. 目录名算法（决定 jsonl 能不能被找到）

```js
// @genie/agent-harness + cli 里各一份，函数体完全相同
function normalizeWorkspacePathBase(t){
  return t.replace(/[/\\:]/g,"-")   // 斜杠、反斜杠、冒号 → "-"
          .replace(/^-+/,"").replace(/-+$/,"").replace(/-+/g,"-")
}
function compressWorkspacePathName(t){
  const i = normalizeWorkspacePathBase(t);
  if (Buffer.byteLength(i,"utf8") <= 255) return i;
  return `${truncateToByteLimitOnCodePoint(i,180)}-${djb2Base36(i)}`;  // 超长才截断+哈希
}
// 调用：join(getHomeProjectsDir(), compressPath(canonicalizeStorePath(cwd)), `<sessionId>.jsonl`)
// canonicalizeStorePath = fs.realpathSync(cwd)（Windows 返回真实大小写，如 C:\Users\...）
```

- 例：`%USERPROFILE%\WorkBuddy AI\2026-11-15-15-59-56` → `C-Users-<user>-WorkBuddy AI-2026-11-15-15-59-56`
- **NTFS 大小写不敏感**，`c-…` 与 `C-…` 是同一个目录，盘符大小写不影响可读性；
  读取侧还有 `resolveWorkspaceProjectKeyCandidates`（同时试 `canonical` 与 `legacy-raw`）兜底。
- 真正会让“会话丢了”的是 **db 行里的 `cwd` 与实际工作区不一致**（工作区被删/被改名）。

## 4. 迁移步骤（SQL 级）

```python
OLD_UID = "<旧用户ID>"; NEW_UID = "<新用户ID>"
NEW_DEV = open(r"<新root>\device-id").read().strip()   # 仅 spaces 表需要
for t in ("sessions", "workspaces", "session_usage", "spaces", "space_folders"):
    cols = [r[1] for r in old.execute(f'PRAGMA table_info("{t}")')]
    for row in old.execute(f'select * from "{t}"'):
        d = dict(zip(cols, row))
        if d.get("user_id") == OLD_UID: d["user_id"] = NEW_UID
        if "device_id" in d: d["device_id"] = NEW_DEV
        new.execute(f'insert or replace into "{t}" ({",".join(cols)}) values ({",".join("?"*len(cols))})',
                    [d[c] for c in cols])
new.commit()
```

- 表之间**没有外键约束**，可以放心按表插入。
- `sessions` 主键是 `id TEXT`（uuid 或雪花串），跨机器不冲突，可原样保留。
- **不要导入**：`__workbuddy_drizzle_migrations`、`migration_meta`、`local_skills`、`skill_sync_meta`
  （属于目标环境自身状态，覆盖会带来脏数据）。
- `spaces.create_folder_path` / `primary_folder_path` 指向的目录要存在，否则打开 space 报错；
  `space_folders` 只是 (space_id, path) 两列。

## 5. 真实案例数字（2026-11-23）

迁入后应有的核对值：

```
sessions 26 行（18 可见 + 7 deleted_at 标记 + 1 playground）
spaces 5   space_folders 4   workspaces 23   session_usage 18   migrations 24
projects\ 18 个目录 / 141 文件 / 约 20 MB
工作区 12 个会话目录 + spaces\ 4 个；工作区记忆 md 14 个
```

当时踩坑：只显示 11 条 → 6 条会话的 `space_id` 指向未导入的 `spaces` 行被过滤；
补 `spaces` + `space_folders` 后变 17 条（+1 条新建会话 = 18 可见）。

旧库里 `deleted_at` 已有值的 6 条（`d48d8025`、`c7679690`、`3bc8f884`、`53a5a03c`、
`54e45a94`、`d1e50009`「打招呼」）导入后自然隐藏，**不要**手动清零。

## 6. 验证清单

1. `python scripts/inspect_workbuddy.py` → 正文 jsonl 命中数应等于有 cwd 的会话数
2. 侧栏条数 == `select count(*) from sessions where deleted_at is null`
3. 打开最长的会话（几 MB 的那种）看正文是否完整
4. 会话里问「你的每日记忆文件绝对路径」→ 应回 `<工作区>\.workbuddy\memory\YYYY-MM-DD.md`
5. 问「跨项目记忆在哪」→ `<root>\user-<新UID>-personal\MEMORY.md`

## 7. 兜底

`export_conversations.py`（随旧备份提供）能把 `projects\` 里所有 jsonl 导出成 Markdown，
**只读不可还原**；db 彻底坏掉时用它的产物保底。
