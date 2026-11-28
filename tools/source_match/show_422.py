import os
import warnings

import httpx

warnings.filterwarnings("ignore")

TOKEN = os.environ.get("AOHUANG_TEST_TOKEN", "script-4bfc132a7d57feaf966f20b1643c280f")
BASE = "http://127.0.0.1:8813/api/v1"
DID = "3562895405"

body = {
    "shopId": "19977183", "targetShopId": "19977183", "accountId": "12",
    "language": "ms", "template": None, "save": True,
    "processImages": True, "mainImageCount": 9, "skuImageCount": 9,
    "detailImageCount": 9, "allowImageReview": True,
    "itemConcurrency": 1, "commonCollectBoxDetailId": None,
}

with httpx.Client(headers={"Authorization": f"Bearer {TOKEN}"}, timeout=900) as c:
    r = c.post(f"{BASE}/collect-box/{DID}/process", json=body)

print("status:", r.status_code)
print("body:", r.text[:800])
