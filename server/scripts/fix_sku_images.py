"""用主图补齐 SKU 颜色缺图（源图已在 1688 删除的情况下）。

背景：少数商品的颜色值 imgUrl 仍指向 `earth-rt.chengji-inc.com`，
而那张图在源站已 404（转存时抓不到）。颜色图缺失会让「规格」区
出现破图占位，发布时也可能被上游判为规格不完整。

策略：优先取该维度的其他值里已经转存好的图，其次退到商品主图。
不伪造图片内容，只做「同商品内的图复用」，并在 payload 里留痕。

默认只报告；--apply 才写库。
"""
from __future__ import annotations

import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

OWN_HOST = "aohuang888.com"


def _is_external(url: object) -> bool:
    text = str(url or "")
    return text.startswith("http") and OWN_HOST not in text


def _pick_fallback(payload: dict, values: list, exclude_index: int) -> str:
    """给第 exclude_index 个值找一张兜底图。

    优先级：同维度其他值的自有图 → 商品主图。
    同维度的图是那个颜色自己的，视觉上最接近；主图至少保证是同一件商品。
    """
    for index, value in enumerate(values):
        if index == exclude_index or not isinstance(value, dict):
            continue
        for key in ("imgUrl", "imageUrl"):
            candidate = str(value.get(key) or "")
            if candidate.startswith("http") and OWN_HOST in candidate:
                return candidate
        for candidate in value.get("imgUrls") or []:
            text = str(candidate or "")
            if text.startswith("http") and OWN_HOST in text:
                return text
    for candidate in payload.get("imgUrls") or []:
        text = str(candidate or "")
        if text.startswith("http") and OWN_HOST in text:
            return text
    return ""


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="补齐 SKU 颜色缺图")
    parser.add_argument("--apply", action="store_true", help="真正写库")
    parser.add_argument("--ids", default="", help="只处理指定商品 id")
    args = parser.parse_args(argv)
    only = [int(x) for x in args.ids.split(",") if x.strip().isdigit()] or None

    from db.session import SessionLocal
    from models.product import Product

    session = SessionLocal()
    try:
        query = session.query(Product).filter(Product.deleted_at.is_(None))
        if only:
            query = query.filter(Product.id.in_(only))
        products = query.all()

        touched = 0
        for product in products:
            payload = product.payload if isinstance(product.payload, dict) else {}
            props = payload.get("skuPropertyList") or []
            if not props:
                continue
            changed = False
            new_props = []
            for prop in props:
                if not isinstance(prop, dict):
                    new_props.append(prop)
                    continue
                values = list(prop.get("attrValueList") or [])
                rewritten = []
                for index, value in enumerate(values):
                    if not isinstance(value, dict):
                        rewritten.append(value)
                        continue
                    broken = any(_is_external(value.get(key))
                                 for key in ("imgUrl", "imageUrl"))
                    broken = broken or any(_is_external(item)
                                           for item in (value.get("imgUrls") or []))
                    if not broken:
                        rewritten.append(value)
                        continue
                    fallback = _pick_fallback(payload, values, index)
                    if not fallback:
                        # 找不到可用兜底就清空引用，让界面走占位而不是破图
                        fixed = dict(value)
                        fixed.pop("imgUrl", None)
                        fixed.pop("imageUrl", None)
                        fixed["imgUrls"] = []
                        rewritten.append(fixed)
                        changed = True
                        print("p%s | %s | %s -> 清空（无兜底图）" % (
                            product.id, prop.get("attrName"), value.get("attrValue")))
                        continue
                    fixed = dict(value)
                    fixed["imgUrl"] = fallback
                    fixed["imageUrl"] = fallback
                    fixed["imgUrls"] = [fallback]
                    fixed["imgUrlReplacedFrom"] = str(
                        value.get("imgUrl") or value.get("imageUrl") or "")
                    rewritten.append(fixed)
                    changed = True
                    print("p%s | %s | %s -> %s" % (
                        product.id, prop.get("attrName"), value.get("attrValue"),
                        fallback[:70]))
                new_props.append({**prop, "attrValueList": rewritten})

            if changed:
                touched += 1
                if args.apply:
                    mutated = dict(payload)
                    mutated["skuPropertyList"] = new_props
                    product.payload = mutated

        print("---")
        print("命中 %d 件" % touched)
        if args.apply:
            session.commit()
            print("已写库")
        else:
            print("仅报告（加 --apply 才写库）")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
