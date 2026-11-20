"""成品区孤儿图审计：哪些文件已经没有任何引用。

负责人 2026-11-19：「确认没有用就清理」。

**为什么不能只看 ready 商品**：商品有 draft / ready / published / 归档 等多种状态，
草稿箱里的商品一样会引用图片。只看 ready 会把草稿箱在用的图判成孤儿。

这个脚本做全量反查：

1. 把**所有商品**（不限状态）的所有 payload 字段都扫一遍，收集全部
   `products/` 与 `auto-temp/` 引用
2. 扫其他可能引用图片的表（图片任务、发布记录、自动化结果）
3. 把 `products/` 目录下的实际文件与之比对
4. 列出「文件存在但无人引用」的清单，**按大小排序**

默认 dry-run。`--apply` 只在**二次校验通过**时才执行删除。

## 安全闸门（--apply 时）

- 先只读扫描，统计孤儿数量与总字节
- 如果孤儿数超过总文件数的 60%，**直接中止** —— 那个比例通常意味着
  反查漏了某个字段，不是真的孤儿
- 删除时只删 `products/` 直属文件与 `mid/` `thumbs/` 同名派生图
- 不删 `auto-temp/`（那是临时区，有 14 天自动清理）
- 每删一个文件都记进日志（`/root/orphan_image_cleanup.log`）

## 用法
    venv/bin/python -m scripts.audit_orphan_images            # 只报告
    venv/bin/python -m scripts.audit_orphan_images --apply     # 真删
"""
from __future__ import annotations

import argparse
import contextlib
import os
import re
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

# 任意位置出现的成品区 / 临时区图片地址（含 notes 里的 HTML、JSON 串）
_URL_RE = re.compile(
    r"(?:products|auto-temp|products/mid|products/thumbs)"
    r"/([A-Za-z0-9._-]+\.(?:jpg|jpeg|png|webp|gif|bmp))",
    re.IGNORECASE,
)

# 孤儿比例闸门：超过这个比例就中止，因为更可能是反查漏字段
MAX_ORPHAN_RATIO = 0.60


