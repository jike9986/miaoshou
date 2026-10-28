"""真实服务端到端冒烟：起 uvicorn → 打全部只读接口 → 检查 500 → 关闭。

只打 GET（不写库），验证修复后没有 500。
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, "server")

from core.config import get_settings  # noqa: E402

PORT = int(os.environ.get("SMOKE_PORT", "8891"))
token = get_settings().local_secret()
base = f"http://127.0.0.1:{PORT}"

env = dict(os.environ)
env["PYTHONPATH"] = os.path.abspath("server")
proc = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
     "--port", str(PORT), "--log-level", "warning"],
    cwd="server", env=env,
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
)

def call(path, method="GET", body=None):
    data = None
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)

try:
    ready = False
    for _ in range(60):
        status, _body = call("/api/v1/system/health")
        if status == 200:
            ready = True
            break
        time.sleep(0.5)
    if not ready:
        print("服务未能启动")
        raise SystemExit(1)
    print(f"服务已就绪 {base}\n")

    endpoints = [
        "/api/v1/system/health",
        "/api/v1/system/version",
        "/api/v1/auth/status",
        "/api/v1/shops",
        "/api/v1/warehouse?pageSize=5",
        "/api/v1/warehouse/stats",
        "/api/v1/groups",
        "/api/v1/categories",
        "/api/v1/schedules",
        "/api/v1/publish/logs?pageSize=5",
        "/api/v1/violation-words?pageSize=5",
        "/api/v1/violation-words/counts",
        "/api/v1/carrier/links",
        "/api/v1/size-charts",
        "/api/v1/size-charts/resolve?site=MY",
        "/api/v1/images/jobs?pageSize=5",
        "/api/v1/images/limits",
        "/api/v1/images/usage",
        "/api/v1/collect-box?pageSize=5",
    ]
    bad = []
    print(f"{'HTTP':5} {'PATH':48} NOTE")
    print("-" * 80)
    for path in endpoints:
        status, body = call(path)
        note = ""
        if status >= 500:
            try:
                code = json.loads(body).get("error", {}).get("code")
            except Exception:
                code = "?"
            note = f"<== 500 {code}"
            bad.append((path, status, code))
        print(f"{status:<5} {path:48} {note}")

    print()
    if bad:
        print("存在 5xx：")
        for path, status, code in bad:
            print("  ", path, status, code)
    else:
        print("全部接口无 5xx ✅")
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    print("服务已停止")
