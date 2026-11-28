"""确认：读回的 actual 里 SKU 图到底是空的，还是名字对不上导致误判。"""
import warnings

warnings.filterwarnings("ignore")

from db.session import SessionLocal
from models.account import Account
from services import account_service, product_service, readback_check

TARGETS = ["3563339780", "3563339725"]
SHOP = "19977183"

db = SessionLocal()
try:
    acc = db.query(Account).filter(Account.id == 12).one()
    for did in TARGETS:
        client = account_service.account_client(acc)
        d = client.shop_detail(did, SHOP)
        client.close()
        actual = product_service.detail_block(d)["info"]

        print("=" * 74)
        print(f"{did}  妙手当前草稿（读回 actual）")
        print("=" * 74)
        for prop in (actual.get("skuPropertyList") or []):
            nm = str(prop.get("attrName") or prop.get("propName") or "")
            print(f"  [{nm}]")
            for v in (prop.get("attrValueList") or []):
                lab = str(v.get("attrValue") or "")
                url = str(v.get("imgUrl") or v.get("imageUrl") or v.get("image") or "")
                print(f"      {lab:<28} {'有图 ' + url[-40:] if url else '(无图)'}")

        refs = readback_check.sku_image_refs(actual)
        print(f"  → sku_image_refs 数量: {len(refs)}")
        withimg = sum(1 for v in ((p.get("attrValueList") or [])
                                  for p in (actual.get("skuPropertyList") or [])
                                  if isinstance(p, dict))
                      for x in v if isinstance(x, dict)
                      and (x.get("imgUrl") or x.get("imageUrl") or x.get("image")))
        print(f"  → 带图规格值: {withimg}")
        print()
finally:
    db.close()