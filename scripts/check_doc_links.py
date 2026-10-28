"""检查所有 markdown 里的相对链接是否指向真实存在的文件。

用法（在仓库根）：
    python scripts/check_doc_links.py

只检查相对路径（`](path)` / `[text](path)` / `![alt](path)`），
跳过 http(s) / mailto / 锚点（`#...`）。退出码 0 表示没有断链。

之所以需要：`docs/` 下截图集中到 `docs/screenshots/` 后，任何还指着
`docs/shots/xxx.png` 或 `docs/page-xxx.png` 的链接都会静默 404。
"""
from __future__ import annotations

import contextlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# [text](target) 或 ![alt](target)；target 里允许出现空格外的任意非 ) 字符
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")

# 不参与检查的目录：依赖、构建产物、缓存
SKIP_DIRS = {"node_modules", "dist", "build", "release", "__pycache__",
             ".git", ".venv", ".ruff_cache", ".pytest_cache", ".mypy_cache",
             ".chrome-profile", ".chrome-profile2", ".runtime-data", ".smoke",
             "_build_tmp", "_bt", "_meas"}


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(OSError, ValueError):
                reconfigure(encoding="utf-8")


def iter_markdown() -> list[Path]:
    out: list[Path] = []
    for path in ROOT.rglob("*.md"):
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        out.append(path)
    return sorted(out)


def main() -> int:
    _utf8_stdio()
    files = iter_markdown()
    broken: list[tuple[Path, str, str]] = []
    checked = 0

    for md in files:
        text = md.read_text(encoding="utf-8", errors="replace")
        for match in LINK_RE.finditer(text):
            target = match.group(1).strip()
            # 去掉标题锚点与尖括号包裹
            target = target.split("#", 1)[0].strip().strip("<>")
            if not target:
                continue
            if target.startswith(("http://", "https://", "mailto:", "tel:", "#")):
                continue
            # 跳过明显的非路径（例如代码里的占位符）
            if target.startswith("$") or "{{" in target:
                continue
            checked += 1
            # 链接相对于 md 文件所在目录解析
            resolved = (md.parent / target).resolve()
            if not resolved.exists():
                broken.append((md, target, str(resolved)))

    print(f"扫描 markdown：{len(files)} 个文件，{checked} 条相对链接")

    if broken:
        print(f"\n断链 {len(broken)} 条：", file=sys.stderr)
        for md, target, resolved in broken:
            print(f"  {md.relative_to(ROOT)} -> {target}", file=sys.stderr)
            print(f"      解析为 {resolved}", file=sys.stderr)
        return 1

    print("[结论] 没有断链")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
