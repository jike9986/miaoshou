"""看那两件 SKU 重量本来就有差异的商品，确认复位不会误伤。"""
import warnings

warnings.filterwarnings("ignore")

from db.session import SessionLocal
from models import Product

db = SessionLocal()
for DID in ("3533606391", "3533601800", "3533603400", "3533481775"):
    row = db.query(Product).filter(Product.detail_id == DID).first()
    if not row:
        continue
    p = row.payload or {}
    print("=" * 70)
    print(DID, " 主重量 =", p.get("weight"))
    skus = p.get("skuMap") or {}
    from collections import Counter
    c = Counter()
    for v in skus.values():
        if isinstance(v, dict):
            c[v.get("weight")] += 1
    print("  SKU 重量分布：", dict(c))
    print("  样本 key:", list(skus)[:2])
db.close()