"""真跑一件，抓 original / payload / actual 三方快照。

只处理 3563328188。逐槽位对比，打印到底哪个颜色丢了图。
"""
import sys
import warnings

warnings.filterwarnings("ignore")

from db.session import SessionLocal
from models.account import Account
from services import account_service, readback_check, warehouse_service as ws

DID = sys.argv[1] if len(sys.argv) > 1 else "3563328188"
SHOP = "19977183"


def slots(info):
    """{属性名|值: (url文件名, 完整url)}"""
    out = {}
    for prop in (info.get("skuPropertyList") or []):
        if not isinstance(prop, dict):
            continue
        nm = str(prop.get("attrName") or prop.get("propName") or "")
        for v in (prop.get("attrValueList") or []):
            if not isinstance(v, dict):
                continue
            val = str(v.get("attrValue") or "")
            url = str(v.get("imgUrl") or v.get("imageUrl") or v.get("image") or "")
            out[f"{nm}|{val}"] = (url.rsplit("/", 1)[-1] if url else "", url)
    return out


def show(tag, s):
    n = sum(1 for f, _ in s.values() if f)
    print(f"  {tag}: {len(s)} 槽位 / {n} 带图")
    for k, (fname, _) in s.items():
        print(f"      {k:<34} {fname or '(无图)'}")


def diff(a, b, la, lb):
    print(f"\n  === {la} vs {lb} ===")
    keys = list(a.keys()) + [k for k in b if k not in a]
    bad = 0
    for k in keys:
        fa = a.get(k, ("", ""))[0]
        fb = b.get(k, ("", ""))[0]
        if fa != fb:
            bad += 1
            print(f"      {k:<34} {la}={fa or '(无)'}  {lb}={fb or '(无)'}")
    if not bad:
        print("      完全一致")


db = SessionLocal()
try:
    acc = db.query(Account).filter(Account.id == 12).one()
    client = account_service.account_client(acc)

    d0 = client.shop_detail(DID, SHOP)
    original = ((d0.get("data") or {}).get("shopCollectItemInfo") or {})
    s_orig = slots(original)
    print("=" * 78)
    print(f"{DID}  处理前")
    print("=" * 78)
    show("original（核验基准）", s_orig)
    print(f"  _colors_need_chinese_cleaning = {ws._colors_need_chinese_cleaning(original)}")

    # 真跑处理
    print("\n  >>> 开始真实处理 <<<\n")
    try:
        product = ws.save_from_collect_box(
            db, acc, client, detail_id=DID, shop_id=SHOP,
            language="ms", price_template=None,
            allow_image_review=True, automation_result=True,
            operator_id=64,
            image_limits={"mainImageCount": 9, "skuImageCount": 9,
                          "detailImageCount": 9},
        )
        print(f"  处理结果: {product}")
    except Exception as ex:  # noqa: BLE001
        print(f"  处理抛错: {type(ex).__name__}: {str(ex)[:400]}")

    # 读回
    d1 = client.shop_detail(DID, SHOP)
    actual = ((d1.get("data") or {}).get("shopCollectItemInfo") or {})
    s_act = slots(actual)
    print("\n" + "=" * 78)
    print(f"{DID}  处理后（妙手读回）")
    print("=" * 78)
    show("actual", s_act)

    diff(s_orig, s_act, "original", "actual")
    ok = readback_check._sku_image_slots_match(original, actual, dropped_slots=None)
    print(f"\n  _sku_image_slots_match(original, actual) = {ok}")

    client.close()
finally:
    db.close()