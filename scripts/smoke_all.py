"""修复后全量冒烟：新增/修复接口逐条打，记录真实状态码。"""
import sys
import json

sys.path.insert(0, ".")

from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402
from core.config import get_settings  # noqa: E402

token = get_settings().local_secret()
client = TestClient(main.app, raise_server_exceptions=False)
H = {"Authorization": f"Bearer {token}"}

cases = [
    # P0-1 采集箱（假凭据应为 502，不再是 500 NameError）
    ("GET", "/api/v1/collect-box?pageSize=2", None),
    # 新增：载体链接池
    ("GET", "/api/v1/carrier/links", None),
    ("POST", "/api/v1/carrier/links",
     {"urls": ["https://detail.1688.com/offer/612345678901.html",
               "https://example.com/offer/612345678901.html"]}),
    # 新增：尺寸表
    ("GET", "/api/v1/size-charts", None),
    ("GET", "/api/v1/size-charts/resolve?site=MY", None),
    ("POST", "/api/v1/size-charts",
     {"name": "冒烟尺寸表", "site": "MY", "url": "https://example.com/size.html"}),
    # 新增：价格预览
    ("POST", "/api/v1/warehouse/price-preview", {"items": [], "template": ""}),
    # 新增：词库启停
    ("PATCH", "/api/v1/violation-words/enabled", {"enabled": False, "words": ["不存在的词"]}),
    # P2-2 非法 JSON
    ("POST", "/api/v1/violation-words", "{bad json"),
    # 既有：仓库、分组、定时、系统
    ("GET", "/api/v1/warehouse?pageSize=2", None),
    ("GET", "/api/v1/groups", None),
    ("GET", "/api/v1/schedules", None),
    ("GET", "/api/v1/system/health", None),
    ("GET", "/api/v1/system/version", None),
]

print(f"{'METHOD':6} {'PATH':52} {'HTTP':4} CODE")
print("-" * 92)
for method, path, body in cases:
    kwargs = {}
    if isinstance(body, str):
        kwargs = {"content": body, "headers": {**H, "Content-Type": "application/json"}}
    elif body is not None:
        kwargs = {"json": body, "headers": H}
    else:
        kwargs = {"headers": H}
    response = client.request(method, path, **kwargs)
    try:
        payload = response.json()
    except Exception:
        payload = {}
    code = (payload.get("error") or {}).get("code") if isinstance(payload, dict) else None
    flag = "  <== 500!" if response.status_code >= 500 else ""
    print(f"{method:6} {path:52} {response.status_code:<4} {code or ''}{flag}")
