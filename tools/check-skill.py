#!/usr/bin/env python
"""校验 skill 与各平台清单之间的一致性。

pi 遇到坏 frontmatter 只给警告然后跳过，Claude Code 装错版本也不会报错，
所以这里把能自动检查的都卡住：

- SKILL.md frontmatter 是否符合 Agent Skills 规范
- skill 目录名、pi package、Claude plugin 三者名字是否一致
- package.json / plugin.json 的版本号是否一致
- marketplace.json 是否指向本仓库且分类、描述齐全
- skill 自带的脚本与参考文档是否齐全、能否通过语法检查

    python tools/check-skill.py
"""
from __future__ import annotations

import json
import py_compile
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "backup-workbuddy"
SKILL = ROOT / "skills" / NAME / "SKILL.md"
NAME_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
REQUIRED = (
    "scripts/inspect_workbuddy.py",
    "scripts/workbuddy_snapshot.py",
    "references/migration-playbook.md",
    "references/app-internals.md",
)


def fail(msg: str) -> None:
    print(f"[x] {msg}", file=sys.stderr)
    sys.exit(1)


def load_json(rel: str) -> dict:
    path = ROOT / rel
    if not path.exists():
        fail(f"缺少 {rel}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(f"{rel} 不是合法 JSON：{exc}")


def check_skill() -> tuple[str, str]:
    if not SKILL.exists():
        fail(f"找不到 {SKILL.relative_to(ROOT)}")

    text = SKILL.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        fail("SKILL.md 开头没有 frontmatter")
    fm = m.group(1)

    def field(key: str) -> str:
        hit = re.search(rf"^{key}:\s*(.+)$", fm, re.M)
        if not hit:
            fail(f"frontmatter 缺少 {key}")
        return hit.group(1).strip().strip("\"'")

    name, desc = field("name"), field("description")

    if len(name) > 64 or not NAME_RE.fullmatch(name):
        fail(f"name 只能用 a-z / 0-9 / 连字符且不超过 64 字符：{name!r}")
    if name != SKILL.parent.name:
        fail(f"name ({name}) 必须和目录名 ({SKILL.parent.name}) 一致")
    if not desc or len(desc) > 1024:
        fail(f"description 为空或超过 1024 字符（当前 {len(desc)}）")

    for rel in REQUIRED:
        if not (SKILL.parent / rel).exists():
            fail(f"skill 缺少 {rel}")

    # 脚本必须能编译（不执行，避免依赖运行环境的数据目录）
    with tempfile.TemporaryDirectory() as tmp:
        for rel in REQUIRED:
            if rel.endswith(".py"):
                try:
                    py_compile.compile(
                        str(SKILL.parent / rel), cfile=str(Path(tmp) / "x.pyc"), doraise=True
                    )
                except py_compile.PyCompileError as exc:
                    fail(f"{rel} 语法错误：{exc}")

    # 脚本里不许写死别人的家目录
    for rel in REQUIRED:
        if rel.endswith(".py"):
            body = (SKILL.parent / rel).read_text(encoding="utf-8")
            if re.search(r"[A-Za-z]:\\\\Users\\\\[A-Za-z0-9_.-]+", body):
                fail(f"{rel} 里写死了绝对用户路径，请用 ~ / %USERPROFILE% / WORKBUDDY_CONFIG_DIR")

    return name, text


def check_manifests(name: str) -> list[str]:
    pkg = load_json("package.json")
    plugin = load_json(".claude-plugin/plugin.json")
    market = load_json(".claude-plugin/marketplace.json")

    if plugin.get("name") != name:
        fail(f"plugin.json 的 name ({plugin.get('name')}) 与 skill 名 ({name}) 不一致")
    if plugin.get("version") != pkg.get("version"):
        fail(
            f"版本号不一致：package.json {pkg.get('version')} vs "
            f"plugin.json {plugin.get('version')}（改完 skill 记得一起 bump）"
        )
    if "pi-package" not in pkg.get("keywords", []):
        fail("package.json 的 keywords 缺 pi-package，pi 画廊不会收录")
    if pkg.get("pi", {}).get("skills") != ["./skills"]:
        fail('package.json 的 pi.skills 应为 ["./skills"]')

    entries = [p for p in market.get("plugins", []) if p.get("name") == name]
    if not entries:
        fail(f"marketplace.json 里没有名为 {name} 的条目")
    entry = entries[0]
    if entry.get("source") not in ("./", "."):
        fail(f"marketplace 条目 source 应指向仓库根，当前是 {entry.get('source')!r}")
    for key in ("description", "category"):
        if not entry.get(key):
            fail(f"marketplace 条目缺少 {key}")

    return [pkg["name"], plugin["version"], entry.get("category", "")]


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    name, text = check_skill()
    pkg_name, version, category = check_manifests(name)
    print(
        f"ok: skill {name} / package {pkg_name}@{version} / "
        f"category {category} / 正文 {len(text.splitlines())} 行"
    )


if __name__ == "__main__":
    main()
