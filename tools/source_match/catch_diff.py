"""抓 skuImages 不一致的**具体差异**。

对失败商品，对比三方：
  original  草稿原值（本次处理前）
  payload   我们提交的（含图片固化后的 URL）
  actual    妙手读回

打印每个颜色槽位的 (值, URL 尾部)，并计算 _sku_image_slots_match 的结论。
"""
import warnings

warnings.filterwarnings("ignore")

from db.session import SessionLocal
from models.account import Account
from services import account_service, readback_check, warehouse_service as ws

SHOP = "19977183"
TARGETS = ["3563328188", "3563328533"]


def slot_table(info, tag):
    print(f"  --- {tag} ---")
    props = info.get("skuPropertyList") if isinstance(info, dict) else None
    if not isinstance(props, list):
        print("      (无 skuPropertyList)")
        return
    for prop in props:
        if not isinstance(prop, dict):
            continue
        nm = str(prop.get("attrName") or "")
        is_col = ws._is_color_property(prop)
        print(f"      [{nm}]{'  <=颜色' if is_col else ''}")
        for v in (prop.get("attrValueList") or []):
            if not isinstance(v, dict):
                continue
            val = str(v.get("attrValue") or "")
            url = str(v.get("imgUrl") or v.get("imageUrl") or v.get("image") or "")
            tail = url[-46:] if url else "(无图)"
            print(f"          {val:<24} {tail}")


db = SessionLocal()
try:
    acc = db.query(Account).filter(Account.id == 12).one()
    for did in TARGETS:
        client = account_service.account_client(acc)
        d = client.shop_detail(did, SHOP)
        client.close()
        info = ((d.get("data") or {}).get("shopCollectItemInfo") or {})

        print("=" * 78)
        print(did)
        print("=" * 78)
        slot_table(info, "妙手当前草稿（= original，也是 actual）")

        before = readback_check.sku_image_refs(info)
        print(f"  sku_image_refs: {len(before)} 个带图槽位")
        print(f"  _colors_need_chinese_cleaning: {ws._colors_need_chinese_cleaning(info)}")
        print()
finally:
    db.close()