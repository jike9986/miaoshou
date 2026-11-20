"""把成品仓库商品里仍指向 auto-temp 临时区的图片搬到 products 成品区。

## 为什么需要这个脚本

入库时本来就会搬运（`warehouse_service` 调 `image_upload.promote_payload_images`），
但两个 bug 让搬运长期失效：

1. `promote_payload_images` 遍历 `sku_image_sources()` 时写的是
   `item.get("imgUrl")`，而那个函数返回 `{path, kind, url, ...}` —— 没有
   `imgUrl` 这个 key。`item.get()` 恒为 None，**循环体从未执行**，SKU 图一张没搬。

2. `sku_image_sources` / `rewrite_sku_image_urls` 只认 `imgUrl` 和
   `supplementarySkuImageUrls`。妙手新数据用 `imageUrl` 和 `imgUrls[]`，
   这两个字段既不枚举也不替换。

实测生产（2026-11-19）：180 件 `status=ready` 商品里 **95 件**的图片 URL
停在 `auto-temp`，共 1784 处引用。而 `auto-temp` 是 **14 天自动清理**的
临时目录 —— 到期后这些商品的 SKU 图会全部变成挂图。

代码已修（见 `services/image_upload.py`、`services/warehouse_service.py`）。
这个脚本处理存量数据。

## 安全措施

- 默认 dry-run，`--apply` 才写库
- 只处理 `status='ready'` 且确实含 `/auto-temp/` 引用的商品
- 搬不动就保留原地址（图还在原处），不阻断、不写坏数据
- 走 `revision_guard()` 乐观并发控制，冲突时跳过并报告
- 同步 `draft_verified_revision`，避免商品从自动化结果页消失

## 用法

    # 预览
    venv/bin/python -m scripts.promote_warehouse_images

    # 真改
    venv/bin/python -m scripts.promote_warehouse_images --apply

    # 只处理某几件
    venv/bin/python -m scripts.promote_warehouse_images --apply --ids 511,534
"""
from __future__ import annotations

import argparse
import re
import sys
from copy import deepcopy
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

AUTO_MARK = "/auto-temp/"
URL_RE = re.compile(
    r"https://[^\s\"'<>]*?/tk-product-automation/auto-temp/[A-Za-z0-9._-]+")


def _count_refs(payload) -> int:
    """统计 payload 里 auto-temp 引用的处数（notes 的 HTML 串按 URL 拆）。"""
    total = 0

    def walk(node):
        nonlocal total
        if isinstance(node, str):
            if AUTO_MARK in node:
                found = URL_RE.findall(node)
                total += len(found) if found else 1
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把成品仓商品图从 auto-temp 搬到 products")
    parser.add_argument("--apply", action="store_true", help="真正写库")
    parser.add_argument("--ids", default="",
                        help="只处理指定商品 ID，逗号分隔")
    parser.add_argument("--limit", type=int, default=0,
                        help="最多处理多少件（0 = 全部）")
    args = parser.parse_args(argv)

    from sqlalchemy.exc import IntegrityError

    from db.session import SessionLocal
    from models.product import Product
    from services import image_upload
    from services.warehouse_service import revision_guard

    only_ids: set[int] = set()
    if args.ids.strip():
        for chunk in args.ids.split(","):
            chunk = chunk.strip()
            if chunk.isdigit():
                only_ids.add(int(chunk))

    db = SessionLocal()
    try:
        query = db.query(Product).filter(Product.status == "ready")
        products = query.all()

        targets = []
        for product in products:
            if only_ids and int(product.id) not in only_ids:
                continue
            payload = product.payload or {}
            if AUTO_MARK not in repr(payload):
                continue
            count = _count_refs(payload)
            if count:
                targets.append((product, count))

        if not targets:
            print("没有需要搬运的商品。")
            return 0

        total_refs = sum(c for _p, c in targets)
        print(f"待处理商品 {len(targets)} 件，共 {total_refs} 处 auto-temp 引用")
        if args.limit:
            targets = targets[:args.limit]
            print(f"（--limit 生效，本次只处理 {len(targets)} 件）")

        if not args.apply:
            print("\n[dry-run] 未写库。加 --apply 执行。")
            for product, count in targets[:10]:
                print(f"  #{product.id} rev={product.revision} {count} 处")
            if len(targets) > 10:
                print(f"  ... 还有 {len(targets) - 10} 件")
            return 0

        moved_total = failed_total = skipped = 0
        touched = 0
        for product, _count in targets:
            try:
                # expected_revision=None：脚本按当前库里的版本推进，
                # 不做外部版本校验（这层保护留给前端 PATCH）。
                revision_guard(db, product, None, action="搬运商品图")
            except Exception as exc:  # noqa: BLE001 - 并发冲突跳过即可
                skipped += 1
                print(f"  [SKIP] #{product.id} 并发冲突：{str(exc)[:80]}")
                continue

            # deepcopy：promote_payload_images 是就地改，
            # 直接传 product.payload 会让 SQLAlchemy 认为对象没变（同一个引用），
            # 静态字段不会标脏，写库变成空操作。
            payload = deepcopy(product.payload or {})
            try:
                report = image_upload.promote_payload_images(payload)
            except Exception as exc:  # noqa: BLE001 - 单件失败不影响其它
                failed_total += 1
                print(f"  [FAIL] #{product.id} 搬运异常：{str(exc)[:100]}")
                continue

            moved = int(report.get("moved") or 0)
            failed = int(report.get("failed") or 0)
            moved_total += moved
            failed_total += failed
            if not moved:
                print(f"  [SKIP] #{product.id} 没有可搬的（failed={failed}）")
                continue

            product.payload = payload
            # 编辑商品必须同步 draft_verified_revision，
            # 否则该商品会从自动化结果页消失（CLAUDE.md 规则 4）。
            if product.automation_result and product.draft_verified_revision is not None:
                product.draft_verified_revision = int(product.revision or 0)
            touched += 1
            print(f"  [OK] #{product.id} 搬了 {moved} 处，失败 {failed}")
            try:
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                failed_total += 1
                print(f"  [FAIL] #{product.id} 写库冲突：{str(exc)[:100]}")

        print(f"\n完成：处理 {touched} 件，搬运 {moved_total} 处，"
              f"失败 {failed_total}，跳过 {skipped}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
