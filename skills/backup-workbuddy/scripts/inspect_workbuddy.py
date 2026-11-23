#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 数据体检（只读，不修改任何文件）
=========================================
用途：接手一台 WorkBuddy 机器时，先跑这个，把「数据根 / 用户 ID / 会话数量 /
      每条会话的 jsonl 是否真的在位 / 记忆目录状态」一次性看清楚，再决定要不要动。

用法：
  python inspect_workbuddy.py
  python inspect_workbuddy.py --root "C:/Users/me/.workbuddy-ai"
  python inspect_workbuddy.py --json          # 机器可读输出
"""

import argparse
import json
import os
import sqlite3
import sys

HOME = os.path.expanduser("~")


def human(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.1f} {u}" if u != "B" else f"{int(n)} B"
        n /= 1024.0


def compress_path(p):
    """与 App 的 normalizeWorkspacePathBase 等价"""
    for ch in ("/", "\\", ":"):
        p = p.replace(ch, "-")
    p = p.strip("-")
    while "--" in p:
        p = p.replace("--", "-")
    return p


def find_roots(explicit=None):
    if explicit:
        cand = os.path.abspath(explicit)
        return [cand] if os.path.isdir(cand) else []
    cands = []
    env = os.environ.get("WORKBUDDY_CONFIG_DIR") or os.environ.get("CODEBUDDY_CONFIG_DIR")
    if env:
        cands.append(env)
    for name in (".workbuddy", ".workbuddy-ai", ".codebuddy"):
        cands.append(os.path.join(HOME, name))
    seen, out = set(), []
    for c in cands:
        if c and os.path.isdir(c) and os.path.abspath(c) not in seen:
            seen.add(os.path.abspath(c))
            out.append(os.path.abspath(c))
    return out


def ro_connect(db):
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def inspect(root, argv):
    rep = {"root": root}
    # 身份
    uids = set()
    for name in os.listdir(root):
        if name.startswith("user-") and name.endswith("-personal"):
            uids.add(name[5:-9])
    memdir = os.path.join(root, "memory")
    if os.path.isdir(memdir):
        for f in os.listdir(memdir):
            if f.endswith("_memory.md") or f.endswith("_memory.md.bak"):
                uids.add(f.rsplit("_memory.md", 1)[0].replace(".bak", ""))
    dev = ""
    dpath = os.path.join(root, "device-id")
    if os.path.isfile(dpath):
        dev = open(dpath, encoding="utf-8", errors="replace").read().strip()
    rep["user_ids_from_files"] = sorted(uids)
    rep["device_id"] = dev

    db = os.path.join(root, "workbuddy.db")
    rep["db_exists"] = os.path.isfile(db)
    if not rep["db_exists"]:
        rep["warn"] = ["没有 workbuddy.db：可能是全新未登录，或数据根指错了"]
        return rep

    rep["db_size"] = human(os.path.getsize(db))
    rep["wal_present"] = [os.path.basename(p) for p in
                          (db + "-wal", db + "-shm") if os.path.exists(p)]
    con = ro_connect(db)
    try:
        tabs = [r[0] for r in con.execute(
            "select name from sqlite_master where type='table' order by name")]
        rep["table_counts"] = {}
        for t in ("sessions", "workspaces", "session_usage", "spaces", "space_folders",
                  "local_skills", "migration_meta", "__workbuddy_drizzle_migrations"):
            if t in tabs:
                try:
                    rep["table_counts"][t] = con.execute(f'select count(*) from "{t}"').fetchone()[0]
                except sqlite3.Error:
                    rep["table_counts"][t] = None
        rep["session_columns"] = len(con.execute("PRAGMA table_info(sessions)").fetchall()) \
            if "sessions" in tabs else 0
        rep["user_ids_from_db"] = [r[0] for r in
                                   con.execute("select distinct user_id from sessions")] \
            if "sessions" in tabs else []
        try:
            rep["sessions_visible"] = con.execute(
                "select count(*) from sessions where deleted_at is null").fetchone()[0]
        except sqlite3.Error:
            rep["sessions_visible"] = None
        try:
            rep["sessions_deleted"] = con.execute(
                "select count(*) from sessions where deleted_at is not null and deleted_at <> -1"
            ).fetchone()[0]
        except sqlite3.Error:
            rep["sessions_deleted"] = None

        # 每条会话的 jsonl 落点
        proj_root = os.path.join(root, "projects")
        rep["projects_root_exists"] = os.path.isdir(proj_root)
        rows = []
        if "sessions" in tabs:
            for sid, cwd, title, deleted in con.execute(
                    "select id, cwd, coalesce(custom_title,title), deleted_at from sessions"):
                item = {"id": sid, "title": title, "cwd": cwd,
                        "deleted": bool(deleted is not None and deleted != -1)}
                if cwd:
                    try:
                        canon = os.path.realpath(cwd)
                    except OSError:
                        canon = cwd
                    item["expect_dir"] = compress_path(canon)
                    item["expect_file"] = os.path.join(proj_root, item["expect_dir"], f"{sid}.jsonl")
                    item["jsonl"] = os.path.exists(item["expect_file"])
                    if item["jsonl"]:
                        item["bytes"] = os.path.getsize(item["expect_file"])
                    else:
                        # 兜底：按 id 在 projects 下搜（目录名规则变了也能找到）
                        found = None
                        if rep["projects_root_exists"]:
                            for d in os.listdir(proj_root):
                                p = os.path.join(proj_root, d, f"{sid}.jsonl")
                                if os.path.exists(p):
                                    found = p
                                    break
                        item["found_elsewhere"] = found
                rows.append(item)
        rep["sessions"] = rows
    finally:
        con.close()

    # 目录名与实际目录的偏差提示
    if rep.get("projects_root_exists"):
        actual = sorted(os.listdir(os.path.join(root, "projects")))
        rep["project_dirs"] = len(actual)
        rep["project_dir_sample"] = actual[:3]
        case_odd = [d for d in actual if d[:1].isupper()]
        if case_odd:
            rep.setdefault("notes", []).append(
                f"{len(case_odd)} 个目录以大写盘符开头（NTFS 大小写不敏感，通常无害）")

    # 工作区记忆目录
    misses, hits = [], []
    wss = set()
    for it in rep["sessions"]:
        if it.get("cwd"):
            wss.add(it["cwd"])
    if "workspaces" in rep["table_counts"]:
        try:
            con = ro_connect(db)
            for (p,) in con.execute("select path from workspaces"):
                if p:
                    wss.add(p)
            con.close()
        except sqlite3.Error:
            pass
    seen_ws = set()
    for ws in sorted(wss):
        if not os.path.isdir(ws):
            continue
        key = os.path.normcase(os.path.abspath(ws))
        if key in seen_ws:      # 同一工作区可能以 C:/… 与 C:\… 两种写法出现
            continue
        seen_ws.add(key)
        newd = os.path.join(ws, ".workbuddy", "memory")
        oldd = os.path.join(ws, ".workbuddy-ai", "memory")
        n_new = len([f for f in os.listdir(newd)]) if os.path.isdir(newd) else 0
        n_old = len([f for f in os.listdir(oldd)]) if os.path.isdir(oldd) else 0
        rec = {"workspace": ws, "new_memory_files": n_new, "legacy_memory_files": n_old}
        (hits if (n_new or n_old) else misses).append(rec)
    rep["workspaces_with_memory"] = hits
    rep["workspaces_without_memory"] = misses
    return rep


def render(rep, json_mode=False):
    if json_mode:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0
    print("=" * 68)
    print(f"数据根 : {rep['root']}")
    if not rep.get("db_exists"):
        print("  [警告] 无 workbuddy.db —— 全新未登录或路径不对")
        for w in rep.get("warn", []):
            print("   ", w)
        return 2
    print(f"库     : workbuddy.db {rep['db_size']}"
          f"{' + ' + '/'.join(rep['wal_present']) if rep['wal_present'] else '（无 wal/shm）'}")
    print(f"用户ID : 文件推断={rep['user_ids_from_files']} 库内={rep['user_ids_from_db']}")
    print(f"设备ID : {rep['device_id'] or '(无 device-id 文件)'}")
    print(f"session 表列数={rep['session_columns']}  drizzle 迁移={rep['table_counts'].get('__workbuddy_drizzle_migrations')}")
    print("-" * 68)
    tc = rep["table_counts"]
    print("行数:", ", ".join(f"{k}={v}" for k, v in tc.items() if k != "__workbuddy_drizzle_migrations"))
    print(f"会话: 可见 {rep['sessions_visible']} / 已删 {rep['sessions_deleted']} / 总计 {tc.get('sessions')}")
    print("-" * 68)
    ok = [s for s in rep["sessions"] if s.get("jsonl")]
    miss = [s for s in rep["sessions"] if s.get("cwd") and not s.get("jsonl")]
    elsew = [s for s in miss if s.get("found_elsewhere")]
    nocwd = [s for s in rep["sessions"] if not s.get("cwd")]
    print(f"正文 jsonl: 命中 {len(ok)} / 缺 {len(miss)} / 无 cwd {len(nocwd)}"
          f"  (projects 目录 {rep.get('project_dirs')} 个)")
    for s in miss:
        if s.get("found_elsewhere"):
            print(f"  ~ {s['title'] or s['id'][:8]}: 在别的目录名下找到了 -> {os.path.basename(os.path.dirname(s['found_elsewhere']))}")
        else:
            print(f"  ! {s['title'] or s['id'][:8]}({s['id'][:8]}): 全文不存在，旧库里就没正文"
                  f"{'（已删）' if s['deleted'] else ''}")
    print("-" * 68)
    print(f"有记忆的工作区: {len(rep['workspaces_with_memory'])} / 无记忆: {len(rep['workspaces_without_memory'])}")
    for w in rep["workspaces_with_memory"]:
        print(f"  {os.path.basename(w['workspace'])[:38]:38s} .workbuddy={w['new_memory_files']}  .workbuddy-ai={w['legacy_memory_files']}")
    for n in rep.get("notes", []):
        print("提示:", n)
    print("=" * 68)
    return 0


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy 数据体检（只读）")
    ap.add_argument("--root", help="指定数据根；省略则自动探测 ~/.workbuddy、~/.workbuddy-ai、~/.codebuddy")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()
    roots = find_roots(args.root)
    if not roots:
        if args.root:
            print(f"数据根不存在或不是目录: {args.root}", file=sys.stderr)
        else:
            print("没找到任何 WorkBuddy 数据根（试过 ~/.workbuddy、~/.workbuddy-ai、~/.codebuddy）",
                  file=sys.stderr)
        return 2
    rc = 0
    for i, r in enumerate(roots):
        rep = inspect(r, args)
        if not args.json and i:
            print()
        rc = max(rc, render(rep, args.json))
    return rc


if __name__ == "__main__":
    sys.exit(main())
