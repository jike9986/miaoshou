"""修复占用「罗蔓」名字的假 key 记录（ak_test_0001）。

策略：先删除这条 id=1 的占位记录，再用真实凭据重新导入「罗蔓」。
不打印任何密钥。
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


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, raw[:200].decode("utf-8", "replace")


# 1) 找到 appIdHint 为空的那条（假 key 占位）
s, p = call("GET", "/auth/accounts")
accounts = p.get("accounts") or []
victim = next((a for a in accounts if not a.get("appIdHint")), None)
print("待替换的占位记录:", victim)

if victim:
    s, p = call("DELETE", f"/auth/accounts/{victim['id']}")
    print(f"删除 id={victim['id']} -> HTTP {s}")

# 2) 重新导入罗蔓真实凭据
key, secret = cs.load_connection("罗蔓")
s, p = call("POST", "/auth/connect",
            {"appId": key, "appSecret": secret, "name": "罗蔓",
             "save": True, "autoScan": False})
print(f"导入罗蔓 -> HTTP {s}")
if s >= 300:
    print("  ", (p.get("error") or {}).get("message"))

# 3) 复核
print()
s, p = call("GET", "/auth/status")
print("connected:", p.get("connected"), "| 当前:", p.get("connectionName"))
print("账号数:", len(p.get("accounts") or []))
for a in p.get("accounts") or []:
    print(f"  id={a['id']} {a['name']:<8} hint={a['appIdHint'] or '(空)':<8} "
          f"status={a['status']:<8} site={a['site']} verified={a['lastVerifiedAt']}")
