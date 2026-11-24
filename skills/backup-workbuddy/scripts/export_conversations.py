#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 会话导出（只读，有损）
================================
把 `<数据根>/projects` 下每个会话的 jsonl 渲染成一份可读的 Markdown。
用途：db 坏掉 / App 打不开 / 只想把聊天记录留个文本底。**不能还原**，真正的备份
请用 workbuddy_snapshot.py。

标题优先取同目录 workbuddy.db 里 sessions 表的 custom_title / title，取不到就用
首条用户消息的前 40 字。db 只是锦上添花，缺了照常导出。

用法：
  python export_conversations.py                                  # 默认数据根 -> ./export-md
  python export_conversations.py --root "C:/Users/me/.workbuddy-ai"
  python export_conversations.py "D:/bak/projects" --out D:/md     # 兼容旧用法：直接给 projects
  python export_conversations.py --root "D:/bak" --db D:/bak/workbuddy.db

环境变量 WORKBUDDY_CONFIG_DIR 可覆盖默认数据根。
退出码：0 成功；1 有 jsonl 读取失败（其余已导出）；2 源目录不存在。
"""

import argparse
import glob
import json
import os
import re
import sqlite3
import sys
from datetime import datetime

HOME = os.path.expanduser("~")
DEFAULT_ROOT = os.environ.get("WORKBUDDY_CONFIG_DIR") or os.path.join(HOME, ".workbuddy")

# 这些路径下的 jsonl 不是会话正文：回滚快照、编辑器沙盒、shell 快照
SKIP_PARTS = (".file-rollback", "editor-sdk-sandbox-ws", "shell-snapshots")

REMINDER = re.compile(r"<system-reminder\b.*?</system-reminder>", re.S)
TEXT_BLOCKS = ("input_text", "output_text", "text")
TOOL_BLOCKS = ("tool_use", "tool_call")


def clean_text(t):
    if not t:
        return ""
    if not isinstance(t, str):
        t = str(t)
    return REMINDER.sub("", t).strip()


def safe_name(s):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", s or "")[:60].strip("_") or "untitled"


def to_datetime(ts):
    """时间戳可能是毫秒、秒或 ISO 字符串，认不出来就返回 None"""
    if ts is None or ts == "":
        return None
    try:
        if isinstance(ts, (int, float)) or re.fullmatch(r"\d+(\.\d+)?", str(ts)):
            v = float(ts)
            if v > 1e11:  # 毫秒
                v /= 1000.0
            return datetime.fromtimestamp(v)
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone()
    except (ValueError, OSError, OverflowError):
        return None


def load_titles(db):
    """session_id -> 标题；db 不存在或读不动就返回空表"""
    titles = {}
    if not db or not os.path.isfile(db):
        return titles
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            for sid, custom, title in con.execute(
                "select id, custom_title, title from sessions"
            ):
                if sid:
                    titles[sid] = clean_text(custom) or clean_text(title)
        finally:
            con.close()
    except sqlite3.Error as e:
        print(f"[警告] db 读取失败，标题回退到首条消息：{e}", file=sys.stderr)
    return titles


def read_messages(jf):
    """读一个 jsonl，返回 ([(role, text)], 首条时间, 是否有坏行)"""
    msgs = []
    first_ts = None
    broken = 0
    with open(jf, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                broken += 1
                continue
            if not isinstance(d, dict) or d.get("type") != "message":
                continue
            if first_ts is None and d.get("timestamp"):
                first_ts = d["timestamp"]
            role = d.get("role") or "?"
            content = d.get("content")
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            parts = []
            for b in content or []:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type", "")
                if bt in TEXT_BLOCKS:
                    t = clean_text(b.get("text"))
                    if t:
                        parts.append(t)
                elif bt in TOOL_BLOCKS:
                    parts.append(f"> [调用工具: {b.get('name', '?')}]")
            if parts:
                msgs.append((role, "\n\n".join(parts)))
    return msgs, first_ts, broken


def merge_runs(msgs):
    """连续同角色的消息合并成一段（工具调用轮次常拆成多条，标题重复很难读）"""
    out = []
    for role, text in msgs:
        if out and out[-1][0] == role:
            out[-1] = (role, out[-1][1] + "\n\n" + text)
        else:
            out.append((role, text))
    return out


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy 会话导出为 Markdown（只读、不可还原）")
    ap.add_argument("src", nargs="?", help="projects 目录（默认 <数据根>/projects）")
    ap.add_argument("--root", help=f"数据根（默认 {DEFAULT_ROOT}）")
    ap.add_argument("--out", default=os.path.join(os.getcwd(), "export-md"), help="输出目录（默认 ./export-md）")
    ap.add_argument("--db", help="标题库 workbuddy.db（默认取数据根下的那份）")
    ap.add_argument("--quiet", "-q", action="store_true", help="只打印结果行")
    args = ap.parse_args()

    src = args.src or os.path.join(args.root or DEFAULT_ROOT, "projects")
    if not os.path.isdir(src):
        print(f"[错误] 找不到会话目录: {src}", file=sys.stderr)
        return 2

    db = args.db
    if db is None:
        candidate = os.path.join(os.path.dirname(os.path.abspath(src)), "workbuddy.db")
        db = candidate if os.path.isfile(candidate) else None

    titles = load_titles(db)
    files = [
        p for p in sorted(glob.glob(os.path.join(src, "**", "*.jsonl"), recursive=True))
        if not any(part in p.replace(os.sep, "/") for part in SKIP_PARTS)
    ]

    os.makedirs(args.out, exist_ok=True)
    manifest = []
    failed = 0
    for jf in files:
        sid = os.path.splitext(os.path.basename(jf))[0]
        try:
            msgs, first_ts, broken = read_messages(jf)
        except OSError as e:
            failed += 1
            print(f"[警告] 跳过 {jf}: {e}", file=sys.stderr)
            continue
        if not msgs:
            continue
        title = titles.get(sid) or next(
            (clean_text(m) for r, m in msgs if r == "user" and clean_text(m)), ""
        )[:40] or sid
        dt = to_datetime(first_ts)
        date = dt.strftime("%Y-%m-%d %H:%M") if dt else ""
        fname = f"{safe_name((date[:10] + '_' if date else '') + title)}_{sid[:8]}.md"
        lines = [
            f"# {title}", "",
            f"- 会话 ID: `{sid}`",
            f"- 开始时间: {date or '未知'}",
            f"- 来源: {jf}",
            f"- 消息数: {len(msgs)}", "",
            "---", "",
        ]
        for role, text in merge_runs(msgs):
            who = {"user": "🙋 用户", "assistant": "🤖 助手"}.get(role, f"❔ {role}")
            lines += [f"## {who}", "", text, ""]
        with open(os.path.join(args.out, fname), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        manifest.append(f"{fname}\t{len(msgs)}\t{title}")
        if broken and not args.quiet:
            print(f"[警告] {fname}: 跳过 {broken} 行无法解析的 jsonl", file=sys.stderr)

    with open(os.path.join(args.out, "export-manifest.tsv"), "w", encoding="utf-8") as fh:
        fh.write("文件\t消息数\t标题\n")
        fh.write("\n".join(sorted(manifest)))

    print(f"导出 {len(manifest)} 个会话 -> {args.out}")
    if titles:
        print(f"标题来源: {db}")
    elif db:
        print(f"[警告] {db} 里没取到标题，已回退到首条消息", file=sys.stderr)
    print(f"清单: {os.path.join(args.out, 'export-manifest.tsv')}")
    return 1 if failed else 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    sys.exit(main())
