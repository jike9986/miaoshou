"""接线验证：save_from_collect_box 是否真的把译文图写回 payload 并挂 imageApplied。

用假 MiaoshouClient，不联网；AOHUANG_DATA_DIR 指临时目录，不碰真实 app.db。
结果写 _meas/probe_img_wire.json。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent
sys.path.insert(0, str(SERVER))

DATA_DIR = Path(tempfile.mkdtemp(prefix="aohuang-probe-wire-"))
os.environ["AOHUANG_DATA_DIR"] = str(DATA_DIR)
os.environ.pop("TK_AUTOMATION_DATA_DIR", None)
os.environ.pop("IMAGE_UPLOAD_URL", None)

import models  # noqa: E402,F401
from core.config import get_settings  # noqa: E402
from db.session import Base, SessionLocal, engine  # noqa: E402
from models.account import Account  # noqa: E402
from models.image_job import ImageJob  # noqa: E402

from services import pre_save_check, warehouse_service  # noqa: E402

Base.metadata.create_all(engine)
settings = get_settings()
settings.ensure_dirs()

MAIN_A = "https://cbu01.alicdn.com/img/main-a.jpg"
MAIN_B = "https://cbu01.alicdn.com/img/main-b.jpg"
DET_1 = "https://cbu01.alicdn.com/img/detail-1.jpg"
SKU_1 = "https://cbu01.alicdn.com/img/sku-1.jpg"
NEW = "https://aohuang888.com/tk-product-automation/translation-temp/"

CHECKS: list[dict] = []


def check(name: str, passed: bool, detail: object = "") -> None:
    CHECKS.append({"name": name, "result": "PASS" if passed else "FAIL", "detail": detail})


INFO = {
    "detailId": "9001", "shopId": "77", "site": "MY", "cid": "100001",
    "title": "Baju Kurung Moden", "description": "Baju Kurung Moden",
    "notes": pre_save_check.detail_images_only([DET_1]),
    "imgUrls": [MAIN_A, MAIN_B],
    "weight": "0.75",
    "packageLength": 30, "packageWidth": 20, "packageHeight": 10,
    "skuPropertyList": [
        {"attrName": "Warna", "attrValueList": [
            {"attrValueId": "v-1", "attrValue": "Hitam", "imgUrl": SKU_1}]},
    ],
    "skuMap": {
        "v-1": {"skuId": "s-1", "price": "19.9", "originPrice": "19.9",
                "stock": "10", "weight": "0.75", "isDelete": "0"},
    },
    "productAttributes": [],
}


class FakeClient:
    def shop_detail(self, detail_id, shop_id):
        return {"data": {"ossMd5": "md5", "editModel": "shop",
                         "shopCollectItemInfo": dict(INFO)}}

    def translate_collect_item(self, info, target_language="ms",
                              translate_sku_property=True):
        return {"data": {"collectItemInfo": dict(INFO)}}

    def close(self):
        pass


db = SessionLocal()
account = Account(name="probe", site="MY")
db.add(account)
db.flush()

client = FakeClient()
first = warehouse_service.save_from_collect_box(
    db, account, client, detail_id="9001", shop_id="77", language="ms",
    enforce_pre_save=False)
check("首次入仓带 imageApplied 字段", "imageApplied" in first, sorted(first)[:6])
check("首次入仓（没有任务）replaced=0",
      first.get("imageApplied", {}).get("replaced") == 0, first.get("imageApplied"))

product_id = int(first["id"])
name = "job-wire-a.png"
(settings.image_results_dir / name).write_bytes(b"\x89PNG\r\n\x1a\nprobe")
db.add(ImageJob(id="job-wire-a", batch_id="wire", product_id=product_id,
                source="main", source_url=MAIN_A, status="ready",
                filename=name, result_url="/api/v1/images/jobs/job-wire-a/file"))
db.flush()

original_upload = warehouse_service._public_result_url


def fake_public(job, uploader=None):
    return NEW + "job-wire-a.png"


warehouse_service._public_result_url = fake_public
try:
    second = warehouse_service.save_from_collect_box(
        db, account, client, detail_id="9001", shop_id="77", language="ms",
        enforce_pre_save=False)
finally:
    warehouse_service._public_result_url = original_upload

applied = second.get("imageApplied") or {}
check("第二次入仓 imageApplied.main=1", applied.get("main") == 1, applied)
check("第二次入仓 imageApplied.replaced=1", applied.get("replaced") == 1, applied)
db.flush()
row = warehouse_service.resolve_product(db, str(product_id))
payload = row.payload or {}
check("payload.imgUrls 第一张换成译文图、第二张原样",
      payload.get("imgUrls") == [NEW + "job-wire-a.png", MAIN_B], payload.get("imgUrls"))
check("展示用 images 字段同步（列表页不会还显示原图）",
      second.get("images") == [NEW + "job-wire-a.png", MAIN_B], second.get("images"))
check("封面同步", second.get("image") == NEW + "job-wire-a.png", second.get("image"))
check("SKU 图没任务命中时保持原样",
      payload["skuPropertyList"][0]["attrValueList"][0]["imgUrl"] == SKU_1,
      payload["skuPropertyList"][0]["attrValueList"][0]["imgUrl"])
check("详情图没任务命中时保持原样",
      pre_save_check.extract_detail_image_urls(payload.get("notes")) == [DET_1],
      payload.get("notes"))

# 写回后重新查库：JSON 列必须真的落盘（不是只在内存里改了）
check("数据库里的 payload 已经是译文图地址",
      (row.payload or {}).get("imgUrls") == [NEW + "job-wire-a.png", MAIN_B],
      (row.payload or {}).get("imgUrls"))
check("数据库里的展示字段 images 也已刷新",
      row.images == [NEW + "job-wire-a.png", MAIN_B], row.images)

db.rollback()
db.close()
shutil.rmtree(DATA_DIR, ignore_errors=True)

passed = sum(1 for c in CHECKS if c["result"] == "PASS")
out = {"passed": passed, "failed": len(CHECKS) - passed, "total": len(CHECKS),
       "checks": CHECKS}
target = HERE / "probe_img_wire.json"
target.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"PASS {passed}/{len(CHECKS)} -> {target}")
sys.exit(0 if passed == len(CHECKS) else 1)
