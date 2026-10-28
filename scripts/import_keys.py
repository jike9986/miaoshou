"""把老版 4 个真实账号导入 tk-desktop（走官方 connect 接口）。

不打印任何密钥；名称与 appKey 前缀只用于确认导入结果。
"""
import io
import json
import sys
import urllib.error
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\codex工作台\p006\tk-product-automation\src")

import credential_store as cs

DATA_DIR = r"E:\codex工作台\p006\tk-desktop\.runtime-data"
BASE = "http://127.0.0.1:8812/api/v1"

token = open(DATA_DIR + r"\secret.key", encoding="utf-8").read().strip()
headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}


def call(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, raw[:200].decode("utf-8", "replace")


# 1) 现有账号
status, payload = call("GET", "/auth/status")
accounts = (payload.get("data") or {}).get("accounts") or []
print("导入前账号数:", len(accounts))
for a in accounts:
    print(f"  已有: {a.get('name')}  appKey={str(a.get('appKey',''))[:8]}")

print()

# 2) 逐个导入
names = cs.list_saved_connections()
print(f"老版共 {len(names)} 个连接，开始导入：")
for name in names:
    key, secret = cs.load_connection(name)
    status, payload = call(
        "POST", "/auth/connect",
        {"appId": key, "appSecret": secret, "name": name, "save": True, "autoScan": False},
    )
    if status < 300:
        data = payload.get("data") or {}
        shops = data.get("shops") or []
        site = data.get("site") or ""
        print(f"  [{name}] 导入成功  site={site}  店铺{len(shops)}家")
    else:
        err = (payload.get("error") or {}) if isinstance(payload, dict) else {}
        print(f"  [{name}] 失败 HTTP {status}: {err.get('message', str(payload)[:100])}")

print()
status, payload = call("GET", "/auth/status")
data = payload.get("data") or {}
accounts = data.get("accounts") or []
print("导入后账号数:", len(accounts))
print("当前连接:", data.get("connectionName"), "site=", data.get("site"))
for a in accounts:
    print(f"  {a.get('name')}  appKey={str(a.get('appKey',''))[:8]}  status={a.get('status')}")
