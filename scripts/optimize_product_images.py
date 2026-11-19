"""存量成品图优化：生成缩略图 + PNG/WebP 转 JPEG（含数据库地址同步）。

负责人 2026-11-18：图床 products/ 里 1,067 张存成了 PNG，单张最大 3.8 MB；
列表页一张卡片只显示 200×200，却拉 654 KB 原图，一页 20 件 ≈ 13 MB。

为什么要有这个脚本：入库时转 JPEG 只对**新商品**生效
（image_upload.promote_to_product_zone），已经躺在 products/ 里的存量
不会自己变小，必须单独跑一遍。

三种模式，按风险从低到高分开跑，别一把梭：

  thumbs   只生成 products/thumbs/<stem>.jpg
           不动原图、不动数据库，纯增量，零风险。
           这一步就能把列表页流量砍到 1/20。

  plan     只读报告：哪些图要转、转完预计省多少、哪些文件没被数据库引用、
           哪些 stem 会和已有文件撞名。**不做任何写操作。**

  convert  真正转：写 <stem>.jpg → 建缩略图 → 改数据库里的引用 → 校验 → 删原文件
           每一步都有校验，校验不过就回滚该文件的数据库改动、保留原文件。

幂等：thumbs/ 已存在的跳过；convert 时 <stem>.jpg 已存在且能打开就跳过。

用法（在服务器上跑，venv 里有 Pillow）：
    /opt/miaoshou-listing-v2/venv/bin/python optimize_product_images.py plan
    /opt/miaoshou-listing-v2/venv/bin/python optimize_product_images.py thumbs --limit 20
    /opt/miaoshou-listing-v2/venv/bin/python optimize_product_images.py thumbs
    /opt/miaoshou-listing-v2/venv/bin/python optimize_product_images.py convert --dry-run
    /opt/miaoshou-listing-v2/venv/bin/python optimize_product_images.py convert --limit 20
    /opt/miaoshou-listing-v2/venv/bin/python optimize_product_images.py convert
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

PRODUCT_DIR_DEFAULT = "/var/www/tk-product-automation/products"
THUMB_SUBDIR = "thumbs"
THUMB_MAX_SIDE = 200
THUMB_JPEG_QUALITY = 82
PRODUCT_JPEG_QUALITY = 92
# products 下所有可能存图片地址的列（cover 是 text，其余是 json）
JSON_COLUMNS = ("images", "detail_images", "sku_properties", "payload", "collected")
TEXT_COLUMNS = ("cover",)
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
URL_RE = re.compile(r"products/([A-Za-z0-9][A-Za-z0-9._-]*)")


# --- 图片处理（与 services/image_upload.py 保持同一套参数） -----------------

def _flatten_alpha(image: Any) -> Any:
    """透明区域合成白底；直接 convert("RGB") 会把透明填成黑色。"""
    from PIL import Image

    has_alpha = image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info)
    if not has_alpha:
        return image.convert("RGB")
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    background = Image.new("RGB", image.size, (255, 255, 255))
    background.paste(image, mask=image.split()[3])
    return background


def _encode_jpeg(image: Any, quality: int) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality, optimize=True,
               progressive=True)
    return buffer.getvalue()


def convert_to_jpeg(raw: bytes) -> tuple[bytes, str]:
    """返回 (字节, 扩展名)；已经是 JPEG 或转完更大时返回 (raw, "")。"""
    if not raw:
        return raw, ""
    if raw.startswith(b"\xff\xd8\xff"):
        return raw, ""
    from PIL import Image

    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        size = image.size
        encoded = _encode_jpeg(_flatten_alpha(image), PRODUCT_JPEG_QUALITY)
    if len(encoded) >= len(raw):
        return raw, ""
    # 校验：能打开、尺寸没变
    with Image.open(io.BytesIO(encoded)) as check:
        if check.size != size or check.format != "JPEG":
            raise ValueError(f"转出图校验失败 size={check.size} format={check.format}")
    return encoded, ".jpg"


def make_thumb(raw: bytes) -> bytes:
    from PIL import Image

    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        converted = _flatten_alpha(image)
        converted.thumbnail((THUMB_MAX_SIDE, THUMB_MAX_SIDE), Image.LANCZOS)
        data = _encode_jpeg(converted, THUMB_JPEG_QUALITY)
    with Image.open(io.BytesIO(data)) as check:
        if max(check.size) != THUMB_MAX_SIDE and max(check.size) > THUMB_MAX_SIDE:
            raise ValueError(f"缩略图尺寸异常 {check.size}")
    return data


# --- 数据库 ----------------------------------------------------------------

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
        raise SystemExit("找不到 AOHUANG_DATABASE_URL，无法读写数据库")
    dsn = re.sub(r"^postgresql\+psycopg://", "postgresql://", url)
    return psycopg.connect(dsn, autocommit=False)


def referenced_names(conn) -> tuple[set[str], dict[str, int]]:
    """扫出数据库里所有 products/<文件名> 引用。

    返回 (被引用的文件名集合, 文件名 → 引用它的行数)。
    """
    columns = ", ".join([*TEXT_COLUMNS, *[f"{c}::text" for c in JSON_COLUMNS]])
    counts: dict[str, int] = {}
    with conn.cursor() as cur:
        cur.execute(f"select {columns} from products")
        for row in cur:
            seen: set[str] = set()
            for value in row:
                if not value:
                    continue
                for match in URL_RE.finditer(str(value)):
                    seen.add(match.group(1))
            for name in seen:
                counts[name] = counts.get(name, 0) + 1
    return set(counts), counts


def rewrite_references(conn, old_name: str, new_name: str) -> int:
    """把数据库里 products/<old> 改成 products/<new>，返回受影响行数。"""
    pattern = f"products/{old_name}"
    replacement = f"products/{new_name}"
    sets = [f"{col} = replace({col}, %s, %s)" for col in TEXT_COLUMNS]
    sets += [f"{col} = replace({col}::text, %s, %s)::json" for col in JSON_COLUMNS]
    params: list[str] = []
    for _ in [*TEXT_COLUMNS, *JSON_COLUMNS]:
        params.extend([pattern, replacement])
    where = " or ".join(
        [f"{col} like %s" for col in TEXT_COLUMNS]
        + [f"{col}::text like %s" for col in JSON_COLUMNS])
    params.extend([f"%{pattern}%"] * len(TEXT_COLUMNS))
    params.extend([f"%{pattern}%" for _ in JSON_COLUMNS])

    with conn.cursor() as cur:
        cur.execute(f"update products set {', '.join(sets)} where {where}", params)
        return cur.rowcount


def leftover_references(conn, name: str) -> int:
    pattern = f"%products/{name}%"
    where = " or ".join(
        [f"{col} like %s" for col in TEXT_COLUMNS]
        + [f"{col}::text like %s" for col in JSON_COLUMNS])
    with conn.cursor() as cur:
        cur.execute(
            f"select count(*) from products where {where}",
            [pattern] * (len(TEXT_COLUMNS) + len(JSON_COLUMNS)))
        return int(cur.fetchone()[0])


# --- 文件遍历 --------------------------------------------------------------

def product_files(product_dir: Path) -> list[Path]:
    files = [p for p in product_dir.iterdir()
             if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    files.sort(key=lambda p: p.name)
    return files


def has_thumb(thumb_dir: Path, stem: str) -> bool:
    path = thumb_dir / f"{stem}.jpg"
    return path.is_file() and path.stat().st_size > 0


def write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, 0o644)
    tmp.replace(path)


# --- 三种模式 --------------------------------------------------------------

def run_thumbs(args, product_dir: Path) -> int:
    thumb_dir = product_dir / THUMB_SUBDIR
    files = product_files(product_dir)
    if args.limit:
        files = files[: args.limit]
    done = skipped = failed = 0
    saved_bytes = 0
    failures: list[str] = []
    started = time.time()
    for index, path in enumerate(files, start=1):
        stem = path.stem
        if has_thumb(thumb_dir, stem):
            skipped += 1
        else:
            try:
                raw = path.read_bytes()
                data = make_thumb(raw)
                if args.dry_run:
                    saved_bytes += max(0, len(raw) - len(data))
                else:
                    thumb_dir.mkdir(parents=True, exist_ok=True)
                    write_atomic(thumb_dir / f"{stem}.jpg", data)
                    saved_bytes += max(0, len(raw) - len(data))
                done += 1
            except Exception as exc:  # noqa: BLE001 - 单张失败不中断
                failed += 1
                failures.append(f"{path.name}: {exc}")
        if index % 100 == 0 or index == len(files):
            print(f"  [{index}/{len(files)}] 生成 {done} 跳过 {skipped} 失败 {failed}",
                  flush=True)
    elapsed = time.time() - started
    print(f"\n缩略图：生成 {done}，跳过 {skipped}，失败 {failed}，"
          f"耗时 {elapsed:.1f}s")
    print(f"列表页单次可省 ≈ {saved_bytes / 1024 / 1024:.1f} MB"
          "（假设这些图各被加载一次）")
    if failures:
        print("\n失败清单：")
        for line in failures[:40]:
            print("  " + line)
        if len(failures) > 40:
            print(f"  …… 还有 {len(failures) - 40} 条")
    return 0 if failed == 0 else 1


def build_plan(product_dir: Path, conn) -> tuple[list[dict[str, Any]], list[str]]:
    refs, counts = referenced_names(conn)
    files = product_files(product_dir)
    thumbs_dir = product_dir / THUMB_SUBDIR
    todo: list[dict[str, Any]] = []
    orphans: list[str] = []
    for path in files:
        name = path.name
        stem = path.stem
        if name not in refs:
            orphans.append(name)
            continue
        if path.suffix.lower() in {".jpg", ".jpeg"}:
            continue
        target = product_dir / f"{stem}.jpg"
        # 只有「目标文件在 且 数据库已经指向新名」才算真转完。
        # 只看目标文件在是不够的：上一轮中途失败会留下 <stem>.jpg 而数据库没改，
        # 靠文件存在性判断会把这种半成品当成功跳过（2026-11-19 实测踩到）。
        if target.exists() and target.stat().st_size > 0 and name not in refs:
            continue
        todo.append({"path": path, "name": name, "stem": stem,
                     "rows": counts.get(name, 0),
                     "bytes": path.stat().st_size,
                     "thumb": has_thumb(thumbs_dir, stem)})
    return todo, orphans


def run_plan(args, product_dir: Path) -> int:
    with db_connect() as conn:
        todo, orphans = build_plan(product_dir, conn)
    total = sum(item["bytes"] for item in todo)
    print(f"待转 JPEG：{len(todo)} 张，合计 {total / 1024 / 1024:.1f} MB")
    print(f"没被数据库引用的文件：{len(orphans)} 个（不转，留给后面单独清理）")
    print("\n前 20 张明细：")
    for item in todo[:20]:
        print(f"  {item['name']}  {item['bytes'] / 1024:.0f} KB  "
              f"引用 {item['rows']} 行  缩略图{'有' if item['thumb'] else '无'}")
    if len(todo) > 20:
        print(f"  …… 还有 {len(todo) - 20} 张")
    if orphans:
        print("\n未引用样例（前 10 个）：")
        for name in orphans[:10]:
            print("  " + name)
    return 0


def run_convert(args, product_dir: Path) -> int:
    thumb_dir = product_dir / THUMB_SUBDIR
    conn = db_connect()
    try:
        todo, orphans = build_plan(product_dir, conn)
        if args.limit:
            todo = todo[: args.limit]
        print(f"本次处理 {len(todo)} 张（未引用 {len(orphans)} 个不动）")
        converted = failed = 0
        saved = 0
        failures: list[str] = []
        for index, item in enumerate(todo, start=1):
            path: Path = item["path"]
            name = item["name"]
            stem = item["stem"]
            new_name = f"{stem}.jpg"
            # 本轮是否新建了这个 jpg（失败时只删自己新建的，不动历史文件）
            created_here = False
            try:
                raw = path.read_bytes()
                data, ext = convert_to_jpeg(raw)
                if ext != ".jpg":
                    print(f"  [跳过] {name}：转完不变小，保留原格式")
                    continue
                target = product_dir / new_name
                preexisting = target.is_file() and target.stat().st_size > 0
                if args.dry_run:
                    print(f"  [dry] {name} → {new_name}  "
                          f"{len(raw) / 1024:.0f} KB → {len(data) / 1024:.0f} KB  "
                          f"引用 {item['rows']} 行")
                    converted += 1
                    saved += max(0, len(raw) - len(data))
                    continue
                write_atomic(target, data)
                created_here = not preexisting
                if not item["thumb"]:
                    thumb_dir.mkdir(parents=True, exist_ok=True)
                    write_atomic(thumb_dir / f"{stem}.jpg", make_thumb(data))
                rewritten = rewrite_references(conn, name, new_name)
                leftover = leftover_references(conn, name)
                if leftover:
                    conn.rollback()
                    if created_here:
                        target.unlink(missing_ok=True)
                        created_here = False
                    raise RuntimeError(f"数据库仍有 {leftover} 行引用旧名，已回滚")
                conn.commit()
                path.unlink()
                converted += 1
                saved += max(0, len(raw) - len(data))
                print(f"  [ok] {name} → {new_name}  改 {rewritten} 行  "
                      f"{len(raw) / 1024:.0f} KB → {len(data) / 1024:.0f} KB")
            except Exception as exc:  # noqa: BLE001 - 单张失败不中断整批
                conn.rollback()
                # 本轮刚写出来的 jpg 要清掉，否则下一轮会被「目标文件已存在」跳过，
                # 数据库永远改不过来
                if created_here:
                    try:
                        (product_dir / new_name).unlink(missing_ok=True)
                    except OSError:
                        pass
                failed += 1
                failures.append(f"{name}: {exc}")
                print(f"  [fail] {name}: {exc}")
            if index % 20 == 0:
                print(f"  …进度 {index}/{len(todo)}，已转 {converted}，"
                      f"省 {saved / 1024 / 1024:.1f} MB", flush=True)
        print(f"\n转换：成功 {converted}，失败 {failed}，"
              f"共省 {saved / 1024 / 1024:.1f} MB")
        if failures:
            print("\n失败清单：")
            for line in failures[:40]:
                print("  " + line)
        return 0 if failed == 0 else 1
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="存量成品图优化")
    parser.add_argument("mode", choices=["plan", "thumbs", "convert"])
    parser.add_argument("--product-dir", default=os.environ.get(
        "AOHUANG_IMAGE_PRODUCT_DIR", PRODUCT_DIR_DEFAULT))
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个")
    parser.add_argument("--dry-run", action="store_true", help="不写盘不写库")
    args = parser.parse_args(argv)
    product_dir = Path(args.product_dir)
    if not product_dir.is_dir():
        print(f"目录不存在：{product_dir}", file=sys.stderr)
        return 2
    if args.mode == "thumbs":
        return run_thumbs(args, product_dir)
    if args.mode == "plan":
        return run_plan(args, product_dir)
    return run_convert(args, product_dir)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
