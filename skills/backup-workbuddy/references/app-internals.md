# 从安装包里挖「路径规则」（WorkBuddy / Electron 应用通用）

当行为与文档不符、目录名/存储位置说不清时，**别猜**，直接读安装包里被 bundler 打包的源码。
WorkBuddy 的安装目录结构：

```
%LOCALAPPDATA%\Programs\WorkBuddy\
├─ WorkBuddy.exe            (Electron 主程序)
└─ resources\
   ├─ app.asar              (打包的 JS，typically 100~200 MB，单行二进制)
   ├─ app.asar.unpacked\    (体积大/含 exe 的文件不打包，直接放这)
   │   ├─ cli\dist\codebuddy-lite-wb.mjs   ← agent CLI（路径与存储逻辑常在这）
   │   └─ node_modules\@genie\agent-harness\cli\dist-server\*.cjs
   └─ install-manifest.json
```

## 1. 先决定要不要解包

- 只想验证某个字符串是否存在 → `grep -a` 直接扫 asar（整个文件是一行，`-c` 只能给 1）。
- 要看某函数实现 → 解析 asar 头，按需取出单个文件（几 MB 的 JS，可读）。

## 2. asar 头格式与提取（Python，无需 node）

```python
import json, struct, os
p = r"...\resources\app.asar"
f = open(p, "rb")
head = f.read(16)
a, b, c = struct.unpack("<III", head[:12])   # b = pickle 大小, c = JSON 长度相关
f.seek(16)
idx, _ = json.JSONDecoder().raw_decode(f.read(c).decode("utf-8", "replace"))  # 容忍尾部多余字节
base = 8 + b                                  # 文件数据区起点

def get(path):
    node = idx
    for part in path.split("/"):
        node = node["files"][part]
    if node.get("unpacked"):                  # 落在 app.asar.unpacked 里
        return open(os.path.join(r"...\app.asar.unpacked", path.replace("/", os.sep)), "rb").read()
    f.seek(base + int(node["offset"]))        # 注意 offset 是字符串，要 int()
    return f.read(node["size"])
```

踩坑：`offset` 在 asar 头里是**字符串**（避免精度丢失），直接相加会报 `int + str`；
JSON 后面可能跟着 4 字节对齐垃圾，用 `raw_decode` 而不是 `json.loads`。

## 3. 该 grep 什么

| 想确认 | 关键字 |
|---|---|
| 数据根 | `".workbuddy"`、`.workbuddy-ai`、`WORKBUDDY_CONFIG_DIR`、`homedir()` |
| 会话正文位置 | `getHomeProjectsDir`、`projects`、`.jsonl`、`tool-results` |
| 目录名算法 | `compressWorkspacePathName`、`normalizeWorkspacePathBase`、`djb2Base36` |
| 每工作区配置目录 | `projectDirName`、`getProjectHomeDirFor`、`dataLayout` |
| 记忆文件 | `MEMORY.md`、`dailyLogPattern`、`getMemoryFilePath`、`memory` |
| db 位置/迁移 | `drizzle`、`workbuddy.db`、`migrations` |

命令示例（asar 内大小写混杂的压缩代码，用宽松正则）：

```bash
grep -aoE '.{160}projectDirName.{200}' app.asar.unpacked/cli/dist/codebuddy-lite-wb.mjs
grep -aoE '.{150}replace\(/\[[^/]{0,25}/g[^)]{0,30}\)' app.asar.unpacked/cli/dist/codebuddy-lite-wb.mjs
```

`sanitizeAgentTypeForPath` 之类同类函数会干扰判断：注意看**调用点**（谁把 cwd 传进去），
不要只看函数定义就下结论。

## 4. 本次用这条路线得到的结论（可直接复用）

- 数据根由"产品配置"决定：国际版 `.workbuddy-ai`、国内版 `.workbuddy`，
  同一份代码换产品常量 → 目录名不同，所以**跨版本绝不能按原路径照搬**。
- `projects\<名>\<id>.jsonl` 的 `<名>` = `compressPath(realpath(cwd))`，规则见
  `references/migration-playbook.md` §3。
- 每工作区配置目录 = `<工作区>\<dataLayout.configDir.projectDirName>`，本机国内版为 `.workbuddy`。
- db schema 由 drizzle 迁移管理，两版迁移集合一致时可直接搬数据。

## 5. 更省事的替代

如果只是想知道"现在这条会话的正文在哪"，不用读 asar：跑
`python scripts/inspect_workbuddy.py`，它按同一套规则重算路径并核对文件是否存在。
只有"规则本身可能变了"的怀疑，才值得去解包。
