"""验证价格校验：自动化处理套模板后不再报 price fields changed。

不写库。用真实商品数据 + 真实模板，走 process_price → pre_save_issues，
对比 automation_result 打开/关闭两种情况下 pricing_enabled 的判定。
"""
import warnings

warnings.filterwarnings("ignore")

from sqlalchemy import text

from db.session import SessionLocal
from models.account import Account
from services import (account_service, pre_save_check, product_service,
                      setting_service)

ACC_ID = 12
SHOP_ID = "19977183"


def rows_of(resp):
    d = resp.get("data") or {}
    return d.get("detailList") or d.get("items") or []


db = SessionLocal()
try:
    acc = db.query(Account).filter(Account.id == ACC_ID).one()
    client = account_service.account_client(acc)
    all_rows = []
    for page in (1, 2, 3):
        r = client.collect_box_list(status="notPublished", page_no=page,
                                    page_size=50)
        rs = rows_of(r)
        if not rs:
            break
        all_rows.extend(rs)

    # 取第一件鞋
    picked = None
    for it in all_rows:
        if "鞋" in str(it.get("title") or ""):
            picked = it
            break
    did = str(picked.get("collectBoxDetailId"))
    print(f"商品: {str(picked.get('title'))[:44]}")
    print(f"detail={did}\n")

    resp = client.shop_detail(did, SHOP_ID)
    info = (resp.get("data") or {}).get("shopCollectItemInfo") or {}

    tpls = setting_service.price_templates(db, operator_id=64)
    template_name = "马来"
    template = tpls.get(template_name)
    print(f"定价模板「{template_name}」: {'找到' if template else '没找到'}")
    assert template, f"模板 {template_name} 不存在"
    print()

    # ===== 场景 A：自动化处理（套模板）=====
    print("=" * 74)
    print("A. 自动化处理（automation_result=True，套模板）")
    print("=" * 74)
    payload_a, report = product_service.process_price(
        dict(info), template=template, exchange_rate=None)
    skus = payload_a.get("skuMap") or {}
    first = next((s for s in skus.values() if isinstance(s, dict)), {})
    print(f"  originPrice = {first.get('originPrice')}")
    print(f"  price       = {first.get('price')}  ← 模板算出的售价")

    issues_a = pre_save_check.pre_save_issues(
        info, payload_a, {},
        main_limit=9, detail_limit=9,
        package_dimensions=None,
        size_chart_url="",
        process_images_enabled=True,
        image_only=False,
        pricing_enabled=True,        # ← 套了模板就是 True
    )
    price_issues_a = [i for i in issues_a if "price" in i.lower()]
    print(f"  价格相关 issues: {price_issues_a or '无'}")
    assert not price_issues_a, f"自动化处理仍报价格错误: {price_issues_a}"
    print("  ✓ 不报 price fields changed")

    # ===== 场景 B：手动入库（不套模板，价格改回拿货价）=====
    print()
    print("=" * 74)
    print("B. 手动入库（automation_result=False，不套模板）")
    print("=" * 74)
    from services import warehouse_service

    payload_b = warehouse_service._price_warehouse_at_purchase(dict(info))
    skus_b = payload_b.get("skuMap") or {}
    first_b = next((s for s in skus_b.values() if isinstance(s, dict)), {})
    print(f"  originPrice = {first_b.get('originPrice')}")
    print(f"  price       = {first_b.get('price')}  ← 拿货价口径")

    issues_b = pre_save_check.pre_save_issues(
        info, payload_b, {},
        main_limit=9, detail_limit=9,
        package_dimensions=None,
        size_chart_url="",
        process_images_enabled=True,
        image_only=False,
        pricing_enabled=False,       # ← 不套模板就是 False
        warehouse_purchase_pricing=True,   # ← 手动入库改写成拿货价，放行
    )
    price_issues_b = [i for i in issues_b if "price" in i.lower()]
    print(f"  价格相关 issues: {price_issues_b or '无'}")
    assert not price_issues_b, f"手动入库仍报价格错误: {price_issues_b}"
    print("  ✓ 不报 price fields changed")

    print()
    print("=" * 74)
    print("结论")
    print("=" * 74)
    print("· 自动化处理：pricing_enabled=True，模板改价被认可 → 不报错")
    print("· 手动入库：pricing_enabled=False，理论上报 changed")
    print("  但手动入库的 price_template 也会传进来，此时 template=None")
    print("  却改了 price —— 这正是要检查的第二个点")
finally:
    db.close()