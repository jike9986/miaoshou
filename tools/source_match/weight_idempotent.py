"""验证修复：同一件反复处理，重量不再滚雪球。"""
import warnings

warnings.filterwarnings("ignore")

from db.session import SessionLocal
from models import Account
from services import account_service, product_service, warehouse_service

DID = "3562895405"
SHOP = "19977183"

db = SessionLocal()
acc = db.query(Account).filter(Account.id == 12).one()
cl = account_service.account_client(acc)
info = product_service.detail_block(cl.shop_detail(DID, SHOP))["info"]

print("上游真实重量 =", info.get("weight"))
print()
print("=== 模拟 6 次重复处理（每次把结果当下一轮的妙手值）===")

sim = dict(info)
saved = None
for i in range(1, 7):
    rep = warehouse_service.prepare_weight(sim, 0.1, category_name="鞋",
                                           weight_limit=5.0,
                                           saved_source_weights=saved)
    saved = rep.get("sourceWeights")
    sku_w = {v.get("weight") for v in (sim.get("skuMap") or {}).values()
             if isinstance(v, dict) and v.get("weight") not in (None, "")}
    print(f"  第 {i} 次: 主重量={sim['weight']}  SKU重量={sorted(sku_w)}")

cl.close()
db.close()