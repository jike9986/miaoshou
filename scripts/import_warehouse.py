"""把老版 runtime/warehouse.json 迁移到新库，用于验证真实数据。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from db.session import init_db, session_scope
from models.category import Category
from models.group import Group
from models.product import Product

OLD = Path(r"E:\codex工作台\p006\tk-product-automation\runtime\warehouse.json")


def main() -> None:
    init_db()
    data = json.loads(OLD.read_text(encoding="utf-8"))
    entries = data if isinstance(data, list) else data.get("entries") or []
    created = 0
    with session_scope() as db:
        groups = {g.name: g for g in db.query(Group).all()}
        for entry in entries:
            payload = entry.get("payload") or {}
            detail_id = str(entry.get("detailId") or "")
            shop_id = str(entry.get("shopId") or "")
            if not detail_id:
                continue
            exists = (
                db.query(Product)
                .filter(Product.detail_id == detail_id, Product.shop_id == shop_id)
                .first()
            )
            if exists:
                continue
            group_name = str(entry.get("group") or "").strip()
            if group_name and group_name not in groups:
                g = Group(name=group_name)
                db.add(g)
                db.flush()
                groups[group_name] = g
            images = [u for u in (payload.get("imgUrls") or []) if u][:9]
            prices = []
            for sku in (payload.get("skuMap") or {}).values():
                if isinstance(sku, dict) and str(sku.get("isDelete", "0")) != "1":
                    try:
                        v = float(sku.get("originPrice"))
                        if v > 0:
                            prices.append(v)
                    except (TypeError, ValueError):
                        pass
            prices_min = min(prices) if prices else None
            product = Product(
                warehouse_key=f"{detail_id}:{shop_id}",
                detail_id=detail_id,
                shop_id=shop_id,
                cid=str(payload.get("cid") or ""),
                price_currency="CNY",
                status="ready",
                title=str(payload.get("title") or entry.get("title") or ""),
                language=str(entry.get("language") or "ms"),
                cover=images[0] if images else "",
                category_name=str(entry.get("categoryName") or ""),
                group_id=groups[group_name].id if group_name else None,
                group_name=group_name,
                price_min=prices_min,
                price_max=max(prices) if prices else None,
                stock=int(entry.get("stock") or 0),
                sku_count=len(payload.get("skuMap") or {}),
                images=images,
                detail_images=[],
                sku_properties=payload.get("skuPropertyList") or [],
                payload=payload,
                claimed=bool(entry.get("claimed")),
                collected=entry.get("collected") or {},
            )
            db.add(product)
            created += 1
    print(f"导入 {created} 件（总条目 {len(entries)}）")


if __name__ == "__main__":
    main()
