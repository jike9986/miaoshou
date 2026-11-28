"""验证妙手草稿里的图片地址已全部可访问（账号 12 / 店铺 19977183）。"""
import os
import re
import warnings

warnings.filterwarnings("ignore")

import httpx
from sqlalchemy import text
from db.session import SessionLocal
from models.account import Account
from services import account_service

db = SessionLocal()
try:
    acc = db.query(Account).filter(Account.id == 12).one()
    rows = db.execute(text("""
        select id, detail_id, shop_id from products
        where deleted_at is null and account_id = 12
          and shop_id = '19977183' and automation_result = true
        order by id desc limit 15
    """)).fetchall()

    print(f"抽查 {len(rows)} 件妙手草稿")
    print("=" * 70)

    total_auto = total_prod = total_404 = 0
    with httpx.Client(timeout=20, follow_redirects=True) as hc:
        for pid, did, shop in rows:
            client = account_service.account_client(acc)
            try:
                r = client.shop_detail(did, str(shop))
                info = (r.get("data") or {}).get("shopCollectItemInfo") or {}
            except Exception as ex:  # noqa: BLE001
                print(f"  #{pid} {did} 拉详情失败 {type(ex).__name__}")
                client.close()
                continue
            client.close()

            blob = str(info)
            auto = re.findall(r'https://[^"\\\s]*auto-temp[^"\\\s]*', blob)
            prod = [u.rstrip('"').rstrip("\\")
                    for u in re.findall(r'https://[^"\\\s]*products/[^"\\\s]*', blob)]

            bad = 0
            for u in dict.fromkeys(prod):
                try:
                    if hc.head(u).status_code != 200:
                        bad += 1
                except Exception:  # noqa: BLE001
                    bad += 1

            total_auto += len(auto)
            total_prod += len(prod)
            total_404 += bad
            mark = "✓" if not bad else "✗"
            print(f"  {mark} #{pid} {did}  妙手中 auto-temp={len(auto)} "
                  f"products={len(prod)} 404={bad}")

    print()
    print(f"合计: 妙手里 auto-temp 残留 {total_auto} 个, "
          f"products {total_prod} 个, 404 {total_404} 个")
finally:
    db.close()