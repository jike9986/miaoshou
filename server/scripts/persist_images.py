"""把历史商品里仍指向外部图床（1688 等）的图片转存到自有图床。

为什么需要这个脚本：
  `services/image_store.persist_payload_images()` 会在「处理入仓」和「认领」
  时把外链图转存到 aohuang888.com。但这套逻辑是后来才补齐 `sizeChart`
  和 SKU 图的 —— 更早入库的商品（2026-10 那批）跳过了这两处，
  于是至今还在直接引用 `earth-rt.chengji-inc.com`。

  1688 的图床会定期清理旧图，实测 2026-11-07 有 7 张详情图 404。
  这个脚本把历史数据补一遍，之后建议定期跑（或与 check_images.py 一起跑）。

覆盖四处：主图 imgUrls、详情图 notes、SKU 图 skuPropertyList、尺码图 sizeChart。
默认只报告不写库；加 --apply 才真改。

用法（服务器上）：
    python scripts/persist_images.py            # 只报告
    python scripts/persist_images.py --apply    # 真转存
"""
from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# 以 `python scripts/xxx.py` 方式运行时 server/ 不在 sys.path 里
SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

URL_RE = re.compile(r"https?://[^\s\"'<>\\]+", re.I)
OWN_HOST = "aohuang888.com"


def _external_urls(payload: dict[str, Any]) -> dict[str, list[str]]:
    """列出 payload 里所有非自有图床的 http(s) 地址，按来源分组。"""
    found: dict[str, list[str]] = {"main": [], "detail": [], "sku": [], "sizeChart": []}
    for url in payload.get("imgUrls") or []:
        if isinstance(url, str) and url.startswith("http") and OWN_HOST not in url:
            found["main"].append(url)
    for url in URL_RE.findall(str(payload.get("notes") or "")):
        if OWN_HOST not in url:
            found["detail"].append(url)
    for prop in payload.get("skuPropertyList") or []:
        if not isinstance(prop, dict):
            continue
        for value in prop.get("attrValueList") or []:
            if not isinstance(value, dict):
                continue
            for key in ("imgUrl", "imageUrl"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.startswith("http") \
                        and OWN_HOST not in candidate:
                    found["sku"].append(candidate)
            for candidate in value.get("imgUrls") or []:
                if isinstance(candidate, str) and candidate.startswith("http") \
                        and OWN_HOST not in candidate:
                    found["sku"].append(candidate)
    chart = payload.get("sizeChart")
    if isinstance(chart, str) and chart.startswith("http") and OWN_HOST not in chart:
        found["sizeChart"].append(chart)
    return {key: list(dict.fromkeys(value)) for key, value in found.items() if value}


def migrate(apply_changes: bool = False,
            only_ids: Iterable[int] | None = None) -> dict[str, Any]:
    from db.session import SessionLocal
    from models.product import Product
    from services import image_store

    session = SessionLocal()
    try:
        query = session.query(Product).filter(Product.deleted_at.is_(None))
        if only_ids:
            query = query.filter(Product.id.in_(list(only_ids)))
        products = query.all()

        report: dict[str, Any] = {"products": len(products), "affected": [],
                                  "stored": 0, "failed": 0, "applied": apply_changes}
        for product in products:
            payload = product.payload if isinstance(product.payload, dict) else {}
            external = _external_urls(payload)
            if not external:
                continue
            report["affected"].append({
                "id": product.id,
                "title": str(product.title or "")[:40],
                "counts": {key: len(value) for key, value in external.items()},
            })
            if not apply_changes:
                continue
            # persist_payload_images 会原地改 payload（dict 是可变对象），
            # 但 SQLAlchemy 的 JSON 列需要重新赋值才会标记为脏。
            mutated = dict(payload)
            stats = image_store.persist_payload_images(mutated)
            product.payload = mutated
            report["stored"] += int(stats.get("stored") or 0)
            report["failed"] += int(stats.get("failed") or 0)

        if apply_changes:
            session.commit()
        return report
    finally:
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="历史商品图片转存到自有图床")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只报告）")
    parser.add_argument("--ids", default="", help="只处理指定商品 id，逗号分隔")
    args = parser.parse_args()
    ids = [int(x) for x in args.ids.split(",") if x.strip().isdigit()] or None

    report = migrate(apply_changes=args.apply, only_ids=ids)
    print(f"商品 {report['products']} 件，含外链图的 {len(report['affected'])} 件")
    for item in report["affected"]:
        print(f"  p{item['id']} {item['title']} -> {item['counts']}")
    if args.apply:
        print(f"转存成功 {report['stored']} 张，失败 {report['failed']} 张")
    else:
        print("仅报告（加 --apply 才写库）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
