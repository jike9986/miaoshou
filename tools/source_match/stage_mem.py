"""逐阶段测量单件商品处理的内存，找出 1.4GB 从哪来。"""
import gc
import warnings

warnings.filterwarnings("ignore")


def rss() -> float:
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def mark(t):
    gc.collect()
    print(f"  {t:<40} {rss():>8.0f} MB")


mark("基线")

from db.session import SessionLocal
from models.account import Account
from services import account_service, product_service, warehouse_service as ws

db = SessionLocal()
acc = db.query(Account).filter(Account.id == 12).one()
client = account_service.account_client(acc)

mark("导入完成")

d = client.shop_detail("3562895768", "19977183")
info = ((d.get("data") or {}).get("shopCollectItemInfo") or {})
mark("拉详情")

from services import image_store

payload = dict(info)
rep = image_store.persist_payload_images(payload)
print(f"  固化报告: {rep}")
mark("图片固化")

from services import image_review_service

plan = image_review_service.analyze_images(payload) if hasattr(
    image_review_service, "analyze_images") else None
mark("图片 OCR 分析")

client.close()
db.close()
mark("结束")