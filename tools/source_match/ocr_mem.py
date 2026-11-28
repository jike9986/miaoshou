"""OCR 处理多张图的内存增长，确认是否是 OCR 造成 1.7GB。"""
import gc
import warnings

warnings.filterwarnings("ignore")


def rss_mb() -> float:
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except Exception:  # noqa: BLE001
        pass
    return 0.0


print(f"基线 = {rss_mb():.0f} MB")

from db.session import SessionLocal
from models.account import Account
from services import account_service, image_store, image_review_service

db = SessionLocal()
acc = db.query(Account).filter(Account.id == 12).one()
client = account_service.account_client(acc)
d = client.shop_detail("3562895768", "19977183")
info = ((d.get("data") or {}).get("shopCollectItemInfo") or {})
client.close()

urls = list(info.get("imgUrls") or [])[:9]
print(f"准备 OCR {len(urls)} 张图")

import tempfile
from pathlib import Path

paths = []
for u in urls:
    try:
        raw = image_store._download(u)
        p = Path(tempfile.gettempdir()) / f"ocr-{abs(hash(u))}.jpg"
        p.write_bytes(raw)
        paths.append(p)
        del raw
    except Exception:  # noqa: BLE001
        pass

gc.collect()
print(f"下载完 {len(paths)} 张，RSS = {rss_mb():.0f} MB")

print("\n=== 逐张 OCR，观察 RSS 增长 ===")
for i, p in enumerate(paths, 1):
    try:
        image_review_service._run_ocr_rapidocr(p)
    except Exception as ex:  # noqa: BLE001
        print(f"  第 {i} 张失败: {type(ex).__name__}")
    gc.collect()
    print(f"  第 {i} 张后 RSS = {rss_mb():>8.0f} MB")

print(f"\n最终 RSS = {rss_mb():.0f} MB")
db.close()