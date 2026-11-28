"""验证：子进程 OCR 跑完后主进程 RSS 不增长。

对比三种做法：
  A. 进程内串行 OCR（旧）
  B. 子进程 OCR（新）
"""
import gc
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path

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


print(f"基线 RSS = {rss():.0f} MB")

# 找几张真实图片
from db.session import SessionLocal
from models.account import Account
from services import account_service, image_review_service, image_store

db = SessionLocal()
acc = db.query(Account).filter(Account.id == 12).one()
client = account_service.account_client(acc)
d = client.shop_detail("3562895405", "19977183")
info = ((d.get("data") or {}).get("shopCollectItemInfo") or {})
client.close()

urls = [u for u in (info.get("imgUrls") or []) if u][:3]
tmpdir = Path(tempfile.mkdtemp())
paths = []
for u in urls:
    try:
        raw = image_store._download(u)
        p = tmpdir / f"t{len(paths)}.jpg"
        p.write_bytes(raw)
        paths.append(p)
        del raw
    except Exception:  # noqa: BLE001
        pass
print(f"准备 {len(paths)} 张图")
gc.collect()

# --- A: 进程内 OCR ---
print("\n=== A. 进程内串行 OCR（旧做法）===")
a0 = rss()
for p in paths:
    try:
        image_review_service._run_ocr_rapidocr(p)
    except Exception as ex:  # noqa: BLE001
        print(f"  失败 {type(ex).__name__}")
gc.collect()
a1 = rss()
print(f"  前 {a0:.0f} MB → 后 {a1:.0f} MB  增长 {a1-a0:.0f} MB")

# --- B: 子进程 OCR ---
print("\n=== B. 子进程 OCR（新做法）===")
from services import ocr_runner

b0 = rss()
for _ in range(3):  # 连跑 3 批，看是否累积
    res = ocr_runner.run_ocr_batch(paths, timeout=300)
    gc.collect()
    print(f"  一批完成，结果 {len(res)} 条，RSS = {rss():.0f} MB")
b1 = rss()
print(f"  前 {b0:.0f} MB → 后 {b1:.0f} MB  增长 {b1-b0:.0f} MB")

print("\n" + "=" * 60)
print(f"A（进程内）增长: {a1-a0:.0f} MB")
print(f"B（子进程）增长: {b1-b0:.0f} MB")
db.close()