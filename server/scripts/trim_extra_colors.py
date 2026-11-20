"""把成品仓库里颜色数超过 9 的商品裁到 9 个颜色（负责人 2026-11-19 要求）。

范围与判据：
  · 只处理**非受限分组**（自营海外仓 / 海外仓定制款 / 精品 三组不动 ——
    那三组走合作厂家，颜色本来就都在 9 以内，实测 0 件超限）。
  · 颜色维度的判定复用 `services.group_access` 之外的同源逻辑：
    优先 attrId=100000，其次属性名（颜色/Warna/Color…），
    最后看「该属性的值是否挂了 SKU 图」—— 只有颜色值会挂图。
  · 保留前 9 个颜色，其余颜色连同其 SKU 组合一起删除。
    删除用 `apply_image_limits`（与入仓流程同一份实现），
    保证「本次清理」和「以后入仓」的裁剪规则永远一致。

安全措施：
  · 默认 dry-run，只打印将要发生什么；`--apply` 才写库。
  · 写库前过 `revision_guard`（乐观并发），并同步 `draft_verified_revision`，
    否则商品会从自动化结果页消失（CLAUDE.md 第 4 条）。
  · 逐件单独提交，某件失败不影响其它件。

用法（在服务器上，source 环境变量后执行）：
    venv/bin/python -m scripts.trim_extra_colors           # 预览
    venv/bin/python -m scripts.trim_extra_colors --apply   # 真改
"""
from __future__ import annotations

import argparse
import sys
from copy import deepcopy
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

MAX_COLORS = 9
RESTRICTED_GROUPS = frozenset({1, 2, 3})


def _color_value_count(payload: dict) -> int | None:
    from services.warehouse_service import _is_color_property

    for prop in (payload.get("skuPropertyList") or []):
        if isinstance(prop, dict) and _is_color_property(prop):
            return len([v for v in (prop.get("attrValueList") or [])
                        if isinstance(v, dict)])
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="裁掉成品仓库里多余的顏色")
    parser.add_argument("--apply", action="store_true", help="真正写库")
    parser.add_argument("--limit", type=int, default=MAX_COLORS,
                        help=f"每个商品保留的颜色数（默认 {MAX_COLORS}）")
    args = parser.parse_args(argv)

    from db.session import SessionLocal
    from models.group import Group
    from models.product import Product
    from services.warehouse_service import apply_image_limits, revision_guard

    db = SessionLocal()
    try:
        groups = {g.id: g.name for g in db.query(Group).all()}
        rows = (db.query(Product)
                .filter(Product.status == "ready")
                .order_by(Product.id)
                .all())
        print(f"成品仓库待检查 {len(rows)} 件（排除受限分组 "
              f"{sorted(RESTRICTED_GROUPS)}）")

        targets = []
        for product in rows:
            if product.group_id in RESTRICTED_GROUPS:
                continue
            payload = product.payload if isinstance(product.payload, dict) else {}
            colors = _color_value_count(payload)
            if colors is None or colors <= args.limit:
                continue
            targets.append((product, colors))

        if not targets:
            print("没有颜色超标的商品，无需清理。")
            return 0

        print(f"\n颜色数 > {args.limit} 的商品 {len(targets)} 件：")
        for product, colors in targets:
            before_sku = len((product.payload or {}).get("skuMap") or {})
            print(f"  #{product.id} [{groups.get(product.group_id)}] "
                  f"{str(product.title)[:46]}")
            print(f"      颜色 {colors} -> {args.limit}，SKU {before_sku} -> ?")

        if not args.apply:
            print("\n[dry-run] 未写库。加 --apply 执行。")
            return 0

        changed = 0
        for product, _declared_colors in targets:
            # 必须深拷贝：apply_image_limits 是**原地修改**入参的。
            # 直接传 product.payload 的话，SQLAlchemy 看到的是同一个对象引用，
            # 认为「这个字段没被重新赋值」，commit 时不会把变更写库 ——
            # 脚本会报告成功，但库里的数据一点没变（2026-11-19 实际踩过一次）。
            raw = product.payload if isinstance(product.payload, dict) else {}
            payload = deepcopy(raw)
            before_sku = len(payload.get("skuMap") or {})
            expected = int(product.revision or 0)
            report = apply_image_limits(payload, main_limit=0, detail_limit=0,
                                        sku_limit=args.limit,
                                        # 受限分组（自营海外仓/海外仓定制款/精品）
                                        # 不套颜色上限 —— 传进去让 apply_image_limits
                                        # 自己跳过，脚本不重复判断（负责人 2026-11-19）
                                        group_id=product.group_id)
            after_sku = len(payload.get("skuMap") or {})
            if not report.get("colors"):
                continue
            try:
                # 乐观并发：清理期间有人改了这件商品就跳过，不覆盖他的改动
                revision_guard(db, product, expected, action="裁剪多余颜色")
                # 重新赋值（新对象），SQLAlchemy 才会把 payload 标记为 dirty
                product.payload = payload
                product.sku_count = after_sku
                # CLAUDE.md 第 4 条：改商品后同步 draft_verified_revision，
                # 否则它会从自动化结果页消失
                if (product.automation_result
                        and product.draft_verified_revision is not None):
                    product.draft_verified_revision = int(product.revision or 0)
                db.commit()
                changed += 1
                print(f"  [OK] #{product.id} 颜色 {report['colors']['before']} -> "
                      f"{report['colors']['after']}，"
                      f"SKU {before_sku} -> {after_sku}")
            except Exception as exc:  # noqa: BLE001 - 单件失败不影响其它件
                db.rollback()
                print(f"  [SKIP] #{product.id} 跳过：{str(exc)[:110]}")

        print(f"\n完成：{changed}/{len(targets)} 件已清理。")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
