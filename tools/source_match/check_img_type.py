import warnings

warnings.filterwarnings("ignore")

from db.session import SessionLocal
from models import Product
from services import image_service

db = SessionLocal()
row = db.query(Product).filter(Product.detail_id == "3562895506").first()
urls = list(row.payload.get("imgUrls") or [])
for p in (row.payload.get("skuPropertyList") or []):
    for v in (p.get("attrValueList") or []):
        u = v.get("imgUrl")
        if u:
            urls.append(u)

print(f"共 {len(urls)} 个图 URL，逐个检查格式\n")
seen = set()
for u in urls:
    if u in seen:
        continue
    seen.add(u)
    try:
        raw = image_service._fetch_source(u)
        if not raw:
            print(f"  取不到: {u[-40:]}")
            continue
        kind = image_service._image_type(raw)
        print(f"  {len(raw)/1024:>7.0f} KB  type={kind}  {u[-45:]}")
    except Exception as ex:  # noqa: BLE001
        print(f"  失败 {type(ex).__name__}: {u[-40:]}")
db.close()
