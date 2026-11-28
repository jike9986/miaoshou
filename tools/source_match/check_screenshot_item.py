"""查截图里那件商品（1044460075526）的草稿 notes 原文 + 每个地址的真实状态。"""
import re
import warnings

import httpx

warnings.filterwarnings("ignore")

from sqlalchemy import text
from db.session import SessionLocal
from models.account import Account
from services import account_service

DETAIL_ID = "1044460075526"

db = SessionLocal()
try:
    row = db.execute(text("""
        select id, detail_id, shop_id, account_id,
               position('auto-temp' in payload::text) > 0 as has_auto,
               position('products/' in payload::text) > 0 as has_products,
               length(notes) as notes_len
        from products where detail_id = :d and deleted_at is null
        order by id desc limit 3
    """), {"d": DETAIL_ID}).fetchall()
    if not row:
        print(f"数据库里没有 detail_id={DETAIL_ID}")
        raise SystemExit(0)
    for r in row:
        print(f"商品 #{r[0]} detail={r[1]} shop={r[2]} acc={r[3]} "
              f"库内auto-temp={r[4]} 库内products={r[5]} notes长度={r[6]}")

    pid, did, shop, acc_id = row[0][0], row[0][1], str(row[0][2]), row[0][3]
    acc = db.query(Account).filter(Account.id == acc_id).one()
    client = account_service.account_client(acc)
    r2 = client.shop_detail(did, shop)
    info = (r2.get("data") or {}).get("shopCollectItemInfo") or {}
    client.close()

    notes = info.get("notes") or ""
    print()
    print(f"妙手 notes 长度 {len(notes)}")
    print("妙手 notes 原文（前 500）:")
    print(repr(notes[:500]))
    print()

    urls = re.findall(r'https://[^\s"\'<>\\,)\]]+', notes)
    print(f"notes 里共 {len(urls)} 个地址：")
    with httpx.Client(timeout=20, follow_redirects=True) as hc:
        for u in dict.fromkeys(urls):
            try:
                resp = hc.get(u)
                print(f"  {resp.status_code} {resp.headers.get('content-type','?'):>12} "
                      f"{len(resp.content):>7}B  {u}")
            except Exception as ex:  # noqa: BLE001
                print(f"  ERR {type(ex).__name__}  {u}")
finally:
    db.close()