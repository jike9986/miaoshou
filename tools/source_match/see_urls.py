"""看妙手草稿里保存的完整地址 + 实际 HTTP 状态。"""
import re
import warnings

import httpx

warnings.filterwarnings("ignore")

from db.session import SessionLocal
from models.account import Account
from services import account_service

STRIP = ('"', "'", "\\", ",", "]", ")", ";")

db = SessionLocal()
try:
    acc = db.query(Account).filter(Account.id == 12).one()
    client = account_service.account_client(acc)
    r = client.shop_detail("3563400336", "19977183")
    info = (r.get("data") or {}).get("shopCollectItemInfo") or {}
    client.close()

    blob = str(info)
    raw = re.findall(r'https://[^"\s\\]*products/[^"\s\\]*', blob)
    urls = []
    for u in raw:
        for ch in STRIP:
            u = u.rstrip(ch)
        urls.append(u)

    print(f"妙手草稿里的 products 地址 {len(set(urls))} 个：")
    ok = bad = 0
    with httpx.Client(timeout=20, follow_redirects=True) as hc:
        for u in dict.fromkeys(urls):
            try:
                code = hc.head(u).status_code
            except Exception as ex:  # noqa: BLE001
                code = type(ex).__name__
            if code == 200:
                ok += 1
            else:
                bad += 1
            print(f"  {code}  {u}")
    print()
    print(f"200: {ok} 个 / 非 200: {bad} 个")
finally:
    db.close()