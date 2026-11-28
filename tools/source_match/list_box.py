import os
import warnings

import httpx

warnings.filterwarnings("ignore")

TOKEN = os.environ.get("AOHUANG_TEST_TOKEN", "script-4bfc132a7d57feaf966f20b1643c280f")
BASE = "http://127.0.0.1:8813/api/v1"

with httpx.Client(headers={"Authorization": f"Bearer {TOKEN}"}, timeout=120) as c:
    r = c.get(f"{BASE}/collect-box", params={"page": 1, "pageSize": 200, "accountId": "12"})
    d = r.json()
    items = d.get("items") or []
    print(f"total={d.get('total')}  返回 {len(items)} 件\n")
    for it in items[:40]:
        print("%-13s %-6s %s" % (
            it.get("detailId"), it.get("shopId"),
            str(it.get("title") or "")[:40]))