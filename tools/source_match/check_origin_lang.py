"""关键核实：妙手草稿里的马来语颜色到底哪来的。

方法：找一件**从未被我们处理过**的商品（products 表无记录），
直接拉它的 shop_detail，看颜色是中文还是马来语。

如果它是中文 → 说明马来语是我们翻译后写回去的，
              那么「已翻译却不标记，下次又洗一遍颜色」就成立。
如果它本来就是马来语 → 上游自己就是马来语。
"""
import os
import warnings

warnings.filterwarnings("ignore")

import psycopg

from db.session import SessionLocal
from models.account import Account
from services import account_service, warehouse_service

url = os.environ["AOHUANG_DATABASE_URL"].replace(
    "postgresql+psycopg://", "postgresql://")

SHOP = "19977183"
ACC = 12


def colors_of(info):
    out = []
    for p in (info.get("skuPropertyList") or []):
        if not isinstance(p, dict):
            continue
        labels = [(str(v.get("attrValue")), bool(v.get("imgUrl")))
                  for v in (p.get("attrValueList") or []) if isinstance(v, dict)]
        out.append((str(p.get("attrName")), labels))
    return out


with psycopg.connect(url) as conn:
    processed = {r[0] for r in conn.execute("""
        select detail_id from products where deleted_at is null
    """).fetchall()}

db = SessionLocal()
try:
    acc = db.query(Account).filter(Account.id == ACC).one()
    client = account_service.account_client(acc)

    items = []
    for page in range(1, 4):
        rs = client.collect_box_list(status="notPublished",
                                     page_no=page, page_size=50)
        if not rs:
            break
        items.extend(rs)
    client.close()
    print(f"采集箱共 {len(items)} 件，本地已处理 {len(processed)} 件")

    fresh = []
    for it in items:
        did = str(it.get("collectBoxDetailId") or it.get("detailId"))
        if did not in processed and (it.get("collectBoxDetailShopList")
                                     or it.get("shopId")):
            fresh.append(did)

    print(f"从未处理过: {len(fresh)} 件")
    print()
    print("=" * 76)
    print("抽 6 件从未处理的，看它原始颜色是中文还是马来语")
    print("=" * 76)

    for did in fresh[:6]:
        c2 = account_service.account_client(acc)
        try:
            d = c2.shop_detail(did, SHOP)
            info = (d.get("data") or {}).get("shopCollectItemInfo") or {}
        except Exception as ex:  # noqa: BLE001
            print(f"  {did} 拉取失败 {type(ex).__name__}")
            c2.close()
            continue
        c2.close()

        title = str(info.get("title") or "")[:40]
        print()
        print(f"  {did}  {title}")
        for name, labels in colors_of(info):
            shown = [f"{lab}{'[图]' if img else ''}" for lab, img in labels[:8]]
            print(f"      {name}: {shown}")
        notes = str(info.get("notes") or "")
        cn = sum(1 for ch in notes if "\u4e00" <= ch <= "\u9fff")
        print(f"      notes 中文字符数: {cn} / 总长 {len(notes)}")
finally:
    db.close()