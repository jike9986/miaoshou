# -*- coding: utf-8 -*-
"""处理商品图里的外链：搬到自家图床，失效的清掉。

## 两类外链（2026-11-24 实测，222 件成品仓商品共 203 处）

1. `earth-rt.chengji-inc.com` — 妙手侧图床，192 处，全在 #1~#18 的详情图
   **实测 HTTP 404 已失效**，留着只会让前端白请求一次再显示裂图。
   → 从 payload 里移除。

2. `cbu01.alicdn.com` — 1688 官方图床，11 处，全在 #19 的详情图
   **实测 HTTP 200 / 280 KB 正常**。
   → 下载 → 转 JPEG → 写进成品区 → 生成 thumbs/mid → 地址换成自家域名。

## 用法

    python tools/source_match/fix_external_images.py --dry-run
    python tools/source_match/fix_external_images.py
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = (HERE / "../../server").resolve()
PRODUCT_DIR = Path("/var/www/tk-product-automation/products")
PRODUCT_BASE = "https://aohuang888.com/tk-product-automation/products/"
DEAD_HOSTS = ("earth-rt.chengji-inc.com",)
FETCH_HOSTS = ("cbu01.alicdn.com", "cbu02.alicdn.com", "sc01.alicdn.com",
               "sc02.alicdn.com", "img.alicdn.com")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--product-dir", default=str(PRODUCT_DIR))
    ap.add_argument("--timeout", type=int, default=45)
    args = ap.parse_args()

    sys.path.insert(0, str(SERVER))
    import warnings

    warnings.filterwarnings("ignore")
    import requests
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
            select id, title, images, detail_images
            from products
            where deleted_at is null and automation_result is false
              and (images::text ~ 'chengji-inc|alicdn'
                   or detail_images::text ~ 'chengji-inc|alicdn')
            order by id
        """)).fetchall()
        print(f"涉及商品 {len(rows)} 件")
        if not rows:
            return 0

        stats = {"dead_removed": 0, "fetched": 0,
                 "fetch_failed": 0, "products_touched": 0}

        for pid, title, imgs, dimgs in rows:
            changed = False
            new_main: list[str] = []
            new_detail: list[str] = []

            for kind, arr, out in (("主图", imgs, new_main),
                                   ("详情图", dimgs, new_detail)):
                for raw in (arr or []):
                    url = str(raw or "").strip()
                    if not url:
                        continue

                    # ① 失效外链直接丢
                    if any(h in url for h in DEAD_HOSTS):
                        stats["dead_removed"] += 1
                        changed = True
                        print(f"  #{pid} [{kind}] 移除失效 {url[:64]}…")
                        continue

                    # ② 可下载的外链：搬进自家图床
                    if any(h in url for h in FETCH_HOSTS):
                        if args.dry_run:
                            stats["fetched"] += 1
                            print(f"  #{pid} [{kind}] 将下载 {url[:64]}…")
                            continue
                        try:
                            local_url = _ingest(url, root, requests,
                                                args.timeout, make_thumbnail,
                                                make_mid_image)
                            if local_url:
                                out.append(local_url)
                                stats["fetched"] += 1
                                changed = True
                                print(f"  #{pid} [{kind}] 已搬入 {local_url.rsplit('/',1)[-1]}")
                            else:
                                stats["fetch_failed"] += 1
                                out.append(url)   # 搬不动就留原地址，至少还能看
                        except Exception as exc:  # noqa: BLE001
                            stats["fetch_failed"] += 1
                            print(f"  #{pid} [{kind}] 搬运失败 {type(exc).__name__}: {str(exc)[:60]}")
                            out.append(url)
                        continue

                    out.append(url)

            if changed and not args.dry_run:
                db.execute(text("""
                    update products
                    set images = cast(:imgs as json),
                        detail_images = cast(:dimgs as json),
                        revision = coalesce(revision, 0) + 1,
                        updated_at = extract(epoch from now())::double precision
                    where id = :pid
                """), {"pid": pid,
                       "imgs": _dumps(new_main),
                       "dimgs": _dumps(new_detail)})
                # 规则 §4：自动化结果商品同步 draft_verified_revision
                db.execute(text("""
                    update products set draft_verified_revision = revision
                    where id = :pid and automation_result is true
                      and draft_verified_revision is not null
                """), {"pid": pid})
                stats["products_touched"] += 1

        if not args.dry_run:
            db.commit()

        print()
        print(f"移除失效 {stats['dead_removed']} 处")
        print(f"搬入自家 {stats['fetched']} 处，失败 {stats['fetch_failed']} 处")
        print(f"更新商品 {stats['products_touched']} 件")
        if args.dry_run:
            print("（dry-run，未写库）")
    finally:
        db.close()
    return 0


def _dumps(values: list[str]) -> str:
    import json
    return json.dumps(values, ensure_ascii=False)


def _ingest(url: str, root: Path, requests, timeout: int,
            make_thumbnail, make_mid_image) -> str:
    """下载 → 转 JPEG → 写盘 → 生成派生图 → 返回自家地址。"""
    from PIL import Image

    resp = requests.get(url, timeout=timeout,
                        headers={"User-Agent": "Mozilla/5.0",
                                 "Referer": "https://www.1688.com/"})
    resp.raise_for_status()
    raw = resp.content
    if len(raw) < 2048:
        return ""

    # 用 URL 内容哈希做文件名：同一张图重复搬运不会产生多份
    stem = hashlib.sha256(raw).hexdigest()
    target = root / f"{stem}.jpg"
    if not target.exists():
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=92, optimize=True)
        data = buf.getvalue()
        target.write_bytes(data)
        os.chmod(target, 0o644)
        # 派生图
        for subdir, maker in (("thumbs", make_thumbnail), ("mid", make_mid_image)):
            d = maker(data)
            if not d:
                continue
            path = root / subdir / f"{stem}.jpg"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(d)
            os.chmod(path, 0o644)
    return PRODUCT_BASE + target.name


if __name__ == "__main__":
    sys.exit(main())