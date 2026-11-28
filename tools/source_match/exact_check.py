"""确认这 6 个「404」是真不存在，还是 URL 尾部粘了引号。"""
import re
import warnings

import httpx

warnings.filterwarnings("ignore")

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

    blob = str(info)
    # 精确匹配：只取 store-xxx.jpg，遇到引号/斜杠/空白就停
    urls = re.findall(r'https://aohuang888\.com/tk-product-automation/products/store-[0-9a-f]{32}\.jpg', blob)
    print(f"精确匹配到的地址: {len(set(urls))} 个（去重后）")
    print()
    ok = bad = 0
    with httpx.Client(timeout=20, follow_redirects=True) as hc:
        for u in sorted(set(urls)):
            try:
                resp = hc.get(u)
                code = resp.status_code
                size = len(resp.content) if code == 200 else 0
            except Exception as ex:  # noqa: BLE001
                code, size = type(ex).__name__, 0
            if code == 200:
                ok += 1
            else:
                bad += 1
            print(f"  {code}  {size:>7} B  {u.split('/')[-1]}")
    print()
    print(f"200: {ok} / 非 200: {bad}")
finally:
    db.close()