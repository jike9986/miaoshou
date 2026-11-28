"""查清妙手详情里 skuMap 的重量结构，决定复位公式。"""
import warnings

warnings.filterwarnings("ignore")

from db.session import SessionLocal
from models import Account
from services import account_service, product_service

db = SessionLocal()
acc = db.query(Account).filter(Account.id == 12).one()
cl = account_service.account_client(acc)

for DID in ("3562895405", "3533608868"):
    print("=" * 60)
    print("detailId", DID)
    info = product_service.detail_block(cl.shop_detail(DID, "19977183"))["info"]
    print("  主 weight =", info.get("weight"))
    skus = info.get("skuMap") or {}
    print("  skuMap 条目数 =", len(skus))
    weights = []
    for key, sku in list(skus.items())[:5]:
        print("    key=%s  weight=%r  price=%r  isDelete=%r" % (
            key, sku.get("weight"), sku.get("price"), sku.get("isDelete")))
    for sku in skus.values():
        if isinstance(sku, dict):
            weights.append(sku.get("weight"))
    print("  全部 SKU weight 取值 =", sorted({w for w in weights}, key=str))
    print()

cl.close()
db.close()