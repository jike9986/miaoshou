"""仓库图片改写（P0-2）本地验证：不联网、不部署、不碰真实 app.db。

重点验三件事：
  · 改的是本地 DB（payload 真的变了），并且派生字段 images / detail_images /
    cover / sku_properties 跟着重算——只改 payload 会出现「详情是新图、
    列表是旧图」的自相矛盾状态；
  · 上游一个字节都没动（save_shop / 妙手客户端一次都没被调用）；
  · 校验边界：confirm 缺失、超过 20 张、非 http(s)、notes 超长、只传一个字段
    时其余字段不动。

控制台输出只用 ASCII，完整结果落到 _meas/_out/probe_images_patch.json。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP_DIR = Path(tempfile.mkdtemp(prefix="imgpatch-probe-"))
os.environ["AOHUANG_DATA_DIR"] = str(_TMP_DIR)
os.environ["TK_AUTOMATION_DATA_DIR"] = str(_TMP_DIR)

from fastapi.testclient import TestClient  # noqa: E402

from api import API_PREFIX  # noqa: E402
from core import errors  # noqa: E402
from core.utils import product_key  # noqa: E402
from db.session import Base, SessionLocal, engine  # noqa: E402
from main import app  # noqa: E402
from models.account import Account  # noqa: E402
from models.product import Product  # noqa: E402
from services import crypto, product_service, warehouse_service  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append((label, bool(ok), "" if ok else detail))


BASE_IMAGES = [
    "https://cdn.example.com/a1.jpg",
    "https://cdn.example.com/a2.jpg",
    "https://cdn.example.com/a3.jpg",
]
BASE_NOTES = (
    "<p>desc</p>"
    "<img src='https://cdn.example.com/d1.jpg'>"
    "<img src='https://cdn.example.com/d2.jpg'>"
)
BASE_PROPS = [
    {"attrName": "颜色", "attrValueList": [
        {"attrValueId": "1", "attrValue": "黑色", "imgUrl": None},
        {"attrValueId": "2", "attrValue": "白色", "imgUrl": None},
    ]},
]


def make_payload() -> dict:
    return {
        "title": "probe product",
        "imgUrls": list(BASE_IMAGES),
        "notes": BASE_NOTES,
        "skuPropertyList": json.loads(json.dumps(BASE_PROPS)),
        "weight": 0.75,
    }


def seed_product(db, account, detail_id: str, shop_id: str) -> Product:
    """直接落一条仓库行：不走上游，payload 与派生字段按入库时的规则填。"""
    payload = make_payload()
    product = Product(
        warehouse_key=product_key(detail_id, shop_id),
        account_id=account.id,
        detail_id=detail_id,
        shop_id=shop_id,
        shop_name="probe-shop",
        title=payload["title"],
        language="ms",
        site="MY",
        cover=BASE_IMAGES[0],
        images=product_service.main_images(payload),
        detail_images=product_service.detail_images(payload),
        sku_properties=product_service.sku_properties(payload),
        payload=payload,
        status="ready",
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


def reload_product(db, product_id: int) -> Product:
    db.expire_all()
    return db.query(Product).filter(Product.id == product_id).one()


# --- 服务层：派生字段同步 --------------------------------------------------
def test_derived_sync(db, account) -> None:
    product = seed_product(db, account, "91001", "7701")
    payload = product.payload
    check("seed: images synced", product.images == BASE_IMAGES, str(product.images))
    check("seed: cover is first image", product.cover == BASE_IMAGES[0], product.cover)
    check("seed: detail_images from notes", product.detail_images
          == ["https://cdn.example.com/d1.jpg", "https://cdn.example.com/d2.jpg"],
          str(product.detail_images))
    check("seed: sku_properties from payload",
          [p["attrName"] for p in product.sku_properties] == ["颜色"],
          str(product.sku_properties))

    # 主图换成 3 张新的：payload / images / cover 必须一起变
    new_urls = ["https://cdn.example.com/n1.jpg",
                "https://cdn.example.com/n2.jpg",
                "https://cdn.example.com/n3.jpg"]
    from api.warehouse import _patch_image_urls

    check("validate: 3 urls accepted", _patch_image_urls(new_urls) == new_urls, "")
    check("validate: dedupe keeps order",
          _patch_image_urls([new_urls[0], new_urls[0], new_urls[1]])
          == [new_urls[0], new_urls[1]], "")

    for label, values in (
        ("validate: >20 rejected",
         [f"https://cdn.example.com/x{i}.jpg" for i in range(21)]),
        ("validate: non-http rejected", ["ftp://cdn.example.com/x.jpg"]),
        ("validate: relative rejected", ["/local/x.jpg"]),
        ("validate: empty url rejected", [""]),
    ):
        try:
            _patch_image_urls(values)
        except errors.AppError as exc:
            check(label, exc.status == 400, f"{exc.status}")
        else:
            check(label, False, "accepted")

    from api.warehouse import _patch_sku_props

    check("validate: props accepted", _patch_sku_props(list(BASE_PROPS)) == BASE_PROPS, "")
    for label, values in (
        ("validate: props non-object rejected", ["not-an-object"]),
        ("validate: props missing attrName rejected", [{"attrValueList": []}]),
        ("validate: props bad attrValueList rejected",
         [{"attrName": "颜色", "attrValueList": "nope"}]),
    ):
        try:
            _patch_sku_props(values)
        except errors.AppError as exc:
            check(label, exc.status == 400, f"{exc.status}")
        else:
            check(label, False, "accepted")


# --- 路由层 ---------------------------------------------------------------
def test_http() -> None:
    from core.config import get_settings

    token = get_settings().local_secret()
    headers = {"Authorization": f"Bearer {token}"}
    base = f"{API_PREFIX}/warehouse"

    # 上游守卫：任何一次妙手调用都算失败（改图片不该碰上游）
    upstream_calls: list[str] = []

    def forbid(*args, **kwargs):
        upstream_calls.append("called")
        raise AssertionError("image patch must not touch upstream")

    with TestClient(app) as client:
        db = SessionLocal()
        try:
            account = db.query(Account).first()
            if account is None:
                account = Account(name="probe", app_key="k",
                                  app_secret_enc=crypto.encrypt("s"),
                                  is_default=True, site="MY", status="active")
                db.add(account)
                db.commit()
                db.refresh(account)
            product = seed_product(db, account, "91002", "7702")
            product_id = product.id
            ident = str(product_id)
            key = product.warehouse_key
        finally:
            db.close()

        # 1) 无 confirm -> 400
        no_confirm = client.patch(f"{base}/{ident}/images", json={
            "imgUrls": ["https://cdn.example.com/c1.jpg"]}, headers=headers)
        check("http: no confirm -> 400", no_confirm.status_code == 400,
              str(no_confirm.status_code))
        check("http: no confirm message",
              no_confirm.json().get("error", {}).get("message") == "改图片需要 confirm: true",
              str(no_confirm.json())[:200])

        # 2) confirm=false 同样 400
        false_confirm = client.patch(f"{base}/{ident}/images", json={
            "imgUrls": ["https://cdn.example.com/c1.jpg"], "confirm": False},
            headers=headers)
        check("http: confirm false -> 400", false_confirm.status_code == 400,
              str(false_confirm.status_code))

        # 3) 改主图 3 张 -> 本地 DB 真变，images / cover 同步
        new_urls = ["https://cdn.example.com/c1.jpg",
                    "https://cdn.example.com/c2.jpg",
                    "https://cdn.example.com/c3.jpg"]
        patched = client.patch(f"{base}/{ident}/images", json={
            "imgUrls": new_urls, "confirm": True}, headers=headers)
        check("http: patch images 200", patched.status_code == 200,
              str(patched.status_code) + " " + str(patched.json())[:200])
        body = patched.json()
        check("http: changed lists imgUrls", body.get("changed") == ["imgUrls"],
              str(body.get("changed")))
        check("http: response has product", "product" in body, str(body.keys()))
        check("http: response images updated",
              body.get("product", {}).get("images") == new_urls,
              str(body.get("product", {}).get("images")))
        check("http: response cover updated",
              body.get("product", {}).get("image") == new_urls[0],
              str(body.get("product", {}).get("image")))

        db = SessionLocal()
        try:
            row = reload_product(db, product_id)
            check("db: payload imgUrls changed", row.payload.get("imgUrls") == new_urls,
                  str(row.payload.get("imgUrls")))
            check("db: images column changed", row.images == new_urls, str(row.images))
            check("db: cover column changed", row.cover == new_urls[0], str(row.cover))
            check("db: notes untouched", row.payload.get("notes") == BASE_NOTES, "")
            check("db: sku props untouched",
                  row.payload.get("skuPropertyList") == BASE_PROPS, "")
            check("db: detail_images untouched",
                  row.detail_images == ["https://cdn.example.com/d1.jpg",
                                        "https://cdn.example.com/d2.jpg"],
                  str(row.detail_images))
        finally:
            db.close()

        # 4) 超过 20 张 -> 400
        too_many = client.patch(f"{base}/{ident}/images", json={
            "imgUrls": [f"https://cdn.example.com/m{i}.jpg" for i in range(21)],
            "confirm": True}, headers=headers)
        check("http: >20 -> 400", too_many.status_code == 400, str(too_many.status_code))

        # 5) 非 http -> 400
        bad_scheme = client.patch(f"{base}/{ident}/images", json={
            "imgUrls": ["ftp://cdn.example.com/x.jpg"], "confirm": True}, headers=headers)
        check("http: non-http -> 400", bad_scheme.status_code == 400,
              str(bad_scheme.status_code))

        # 6) notes 超长 -> 400
        huge = client.patch(f"{base}/{ident}/images", json={
            "notes": "x" * 100001, "confirm": True}, headers=headers)
        check("http: notes too long -> 400", huge.status_code == 400,
              str(huge.status_code))

        # 7) 改 notes -> detail_images 同步
        new_notes = ("<p>new</p>"
                     "<img src='https://cdn.example.com/e1.jpg'>"
                     "<img src='https://cdn.example.com/e2.jpg'>"
                     "<img src='https://cdn.example.com/e3.jpg'>")
        notes_patch = client.patch(f"{base}/{ident}/images", json={
            "notes": new_notes, "confirm": True}, headers=headers)
        check("http: patch notes 200", notes_patch.status_code == 200,
              str(notes_patch.status_code))
        check("http: notes changed list",
              notes_patch.json().get("changed") == ["notes"],
              str(notes_patch.json().get("changed")))
        db = SessionLocal()
        try:
            row = reload_product(db, product_id)
            check("db: notes written", row.payload.get("notes") == new_notes, "")
            check("db: detail_images synced", row.detail_images == [
                "https://cdn.example.com/e1.jpg",
                "https://cdn.example.com/e2.jpg",
                "https://cdn.example.com/e3.jpg"], str(row.detail_images))
            check("db: images untouched by notes patch", row.images == new_urls,
                  str(row.images))
        finally:
            db.close()

        # 8) 改 skuPropertyList -> sku_properties 同步
        new_props = [{"attrName": "尺码", "attrValueList": [
            {"attrValueId": "9", "attrValue": "L", "imgUrl": None}]}]
        props_patch = client.patch(f"{base}/{ident}/images", json={
            "skuPropertyList": new_props, "confirm": True}, headers=headers)
        check("http: patch props 200", props_patch.status_code == 200,
              str(props_patch.status_code))
        check("http: props changed list",
              props_patch.json().get("changed") == ["skuPropertyList"],
              str(props_patch.json().get("changed")))
        db = SessionLocal()
        try:
            row = reload_product(db, product_id)
            check("db: sku_properties synced",
                  [p["attrName"] for p in row.sku_properties] == ["尺码"],
                  str(row.sku_properties))
            check("db: payload props written",
                  row.payload.get("skuPropertyList") == new_props, "")
            check("db: notes untouched by props patch",
                  row.payload.get("notes") == new_notes, "")
        finally:
            db.close()

        # 9) 幂等：同样的内容再传一次，changed 为空、不写库
        again = client.patch(f"{base}/{ident}/images", json={
            "imgUrls": new_urls, "confirm": True}, headers=headers)
        check("http: idempotent patch 200", again.status_code == 200,
              str(again.status_code))
        check("http: idempotent changed empty", again.json().get("changed") == [],
              str(again.json().get("changed")))

        # 10) 空 body -> 400
        empty = client.patch(f"{base}/{ident}/images", json={"confirm": True},
                             headers=headers)
        check("http: empty body -> 400", empty.status_code == 400, str(empty.status_code))

        # 11) 不存在的商品 -> 404
        missing = client.patch(f"{base}/999999/images", json={
            "imgUrls": ["https://cdn.example.com/z.jpg"], "confirm": True},
            headers=headers)
        check("http: missing product -> 404", missing.status_code == 404,
              str(missing.status_code))

        # 12) warehouseKey 也能定位（与 /{ident} 同一套解析）
        by_key = client.patch(f"{base}/{key}/images", json={
            "imgUrls": ["https://cdn.example.com/k1.jpg"], "confirm": True},
            headers=headers)
        check("http: patch by warehouseKey 200", by_key.status_code == 200,
              str(by_key.status_code))

        # 13) 只读模式 -> 403
        db = SessionLocal()
        try:
            from services import setting_service

            setting_service.set_read_only(db, True)
            db.commit()
        finally:
            db.close()
        read_only = client.patch(f"{base}/{ident}/images", json={
            "imgUrls": ["https://cdn.example.com/r1.jpg"], "confirm": True},
            headers=headers)
        check("http: read-only -> 403", read_only.status_code == 403,
              str(read_only.status_code))
        db = SessionLocal()
        try:
            from services import setting_service

            setting_service.set_read_only(db, False)
            db.commit()
        finally:
            db.close()

        # 14) 未鉴权 -> 401
        anon = client.patch(f"{base}/{ident}/images", json={
            "imgUrls": ["https://cdn.example.com/a.jpg"], "confirm": True})
        check("http: no token -> 401", anon.status_code == 401, str(anon.status_code))

        # 15) 原有 PATCH /{ident} 仍然可用（新路由没抢掉它）
        # 注意：老路由的返回是 {product, facets}，标题在 product 里
        legacy = client.patch(f"{base}/{ident}", json={"title": "renamed"},
                              headers=headers)
        check("http: legacy patch still works", legacy.status_code == 200,
              str(legacy.status_code))
        check("http: legacy patch applied",
              legacy.json().get("product", {}).get("title") == "renamed",
              str(legacy.json())[:200])

        # 16) 主图清空 -> cover 一起清
        cleared = client.patch(f"{base}/{ident}/images", json={
            "imgUrls": [], "confirm": True}, headers=headers)
        check("http: clear images 200", cleared.status_code == 200,
              str(cleared.status_code))
        db = SessionLocal()
        try:
            row = reload_product(db, product_id)
            check("db: cleared images empty", row.images == [], str(row.images))
            check("db: cleared cover empty", (row.cover or "") == "", row.cover)
        finally:
            db.close()

        check("upstream: never called", upstream_calls == [], str(upstream_calls))


def test_no_upstream_symbol() -> None:
    """静态确认：新路由不引用 save_shop / publish_service。"""
    source = (ROOT / "api" / "warehouse.py").read_text(encoding="utf-8")
    start = source.find('@router.patch("/{ident}/images"')
    check("source: images route present", start > 0, str(start))
    segment = source[start:] if start > 0 else ""
    for forbidden in ("save_shop", "publish_service", "account_client", "MiaoshouClient"):
        check(f"source: route does not reference {forbidden}", forbidden not in segment,
              "found")


def main() -> int:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        account = Account(name="probe", app_key="probe-key",
                          app_secret_enc=crypto.encrypt("probe-secret"),
                          is_default=True, site="MY", status="active")
        db.add(account)
        db.commit()
        db.refresh(account)
        test_derived_sync(db, account)
    finally:
        db.close()

    test_http()
    test_no_upstream_symbol()

    failed = [row for row in results if not row[1]]
    for label, ok, detail in results:
        print(("PASS " if ok else "FAIL ") + label
              + (("  [" + detail + "]") if detail and not ok else ""))
    print("TOTAL %d  PASS %d  FAIL %d"
          % (len(results), len(results) - len(failed), len(failed)))

    out_dir = ROOT / "_meas" / "_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "probe_images_patch.json").write_text(json.dumps(
        {"total": len(results), "failed": len(failed),
         "rows": [{"label": l, "ok": o, "detail": d} for l, o, d in results]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
