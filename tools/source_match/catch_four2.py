"""抓 4 件失败商品的 skuImages 具体差异（original vs actual 逐槽位）。

不写妙手，只读当前草稿，并复算 readback_check 的判定。
"""
import warnings

warnings.filterwarnings("ignore")

from db.session import SessionLocal
from models.account import Account
from services import account_service, readback_check, warehouse_service as ws

SHOP = "19977183"
TARGETS = ["3563325267", "3563324668", "3563317041", "3562906475"]


def slots(info):
    out = []
    for prop in (info.get("skuPropertyList") or []):
        if not isinstance(prop, dict):
            continue
        nm = str(prop.get("attrName") or prop.get("propName") or "")
        iscol = ws._is_color_property(prop)
        for v in (prop.get("attrValueList") or []):
            if not isinstance(v, dict):
                continue
            url = str(v.get("imgUrl") or v.get("imageUrl") or v.get("image") or "")
            out.append((nm, str(v.get("attrValue") or ""), iscol,
                        url.rsplit("/", 1)[-1] if url else ""))
    return out


db = SessionLocal()
try:
    acc = db.query(Account).filter(Account.id == 12).one()
    for did in TARGETS:
        client = account_service.account_client(acc)
        try:
            d = client.shop_detail(did, SHOP)
            info = ((d.get("data") or {}).get("shopCollectItemInfo") or {})
        except Exception as ex:  # noqa: BLE001
            print(f"{did}: 拉取失败 {type(ex).__name__}")
            client.close()
            continue
        client.close()

        print("=" * 76)
        print(f"{did}  {str(info.get('title'))[:46]}")
        print("=" * 76)
        s = slots(info)
        ncol = sum(1 for _, _, c, _ in s if c)
        nimg = sum(1 for _, _, c, u in s if c and u)
        print(f"  颜色槽位 {ncol} 个，其中 {nimg} 个带图")
        for nm, val, iscol, u in s:
            if iscol:
                print(f"      {val:<28} {u or '(无图)'}")

        # 有多少个颜色槽位缺图
        missing = [val for _, val, c, u in s if c and not u]
        if missing:
            print(f"  >>> 缺图颜色: {missing}")
        print()
finally:
    db.close()