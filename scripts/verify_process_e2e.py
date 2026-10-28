"""P0-3 端到端：POST /collect-box/{detailId}/process 真能把商品入仓。

mock 掉上游 client.shop_detail 与翻译，验证：
  1. save=true 写入仓库（products 行数 +1）
  2. save=false 只预览，回滚后行数不变
  3. 颜色规范化两个字段一致（P0-7 联动）
"""
import sys

sys.path.insert(0, ".")

from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402
from core.config import get_settings  # noqa: E402
from db.session import SessionLocal  # noqa: E402
from models.product import Product  # noqa: E402
from services import account_service, product_service  # noqa: E402

DETAIL_ID = "900000777"
SHOP_ID = "18077147"

FIXTURE = {
    "data": {
        "ossMd5": "smoke-md5",
        "editModel": "shop",
        "shopCollectItemInfo": {
            "detailId": DETAIL_ID,
            "shopId": SHOP_ID,
            "title": "Smoke Test Shoe Y18",
            "mainImgUrl": "https://img.example.com/main.jpg",
            "imgUrls": ["https://img.example.com/main.jpg"],
            "cid": "200000001",
            "skuPropertyList": [
                {"attrName": "颜色", "attrValueList": [
                    {"attrValueId": "1", "attrValue": "Y18[beige]"},
                    {"attrValueId": "2", "attrValue": "Beige + Black [Two-Piece Set]"},
                ]},
            ],
            "skuMap": {
                "1": {"skuId": "s1", "price": "30", "originPrice": "30",
                      "stock": "10", "weight": "0.5", "propPath": "1",
                      "isDelete": "0", "skuPropertyList": []},
            },
            "description": "smoke description",
        },
    }
}


class FakeClient:
    def shop_detail(self, detail_id, shop_id=None, **kwargs):
        return FIXTURE

    def close(self):
        pass


def fake_translated_info(client, info, language=""):
    return dict(info), {"translated": False, "language": language, "changedFields": 0}


client = TestClient(main.app, raise_server_exceptions=False)
H = {"Authorization": f"Bearer {get_settings().local_secret()}"}

original_client = account_service.account_client
original_translate = product_service.translated_info
account_service.account_client = lambda account: FakeClient()
product_service.translated_info = fake_translated_info

db = SessionLocal()
before = db.query(Product).count()
db.close()
print("入仓前商品数:", before)

try:
    print("\n--- save=false（只预览） ---")
    response = client.post(f"/api/v1/collect-box/{DETAIL_ID}/process", headers=H, json={
        "shopId": SHOP_ID, "language": "ms", "save": False,
    })
    print("HTTP:", response.status_code)
    if response.status_code >= 400:
        print(response.text[:500])
    db = SessionLocal()
    after_preview = db.query(Product).count()
    db.close()
    print("预览后商品数:", after_preview, "（应与入仓前相同）")

    print("\n--- save=true（写库） ---")
    response = client.post(f"/api/v1/collect-box/{DETAIL_ID}/process", headers=H, json={
        "shopId": SHOP_ID, "language": "ms", "save": True,
    })
    print("HTTP:", response.status_code)
    body = response.json() if response.status_code < 400 else {}
    print("saved:", body.get("saved"))
    product = body.get("product") or {}
    print("product.detailId:", product.get("detailId"), "skuCount:", product.get("skuCount"))

    db = SessionLocal()
    after_save = db.query(Product).count()
    print("写库后商品数:", after_save, "（应 +1）")

    row = (db.query(Product)
           .filter(Product.detail_id == DETAIL_ID, Product.shop_id == SHOP_ID)
           .one_or_none())
    if row:
        def colors_of(source):
            return [v.get("attrValue") for p in (source or [])
                    if str(p.get("attrName")) == "颜色"
                    for v in (p.get("attrValueList") or [])]

        sku_colors = colors_of(row.sku_properties)
        payload_colors = colors_of((row.payload or {}).get("skuPropertyList"))
        print("sku_properties 颜色:", sku_colors)
        print("payload 颜色     :", payload_colors)
        print("两字段一致:", sku_colors == payload_colors)
        print("已规范化:", all("Y18" not in c and "[" not in c for c in sku_colors))
        db.delete(row)
        db.commit()
    db.close()
finally:
    account_service.account_client = original_client
    product_service.translated_info = original_translate

db = SessionLocal()
print("\n清理后商品数:", db.query(Product).count(), "(应回到", before, ")")
db.close()
