"""验证采集箱：逐个账号切换并拉取，确认不再 502。"""
import io
import json
import sys
import urllib.error
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

DATA_DIR = r"E:\codex工作台\p006\tk-desktop\.runtime-data"
BASE = "http://127.0.0.1:8812/api/v1"
token = open(DATA_DIR + r"\secret.key", encoding="utf-8").read().strip()
headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, raw[:200].decode("utf-8", "replace")


s, p = call("GET", "/auth/accounts")
accounts = p.get("accounts") or []

print("=== 逐个账号拉采集箱 ===")
for a in accounts:
    # 切到该账号
    s, r = call("POST", "/auth/connect", {"accountId": a["id"], "save": True, "autoScan": False})
    if s >= 300:
        print(f"  [{a['name']}] 切号失败 HTTP {s}")
        continue
    s, box = call("GET", "/collect-box?page=1&pageSize=20")
    if s == 200:
        items = box.get("items") or []
        total = box.get("total")
        cached = box.get("cached")
        degraded = box.get("degraded")
        print(f"  [{a['name']}] OK  共 {total} 件，本页 {len(items)} 件"
              f"{' (缓存)' if cached else ''}{' (降级)' if degraded else ''}")
        for it in items[:3]:
            title = str(it.get("title") or "")[:38]
            print(f"        - {title}")
    else:
        err = (box.get("error") or {}) if isinstance(box, dict) else {}
        print(f"  [{a['name']}] HTTP {s}  {err.get('message', str(box)[:90])}")
