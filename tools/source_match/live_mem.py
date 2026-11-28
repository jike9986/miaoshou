"""真实 HTTP 处理一件，持续采样进程 RSS，画出内存曲线。"""
import os
import threading
import time
import warnings

import httpx

warnings.filterwarnings("ignore")

PID_FILE = "/tmp/svc.pid"


def svc_rss() -> float:
    try:
        with open(PID_FILE) as fh:
            pid = fh.read().strip()
        with open(f"/proc/{pid}/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except Exception:  # noqa: BLE001
        pass
    return 0.0


TOKEN = os.environ.get("AOHUANG_TEST_TOKEN", "script-4bfc132a7d57feaf966f20b1643c280f")
DID = "3562895405"
BASE = "http://127.0.0.1:8813/api/v1"

peak = [0.0]
stop = [False]


def sampler():
    while not stop[0]:
        peak[0] = max(peak[0], svc_rss())
        time.sleep(0.3)


print(f"起跑 RSS = {svc_rss():.0f} MB")
t = threading.Thread(target=sampler, daemon=True)
t.start()

body = {
    "shopId": "19977183", "targetShopId": "19977183", "accountId": "12",
    "language": "ms", "template": None, "save": True,
    "processImages": True, "mainImageCount": 9, "skuImageCount": 9,
    "detailImageCount": 9, "allowImageReview": True,
    "itemConcurrency": 1, "commonCollectBoxDetailId": None,
}

t0 = time.time()
with httpx.Client(headers={"Authorization": f"Bearer {TOKEN}"}, timeout=900) as c:
    r = c.post(f"{BASE}/collect-box/{DID}/process", json=body)
elapsed = time.time() - t0
stop[0] = True

print(f"status={r.status_code}  耗时 {elapsed:.0f}s")
if r.status_code < 400:
    d = r.json()
    print(f"  draftVerified={d.get('draftVerified')}")
    print(f"  imageApplied={d.get('imageApplied')}")
else:
    print(f"  错误: {r.text[:300]}")

print(f"\n峰值 RSS = {peak[0]:.0f} MB")
print(f"结束 RSS = {svc_rss():.0f} MB")