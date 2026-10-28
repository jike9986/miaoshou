"""图片翻译结果写回 payload 的验证脚本（临时，不碰真实 app.db）。

跑法：
    pwsh> $env:AOHUANG_DATA_DIR = <temp>; python _meas/probe_img_apply.py
脚本自己也会在导入 db 之前把 AOHUANG_DATA_DIR 指到临时目录，双保险。

结果写 _meas/probe_img_apply.json（中文在 PowerShell 控制台会乱码）。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from copy import deepcopy
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent
sys.path.insert(0, str(SERVER))

DATA_DIR = Path(tempfile.mkdtemp(prefix="aohuang-probe-img-"))
os.environ["AOHUANG_DATA_DIR"] = str(DATA_DIR)
os.environ.pop("TK_AUTOMATION_DATA_DIR", None)
# 图床地址不给：本脚本一律注入假 uploader，绝不真的上传
os.environ.pop("IMAGE_UPLOAD_URL", None)

from core.config import get_settings  # noqa: E402
from db.session import Base, SessionLocal, engine  # noqa: E402
from models.image_job import ImageJob  # noqa: E402
from models.product import Product  # noqa: E402

import models  # noqa: E402,F401  - 注册所有表
from services import pre_save_check, warehouse_service  # noqa: E402

Base.metadata.create_all(engine)
settings = get_settings()
settings.ensure_dirs()

checks: list[dict] = []


def check(name: str, passed: bool, detail: object = "") -> None:
    checks.append({"name": name, "result": "PASS" if passed else "FAIL",
                   "detail": detail})


MAIN_A = "https://cbu01.alicdn.com/img/main-a.jpg"
MAIN_B = "https://cbu01.alicdn.com/img/main-b.jpg"
MAIN_C = "https://cbu01.alicdn.com/img/main-c.jpg"
DET_1 = "https://cbu01.alicdn.com/img/detail-1.jpg"
DET_2 = "https://cbu01.alicdn.com/img/detail-2.jpg"
SKU_1 = "https://cbu01.alicdn.com/img/sku-1.jpg"
SKU_2 = "https://cbu01.alicdn.com/img/sku-2.jpg"
SUP_1 = "https://cbu01.alicdn.com/img/sup-1.jpg"
SUP_2 = "https://cbu01.alicdn.com/img/sup-2.jpg"
PENDING_MAIN = MAIN_C

NEW = "https://aohuang888.com/tk-product-automation/translation-temp/"


def fake_uploader(path: Path) -> str:
    return NEW + Path(path).name


def new_payload() -> dict:
    return {
        "title": "Baju Kurung Moden",
        "cid": "100001",
        "weight": 0.75,
        "imgUrls": [MAIN_A, MAIN_B, MAIN_C],
        "notes": pre_save_check.detail_images_only([DET_1, DET_2]),
        "skuPropertyList": [
            {
                "attrName": "Warna",
                "attrValueList": [
                    {"attrValueId": "v-1", "attrValue": "Hitam", "imgUrl": SKU_1,
                     "supplementarySkuImageUrls": [SUP_1, SUP_2]},
                    {"attrValueId": "v-2", "attrValue": "Putih", "imgUrl": ""},
                ],
            },
            {
                "attrName": "Saiz",
                "attrValueList": [
                    {"attrValueId": "v-3", "attrValue": "M", "imgUrl": SKU_2},
                ],
            },
        ],
        "packageLength": 30, "packageWidth": 20, "packageHeight": 10,
    }


db = SessionLocal()
product = Product(warehouse_key="probe:1", detail_id="probe", shop_id="1",
                  account_id=None, title="probe", payload={})
db.add(product)
db.flush()
pid = product.id


def add_job(key: str, source_url: str, *, status: str = "ready",
            filename: str = "", result_url: str = "") -> None:
    row = ImageJob(id=f"job-{key}", batch_id="probe", product_id=pid,
                   source="main", source_url=source_url, status=status,
                   filename=filename, result_url=result_url)
    db.add(row)
    db.flush()


def write_result(key: str) -> str:
    name = f"job-{key}.png"
    (settings.image_results_dir / name).write_bytes(b"\x89PNG\r\n\x1a\nprobe")
    return name


# --- 1. 有结果的任务：2 张主图 + 1 张详情图 + 1 个 SKU 图 --------------------
for key, url in (("a", MAIN_A), ("b", MAIN_B), ("d2", DET_2), ("s1", SKU_1)):
    add_job(key, url, filename=write_result(key),
            result_url=f"/api/v1/images/jobs/job-{key}/file")
# 有待翻译任务但没结果：主图 C 还在排队
add_job("pending", PENDING_MAIN, status="submitted")

payload = new_payload()
before = deepcopy(payload)
report = warehouse_service.apply_image_results(db, product, payload, uploader=fake_uploader)

main_after = payload["imgUrls"]
detail_after = pre_save_check.extract_detail_image_urls(payload["notes"])
props_after = payload["skuPropertyList"]
color_values = props_after[0]["attrValueList"]

check("主图 3 张顺序与数量不变", main_after == [NEW + "job-a.png", NEW + "job-b.png", MAIN_C],
      main_after)
check("未命中的主图保持原样", main_after[2] == MAIN_C, main_after[2])
check("详情图只换命中的那张，顺序不变",
      detail_after == [DET_1, NEW + "job-d2.png"], detail_after)
check("笔记仍是「只含图片」的紧凑 HTML",
      payload["notes"] == pre_save_check.detail_images_only(detail_after), payload["notes"])
check("SKU imgUrl 被替换", color_values[0]["imgUrl"] == NEW + "job-s1.png",
      color_values[0]["imgUrl"])
check("SKU 补充图列表顺序不变、未命中的保持原样",
      color_values[0]["supplementarySkuImageUrls"] == [SUP_1, SUP_2],
      color_values[0]["supplementarySkuImageUrls"])
check("SKU 文本字段未被改动",
      [(v.get("attrValueId"), v.get("attrValue")) for v in color_values] ==
      [("v-1", "Hitam"), ("v-2", "Putih")],
      [(v.get("attrValueId"), v.get("attrValue")) for v in color_values])
check("SKU 属性名未被改动",
      [p.get("attrName") for p in props_after] == ["Warna", "Saiz"],
      [p.get("attrName") for p in props_after])
check("没有任务命中的 SKU 图保持原样",
      props_after[1]["attrValueList"][0]["imgUrl"] == SKU_2,
      props_after[1]["attrValueList"][0]["imgUrl"])
check("计数：main=2 detail=1 sku=1 replaced=4",
      (report["main"], report["detail"], report["sku"], report["replaced"]) == (2, 1, 1, 4),
      report)
check("missing 只列出「有任务没结果」的 URL",
      report["missing"] == [PENDING_MAIN], report["missing"])
check("未命中项之外的字段完全没动",
      payload["title"] == before["title"] and payload["weight"] == before["weight"]
      and payload["cid"] == before["cid"], payload["title"])

# --- 2. 空映射：没有任何任务 → replaced 0，payload 一字未改 ------------------
db.query(ImageJob).delete()
db.flush()
payload2 = new_payload()
before2 = deepcopy(payload2)
report2 = warehouse_service.apply_image_results(db, product, payload2, uploader=fake_uploader)
check("没有任何任务时 replaced=0", report2["replaced"] == 0, report2)
check("没有任何任务时 payload 一字未改", payload2 == before2, "unchanged")
check("没有任何任务时 missing 为空", report2["missing"] == [], report2["missing"])

# --- 3. supplementarySkuImageUrls 是单个字符串 ------------------------------
add_job("sup", SUP_2, filename=write_result("sup"),
        result_url="/api/v1/images/jobs/job-sup/file")
payload3 = new_payload()
payload3["skuPropertyList"][0]["attrValueList"][0]["supplementarySkuImageUrls"] = SUP_2
report3 = warehouse_service.apply_image_results(db, product, payload3, uploader=fake_uploader)
sup_after = payload3["skuPropertyList"][0]["attrValueList"][0]["supplementarySkuImageUrls"]
check("supplementary 单字符串形态被替换成新地址", sup_after == NEW + "job-sup.png", sup_after)
check("supplementary 单字符串形态仍是字符串", isinstance(sup_after, str), type(sup_after).__name__)
check("单字符串情形下 SKU 计数为 1", report3["sku"] == 1, report3)

# 枚举：两种形态都能被 sku_image_sources 找到
sources_str = warehouse_service.sku_image_sources(
    {"skuPropertyList": [{"attrName": "W", "attrValueList": [
        {"attrValueId": "1", "imgUrl": SKU_1, "supplementarySkuImageUrls": SUP_1}]}]})
sources_list = warehouse_service.sku_image_sources(
    {"skuPropertyList": [{"attrName": "W", "attrValueList": [
        {"attrValueId": "1", "imgUrl": SKU_1,
         "supplementarySkuImageUrls": [SUP_1, SUP_2]}]}]})
check("sku_image_sources 单字符串形态枚举 2 项",
      [s["kind"] for s in sources_str] == ["imgUrl", "supplementarySkuImageUrls"],
      [s["path"] for s in sources_str])
check("sku_image_sources 列表形态枚举 3 项",
      len(sources_list) == 3 and sources_list[2]["imageIndex"] == 1,
      [s["path"] for s in sources_list])
check("sku_image_sources 不收非 URL（空串被跳过）",
      all(s["url"].startswith("http") for s in sources_str), sources_str)

# --- 4. rewrite 不能就地改原对象 -------------------------------------------
original_props = new_payload()["skuPropertyList"]
original_snapshot = deepcopy(original_props)
rewritten = warehouse_service.rewrite_sku_image_urls(original_props, {SKU_1: NEW + "x.png"})
check("rewrite 返回新对象并命中替换",
      rewritten[0]["attrValueList"][0]["imgUrl"] == NEW + "x.png",
      rewritten[0]["attrValueList"][0]["imgUrl"])
check("rewrite 不就地改入参", original_props == original_snapshot, "unchanged")
check("rewrite 非列表入参原样返回",
      warehouse_service.rewrite_sku_image_urls(None, {}) is None, "None")

# --- 5. 上传失败 / 文件缺失 只降级 -----------------------------------------
db.query(ImageJob).delete()
db.flush()
add_job("gone", MAIN_A, filename="job-gone.png",
        result_url="/api/v1/images/jobs/job-gone/file")  # 文件没写盘
payload5 = new_payload()
before5 = deepcopy(payload5)
report5 = warehouse_service.apply_image_results(db, product, payload5, uploader=fake_uploader)
check("文件缺失时 replaced=0 且 payload 不变",
      report5["replaced"] == 0 and payload5 == before5, report5)
check("文件缺失的 URL 进 missing", report5["missing"] == [MAIN_A], report5["missing"])

# uploader 抛错也不能掀翻
add_job("boom", MAIN_B, filename=write_result("boom"),
        result_url="/api/v1/images/jobs/job-boom/file")


def boom_uploader(_path: Path) -> str:
    raise RuntimeError("upload down")


payload6 = new_payload()
report6 = warehouse_service.apply_image_results(db, product, payload6, uploader=boom_uploader)
check("上传抛错时 replaced=0、payload 不变、无异常外泄",
      report6["replaced"] == 0 and payload6 == new_payload(), report6)

# payload 不是 dict
report7 = warehouse_service.apply_image_results(db, product, "not-a-dict", uploader=fake_uploader)
check("payload 非 dict 时返回 replaced=0 + error",
      report7["replaced"] == 0 and bool(report7["error"]), report7)

# --- 6. result_url 已经是公网地址时直接复用，不再上传 ----------------------
db.query(ImageJob).delete()
db.flush()
add_job("remote", MAIN_A, result_url="https://cdn.example.com/done.png")


def never_called(_path: Path) -> str:
    raise AssertionError("已有公网 result_url 时不该再上传")


payload8 = new_payload()
report8 = warehouse_service.apply_image_results(db, product, payload8, uploader=never_called)
check("result_url 是公网地址时直接复用",
      payload8["imgUrls"][0] == "https://cdn.example.com/done.png", payload8["imgUrls"][0])
check("复用公网地址时计数正确", report8["main"] == 1 and report8["replaced"] == 1, report8)

db.rollback()
db.close()
shutil.rmtree(DATA_DIR, ignore_errors=True)

passed = sum(1 for c in checks if c["result"] == "PASS")
out = {"passed": passed, "failed": len(checks) - passed, "total": len(checks),
       "checks": checks}
target = HERE / "probe_img_apply.json"
target.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"PASS {passed}/{len(checks)} -> {target}")
sys.exit(0 if passed == len(checks) else 1)
