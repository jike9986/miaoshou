"""定位 6.5GB 内存来自哪个环节。

逐段测量 RSS：
  1. 启动基线
  2. 加载 RapidOCR
  3. OCR 处理 1 张图
  4. 下载 20 张图
  5. 完整处理 1 件商品
"""
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


def mark(tag: str) -> None:
    gc.collect()
    print(f"  {tag:<34} RSS = {rss_mb():>8.0f} MB")


print("=== 内存分段测量 ===")
mark("启动基线")

from services import image_review_service

eng = image_review_service._rapidocr_engine() if hasattr(
    image_review_service, "_rapidocr_engine") else None
if eng is None:
    # 按实际函数名取
    for name in dir(image_review_service):
        if "rapidocr" in name.lower() and callable(
                getattr(image_review_service, name)):
            print(f"  (候选函数 {name})")
mark("加载 RapidOCR 之后")

# 找一张真实图片做 OCR
from db.session import SessionLocal
from models.account import Account
from services import account_service, product_service

db = SessionLocal()
acc = db.query(Account).filter(Account.id == 12).one()
client = account_service.account_client(acc)
d = client.shop_detail("3562895768", "19977183")
info = ((d.get("data") or {}).get("shopCollectItemInfo") or {})
client.close()

urls = list(info.get("imgUrls") or [])
print(f"\n  该商品主图 {len(urls)} 张")

from services import image_store

if urls:
    raw = image_store._download(urls[0])
    print(f"  下载 1 张: {len(raw)/1024/1024:.2f} MB")
    mark("下载 1 张图之后")
    del raw
    mark("释放之后")

# 串行下载 20 张，看峰值
mark("开始下载 20 张之前")
peak = rss_mb()
for i, u in enumerate(urls[:20]):
    try:
        r = image_store._download(u)
        cur = rss_mb()
        peak = max(peak, cur)
        del r
    except Exception:  # noqa: BLE001
        pass
mark("下载 20 张之后")
print(f"  峰值 RSS = {peak:.0f} MB")

db.close()