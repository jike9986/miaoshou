"""完整复现一次处理（真写妙手），在保存前后各抓一次快照，打印差异。

只处理 1 件，避免批量。负责人禁止未经许可的批量。
"""
import sys
import warnings

warnings.filterwarnings("ignore")

from db.session import SessionLocal
from models.account import Account
from services import account_service, readback_check, warehouse_service as ws

DID = sys.argv[1] if len(sys.argv) > 1 else "3563328188"
SHOP = "19977183"


def snap(info):
    """颜色槽位快照：值 → URL 文件名"""
    out = []
    for prop in (info.get("skuPropertyList") or []):
        if not isinstance(prop, dict) or not ws._is_color_property(prop):
            continue
        for v in (prop.get("attrValueList") or []):
            if not isinstance(v, dict):
                continue
            url = str(v.get("imgUrl") or v.get("imageUrl") or v.get("image") or "")
            name = url.rsplit("/", 1)[-1] if url else ""
            out.append((str(v.get("attrValue") or ""), name))
    return out


def show(tag, s):
    print(f"  {tag}: {len(s)} 槽位 / {sum(1 for _, n in s if n)} 带图")
    for val, name in s:
        print(f"      {val:<26} {name or '(无图)'}")


db = SessionLocal()
try:
    acc = db.query(Account).filter(Account.id == 12).one()
    client = account_service.account_client(acc)

    d = client.shop_detail(DID, SHOP)
    before = ((d.get("data") or {}).get("shopCollectItemInfo") or {})
    s_before = snap(before)
    show("处理前（original）", s_before)

    payload = dict(before)
    print(f"\n  _colors_need_chinese_cleaning = {ws._colors_need_chinese_cleaning(payload)}")

    # 只做图片固化相关：看 SKU 图会被怎么处理
    print("\n  --- 走一遍 apply_image_limits（sku_limit=9 即不截断）---")
    try:
        rep = ws.apply_image_limits(payload, main_limit=9, detail_limit=9,
                                   sku_limit=9, group_id=None)
        print(f"      report: {rep}")
    except Exception as ex:  # noqa: BLE001
        print(f"      抛错: {type(ex).__name__}: {ex}")
    show("apply_image_limits 后（payload）", snap(payload))

    # 核验：用 original vs payload 自己比
    ok = readback_check._sku_image_slots_match(
        before, payload, dropped_slots=payload.get("_droppedSkuImages"))
    print(f"\n  _sku_image_slots_match(original, payload) = {ok}")

    client.close()
finally:
    db.close()