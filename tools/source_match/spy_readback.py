"""拦截一次真实处理，直接打印 readback_issues 的 details() 明细。

用 monkeypatch 包住 readback_check.readback_issues，让它把入参和结果都落盘，
这样不用改生产代码就能看到 original / upstream_payload / actual 三方差异。
"""
import json
import sys
import warnings

warnings.filterwarnings("ignore")

from db.session import SessionLocal
from models.account import Account
from services import account_service, readback_check, warehouse_service as ws

DID = sys.argv[1] if len(sys.argv) > 1 else "3563325267"
SHOP = "19977183"

_orig_readback = readback_check.readback_issues
_calls = []


def spy(payload, actual, original, coverage, **kw):
    res = _orig_readback(payload, actual, original, coverage, **kw)
    _calls.append({"payload": payload, "actual": actual,
                   "original": original, "result": res})
    return res


readback_check.readback_issues = spy


def color_slots(info):
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
    client = account_service.account_client(acc)
    try:
        res = ws.save_from_collect_box(
            db, acc, client, detail_id=DID, shop_id=SHOP,
            language="ms", price_template=None,
            allow_image_review=True, automation_result=True,
            operator_id=64,
            image_limits={"mainImageCount": 9, "skuImageCount": 9,
                          "detailImageCount": 9},
        )
        print(f"处理成功 draftVerified={res.get('draftVerified')}")
    except Exception as ex:  # noqa: BLE001
        print(f"处理失败: {type(ex).__name__}: {str(ex)[:200]}")
    client.close()

    print(f"\nreadback_issues 被调用 {len(_calls)} 次")
    for n, c in enumerate(_calls, 1):
        print("=" * 74)
        print(f"第 {n} 次  结果 = {c['result']}")
        print("=" * 74)
        o = color_slots(c["original"])
        p = color_slots(c["payload"])
        a = color_slots(c["actual"])
        keys = list(dict.fromkeys(list(o) + list(p) + list(a)))
        print(f"  {'颜色值':<26} {'original':<46} {'payload':<46} actual")
        for k in keys:
            print(f"  {k:<26} {o.get(k,'—'):<46} {p.get(k,'—'):<46} {a.get(k,'—')}")

        # 直接算 sku slots match
        m = readback_check._sku_image_slots_match(
            c["original"], c["actual"],
            dropped_slots=(c["payload"].get("_droppedSkuImages")
                           if isinstance(c["payload"], dict) else None))
        print(f"\n  _sku_image_slots_match(original, actual) = {m}")
finally:
    db.close()