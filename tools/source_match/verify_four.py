"""验证 4 件失败商品修复后能否通过。

顺序执行（不并发），每件打印：
  · 处理前颜色槽位
  · 处理结果 draftVerified
  · 处理后颜色槽位
  · 三方是否一致
"""
import warnings

warnings.filterwarnings("ignore")

from db.session import SessionLocal
from models.account import Account
from services import account_service, readback_check, warehouse_service as ws

SHOP = "19977183"
TARGETS = ["3563328216", "3563328188", "3563328281", "3563328533"]


def slots(info):
    out = {}
    for prop in (info.get("skuPropertyList") or []):
        if not isinstance(prop, dict) or not ws._is_color_property(prop):
            continue
        for v in (prop.get("attrValueList") or []):
            if not isinstance(v, dict):
                continue
            url = str(v.get("imgUrl") or v.get("imageUrl") or v.get("image") or "")
            out[str(v.get("attrValue") or "")] = url.rsplit("/", 1)[-1] if url else ""
    return out


db = SessionLocal()
try:
    acc = db.query(Account).filter(Account.id == 12).one()
    ok = bad = 0
    for did in TARGETS:
        client = account_service.account_client(acc)
        d0 = client.shop_detail(did, SHOP)
        before = ((d0.get("data") or {}).get("shopCollectItemInfo") or {})
        s0 = slots(before)

        print("=" * 74)
        print(f"{did}  处理前 {len(s0)} 色 / {sum(1 for v in s0.values() if v)} 带图")
        for k, v in s0.items():
            print(f"    {k:<26} {v or '(无图)'}")

        try:
            res = ws.save_from_collect_box(
                db, acc, client, detail_id=did, shop_id=SHOP,
                language="ms", price_template=None,
                allow_image_review=True, automation_result=True,
                operator_id=64,
                image_limits={"mainImageCount": 9, "skuImageCount": 9,
                              "detailImageCount": 9},
            )
            verified = res.get("draftVerified")
            repairs = (res.get("standardRepair") or {}).get("fixes") or []
        except Exception as ex:  # noqa: BLE001
            print(f"  抛错: {type(ex).__name__}: {str(ex)[:200]}")
            client.close()
            bad += 1
            continue

        d1 = client.shop_detail(did, SHOP)
        after = ((d1.get("data") or {}).get("shopCollectItemInfo") or {})
        s1 = slots(after)
        client.close()

        match = readback_check._sku_image_slots_match(before, after, dropped_slots=None)
        print(f"  处理后 {len(s1)} 色 / {sum(1 for v in s1.values() if v)} 带图")
        for k, v in s1.items():
            print(f"    {k:<26} {v or '(无图)'}")
        print(f"  draftVerified={verified}  修复项={repairs}")
        print(f"  核验 _sku_image_slots_match = {match}")

        if verified and match:
            ok += 1
            print("  >>> 通过 <<<")
        else:
            bad += 1
            print("  >>> 仍失败 <<<")
        print()

    print("=" * 74)
    print(f"结果: {ok}/{len(TARGETS)} 通过，{bad} 件失败")
finally:
    db.close()