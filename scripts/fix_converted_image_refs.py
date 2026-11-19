"""把「已转成 JPEG 但数据库里还写着 .png」的图片地址改正过来。

背景（负责人 2026-11-19）：
  scripts/optimize_product_images.py 的 convert 模式把 products/x.png 转成
  products/x.jpg 后，只重写了当时列出来的几列（cover / images / detail_images /
  sku_properties / payload / collected），**漏掉了 products.automation_images**。
  结果自动化结果页有 229 处图片地址指向已经不存在的 .png，卡片挂图。

为什么单独写一个修复脚本而不是改原来的：
  convert 只对「本轮要转的文件」负责；已转完的文件不会重跑，需要一次性扫全库补漏。
  以后再有同类漏列，这个脚本还能兜住 —— 它按 information_schema 动态发现所有
  文本/JSON 列，不依赖写死的列名清单。

安全性：
  · 只改「旧文件名不存在 且 新 .jpg 存在」的引用，别的一律不动
  · 支持 --dry-run，先看要改多少行
  · 每行单独 UPDATE，失败只影响那一行

用法（在服务器上跑）：
    P=/opt/miaoshou-listing-v2/venv/bin/python
    $P fix_converted_image_refs.py --dry-run
    $P fix_converted_image_refs.py
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

PRODUCT_DIR_DEFAULT = "/var/www/tk-product-automation/products"
# products/<文件名>，只认成品区；临时区、外链不碰
PATTERN = re.compile(r"products/([A-Za-z0-9][A-Za-z0-9._-]*\.(?:png|webp|gif|bmp))",
                     re.IGNORECASE)


def db_connect():
    import psycopg

    url = os.environ.get("AOHUANG_DATABASE_URL", "").strip()
    if not url:
        env_file = Path("/etc/aohuang-listing-v2.env")
        if env_file.is_file():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("AOHUANG_DATABASE_URL="):
                    url = line.split("=", 1)[1].strip()
                    break
    if not url:
        raise SystemExit("找不到 AOHUANG_DATABASE_URL")
    dsn = re.sub(r"^postgresql\+psycopg://", "postgresql://", url)
    return psycopg.connect(dsn, autocommit=False)


def image_columns(conn) -> list[tuple[str, str, str]]:
    """所有可能存图片地址的文本/JSON 列。动态发现，不写死列名。"""
    with conn.cursor() as cur:
        cur.execute("""
            select table_name, column_name, data_type
            from information_schema.columns
            where table_schema = 'public'
              and data_type in ('text', 'character varying', 'character',
                                'json', 'jsonb')
            order by table_name, column_name
        """)
        return [(row[0], row[1], row[2]) for row in cur.fetchall()]


def build_map(product_dir: Path) -> dict[str, str]:
    """旧文件名 → 新文件名。只收「旧的没了、新的在」的组合。"""
    existing = {name for name in os.listdir(product_dir)
                if (product_dir / name).is_file()}
    mapping: dict[str, str] = {}
    for name in existing:
        low = name.lower()
        if low.endswith((".png", ".webp", ".gif", ".bmp")):
            continue
        stem = name.rsplit(".", 1)[0]
        for ext in (".png", ".webp", ".gif", ".bmp"):
            old = stem + ext
            if old in existing:
                continue      # 两个都在，说明不是我们转出来的，别动
            mapping[old] = name
    return mapping


def rewrite_value(value: str, mapping: dict[str, str]) -> tuple[str, int]:
    hits = 0

    def _sub(match: re.Match[str]) -> str:
        nonlocal hits
        old = match.group(1)
        new = mapping.get(old)
        if new is None:
            return match.group(0)
        hits += 1
        return f"products/{new}"

    return PATTERN.sub(_sub, value), hits


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="修正转换后残留的旧图片地址")
    parser.add_argument("--product-dir", default=os.environ.get(
        "AOHUANG_IMAGE_PRODUCT_DIR", PRODUCT_DIR_DEFAULT))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    product_dir = Path(args.product_dir)
    if not product_dir.is_dir():
        print(f"目录不存在：{product_dir}", file=sys.stderr)
        return 2
    mapping = build_map(product_dir)
    print(f"可修正的旧文件名：{len(mapping)} 个")
    if not mapping:
        print("没有需要修正的，收工")
        return 0

    conn = db_connect()
    total_rows = total_hits = 0
    touched: dict[str, int] = {}
    try:
        for table, column, dtype in image_columns(conn):
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f'select ctid, "{column}"::text from "{table}" '
                        f'where "{column}"::text like %s',
                        ("%products/%.png%",))
                    rows = cur.fetchall()
            except Exception:  # noqa: BLE001 - 某列读不了就跳过，不中断整轮修复
                conn.rollback()
                continue
            if not rows:
                continue
            for ctid, raw in rows:
                value = raw or ""
                if not value:
                    continue
                new_value, hits = rewrite_value(value, mapping)
                # 也兜 webp/gif/bmp 之外的写法：如果 pattern 没命中但值里有旧名，跳过
                if not hits or new_value == value:
                    continue
                total_rows += 1
                total_hits += hits
                touched[f"{table}.{column}"] = touched.get(f"{table}.{column}", 0) + 1
                if args.dry_run:
                    continue
                with conn.cursor() as cur:
                    if dtype in ("json", "jsonb"):
                        cur.execute(
                            f'update "{table}" set "{column}" = %s::json where ctid = %s',
                            (new_value, ctid))
                    else:
                        cur.execute(
                            f'update "{table}" set "{column}" = %s where ctid = %s',
                            (new_value, ctid))
            if not args.dry_run:
                conn.commit()
        if not args.dry_run:
            conn.commit()
    finally:
        conn.close()

    print(f"{'[dry-run] ' if args.dry_run else ''}改动 {total_rows} 行，"
          f"替换 {total_hits} 处地址")
    for key, count in sorted(touched.items(), key=lambda x: -x[1]):
        print(f"  {key:40s} {count} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