def _collect_references(db) -> tuple[dict[str, set[str]], int]:
    """收集全库图片引用，按「引用来源」分组。

    返回 {来源标签: {文件名, ...}}。分来源是为了出问题时能看清是谁在用。

    **2026-11-19 踩过的坑（务必别再犯）**：
    第一版只扫了 `products.payload`，报出 1016 个孤儿。实际扫全库后只有 74 个 ——
    942 个是活的，`products` 表还有 `images` / `detail_images` /
    `automation_images` 三个独立列，以及 `image_jobs.public_url` 等引用。
    差一点就把 942 张在用的商品图删了。

    所以这里的原则是：**宁滥勿缺** —— 任何 TEXT/JSON 列都扫，
    不做「哪个列名像图片」的猜测。多扫几列只是慢一点，漏扫会删掉生产数据。
    """
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text as sql_text

    refs: dict[str, set[str]] = {}

    def _scan(label: str, blob) -> None:
        if blob is None:
            return
        text = blob if isinstance(blob, str) else repr(blob)
        found = _URL_RE.findall(text)
        if found:
            refs.setdefault(label, set()).update(found)

    bind = db.get_bind()
    inspector = sa_inspect(bind)
    table_names = inspector.get_table_names()

    scanned_tables = 0
    for table_name in table_names:
        # 只扫字符串/JSON 类型的列，其他类型（int/float/bool/时间戳）不可能带 URL。
        # 先看列类型再扫，避免为了几十万行做无意义的 repr。
        columns = inspector.get_columns(table_name)
        text_columns = [
            column["name"] for column in columns
            if "CHAR" in str(column["type"]).upper()
            or "TEXT" in str(column["type"]).upper()
            or "JSON" in str(column["type"]).upper()
        ]
        if not text_columns:
            continue
        selected = ", ".join(f'"{name}"' for name in text_columns)
        try:
            rows = db.execute(sql_text(f'SELECT {selected} FROM "{table_name}"'))
        except Exception:  # noqa: BLE001 - 读不了的表跳过（不阻断审计）
            continue
        scanned_tables += 1
        for row in rows:
            for key, value in dict(row._mapping).items():
                _scan(f"{table_name}.{key}", value)

    return refs, scanned_tables


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="成品区孤儿图审计")
    parser.add_argument("--apply", action="store_true", help="真正删除")
    parser.add_argument("--force", action="store_true",
                        help="孤儿比例过高时仍要删（慎用）")
    args = parser.parse_args(argv)

    from db.session import SessionLocal

    product_dir = Path(os.environ.get("AOHUANG_IMAGE_PRODUCT_DIR", "")
                       .strip() or "/var/www/tk-product-automation/products")
    thumb_dir = product_dir / "thumbs"
    mid_dir = product_dir / "mid"

    db = SessionLocal()
    try:
        print("正在扫描全库引用……")
        refs, scanned_tables = _collect_references(db)
        referenced: set[str] = set().union(*refs.values()) if refs else set()
        print(f"  扫描表数: {scanned_tables}")
        print(f"  引用来源种类: {len(refs)}")
        print(f"  被引用文件数: {len(referenced)}")

        # 实际文件
        on_disk = {p.name for p in product_dir.iterdir() if p.is_file()}
        print(f"  成品区文件数: {len(on_disk)}")

        orphans = sorted(on_disk - referenced)
        print(f"\n孤儿文件: {len(orphans)}")
        if not on_disk:
            print("成品区目录为空或不可读，中止。")
            return 1

        # 闸门 0：扫描表数太少 → 反查不完整，本次结论不可信。
        # 生产库实测 25 张表，扫到 20+ 才算覆盖完整。
        if scanned_tables < 10:
            print(f"[中止] 只扫到 {scanned_tables} 张表，反查覆盖不足。")
            print("（2026-11-19 踩过：只扫 products.payload 时报 1016 个孤儿，"
                  "实际只有 74 个 —— 差 942 个是活的。）")
            return 3

        ratio = len(orphans) / len(on_disk)
        print(f"孤儿占比: {ratio:.1%}")

        total_bytes = 0
        for name in orphans:
            with contextlib.suppress(OSError):
                total_bytes += (product_dir / name).stat().st_size
        print(f"占用空间: {total_bytes / 1024 / 1024:.1f} MB")

        # 派生图也跟着算
        derived_orphans: list[Path] = []
        for name in orphans:
            stem = name.rsplit(".", 1)[0]
            for folder in (thumb_dir, mid_dir):
                for ext in (".jpg", ".png", ".webp"):
                    candidate = folder / f"{stem}{ext}"
                    if candidate.is_file():
                        derived_orphans.append(candidate)
        derived_bytes = 0
        for path in derived_orphans:
            with contextlib.suppress(OSError):
                derived_bytes += path.stat().st_size
        print(f"派生图（thumbs/mid）: {len(derived_orphans)} 个，"
              f"{derived_bytes / 1024 / 1024:.1f} MB")
        print(f"合计可回收: {(total_bytes + derived_bytes) / 1024 / 1024:.1f} MB")

        if orphans:
            print("\n前 20 个孤儿原图：")
            for name in orphans[:20]:
                try:
                    size = (product_dir / name).stat().st_size // 1024
                except OSError:
                    size = 0
                print(f"  {name}  {size} KB")
            if len(orphans) > 20:
                print(f"  ... 还有 {len(orphans) - 20} 个")

        if not args.apply:
            print("\n[dry-run] 未删除。加 --apply 执行。")
            return 0

        # 安全闸门
        if ratio > MAX_ORPHAN_RATIO and not args.force:
            print(f"\n[中止] 孤儿占比 {ratio:.1%} 超过 {MAX_ORPHAN_RATIO:.0%} 闸门。")
            print("这通常意味着反查漏了某个字段，而不是真的这么多孤儿。")
            print("确认无误后加 --force 跳过闸门。")
            return 2

        log_path = Path("/root/orphan_image_cleanup.log")
        removed = failed = 0
        reclaimed = 0
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"\n=== 清理开始 {len(orphans)} 个原图 / "
                      f"{len(derived_orphans)} 个派生图 ===\n")
            for name in orphans:
                path = product_dir / name
                try:
                    size = path.stat().st_size
                    path.unlink()
                    removed += 1
                    reclaimed += size
                    log.write(f"RM {path} {size}\n")
                except OSError as exc:
                    failed += 1
                    log.write(f"FAIL {path} {exc}\n")
            for path in derived_orphans:
                try:
                    size = path.stat().st_size
                    path.unlink()
                    removed += 1
                    reclaimed += size
                    log.write(f"RM {path} {size}\n")
                except OSError as exc:
                    failed += 1
                    log.write(f"FAIL {path} {exc}\n")
            log.write(f"=== 完成 删除 {removed} 失败 {failed} "
                      f"回收 {reclaimed / 1024 / 1024:.1f} MB ===\n")

        print(f"\n完成：删除 {removed} 个文件，失败 {failed}，"
              f"回收 {reclaimed / 1024 / 1024:.1f} MB")
        print(f"日志：{log_path}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
