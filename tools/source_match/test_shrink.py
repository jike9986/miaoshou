import warnings

warnings.filterwarnings("ignore")

from db.session import SessionLocal
from models import Product
from services import image_service

db = SessionLocal()
row = db.query(Product).filter(Product.detail_id == "3562895506").first()
urls = list(row.payload.get("imgUrls") or [])

for u in urls:
    raw = image_service._fetch_source(u)
    if not raw:
        continue
    before = image_service._image_type(raw)
    shrunk = image_service._shrink(raw)
    after = image_service._image_type(shrunk)
    flag = "" if after else "   <<< 缩图后认不出格式 <<<"
    print("%7.0f KB  %-14s -> %7.0f KB  %-14s%s" % (
        len(raw) / 1024, str(before), len(shrunk) / 1024, str(after), flag))
    if after is None:
        print("     前 32 字节:", shrunk[:32].hex())
        # 看 Pillow 怎么理解
        import io
        from PIL import Image
        try:
            with Image.open(io.BytesIO(shrunk)) as im:
                print("     Pillow 读出:", im.format, im.mode, im.size)
        except Exception as ex:  # noqa: BLE001
            print("     Pillow 打不开:", type(ex).__name__, ex)
db.close()
