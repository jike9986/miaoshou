"""复现一次完整处理，逐步打印颜色状态，定位 skuImages 不一致发生在哪一步。

不写回妙手（dry-run）：只在本地跑流程前半段，对比
  草稿原值 → 清洗后 → 图片固化后 → 准备提交的 payload
每一步的「颜色值 + 带图槽位数」。
"""
import warnings

warnings.filterwarnings("ignore")

from db.session import SessionLocal
from models.account import Account
from services import account_service, warehouse_service as ws

SHOP = "19977183"
TARGETS = ["3563328188", "3563328533"]


def slots(info):
    out = []
    for prop in (info.get("skuPropertyList") or []):
        if not isinstance(prop, dict) or not ws._is_color_property(prop):
            continue
        for v in (prop.get("attrValueList") or []):
            if not isinstance(v, dict):
                continue
            url = str(v.get("imgUrl") or v.get("imageUrl") or v.get("image") or "")
            out.append((str(v.get("attrValue") or ""), url))
    return out


def show(tag, info):
    s = slots(info)
    withimg = sum(1 for _, u in s if u)
    print(f"  {tag}: {len(s)} 值 / {withimg} 带图")
    for val, u in s:
        print(f"      {val:<26} {'有图' if u else '(无图)'}")


db = SessionLocal()
try:
    acc = db.query(Account).filter(Account.id == 12).one()
    for did in TARGETS:
        print("=" * 76)
        print(did)
        print("=" * 76)
        client = account_service.account_client(acc)
        d = client.shop_detail(did, SHOP)
        info = ((d.get("data") or {}).get("shopCollectItemInfo") or {})

        show("草稿原值（核验基准 original）", info)

        payload = dict(info)
        need = ws._colors_need_chinese_cleaning(payload)
        print(f"  _colors_need_chinese_cleaning = {need}")

        if need:
            try:
                ws._clean_color_props(payload)
                show("清洗后（payload）", payload)
            except Exception as ex:  # noqa: BLE001
                print(f"  清洗抛错: {ex}")
        else:
            print("  → 跳过清洗")

        client.close()
        print()
finally:
    db.close()