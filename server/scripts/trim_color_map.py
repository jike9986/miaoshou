"""清理 colorMap / sizeMap 里多余的条目，并删除已搬进成品区的冗余图。

负责人 2026-11-19 指出：「有些成品是处理过最多保留九种颜色，
多余的颜色是清理的，这些图片不能同步传到成品区里面。」

历史数据里两处问题：
1. `apply_image_limits` 之前不裁 `colorMap` → 被删颜色的条目还留着，
   连带它们的图片地址（入库搬运时被搬进 products）。
2. `product_standard` 把尺码写进了 `colorMap` → EUR33/32码 这类值
   被当成颜色。

这个脚本：
· 按 `skuPropertyList` 里实际保留的颜色，裁掉 `colorMap` 的多余条目
· 把 `sizeMap` 里混进来的颜色条目挪回 `colorMap`（反之亦然）
· 报告哪些图片因此变成无人引用（**不自动删文件**，只报告）

## 安全措施
- 默认 dry-run，`--apply` 才写库
- 只动 `status='ready'` 的商品
- 走 `revision_guard`，同步 `draft_verified_revision`
- 只删 `colorMap`/`sizeMap` 的多余 key，**不删文件**

## 用法
    venv/bin/python -m scripts.trim_color_map
    venv/bin/python -m scripts.trim_color_map --apply
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

# 尺码长这样：40 / 42码 / EUR33 / US7 / L(37-39) / M(34-36) / 36-38
_SIZE_LIKE_RE = re.compile(
    r"^\s*(?:EUR|US|UK|EU|CM|MM)?\s*\d{1,3}(?:\.\d)?\s*(?:码|kg|cm)?\s*$"
    r"|^\s*(?:EUR|US|UK|EU)?\s*\d{1,3}(?:\.\d)?\s*[-–~]\s*\d{1,3}(?:\.\d)?\s*$"
    r"|^\s*[XSML]{1,3}\s*[（(]?\s*\d{1,3}\s*[-–~]\s*\d{1,3}\s*[)）]?\s*$"
    r"|^\s*(?:Saiz|Size|尺码|尺寸)\s*$",
    re.IGNORECASE,
)


def _kept_colors(payload: dict) -> tuple[set[str], set[str]]:
    """从 skuPropertyList 取出实际保留的 (颜色值, 尺码值)。"""
    from services.warehouse_service import _is_color_property

    colors: set[str] = set()
    sizes: set[str] = set()
    for prop in (payload.get("skuPropertyList") or []):
        if not isinstance(prop, dict):
            continue
        values = [v for v in (prop.get("attrValueList") or []) if isinstance(v, dict)]
        target = colors if _is_color_property(prop) else sizes
        for value in values:
            label = str(value.get("attrValue") or "").strip()
            if label:
                target.add(label)
    return colors, sizes


def _entry_urls(entry) -> list[str]:
    urls: list[str] = []
    if isinstance(entry, str):
        urls.append(entry)
    elif isinstance(entry, dict):
        for field in ("imgUrl", "imageUrl"):
            value = entry.get(field)
            if isinstance(value, str) and value:
                urls.append(value)
        multi = entry.get("imgUrls")
        if isinstance(multi, list):
            urls.extend(u for u in multi if isinstance(u, str))
        elif isinstance(multi, str) and multi:
            urls.append(multi)
    return urls


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理 colorMap/sizeMap 多余条目")
    parser.add_argument("--apply", action="store_true", help="真正写库")
    parser.add_argument("--ids", default="", help="只处理指定商品 ID")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少件")
    args = parser.parse_args(argv)

    from copy import deepcopy

    from db.session import SessionLocal
    from models.product import Product
    from services.group_access import is_restricted_group_id
    from services.warehouse_service import revision_guard

    only_ids: set[int] = set()
    if args.ids.strip():
        for chunk in args.ids.split(","):
            if chunk.strip().isdigit():
                only_ids.add(int(chunk.strip()))

    db = SessionLocal()
    try:
        products = db.query(Product).filter(Product.status == "ready").all()
        targets = []
        for product in products:
            if only_ids and int(product.id) not in only_ids:
                continue
            # 受限分组（自营海外仓 / 海外仓定制款 / 精品）不受颜色上限约束，
            # 颜色超过 9 是正常的 —— 不能按「上限 9」去裁它们的 colorMap
            # （负责人 2026-11-19）。这些商品的 colorMap 只清理混进来的尺码条目。
            restricted = is_restricted_group_id(product.group_id)
            payload = product.payload or {}
            color_map = payload.get("colorMap")
            if not isinstance(color_map, dict) or not color_map:
                continue
            kept_colors, kept_sizes = _kept_colors(payload)
            if not kept_colors:
                continue
            if restricted:
                # 只挑「明显是尺码」的 key，绝不按颜色数裁
                extra = [k for k in color_map
                         if str(k).strip() not in kept_colors
                         and (str(k).strip() in kept_sizes
                              or _SIZE_LIKE_RE.match(str(k).strip()))]
                if extra:
                    targets.append((product, sorted(extra), kept_colors, kept_sizes))
                continue
            extra = [k for k in color_map if str(k).strip() not in kept_colors]
            # colorMap 里混进来的尺码条目（历史 product_standard bug 的残留）。
            # 这些 key 不在 kept_sizes 里也不在 kept_colors 里 —— 用启发式识别：
            #   · 纯数字 / 带括号的尺码区间（"40" / "L(37-39)" / "EUR33"）
            #   · 或者 key 出现在 sizeMap 里
            size_map = payload.get("sizeMap")
            size_keys = set(size_map.keys()) if isinstance(size_map, dict) else set()
            for label in color_map:
                text = str(label).strip()
                if text in kept_colors:
                    continue
                if text in extra:
                    continue
                looks_like_size = (
                    text in size_keys
                    or text in kept_sizes
                    or _SIZE_LIKE_RE.match(text) is not None
                )
                if looks_like_size:
                    extra.append(label)
            if not extra:
                continue
            targets.append((product, sorted(extra), kept_colors, kept_sizes))

        if not targets:
            print("没有需要清理的商品。")
            return 0

        print(f"待清理商品 {len(targets)} 件")
        if args.limit:
            targets = targets[:args.limit]

        total_extra = sum(len(e) for _p, e, _c, _s in targets)
        orphan_urls: set[str] = set()
        for product, extra, _colors, _sizes in targets:
            color_map = (product.payload or {}).get("colorMap") or {}
            for label in extra:
                for url in _entry_urls(color_map.get(label)):
                    orphan_urls.add(url)

        print(f"多余条目 {total_extra} 条，涉及图片 URL {len(orphan_urls)} 个")

        if not args.apply:
            print("\n[dry-run] 未写库。加 --apply 执行。")
            for product, extra, _c, _s in targets[:10]:
                print(f"  #{product.id} rev={product.revision} 多余 {len(extra)} 条："
                      f"{extra[:4]}")
            if len(targets) > 10:
                print(f"  ... 还有 {len(targets) - 10} 件")
            return 0

        cleaned = removed_total = 0
        for product, extra, _colors, _sizes in targets:
            try:
                revision_guard(db, product, None, action="清理多余颜色")
            except Exception as exc:  # noqa: BLE001
                print(f"  [SKIP] #{product.id} 并发冲突：{str(exc)[:80]}")
                continue

            # deepcopy：就地改 product.payload 同一个引用时，
            # SQLAlchemy 不会标脏，写库会变成空操作。
            payload = deepcopy(product.payload or {})
            color_map = payload.get("colorMap") or {}
            removed = 0
            for label in extra:
                if color_map.pop(label, None) is not None:
                    removed += 1
            if not removed:
                continue
            product.payload = payload
            if product.automation_result and product.draft_verified_revision is not None:
                product.draft_verified_revision = int(product.revision or 0)
            db.commit()
            cleaned += 1
            removed_total += removed
            print(f"  [OK] #{product.id} 清掉 {removed} 条")

        print(f"\n完成：清理 {cleaned} 件，删掉 {removed_total} 条多余颜色条目")
        if orphan_urls:
            print(f"\n以下 {len(orphan_urls)} 个图片现在无人引用（脚本不删文件）：")
            for url in sorted(orphan_urls)[:15]:
                print(f"  {url.rsplit('/', 1)[-1]}")
            if len(orphan_urls) > 15:
                print(f"  ... 还有 {len(orphan_urls) - 15} 个")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
