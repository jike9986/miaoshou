"""中文商品的 skuImages 失败：清洗是否把「颜色+跟高」合并掉了。

假设：这类商品的颜色值里嵌了跟高（裸色7cm / 裸色9cm / 白色【7厘米】），
清洗把跟高当型号/后缀剥掉后，`裸色7cm` 和 `裸色9cm` 都变成 `裸色`，
去重 → 2 个颜色剩 1 个 → 对应的 SKU 图也少一半。
读回核验拿「提交前草稿」当基准，图数量少了 → 恒报 skuImages。

对每件商品打印：
  清洗前的颜色值 + 各自有没有图
  清洗后的颜色值 + 各自有没有图
  带图槽位数量 before → after
"""
import warnings

warnings.filterwarnings("ignore")

from db.session import SessionLocal
from models.account import Account
from services import account_service, color_service, warehouse_service as ws

SHOP = "19977183"
TARGETS = ["3563328216", "3563328188", "3563328281", "3563328533"]


def color_slots(info):
    """[(属性名, 值, 有无图)]，只看颜色维度。"""
    out = []
    for prop in (info.get("skuPropertyList") or []):
        if not isinstance(prop, dict) or not ws._is_color_property(prop):
            continue
        name = str(prop.get("attrName") or "")
        for v in (prop.get("attrValueList") or []):
            if not isinstance(v, dict):
                continue
            url = str(v.get("imgUrl") or v.get("imageUrl") or v.get("image") or "")
            out.append((name, str(v.get("attrValue") or ""), bool(url)))
    return out


db = SessionLocal()
try:
    acc = db.query(Account).filter(Account.id == 12).one()
    for did in TARGETS:
        client = account_service.account_client(acc)
        d = client.shop_detail(did, SHOP)
        client.close()
        info = ((d.get("data") or {}).get("shopCollectItemInfo") or {})

        before = color_slots(info)
        print("=" * 76)
        print(f"{did}  {str(info.get('title'))[:50]}")
        print("=" * 76)
        print("  清洗前:")
        for name, val, img in before:
            print(f"      {val:<24} {'有图' if img else '(无图)'}")
        n_before = sum(1 for _, _, i in before if i)
        print(f"      → {len(before)} 个值，其中 {n_before} 个带图")

        # 真的跑一遍清洗，看结果
        payload = dict(info)
        try:
            ws._clean_color_props(payload)
        except Exception as ex:  # noqa: BLE001
            print(f"  清洗抛错: {type(ex).__name__}: {ex}")
            continue
        after = color_slots(payload)
        print("  清洗后:")
        for name, val, img in after:
            print(f"      {val:<24} {'有图' if img else '(无图)'}")
        n_after = sum(1 for _, _, i in after if i)
        print(f"      → {len(after)} 个值，其中 {n_after} 个带图")

        if n_after < n_before:
            print(f"  >>> 带图槽位减少 {n_before} → {n_after}，读回必然判失败 <<<")
        print()
finally:
    db.close()