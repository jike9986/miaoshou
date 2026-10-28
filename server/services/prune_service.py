"""清理可重新生成的运行时数据（老版 prune_runtime.py 的移植）。

`runtime/` 会无限增长：翻译后的图片、图片分析缓存都能重下或重算。但运行记录、
批次状态、保存意图正相反——丢了会让程序以为已处理过的商品是新的，
从而把重量再加一遍。

所以这条线画得很硬：
  · **任何开关都不删**：运行记录、保存意图、图片生成台账
  · **可删**：图片缓存与图片结果目录下，且只删早于 N 天的
  · 即使落在可删目录里，只要文件名还被某条运行记录引用，也放过——
    记录的 payload 可能按 sha256 引用生成图的 URL

删除必须 `confirm=True`；不给只出报告。老版是 CLI 脚本，新版做成服务 + API，
界面上的「清理」按钮才有东西可调。
"""
from __future__ import annotations

import contextlib
import logging
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 只有这两个目录放可重新生成的产物
PRUNABLE_DIRECTORIES = ("image-cache", "image-results")

# 根目录下这些后缀属于记录类，任何开关都不碰
PROTECTED_SUFFIXES = (".json", ".jsonl", ".lock", ".gitkeep", ".log")

_HASH_NAME = re.compile(r"([0-9a-f]{64})")
# 图片结果文件名本身就是 sha256，扩展名从这几种里挑
_HASH_EXTS = ("", ".png", ".jpg", ".webp", ".json", ".image")


def _protected_names(root: Path) -> set[str]:
    """任何运行记录还引用着的文件名（按 64 位哈希反查）。"""
    names: set[str] = set()
    if not root.is_dir():
        return names
    for record in root.glob("*.json*"):
        try:
            text = record.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in _HASH_NAME.finditer(text):
            digest = match.group(1)
            for ext in _HASH_EXTS:
                names.add(f"{digest}{ext}")
    return names


def _candidates(root: Path, days: float, now: float) -> Iterator[tuple[Path, int]]:
    cutoff = now - days * 86400
    for directory in PRUNABLE_DIRECTORIES:
        base = root / directory
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
                yield path, path.stat().st_size
            except OSError:
                continue


def human_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def survey(root: Path, days: float = 7.0) -> dict[str, Any]:
    """清点：多少可删、多少因被引用而放过。"""
    now = time.time()
    protected = _protected_names(root)
    deletable: list[Path] = []
    spared: list[Path] = []
    deletable_bytes = 0
    spared_bytes = 0
    for path, size in _candidates(root, days, now):
        if path.name in protected:
            spared.append(path)
            spared_bytes += size
            continue
        deletable.append(path)
        deletable_bytes += size
    protected_root = [path for path in (root.glob("*") if root.is_dir() else [])
                      if path.is_file() and path.suffix in PROTECTED_SUFFIXES]
    return {
        "root": str(root),
        "olderThanDays": days,
        "protectedRootRecords": len(protected_root),
        "protectedNames": len(protected),
        "deletable": deletable,
        "deletableBytes": deletable_bytes,
        "deletableSize": human_size(deletable_bytes),
        "spared": spared,
        "sparedBytes": spared_bytes,
        "sparedSize": human_size(spared_bytes),
    }


def prune(root: Path, days: float = 7.0, *, confirm: bool = False) -> dict[str, Any]:
    """清点并按需删除；confirm=False 时一个文件都不动。"""
    if days < 0:
        raise ValueError("天数不能为负")
    report = survey(root, days)
    removed = 0
    freed = 0
    if confirm:
        for path in report["deletable"]:
            try:
                size = path.stat().st_size
                path.unlink()
            except OSError:
                continue
            removed += 1
            freed += size
        # 删空的目录一并收掉，深的先收
        for directory in PRUNABLE_DIRECTORIES:
            base = root / directory
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*"), key=lambda p: len(p.parts),
                               reverse=True):
                if path.is_dir() and not any(path.iterdir()):
                    # 目录被别的进程刚占住就跳过，清理是尽力而为
                    with contextlib.suppress(OSError):
                        path.rmdir()
        logger.info("运行时清理：删除 %s 个文件，释放 %s", removed, human_size(freed))
    return {
        "root": report["root"],
        "olderThanDays": days,
        "confirmed": confirm,
        "protectedRootRecords": report["protectedRootRecords"],
        "protectedNamesReferencedByRecords": report["protectedNames"],
        "deletableFiles": len(report["deletable"]),
        "deletableSize": report["deletableSize"],
        "sparedBecauseStillReferenced": len(report["spared"]),
        "sparedSize": report["sparedSize"],
        "removed": removed,
        "freedBytes": freed,
        "freed": human_size(freed),
    }
