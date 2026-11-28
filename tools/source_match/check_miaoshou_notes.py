"""确认：本地 payload 已修，但妙手草稿里还是旧地址吗？"""
import os
import re

import psycopg

url = os.environ["AOHUANG_DATABASE_URL"].replace(
    "postgresql+psycopg://", "postgresql://")

with psycopg.connect(url) as conn:
    row = conn.execute("""
        select id, detail_id, shop_id, account_id
        from products where id = 639
    """).fetchone()
    print(f"商品 #{row[0]} {row[1]} shop={row[2]} acc={row[3]}")

from db.session import SessionLocal
from models.account import Account
from services import account_service

db = SessionLocal()
try:
    acc = db.query(Account).filter(Account.id == 12).one()
    client = account_service.account_client(acc)
    r = client.shop_detail("3563400336", "19977183")
    info = (r.get("data") or {}).get("shopCollectItemInfo") or {}
    client.close()

    notes = info.get("notes") or ""
    print(f"妙手 notes 长度: {len(notes)}")
    auto = re.findall(r'https://[^"\\\s]*auto-temp[^"\\\s]*', notes)
    prod = re.findall(r'https://[^"\\\s]*products/[^"\\\s]*', notes)
    print(f"  妙手里 auto-temp 地址: {len(auto)} 个")
    print(f"  妙手里 products 地址 : {len(prod)} 个")
    print()
    print("妙手 notes 前 300:")
    print(notes[:300])
finally:
    db.close()