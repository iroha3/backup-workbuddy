#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy（国内版）状态留底脚本  v1.0
================================================
作用：把「能原样还原」的东西打成一个 zip —— 包括元数据库、会话正文 jsonl、记忆、
      登录密钥、本地存储、各工作区记忆。排除可重装的 plugins/binaries/缓存/日志。

和 export_conversations.py 的区别：
  export_conversations.py  -> 只读导出 Markdown（有损、不能还原）
  本脚本                    -> 可还原快照（db + 原始 jsonl + 记忆 + 密钥）

用法：
  python workbuddy-snapshot.py                # 打包到 ~/WorkBuddy-Backups，保留最近 5 份
  python workbuddy-snapshot.py --keep 10      # 保留最近 10 份
  python workbuddy-snapshot.py --out D:\\bak    # 换输出目录
  python workbuddy-snapshot.py --dry-run      # 只统计，不写文件
  python workbuddy-snapshot.py --list         # 列出会收录的大项和体积

说明：
  * App 运行时也能跑：db 用 sqlite3 的在线备份接口，不会读到半个事务。
  * 环境变量 WORKBUDDY_CONFIG_DIR 可覆盖数据根（默认 ~/.workbuddy）。
  * 退出码：0 成功；1 有文件读取失败但仍生成；2 致命错误。
