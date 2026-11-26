"""抽查 28 件，用类目名优先的最终版判定。"""
import warnings

warnings.filterwarnings("ignore")

from collections import Counter

from db.session import SessionLocal
from models.account import Account
from services import account_service, warehouse_service

ACC_ID = 12
SHOP_ID = "19977183"


def rows_of(resp):
    d = resp.get("data") or {}
    return d.get("detailList") or d.get("items") or []


def cls(title):
    t = str(title or "")
    if "帽" in t:
        return "帽"
    if "椅" in t or "凳" in t:
        return "椅"
    if any(k in t for k in ("鞋", "靴")):
        return "鞋"
    return "其它"


db = SessionLocal()
try:
    acc = db.query(Account).filter(Account.id == ACC_ID).one()
    client = account_service.account_client(acc)
    all_rows = []
    for page in (1, 2, 3):
        r = client.collect_box_list(status="notPublished", page_no=page, page_size=50)
        rs = rows_of(r)
        if not rs:
            break
        all_rows.extend(rs)

    picks = {"帽": [], "椅": [], "鞋": [], "其它": []}
    for it in all_rows:
        c = cls(it.get("title"))
        if len(picks[c]) < 7:
            picks[c].append(it)

    stats = Counter()
    bad = []
    for cat, items in picks.items():
        for it in items:
            did = it.get("collectBoxDetailId")
            try:
                resp = client.shop_detail(did, SHOP_ID)
                info = (resp.get("data") or {}).get("shopCollectItemInfo") or {}
            except Exception:  # noqa: BLE001
                continue
            needs = warehouse_service._payload_needs_size_chart(info)
            mark = "需要" if needs else "不需要"
            stats[(cat, mark)] += 1

            from services import product_service
            cid = str(info.get("cid") or "")
            catname = product_service._category_name_from_db(cid) if cid else ""
            vals = []
            for p in (info.get("skuPropertyList") or []):
                if not isinstance(p, dict):
                    continue
                nm = str(p.get("attrNameCn") or p.get("attrName")
                         or p.get("propName") or "").lower()
                if nm in warehouse_service._SIZE_ATTR_NAMES:
                    vals.extend(str(av.get("attrValue") or "")
                                for av in (p.get("attrValueList") or [])
                                if isinstance(av, dict))
            wrong = ""
            if cat in ("帽", "椅") and needs:
                wrong = " ✗"
                bad.append(it.get("title"))
            if cat == "鞋" and not needs:
                wrong = " ✗"
                bad.append(it.get("title"))
            print(f"  {cat:<3} {mark:<4} 类目={catname:<12} "
                  f"尺码={str(vals)[:28]:<30}{wrong}", flush=True)

    print(flush=True)
    print("统计:", flush=True)
    for (c, m), n in sorted(stats.items()):
        print(f"  {c:<4} {m:<5} {n}", flush=True)
    print(flush=True)
    print("判错数: %d" % len(bad), flush=True)
    for t in bad:
        print(f"  {t}", flush=True)
finally:
    db.close()