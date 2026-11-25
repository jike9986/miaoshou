"""Upload changed server files and restart the backend service."""
from pathlib import Path
import paramiko

HOST = "38.95.75.47"
USER = "root"
PASSWORD_FILE = Path(r"C:\Users\Administrator\Desktop\雨云.txt")
REMOTE_BASE = "/opt/miaoshou-listing-v2/server"
LOCAL_BASE = Path(r"D:\缓存文件夹\codex工作台\p006\miaoshou\server")

FILES = [
    ("services/automation_run_service.py", "services/automation_run_service.py"),
    ("services/color_service.py",          "services/color_service.py"),
    ("services/warehouse_service.py",       "services/warehouse_service.py"),
    ("services/schedule_service.py",        "services/schedule_service.py"),
    ("services/setting_service.py",         "services/setting_service.py"),
    ("services/price_template.py",          "services/price_template.py"),
    ("services/sync_service.py",            "services/sync_service.py"),
    # 认领价格口径（负责人 2026-11-25）：
    #   · carrier_service   —— 微微上挑 + 抬到成本线
    #   · collect_box_service —— 认领时把 skuMap.price 重置成拿货价口径
    #   · product_service   —— purchase_range 不再回退到 price
    ("services/carrier_service.py",         "services/carrier_service.py"),
    ("services/collect_box_service.py",     "services/collect_box_service.py"),
    ("services/product_service.py",         "services/product_service.py"),
    ("api/sync.py",                         "api/sync.py"),
    ("services/publish_service.py",         "services/publish_service.py"),
    ("services/group_service.py",           "services/group_service.py"),
    ("api/system.py",                       "api/system.py"),
    ("api/groups.py",                       "api/groups.py"),
    ("api/warehouse.py",                    "api/warehouse.py"),
    ("api/collect_box.py",                  "api/collect_box.py"),
    ("schemas/requests.py",                 "schemas/requests.py"),
    ("core/constants.py",                   "core/constants.py"),
    ("core/config.py",                      "core/config.py"),
    ("main.py",                             "main.py"),
]

def read_password() -> str:
    lines = [l.strip() for l in PASSWORD_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    return lines[-1]

def main():
    pw = read_password()
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"Connecting to {HOST}...")
    c.connect(HOST, 22, USER, pw, timeout=30, allow_agent=False, look_for_keys=False)
    print("Connected.")

    sftp = c.open_sftp()
    for local_rel, remote_rel in FILES:
        local_path = LOCAL_BASE / local_rel
        remote_path = f"{REMOTE_BASE}/{remote_rel}"
        print(f"  uploading {local_rel} -> {remote_path}")
        sftp.put(str(local_path), remote_path)
        print(f"  OK")
    sftp.close()

    print("Restarting service...")
    _, stdout, stderr = c.exec_command("systemctl restart aohuang-listing-v2", timeout=30)
    out = stdout.read().decode().strip()
    err = stderr.read().decode().strip()
    if out:
        print("stdout:", out)
    if err:
        print("stderr:", err)

    # Verify service is running
    _, stdout, _ = c.exec_command("systemctl is-active aohuang-listing-v2", timeout=10)
    status = stdout.read().decode().strip()
    print(f"Service status: {status}")

    c.close()
    print("Done.")

if __name__ == "__main__":
    main()
