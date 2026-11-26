"""端到端验证：帽/椅/鞋各 3 件，确认 500 修好且判定正确。"""
import warnings

warnings.filterwarnings("ignore")

from db.session import SessionLocal
from models.account import Account
from services import account_service, product_service, warehouse_service

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
    for page in (1, 2, 3, 4, 5):
        r = client.collect_box_list(status="notPublished", page_no=page,
                                    page_size=50)
        rs = rows_of(r)
        if not rs:
            break
        all_rows.extend(rs)

    seen = {"帽": 0, "椅": 0, "鞋": 0, "其它": 0}
    picks = []
    for it in all_rows:
        c = cls(it.get("title"))
        if seen[c] < 3:
            picks.append((c, it))
            seen[c] += 1

    print(f"共抓 {len(all_rows)} 件，抽 {len(picks)} 件（每类 3）\n")
    print(f"{'类':<3} {'标题':<30} {'类目':<14} {'尺码图':<7} {'判定'}")
    print("-" * 70)

    bad = 0
    for c, it in picks:
        did = it.get("collectBoxDetailId")
        resp = client.shop_detail(did, SHOP_ID)
        info = (resp.get("data") or {}).get("shopCollectItemInfo") or {}

        cat = product_service.category_label(info)
        needs = warehouse_service._payload_needs_size_chart(info, cat)
        mark = "套" if needs else "不套"

        expect = "不套" if c in ("帽", "椅", "其它") else "套"
        verdict = "OK" if mark == expect else "✗ 判错"
        if mark != expect:
            bad += 1
        print(f"{c:<3} {str(it.get('title'))[:28]:<30} "
              f"{cat or '-':<14} {mark:<7} {verdict}")

    print()
    print(f"✓ 全部跑通，无 UnboundLocalError")
    print(f"  判错 {bad} 件")
finally:
    db.close()