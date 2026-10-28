"""P0 回归冒烟：采集箱与类目接口不再 500。"""
import sys
import json

sys.path.insert(0, ".")

from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402
from core.config import get_settings  # noqa: E402

token = get_settings().local_secret()
client = TestClient(main.app, raise_server_exceptions=False)
headers = {"Authorization": f"Bearer {token}"}

cases = [
    ("GET", "/api/v1/collect-box?pageSize=2", None),
    ("GET", "/api/v1/collect-box?pageSize=2&status=all", None),
    ("POST", "/api/v1/categories/sync", {"site": "MY"}),
    ("POST", "/api/v1/collect-box/delete", {"detailIds": ["999999999"]}),
    ("POST", "/api/v1/collect-box/claim", {"keys": ["3482714692:18077147"], "targetShopId": "18077147"}),
]
for method, path, body in cases:
    response = client.request(method, path, json=body, headers=headers)
    payload = {}
    try:
        payload = response.json()
    except Exception:
        payload = {"raw": response.text[:120]}
    code = (payload.get("error") or {}).get("code") if isinstance(payload, dict) else None
    print(f"{method:5} {path:58} -> {response.status_code} {code or ''}")
    if response.status_code >= 500:
        print("   !! 500:", json.dumps(payload, ensure_ascii=False)[:300])
