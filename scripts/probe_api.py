"""打印 /auth/status 与 /auth/connect 的真实响应结构（去敏）。"""
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
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, raw[:300].decode("utf-8", "replace")


def redact(obj):
    """把疑似密钥的值打码，避免输出到聊天。"""
    SECRET_KEYS = {"appSecret", "app_secret", "secret", "secretHint", "appKey", "appId"}
    if isinstance(obj, dict):
        return {k: ("***" if k in SECRET_KEYS else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


print("=== GET /auth/status ===")
s, p = call("GET", "/auth/status")
print("HTTP", s)
print(json.dumps(redact(p), ensure_ascii=False, indent=1)[:2000])

print()
print("=== GET /auth/accounts ===")
s, p = call("GET", "/auth/accounts")
print("HTTP", s)
print(json.dumps(redact(p), ensure_ascii=False, indent=1)[:1500])

print()
print("=== POST /auth/connect （罗蔓，save=True）===")
key, secret = cs.load_connection("罗蔓")
s, p = call("POST", "/auth/connect",
            {"appId": key, "appSecret": secret, "name": "罗蔓", "save": True, "autoScan": False})
print("HTTP", s)
print(json.dumps(redact(p), ensure_ascii=False, indent=1)[:2000])
