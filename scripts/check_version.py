#!/usr/bin/env python
"""版本号四处一致性校验（CI job: version-consistency）。

为什么需要这个脚本：安装包显示给用户的版本、界面侧栏显示的版本、后端
`/api/v1/system/version` 返回的版本、PyInstaller 打出来的 exe 属性版本，
来源是四个互不相干的文件。改版本时漏改一处，用户看到的就是「装的是
0.1.6、界面写着 0.1.5」，而自动更新逻辑会据此判定「有新版本」并在侧栏
挂一个永不消失的角标。

四个来源：
  1. package.json            根：workspace 版本 / electron-builder 打包版本
  2. web/package.json        前端包版本
  3. app/package.json        Electron 壳版本（electron-builder 读这个出安装包）
  4. server/core/constants.py 的 `_package_version()` 兜底字面量
     —— 打包后读不到仓库里的 package.json，只有这个兜底值可用

用法：
    python scripts/check_version.py          # 校验，不一致时 exit 1
    python scripts/check_version.py --json   # 机器可读输出
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 语义化版本：主.次.修订，允许 -beta.1 / +build 后缀（与 package.json 的 npm 规则一致）
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")


class VersionError(RuntimeError):
    """读不到版本号（文件缺失 / 结构变了），与「版本不一致」区分开。"""


def read_package_version(relative_path: str) -> str:
    """从 package.json 的顶层 `version` 字段取版本。"""
    path = ROOT / relative_path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise VersionError(f"找不到文件：{relative_path}") from exc
    except json.JSONDecodeError as exc:
        raise VersionError(f"{relative_path} 不是合法 JSON：{exc}") from exc
    value = data.get("version")
    if not isinstance(value, str) or not value.strip():
        raise VersionError(f"{relative_path} 缺少顶层 version 字段")
    return value.strip()


def read_constants_fallback(relative_path: str = "server/core/constants.py") -> str:
    """取 constants.py 里 `_package_version()` 的兜底返回值。

    刻意不用 import：导入 constants 会连带导入 core.config 并触发
    `get_settings().ensure_dirs()`，在 CI 里凭空造出一个数据目录。
    这里用 ast 静态读，顺带能容忍函数体里将来加的注释和日志。
    """
    path = ROOT / relative_path
    try:
        source = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise VersionError(f"找不到文件：{relative_path}") from exc
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise VersionError(f"{relative_path} 语法错误：{exc}") from exc

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "_package_version":
            continue
        # 取函数体内最后一个「返回字符串字面量」的 return，即兜底值
        fallback = None
        for child in ast.walk(node):
            if isinstance(child, ast.Return) and isinstance(child.value, ast.Constant):
                if isinstance(child.value.value, str) and SEMVER_RE.match(child.value.value):
                    fallback = child.value.value
        if fallback:
            return fallback
        raise VersionError(
            f"{relative_path} 的 _package_version() 里找不到字符串字面量兜底版本"
        )
    raise VersionError(f"{relative_path} 里找不到 _package_version() 函数")


def collect() -> dict[str, str]:
    """按固定顺序收集四处版本，顺序即报告顺序。"""
    return {
        "package.json（根 / electron-builder）": read_package_version("package.json"),
        "web/package.json（前端）": read_package_version("web/package.json"),
        "app/package.json（Electron 壳）": read_package_version("app/package.json"),
        "server/core/constants.py（APP_VERSION 兜底）": read_constants_fallback(),
    }


def _force_utf8_stdio() -> None:
    """Windows 控制台默认 GBK，中文报告会变成乱码；CI 是 UTF-8，不必动。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(description="校验版本号四处一致")
    parser.add_argument("--json", action="store_true", help="输出 JSON（给其它工具消费）")
    args = parser.parse_args(argv)

    try:
        versions = collect()
    except VersionError as exc:
        print(f"[版本校验] 读取失败：{exc}", file=sys.stderr)
        return 2

    distinct = sorted(set(versions.values()))
    consistent = len(distinct) == 1

    if args.json:
        print(json.dumps(
            {"consistent": consistent, "versions": versions, "distinct": distinct},
            ensure_ascii=False, indent=2,
        ))
    else:
        width = max(len(name) for name in versions)
        print("版本号四处对照：")
        for name, value in versions.items():
            # 与期望值（第一个文件）不同的标出来，人眼一眼能看出该改哪个
            mark = "" if value == versions["package.json（根 / electron-builder）"] else "  <-- 不一致"
            print(f"  {name.ljust(width)}  {value}{mark}")
        print()
        if consistent:
            print(f"[版本校验] 通过：四处均为 {distinct[0]}")
        else:
            print(
                "[版本校验] 失败：版本号不一致，实际取值 "
                + " / ".join(distinct)
                + "。改版本时这四个文件必须同改（见 CONTRIBUTING.md「改版本号」）。",
                file=sys.stderr,
            )

    return 0 if consistent else 1


if __name__ == "__main__":
    raise SystemExit(main())
