"""追：原草稿 2 个颜色（Beras hitam. / Meter）什么时候变成 1 个（Hitam）。"""
import copy
import json
import warnings

warnings.filterwarnings("ignore")

from db.session import SessionLocal
from models.account import Account
from services import account_service, product_service, warehouse_service

DETAIL = "3563357906"
SHOP = "19977183"


def colors(payload):
    out = []
    for p in (payload.get("skuPropertyList") or []):
        if not isinstance(p, dict):
            continue
        if not warehouse_service._is_color_property(p):
            continue
        for v in (p.get("attrValueList") or []):
            if isinstance(v, dict):
                out.append((str(p.get("attrName") or ""),
                            str(v.get("attrValue") or ""),
                            str(v.get("imgUrl") or "")[-30:]))
    return out


db = SessionLocal()
try:
    acc = db.query(Account).filter(Account.id == 12).one()

    # ① 上游原始草稿
    client = account_service.account_client(acc)
    detail = client.shop_detail(DETAIL, SHOP)
    info = (detail.get("data") or {}).get("shopCollectItemInfo") or {}
    client.close()

    print("=" * 72)
    print("① 妙手原始草稿的颜色")
    print("=" * 72)
    for row in colors(info):
        print(f"    {row[0]} = {row[1]!r}  img=...{row[2]}")

    # ② 走完整处理，但每一步都打印
    import services.warehouse_service as ws
    orig_apply = ws.apply_image_limits
    orig_build = getattr(ws, "_apply_color_translation", None)

    def traced_apply(payload, **kw):
        before = colors(payload)
        r = orig_apply(payload, **kw)
        after = colors(payload)
        print()
        print("=" * 72)
        print("② apply_image_limits 前后")
        print("=" * 72)
        print(f"  参数: {kw}")
        print(f"  前: {[(a,b) for a,b,_ in before]}")
        print(f"  后: {[(a,b) for a,b,_ in after]}")
        print(f"  report: {json.dumps(r, ensure_ascii=False, default=str)[:300]}")
        return r

    ws.apply_image_limits = traced_apply

    # 也试试拦 translate 阶段
    try:
        from services import translate_service as ts
        orig_tr = ts.translate_payload
        def traced_tr(*a, **k):
            before = colors(a[0]) if a else []
            res = orig_tr(*a, **k)
            after = colors(a[0]) if a else []
            if before or after:
                print()
                print("=" * 72)
                print("③ translate_payload 前后")
                print("=" * 72)
                print(f"  前: {[(x,y) for x,y,_ in before]}")
                print(f"  后: {[(x,y) for x,y,_ in after]}")
            return res
        ts.translate_payload = traced_tr
    except Exception as ex:  # noqa: BLE001
        print(f"  （translate 钩子装不上: {type(ex).__name__}）")

    client = account_service.account_client(acc)
    try:
        p = ws.save_from_collect_box(
            db, acc, client, detail_id=DETAIL, shop_id=SHOP,
            language="ms", price_template="马来", enforce_pre_save=True,
            allow_title_review=True, allow_image_review=True,
            skip_image_review=False, operator_id=64, automation_result=True,
            image_limits={"main": 9, "sku": 9, "detail": 9},
        )
        db.commit()
        print(f"\n✓ 成功 id={p.get('id')}")
        print(f"  最终颜色: {[(a,b) for a,b,_ in colors(p.get('payload') or {})]}")
    except Exception as ex:  # noqa: BLE001
        db.rollback()
        print(f"\n✗ {type(ex).__name__}: {str(ex)[:150]}")
        # 即便失败也尝试从库/内存看当前
    finally:
        client.close()
finally:
    db.close()
