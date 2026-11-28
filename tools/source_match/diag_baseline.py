"""确认 skuImages 不一致的确切成因。

读回核验 13 项里只有 skuImages 用 original（处理前草稿）当基准，
其余 12 项都用 payload（提交的那份）。这在**我们故意改了规格**时必然失败：

  · apply_image_limits 按颜色数截断 → original 有 N 色，payload 只有 M 色
  · 颜色清洗改值 → original `Warna putih`，payload `Putih`，名字对不上
  · 图片固化替换 URL → 名字对上但 URL 全变

_sku_image_slots_match 内部那堆容错（名字对不上就退化成比数量）
全是在给「基准选错」打补丁。

这里对失败商品逐项打印 original / payload / actual 三者，定位到底哪一步不同。
"""
import warnings

warnings.filterwarnings("ignore")

from db.session import SessionLocal
from models.account import Account
from services import account_service, product_service, warehouse_service

TARGETS = ["3563339780", "3563339725", "3563339835", "3563339868"]
SHOP = "19977183"


def slots(item):
    """属性名|值名 → 是否有图。"""
    out = {}
    for prop in (item.get("skuPropertyList") or []):
        if not isinstance(prop, dict):
            continue
        name = str(prop.get("attrName") or prop.get("propName") or "")
        for v in (prop.get("attrValueList") or []):
            if not isinstance(v, dict):
                continue
            label = str(v.get("attrValue") or "")
            url = str(v.get("imgUrl") or v.get("imageUrl") or "")
            out[f"{name}|{label}"] = url
    return out


db = SessionLocal()
try:
    acc = db.query(Account).filter(Account.id == 12).one()
    for did in TARGETS:
        client = account_service.account_client(acc)
        try:
            d = client.shop_detail(did, SHOP)
            info = product_service.detail_block(d)["info"]
        except Exception as ex:  # noqa: BLE001
            print(f"{did}: 拉取失败 {type(ex).__name__}")
            client.close()
            continue
        client.close()

        orig = slots(info)
        print("=" * 74)
        print(f"{did}")
        print("=" * 74)
        print("  草稿 original（处理前）:")
        for k, v in orig.items():
            print(f"      {k:<32} {'有图' if v else '无图'}")
        print(f"  → 共 {sum(1 for v in orig.values() if v)} 个带图槽位")

        # 与 readback 同口径：名字对不上时退化比数量
        print()
        print("  核验基准错位的后果：")
        print(f"      original 带图 {sum(1 for v in orig.values() if v)} 个")
        print("      若 payload 因截断/清洗少于这个数 → 判 False → 报 skuImages")
finally:
    db.close()