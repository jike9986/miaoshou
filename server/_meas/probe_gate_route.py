"""两个新路由的端到端冒烟：用仓库里已有的商品行，不打上游、不写库。

运行：python _meas/probe_gate_route.py（从 server 目录）
结果写 _meas/probe_gate_route.json。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.getcwd())
os.environ["AOHUANG_DATA_DIR"] = tempfile.mkdtemp(prefix="probe_gate_route_")

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from core.config import get_settings  # noqa: E402
from core.utils import now_ts  # noqa: E402
from db.session import Base, SessionLocal, engine  # noqa: E402
from models.product import Product  # noqa: E402
from models.violation_word import ViolationWord  # noqa: E402
from services import image_review_service  # noqa: E402

Base.metadata.create_all(engine)
db = SessionLocal()
db.add(ViolationWord(word="限量版", normalized="限量版", action="review",
                     source="probe", enabled=True, created_at=now_ts()))
db.add(Product(
    warehouse_key="900001:800001", detail_id="900001", shop_id="800001",
    title="Baju Sukan 限量版", status="ready", saved_at=now_ts(), updated_at=now_ts(),
    images=["https://cbu01.alicdn.com/a.jpg", "https://cbu01.alicdn.com/b.jpg"],
    payload={"title": "Baju Sukan 限量版",
             "imgUrls": ["https://cbu01.alicdn.com/a.jpg",
                         "https://cbu01.alicdn.com/b.jpg"]},
))
db.commit()
wid = db.query(Product).one().id
db.close()

_original = image_review_service.analyze
image_review_service.analyze = lambda images, **kw: [
    {"index": i, "url": u, "action": "keep", "reason": "", "hasChinese": False,
     "chineseText": "", "text": "", "error": False}
    for i, u in enumerate(images, 1)]

client = TestClient(main.app)
headers = {"Authorization": f"Bearer {get_settings().local_secret()}"}
out = {}
try:
    out["preSaveGate"] = client.post(
        "/api/v1/warehouse/pre-save-gate", headers=headers,
        json={"warehouseId": str(wid)}).json()
    out["preSaveGateAllowed"] = client.post(
        "/api/v1/warehouse/pre-save-gate", headers=headers,
        json={"warehouseId": str(wid), "allowTitleReview": True,
              "allowImageReview": True}).json()
    out["reviewGate"] = client.post(
        "/api/v1/images/review-gate", headers=headers,
        json={"detailId": "900001", "shopId": "800001"}).json()
finally:
    image_review_service.analyze = _original

with open(os.path.join("_meas", "probe_gate_route.json"), "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False, indent=2)

ok = (out["preSaveGate"].get("ok") is False
      and len(out["preSaveGate"].get("blocks") or []) == 1
      and out["preSaveGate"]["identity"]["source"] == "warehouse"
      and out["preSaveGateAllowed"].get("ok") is True
      and out["reviewGate"].get("blocked") is False
      and out["reviewGate"].get("imageCount") == 2)
print("ROUTE OK" if ok else "ROUTE FAIL")
sys.exit(0 if ok else 1)
