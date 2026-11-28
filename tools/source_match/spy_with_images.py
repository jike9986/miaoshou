"""带图片翻译跑一件，抓 readback 明细。

上次不带 process_images，所以没触发图片翻译链路。
这次按 UI 真实参数：文字+图片都处理。
"""
import sys
import warnings

warnings.filterwarnings("ignore")

from db.session import SessionLocal
from models.account import Account
from services import account_service, readback_check, warehouse_service as ws

DID = sys.argv[1] if len(sys.argv) > 1 else "3563317041"
SHOP = "19977183"

_orig = readback_check.readback_issues
_calls = []


def spy(payload, actual, original, coverage, **kw):
    res = _orig(payload, actual, original, coverage, **kw)
    _calls.append({"payload": payload, "actual": actual,
                   "original": original, "result": res})
    return res


readback_check.readback_issues = spy


def col(info):
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
        sr = res.get("standardRepair") or {}
        print(f"  repair before: {sr.get('before')}")
        print(f"  repair fixes : {sr.get('fixes')}")
        print(f"  repair after : {sr.get('after')}")
    except Exception as ex:  # noqa: BLE001
        print(f"处理失败: {type(ex).__name__}: {str(ex)[:300]}")
    client.close()

    print(f"\nreadback 调用 {len(_calls)} 次")
    for n, c in enumerate(_calls, 1):
        print("=" * 74)
        print(f"第 {n} 次 结果 = {c['result']}")
        o, p, a = col(c["original"]), col(c["payload"]), col(c["actual"])
        for k in dict.fromkeys(list(o) + list(p) + list(a)):
            print(f"  {k:<22} orig={o.get(k,'—'):<44} pay={p.get(k,'—'):<44} act={a.get(k,'—')}")
finally:
    db.close()