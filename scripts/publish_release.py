"""
发布新版：上传安装包 + 更新软链接 + 更新 version.json

用法（在仓库根目录）：
    python scripts/publish_release.py release/奥皇上品-0.1.20-Setup.exe 0.1.20 "更新说明"

服务器密码从 桌面/雨云.txt 读取。
每次发新版都跑一次：软链接会指向新安装包，version.json 会刷新，
https://aohuang888.com/miaoshou-downloads/latest-Setup.exe 永远是最新版。
"""
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import paramiko

HOST = "38.95.75.47"
USER = "root"
PASSWORD_FILE = Path(r"C:\Users\Administrator\Desktop\雨云.txt")
REMOTE_DIR = "/var/www/miaoshou-downloads"
DOWNLOAD_BASE = "https://aohuang888.com/miaoshou-downloads"

def read_password() -> str:
    return [l.strip() for l in PASSWORD_FILE.read_text(encoding="utf-8").splitlines() if l.strip()][-1]

def sha256_and_size(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(262144), b""):
            h.update(chunk)
    return h.hexdigest(), path.stat().st_size

def main():
    if len(sys.argv) < 3:
        print("用法: python scripts/publish_release.py <安装包路径> <版本号> [更新说明]")
        sys.exit(1)
    installer = Path(sys.argv[1])
    version = sys.argv[2]
    notes = sys.argv[3] if len(sys.argv) > 3 else f"v{version}"
    if not installer.is_file():
        print(f"安装包不存在: {installer}")
        sys.exit(1)

    sha, size = sha256_and_size(installer)
    print(f"SHA256: {sha}")
    print(f"Size: {size}")

    pw = read_password()
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, 22, USER, pw, timeout=30, allow_agent=False, look_for_keys=False)

    def run(cmd, t=30):
        _, o, e = c.exec_command(cmd, timeout=t)
        return o.read().decode() + e.read().decode()

    exe_name = f"AohuangShangpin-{version}-Setup.exe"
    remote_exe = f"{REMOTE_DIR}/{exe_name}"

    # 上传安装包
    print(f"上传 {installer.name} ...")
    sftp = c.open_sftp()
    sftp.put(str(installer), remote_exe)
    sftp.chmod(remote_exe, 0o644)

    # 更新软链接指向新安装包
    run(f"ln -sf {remote_exe} {REMOTE_DIR}/latest-Setup.exe")

    # 更新 version.json
    manifest = {
        "version": version,
        "url": f"{DOWNLOAD_BASE}/{exe_name}",
        "sha256": sha,
        "sizeBytes": size,
        "notes": notes,
        "publishedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "channel": "stable",
    }
    content = json.dumps(manifest, indent=2)
    run(f"cat > {REMOTE_DIR}/version.json << 'ENDJSON'\n{content}\nENDJSON")
    sftp.close()

    # 验证
    print("\n=== 验证 ===")
    print(run(f"cat {REMOTE_DIR}/version.json"))
    print(run(f"ls -lh {REMOTE_DIR}/latest-Setup.exe"))
    print(run(f"curl -sI {DOWNLOAD_BASE}/latest-Setup.exe | head -3"))
    c.close()
    print("\n发布完成!")
    print(f"固定下载链接: {DOWNLOAD_BASE}/latest-Setup.exe")
    print(f"版本清单: {DOWNLOAD_BASE}/version.json")

if __name__ == "__main__":
    main()
