"""端到端验证：处理一件，确认重量不再上涨。"""
import os
import warnings

import httpx
import psycopg

warnings.filterwarnings("ignore")

TOKEN = os.environ.get("AOHUANG_TEST_TOKEN", "script-4bfc132a7d57feaf966f20b1643c280f")
BASE = "http://127.0.0.1:8813/api/v1"
DID = "3562895405"
SHOP = "19977183"

u = os.environ["AOHUANG_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")


def show(tag: str) -> None:
    with psycopg.connect(u) as conn:
        r = conn.execute(
            "select payload from products where detail_id=%s and deleted_at is null",
            (DID,)).fetchone()
        if not r:
            print(f"  {tag}: (库里没有)")
            return
        p = r[0]
        w = sorted({v.get("weight") for v in (p.get("skuMap") or {}).values()
                    if isinstance(v, dict) and v.get("weight") is not None}, key=str)
        print(f"  {tag}: 主重量={p.get('weight')}  SKU={w}")


show("处理前")

body = {
    "shopId": SHOP, "targetShopId": SHOP, "accountId": "12",
    "language": "ms", "template": None, "save": True,
    "processImages": False, "mainImageCount": 9, "skuImageCount": 9,
    "detailImageCount": 9, "allowImageReview": True,
    "itemConcurrency": 1, "commonCollectBoxDetailId": None,
}
with httpx.Client(headers={"Authorization": f"Bearer {TOKEN}"}, timeout=900) as c:
    r = c.post(f"{BASE}/collect-box/{DID}/process", json=body)
print(f"\n处理 status={r.status_code}")
if r.status_code >= 400:
    print("  ", r.text[:300])

show("第 1 次后")
with httpx.Client(headers={"Authorization": f"Bearer {TOKEN}"}, timeout=900) as c:
    r2 = c.post(f"{BASE}/collect-box/{DID}/process", json=body)
print(f"\n重跑 status={r2.status_code}")
show("第 2 次后")