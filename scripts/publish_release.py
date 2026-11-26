"""发布新版：上传安装包 + 更新软链接 + 更新 version.json + 重写固定链接跳转

用法（在仓库根目录）：
    python -X utf8 scripts/publish_release.py release/奥皇上品-0.1.20-Setup.exe 0.1.20 "更新说明"

服务器密码从 桌面/雨云.txt 读取。

## 三个入口，各管各的

| 入口 | 内容 | 缓存策略 |
|---|---|---|
| `version.json` | 版本清单，永远是判据 | `no-store`，CDN 也绕过 |
| `AohuangShangpin-<ver>-<hash8>-Setup.exe` | 真正的字节 | 长缓存（名字里带哈希，内容不变） |
| `latest-Setup.exe` | 固定链接，给人贴、给老客户端 | `no-store` + 302 到上面那行 |

## 2026-11-26 事故：固定链接给用户下发旧包

以前 `latest-Setup.exe` 是个软链接，落在目录级
`Cache-Control: immutable, max-age=31536000` 里 → Cloudflare 把 0.1.44 的
字节缓存了一整年（`cf-cache-status: HIT`, `Age: 235408`）。源站早就指向
0.1.54，用户下载到的还是 0.1.44。`version.json` 链路一直是好的，把这个问题
盖住了 6 个版本。

现在：nginx 用**精确匹配 location** 处理固定链接，强制 `no-store`，并 302 到
**带哈希的文件名**（每次发版 URL 都变，缓存自然失效）。跳转目标写在这个脚本
维护的 snippet 里 —— 所以发版时**必须**跑完本脚本，不能只上传文件。
"""
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import paramiko

sys.path.insert(0, str(Path(__file__).resolve().parent))

from release_fixed_link import (  # noqa: E402
    SNIPPET_REMOTE_PATH,
    artifact_name,
    artifact_url,
    redirect_snippet,
)

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

    def run(cmd, t=60):
        _, o, e = c.exec_command(cmd, timeout=t)
        return o.read().decode() + e.read().decode()

    # 文件名带内容 hash：同名覆盖会被 Cloudflare 缓存一年，新文件名必然重新回源。
    exe_name = artifact_name(version, sha)
    remote_exe = f"{REMOTE_DIR}/{exe_name}"

    # 上传安装包
    print(f"上传 {installer.name} -> {exe_name} ...")
    sftp = c.open_sftp()
    sftp.put(str(installer), remote_exe)
    sftp.chmod(remote_exe, 0o644)

    # 校验落地的字节数（上传静默截断是历史上真出过的事故）
    remote_size = run(f"stat -c %s {remote_exe}").strip()
    if remote_size != str(size):
        c.close()
        raise SystemExit(f"上传后大小不符：本地 {size}，服务器 {remote_size}")

    # 保留软链接（老文档/老脚本可能还在用），但它已经不在任何缓存路径上：
    # 同名的 latest-Setup.exe 请求被精确匹配的 location 先截走，不会摸到软链接。
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
    content = json.dumps(manifest, indent=2, ensure_ascii=False)
    run(f"cat > {REMOTE_DIR}/version.json << 'ENDJSON'\n{content}\nENDJSON")

    # 重写固定链接的跳转目标（这一步是本次事故的补丁，别删）
    print(f"重写固定链接跳转 -> {artifact_url(version, sha)}")
    snippet = redirect_snippet(artifact_url(version, sha))
    with sftp.open(SNIPPET_REMOTE_PATH, "w") as fh:
        fh.write(snippet)
    sftp.chmod(SNIPPET_REMOTE_PATH, 0o644)
    sftp.close()

    # nginx 必须 reload 才会读到新 snippet；先 test 再 reload，配错就不动线上
    check = run("nginx -t 2>&1")
    print(check.strip())
    if "test is successful" not in check:
        c.close()
        raise SystemExit("nginx -t 失败，未 reload。先修配置再发版。")
    print(run("systemctl reload nginx && sleep 1 && systemctl is-active nginx").strip())

    # 验证
    print("\n=== 验证 ===")
    print(run(f"cat {REMOTE_DIR}/version.json"))
    print(run(f"cat {SNIPPET_REMOTE_PATH}"))
    print(run(f"ls -lh {remote_exe}"))
    print(run(
        "curl -sI --resolve aohuang888.com:443:127.0.0.1 "
        f"https://aohuang888.com/miaoshou-downloads/latest-Setup.exe | head -6"
    ))
    c.close()
    print("\n发布完成!")
    print(f"固定下载链接: {DOWNLOAD_BASE}/latest-Setup.exe")
    print(f"版本清单: {DOWNLOAD_BASE}/version.json")
    print("\n下一步必须跑: python -X utf8 scripts/verify_release.py")
    print("它会字节级校验「用户从固定链接和清单链接真正下载到的东西」是否一致。")


if __name__ == "__main__":
    main()