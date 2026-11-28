"""抓 skuImages 读回不一致的真实现场：payload 提交了什么、读回什么。"""
import json
import warnings

warnings.filterwarnings("ignore")

import sys

DETAIL = sys.argv[1] if len(sys.argv) > 1 else "3563357906"
SHOP = "19977183"

from db.session import SessionLocal
from models.account import Account
from services import account_service, readback_check, warehouse_service

captured = {}
db = SessionLocal()
try:
    acc = db.query(Account).filter(Account.id == 12).one()
    client = account_service.account_client(acc)

    orig_rb = readback_check.readback_issues

    def slots(item):
        out = []
        props = item.get("skuPropertyList") if isinstance(item, dict) else None
        for p in (props or []):
            if not isinstance(p, dict):
                continue
            name = str(p.get("attrNameCn") or p.get("attrName") or "")
            for v in (p.get("attrValueList") or []):
                if not isinstance(v, dict):
                    continue
                lab = str(v.get("attrValue") or "").strip()
                u = str(v.get("imgUrl") or v.get("imageUrl")
                        or v.get("image") or "").strip()
                out.append((f"{name}|{lab}", u))
        return out

    def patched(payload, actual, original, coverage, **kw):
        captured["original"] = slots(original)
        captured["payload"] = slots(payload)
        captured["actual"] = slots(actual)
        captured["dropped"] = payload.get("_droppedSkuImages")
        captured["match"] = readback_check._sku_image_slots_match(
            original, actual, dropped_slots=payload.get("_droppedSkuImages"))
        return orig_rb(payload, actual, original, coverage, **kw)

    readback_check.readback_issues = patched
    warehouse_service.readback_check = readback_check

    print(f"跑 {DETAIL}", flush=True)
    try:
        p = warehouse_service.save_from_collect_box(
            db, acc, client, detail_id=DETAIL, shop_id=SHOP,
            language="ms", price_template="马来", enforce_pre_save=True,
            allow_title_review=True, allow_image_review=True,
            skip_image_review=False, operator_id=64, automation_result=True,
            image_limits={"main": 9, "sku": 9, "detail": 9},
        )
        db.commit()
        print(f"✓ 成功 id={p.get('id')}", flush=True)
    except Exception as ex:  # noqa: BLE001
        db.rollback()
        print(f"✗ {type(ex).__name__}: {str(ex)[:200]}", flush=True)

    print()
    o = captured.get("original") or []
    pl = captured.get("payload") or []
    a = captured.get("actual") or []
    print(f"match={captured.get('match')} dropped={captured.get('dropped')}")
    print()
    print(f"{'规格值':<28} {'original':<10} {'payload':<10} {'actual':<10}")
    print("-" * 72)
    om = dict(o)
    pm = dict(pl)
    am = dict(a)
    keys = list(dict.fromkeys([k for k, _ in o] + [k for k, _ in a] + [k for k, _ in pl]))
    for k in keys:
        ov = "有" if om.get(k) else ("无" if k in om else "缺")
        pv = "有" if pm.get(k) else ("无" if k in pm else "缺")
        av = "有" if am.get(k) else ("无" if k in am else "缺")
        flag = ""
        if ov == "有" and av == "无":
            flag = "  ← 图丢了"
        elif ov == "有" and av == "缺":
            flag = "  ← 读回没这个值"
        print(f"{k:<28} {ov:<10} {pv:<10} {av:<10}{flag}")
    print()
    for tag, lst in (("original", o), ("payload", pl), ("actual", a)):
        print(f"[{tag}]")
        for k, u in lst:
            print(f"    {k:<28} {u[-46:] if u else '(空)'}")
        print()
finally:
    db.close()
