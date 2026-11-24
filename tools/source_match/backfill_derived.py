# -*- coding: utf-8 -*-
"""补齐缺失的派生图（thumbs/ 与 mid/）。

## 为什么需要

派生图（200px 缩略图 / 900px 中等图）是在 `promote_to_product_zone()`
里生成的，只在图片**第一次搬进成品区**时跑。所以：

  · 2026-11-11 那批先入库、派生图机制后来才上线的图 → 只有原图
  · 尺码图 `sizeChart` 走 `promote_payload_images` 的 `_swap`，
    但原图早就在成品区了，`auto_temp_name()` 返回空 → 直接跳过，
    派生图永远不生成

实测（222 件成品仓商品）：
  · 正文图片 3060 处，缺 thumbs/mid 各 203 处（6.6%）
  · 尺码图 222 张，缺 thumbs 221 张（99.5%）

## 用法

    python tools/source_match/backfill_derived.py --dry-run
    python tools/source_match/backfill_derived.py
"""
from __future__ import annotations

import argparse
import io
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = (HERE / "../../server").resolve()
PRODUCT_DIR = Path("/var/www/tk-product-automation/products")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--product-dir", default=str(PRODUCT_DIR))
    args = ap.parse_args()

    sys.path.insert(0, str(SERVER))
    import warnings

    warnings.filterwarnings("ignore")
    from PIL import Image
    from sqlalchemy import text

    from db.session import SessionLocal
    from services.image_upload import make_mid_image, make_thumbnail

    root = Path(args.product_dir)
    if not root.is_dir():
        print(f"找不到 {root}")
        return 1

    db = SessionLocal()
    try:
        rows = db.execute(text("""
            select id, images, detail_images, sku_properties, payload
            from products
            where deleted_at is null and automation_result is false
        """)).fetchall()

        # 收集所有被引用的图片文件名
        needed: set[str] = set()
        for _pid, imgs, dimgs, skus, payload in rows:
            for u in list(imgs or []) + list(dimgs or []):
                fn = str(u).rsplit("/", 1)[-1]
                if fn and "/" not in fn:
                    needed.add(fn)
            if skus:
                try:
                    for dim in (skus.get("skuPropertyList") or []):
                        for v in (dim.get("values") or []):
                            u = v.get("imageUrl") or v.get("imgUrl")
                            if u:
                                fn = str(u).rsplit("/", 1)[-1]
                                if fn and "/" not in fn:
                                    needed.add(fn)
                except Exception:
                    pass
            chart = str((payload or {}).get("sizeChart") or "")
            if chart:
                fn = chart.rsplit("/", 1)[-1]
                if fn and "/" not in fn:
                    needed.add(fn)

        print(f"商品 {len(rows)} 件，引用图片 {len(needed)} 个文件")

        made_th = made_mid = skip_exist = no_src = failed = 0
        for i, fn in enumerate(sorted(needed), 1):
            src = root / fn
            if not src.is_file():
                no_src += 1
                continue
            stem = fn.rsplit(".", 1)[0]
            th = root / "thumbs" / f"{stem}.jpg"
            mid = root / "mid" / f"{stem}.jpg"

            want_th = not th.is_file()
            want_mid = not mid.is_file()
            if not want_th and not want_mid:
                skip_exist += 1
                continue
            if args.dry_run:
                made_th += int(want_th)
                made_mid += int(want_mid)
                continue

            try:
                raw = src.read_bytes()
                # 原图可能已经是优化过的 JPEG；直接喂给派生函数
                if want_th:
                    data = make_thumbnail(raw)
                    if data:
                        th.parent.mkdir(parents=True, exist_ok=True)
                        th.write_bytes(data)
                        os.chmod(th, 0o644)
                        made_th += 1
                if want_mid:
                    data = make_mid_image(raw)
                    if data:
                        mid.parent.mkdir(parents=True, exist_ok=True)
                        mid.write_bytes(data)
                        os.chmod(mid, 0o644)
                        made_mid += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                if failed <= 5:
                    print(f"  {fn[:40]} 失败 {type(exc).__name__}: {str(exc)[:60]}")
            if i % 200 == 0:
                print(f"  进度 {i}/{len(needed)}  thumbs+{made_th} mid+{made_mid}",
                      flush=True)

        verb = "将生成" if args.dry_run else "已生成"
        print(f"\n{verb}：thumbs {made_th}，mid {made_mid}")
        print(f"已存在跳过 {skip_exist}，原图缺失 {no_src}，失败 {failed}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())