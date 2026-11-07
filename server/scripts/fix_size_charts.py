"""用默认尺码图覆盖失效的尺码图引用。

背景：2026-10-27 那批 17 件运动鞋（cid 603751）的 sizeChart 指向
`earth-rt.chengji-inc.com`，而该图床已把那些图删了（404），
既显示破图，发布时也会因「缺尺码图」被上游拒。

做法：把 sizeChart 换成项目里配置的默认尺码图（settings 的 sizeChartUrl，
或 size_charts 表里的默认项），并在 payload 里记录替换原因，便于追溯。

默认只报告；--apply 才写库。
"""
from __future__ import annotations

import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

# 项目内置默认尺码图（services 里 size_chart_service 的兜底值）
FALLBACK_CHART = (
    "https://aohuang888.com/tk-product-automation/size-chart.png"
)


def _default_chart(session) -> str:
    """取当前生效的默认尺码图：设置项优先，其次尺码图库的默认项。"""
    from services import setting_service, size_chart_service

    try:
        configured = str(setting_service.workflow_defaults(session).get("sizeChartUrl") or "").strip()
        if configured.startswith("http"):
            return configured
    except Exception:  # noqa: BLE001 - 读设置失败就走下一级兜底
        pass
    try:
        listing = size_chart_service.list_charts(session)
        candidates = [
            str(listing.get("defaultUrl") or "").strip(),
            *(str(item.get("url") or "").strip()
              for item in (listing.get("items") or [])
              if isinstance(item, dict) and item.get("isDefault")),
        ]
        for value in candidates:
            if value.startswith("http"):
                return value
    except Exception:  # noqa: BLE001
        pass
    return FALLBACK_CHART


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="覆盖失效尺码图")
    parser.add_argument("--apply", action="store_true", help="真正写库")
    parser.add_argument("--ids", default="", help="只处理指定商品 id")
    args = parser.parse_args(argv)
    only = [int(x) for x in args.ids.split(",") if x.strip().isdigit()] or None

    from db.session import SessionLocal
    from models.product import Product

    session = SessionLocal()
    try:
        chart = _default_chart(session)
        print("使用尺码图：", chart)

        query = session.query(Product).filter(Product.deleted_at.is_(None))
        if only:
            query = query.filter(Product.id.in_(only))
        products = query.all()

        affected = []
        for product in products:
            payload = product.payload if isinstance(product.payload, dict) else {}
            current = str(payload.get("sizeChart") or "").strip()
            if not current or "aohuang888.com" in current:
                continue
            affected.append(product)
            print("p%s | %s | 旧=%s" % (
                product.id, str(product.title or "")[:40], current[:70]))
            if not args.apply:
                continue
            mutated = dict(payload)
            mutated["sizeChart"] = chart
            mutated["sizeChartType"] = "image"
            # 留痕：以后能查出来这张是替换过的，不是商品原图
            mutated["sizeChartReplacedFrom"] = current
            product.payload = mutated

        print("---")
        print("命中 %d 件" % len(affected))
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
