"""重跑之前因 GIF 失败的商品，确认现在能过。"""
import os
import warnings

import httpx

warnings.filterwarnings("ignore")

TOKEN = os.environ.get("AOHUANG_TEST_TOKEN", "script-4bfc132a7d57feaf966f20b1643c280f")
BASE = "http://127.0.0.1:8813/api/v1"

TARGETS = [("3562895506", "19977183"), ("3562895236", "19977183")]

for did, shop in TARGETS:
    body = {
        "shopId": shop, "targetShopId": shop, "accountId": "12",
        "language": "ms", "template": None, "save": True,
        "processImages": True, "mainImageCount": 9, "skuImageCount": 9,
        "detailImageCount": 9, "allowImageReview": True,
        "itemConcurrency": 1, "commonCollectBoxDetailId": None,
    }
    with httpx.Client(headers={"Authorization": f"Bearer {TOKEN}"}, timeout=1800) as c:
        r = c.post(f"{BASE}/collect-box/{did}/process", json=body)
    print(f"{did}  status={r.status_code}")
    if r.status_code >= 400:
        print("   ", r.text[:260])