"""

import argparse
import glob
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile

HOME = os.path.expanduser("~")
SRC = os.environ.get("WORKBUDDY_CONFIG_DIR") or os.path.join(HOME, ".workbuddy")
OUT_DIR = os.path.join(HOME, "WorkBuddy-Backups")
PREFIX = "workbuddy-state-"
KEEP_DEFAULT = 5

# 各工作区（含 spaces）可能存在的根，工作区记忆从这些位置抓
WS_ROOTS = [os.path.join(HOME, "WorkBuddy"), os.path.join(HOME, "WorkBuddy AI")]

# 不进包的东西（相对数据根的 posix 路径前缀）：可重装 / 纯缓存 / 纯日志 / 威胁库
EXCLUDE_PREFIXES = (
    "plugins/", "binaries/", "cache/", "logs/", "traces/",
    "connectors-marketplace-sources/", "external-packages/", "appearance-resources/",
    "security/threat-database/",
    "app/CodeCache/", "app/cache/", "app/Crashpad/", "app/GPUCache/",
    # Chromium 配置目录（缓存/Service Worker/字体缓存…）与下载的封面图、拼写词典
    "app/session/", "app/playbook-covers/", "app/en-US-10-1.bdic", "app/lockfile",
    "agent-cli-prompt-vars/", "knowledge-upload-tmp/", "pending-telemetry/",
    "editor-sdk-sandbox-ws/", "shell-snapshots/", "file-tree-manifests/",
)
# db 由在线备份接口单独处理，不直接复制（避免复制到写了一半的 wal）
SKIP_TOP_FILES = ("workbuddy.db", "workbuddy.db-wal", "workbuddy.db-shm",
                  "workbuddy.db.bak-before-import", "workbuddy.db.bak2-before-spaces")


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0


def is_excluded(rel: str) -> bool:
    rel = rel.replace(os.sep, "/")
    return rel.startswith(EXCLUDE_PREFIXES)


def collect_data_root(verbose=False):
    """遍历数据根，返回 [(abs_path, arcname, size)]"""
    items = []
    for dirpath, dirnames, filenames in os.walk(SRC):
        rel_dir = os.path.relpath(dirpath, SRC)
        if rel_dir == ".":
            rel_dir = ""
        # 剪枝：整棵被排除的目录不再往下走（大小写不敏感比较）
        keep = []
        for d in dirnames:
            rel = (rel_dir + "/" + d).lstrip("/")
            if not is_excluded(rel + "/"):
                keep.append(d)
            elif verbose:
                print(f"    [跳过目录] {rel}/")
        dirnames[:] = keep
        for f in filenames:
            rel = (rel_dir + "/" + f).lstrip("/")
            if rel in SKIP_TOP_FILES or is_excluded(rel):
                continue
            ap = os.path.join(dirpath, f)
            try:
                size = os.path.getsize(ap)
            except OSError:
                size = 0
            items.append((ap, rel.replace(os.sep, "/"), size))
    return items


def collect_workspace_memory(verbose=False):
    """各工作区里的记忆：<工作区>/.workbuddy/memory/*.md 以及 <工作区>/MEMORY.md"""
    items = []
    for root in WS_ROOTS:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            ws = os.path.join(root, name)
            if not os.path.isdir(ws):
                continue
            for sub in (ws, *[os.path.join(ws, n) for n in ("spaces",)]):  # spaces 再下一层另处理
                pass
            targets = [ws]
            if os.path.isdir(os.path.join(ws, "spaces")):
                for s in sorted(os.listdir(os.path.join(ws, "spaces"))):
                    sp = os.path.join(ws, "spaces", s)
                    if os.path.isdir(sp):
                        targets.append(sp)
            for t in targets:
                memdir = os.path.join(t, ".workbuddy", "memory")
                if os.path.isdir(memdir):
                    for f in sorted(os.listdir(memdir)):
                        ap = os.path.join(memdir, f)
                        if os.path.isfile(ap):
                            rel = os.path.relpath(ap, HOME).replace(os.sep, "/")
                            items.append((ap, "workspaces/" + rel, os.path.getsize(ap)))
                top_mem = os.path.join(t, "MEMORY.md")
                if os.path.isfile(top_mem):
                    rel = os.path.relpath(top_mem, HOME).replace(os.sep, "/")
                    items.append((top_mem, "workspaces/" + rel, os.path.getsize(top_mem)))
            if verbose and items:
                pass
    return items


def backup_db(stage_dir):
    """用 sqlite3 在线备份接口复制 db（App 在跑也安全），返回 (路径, 统计 dict)"""
    src_db = os.path.join(SRC, "workbuddy.db")
    if not os.path.isfile(src_db):
        return None, {}
    dst = os.path.join(stage_dir, "workbuddy.db")
    con = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(dst)
        with out:
            con.backup(out)
        stats = {}
        for t in ("sessions", "spaces", "space_folders", "workspaces", "session_usage"):
            try:
                stats[t] = out.execute(f'select count(*) from "{t}"').fetchone()[0]
            except sqlite3.Error:
                stats[t] = None
        try:
            stats["sessions_visible"] = out.execute(
                "select count(*) from sessions where deleted_at is null").fetchone()[0]
        except sqlite3.Error:
            pass
        out.close()
    finally:
        con.close()
    return dst, stats


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy 状态留底")
    ap.add_argument("--out", default=OUT_DIR, help=f"输出目录（默认 {OUT_DIR}）")
    ap.add_argument("--keep", type=int, default=KEEP_DEFAULT, help="保留最近几份（默认 5）")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写文件")
    ap.add_argument("--list", action="store_true", help="列出收录项和体积")
    ap.add_argument("--verbose", "-v", action="store_true", help="打印跳过/失败细节")
    args = ap.parse_args()

    if not os.path.isdir(SRC):
        print(f"[错误] 数据目录不存在: {SRC}", file=sys.stderr)
        return 2

    t0 = time.time()
    print(f"数据根 : {SRC}")
    print(f"输出   : {args.out}  (保留最近 {args.keep} 份)")
    print("-" * 60)

    items = collect_data_root(args.verbose)
    ws_items = collect_workspace_memory(args.verbose)

    # 按顶层目录汇总，便于肉眼看
    if args.list:
        agg = {}
        for _ap_, arc, size in items + ws_items:
            top = arc.split("/")[0] if "/" in arc else "(根目录文件)"
            a = agg.setdefault(top, [0, 0])
            a[0] += 1
            a[1] += size
        print(f"{'项':32s} {'文件数':>7s} {'体积':>10s}")
        for k, (n, s) in sorted(agg.items(), key=lambda x: -x[1][1]):
            print(f"{k:32s} {n:7d} {human(s):>10s}")
        print("-" * 60)
        total = sum(s for _, _, s in items + ws_items)
        print(f"合计 {len(items) + len(ws_items)} 个文件 / {human(total)}（db 另算）")

    stage = tempfile.mkdtemp(prefix="wb-snap-")
    failures = []
    try:
        db_path, db_stats = backup_db(stage)
        if args.list:
            print("[--list] 只统计不写文件（同 --dry-run）")
            return 0
        if args.dry_run:
            print(f"[dry-run] 数据文件 {len(items)} 个 + 工作区记忆 {len(ws_items)} 个，"
                  f"{human(sum(s for _, _, s in items + ws_items))}；db {human(os.path.getsize(db_path)) if db_path else '无'}")
            print(f"[dry-run] db 统计: {db_stats}")
            return 0

        os.makedirs(args.out, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        zip_path = os.path.join(args.out, f"{PREFIX}{stamp}.zip")
        part = zip_path + ".part"

        written = 0
        written_bytes = 0
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            if db_path:
                z.write(db_path, "workbuddy.db")
                written += 1
                written_bytes += os.path.getsize(db_path)
            for ap_, arc, size in items:
                try:
                    z.write(ap_, arc)
                    written += 1
                    written_bytes += size
                except (OSError, PermissionError) as e:
                    failures.append((arc, str(e)))
                    if args.verbose:
                        print(f"    [读取失败] {arc}: {e}")
            for ap_, arc, size in ws_items:
                try:
                    z.write(ap_, arc)
                    written += 1
                    written_bytes += size
                except (OSError, PermissionError) as e:
                    failures.append((arc, str(e)))
                    if args.verbose:
                        print(f"    [读取失败] {arc}: {e}")
            manifest = {
                "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                "source_root": SRC,
                "database": db_stats,
                "files": written,
                "bytes_before_zip": written_bytes,
                "workspace_memory_files": len(ws_items),
                "read_failures": [f[0] for f in failures],
            }
            z.writestr("MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        os.replace(part, zip_path)

        zsize = os.path.getsize(zip_path)
        # 自检
        with zipfile.ZipFile(zip_path) as z:
            bad = z.testzip()
            n_entries = len(z.namelist())
        print(f"完成   : {zip_path}")
        print(f"内容   : {written} 个文件（原始 {human(written_bytes)}）-> 压缩后 {human(zsize)}")
        print(f"自检   : 条目 {n_entries}，损坏 {bad or '无'}")
        print(f"db     : {db_stats}")
        print(f"耗时   : {time.time() - t0:.1f}s")
        if failures:
            print(f"警告   : {len(failures)} 个文件读取失败（多半是被独占占用），已跳过", file=sys.stderr)

        # 轮转
        old = sorted(glob.glob(os.path.join(args.out, f"{PREFIX}*.zip")))
        while len(old) > args.keep:
            victim = old.pop(0)
            try:
                os.remove(victim)
                print(f"轮转   : 删除旧份 {os.path.basename(victim)}")
            except OSError as e:
                print(f"[警告] 删除失败 {victim}: {e}", file=sys.stderr)
        remaining = sorted(glob.glob(os.path.join(args.out, f"{PREFIX}*.zip")))
        print(f"现存   : {len(remaining)} 份")
        for p in remaining:
            print(f"         {os.path.basename(p)}  {human(os.path.getsize(p))}")
        return 1 if failures else 0
    finally:
        shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
