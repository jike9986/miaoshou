"""验证：同一件商品反复处理时重量是否滚雪球。"""
import warnings

warnings.filterwarnings("ignore")

from db.session import SessionLocal
from models import Account, Product
from services import account_service, product_service, warehouse_service

DID = "3562895405"
SHOP = "19977183"

db = SessionLocal()
acc = db.query(Account).filter(Account.id == 12).one()
row = db.query(Product).filter(Product.detail_id == DID).first()

print("=== 库里现状 ===")
if row:
    print("  payload weight      =", (row.payload or {}).get("weight"))
    print("  payload _sourceWeight =", (row.payload or {}).get("_sourceWeight"))
    print("  skuMap weights      =", {
        k: v.get("weight") for k, v in ((row.payload or {}).get("skuMap") or {}).items()
        if isinstance(v, dict) and v.get("weight") not in (None, "")
    })

print()
print("=== 上游真实重量 ===")
cl = account_service.account_client(acc)
info = product_service.detail_block(cl.shop_detail(DID, SHOP))["info"]
print("  上游 payload['weight'] =", info.get("weight"))

print()
print("=== 模拟 5 次重复处理 ===")
sim = dict(info)
for i in range(1, 6):
    rep = warehouse_service.prepare_weight(sim, 0.1, category_name="鞋",
                                           weight_limit=5.0)
    print(f"  第 {i} 次: before={rep.get('before')} after={sim['weight']}")
    sim["weight"] = sim["weight"]  # 妙手写回后 payload 就是 after

cl.close()
db.close()