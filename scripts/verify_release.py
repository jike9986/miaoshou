"""发布前最终校验：固定链接、清单、CDN 上的实际字节必须完全一致。

用法：
    python -X utf8 scripts/verify_release.py

退出码 0 = 可以发布；非 0 = 不能发布。

这是唯一能证明「用户下载到的东西是对的」的方法。
只看 Content-Length 不够 —— 曾出现过 Content-Length 和 sha256 都指向旧文件、
但清单已更新的事故（CDN 缓存同名文件）。

## 2026-11-26 补的一课：必须校验「固定链接」

以前的校验只覆盖 `version.json` 里那个带哈希的地址，那条链路一直是对的，
于是 0.1.44→0.1.54 六个版本全绿 —— 而用户实际被贴的固定链接
`latest-Setup.exe` 一直在下发 **0.1.44**（Cloudflare 把软链接的结果
缓存了一整年）。清单绿了，用户装的是旧包。

所以现在有三道检查，缺一不可：

1. 两个域名的清单一致
2. 清单里那个带哈希的地址，字节级正确
3. **固定链接最终落地的那份字节，也要和清单一致**（这才是用户真正下载的东西）
"""
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from release_fixed_link import FIXED_LINK_PATH, fixed_link_is_safe  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

MANIFEST_HOSTS = [
    "https://aohuang888.com/miaoshou-downloads/version.json",
    "https://aohuang888.xyz/miaoshou-downloads/version.json",
]

#: 用户被贴的固定链接。**主域是唯一被广告出去的入口**（主域直连源站，
#: 不经 Cloudflare），所以它是硬性闸门。
PRIMARY_FIXED_LINK = f"https://aohuang888.com{FIXED_LINK_PATH}"

#: .xyz 走 Cloudflare。它的缓存无法在本脚本里清除（需要 CF token 或
#: Dashboard 操作），所以默认只报警告；加 --strict-cdn 才当闸门用。
SECONDARY_FIXED_LINK = f"https://aohuang888.xyz{FIXED_LINK_PATH}"

STRICT = "--strict-cdn" in sys.argv


def fetch_manifest(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def hash_url(url: str) -> tuple[str, int]:
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    digest = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(request, timeout=900) as response:
        while True:
            block = response.read(262144)
            if not block:
                break
            digest.update(block)
            total += len(block)
    return digest.hexdigest(), total


print("=== 1. 两个入口的清单是否一致 ===")
manifests = {}
for url in MANIFEST_HOSTS:
    data = fetch_manifest(url)
    manifests[url] = data
    print(f"  {url.split('/')[2]:20} version={data['version']}  "
          f"size={data['sizeBytes']}  sha={data['sha256'][:16]}")

same = all(json.dumps(manifests[u], sort_keys=True) ==
           json.dumps(manifests[MANIFEST_HOSTS[0]], sort_keys=True)
           for u in MANIFEST_HOSTS)
print(f"  → 一致性: {'OK' if same else 'FAIL'}")

manifest = manifests[MANIFEST_HOSTS[0]]
expected_sha = str(manifest.get("sha256") or "").strip().lower()
expected_size = int(manifest.get("sizeBytes") or 0)

print()
print("=== 2. 清单地址带哈希、且不是 latest-Setup.exe 自身 ===")
url_ok, url_reason = fixed_link_is_safe(manifest)
print(f"  url: {manifest.get('url')}")
print(f"  → {url_reason}: {'OK' if url_ok else 'FAIL'}")

print()
print("=== 3. 清单地址的实际字节 sha256 是否与清单一致 ===")
print("  （下载完整文件算 hash，这步能发现 CDN 缓存旧文件）")
actual, size = hash_url(manifest["url"])
print(f"  清单 sha256 : {expected_sha}")
print(f"  实际 sha256 : {actual}")
print(f"  清单 size   : {expected_size}")
print(f"  实际 size   : {size}")
ok_manifest_bytes = actual == expected_sha and size == expected_size
print(f"  → {'OK' if ok_manifest_bytes else 'FAIL'}")

print()
print("=== 4. ★ 固定下载链接最终落地的字节（用户真正下到的东西）===")
print("  （2026-11-26 之前没查这一项，导致 6 个版本都在给用户发旧包）")
print("  主域 = 硬性闸门（直连源站，就是被广告出去的那个链接）")
print("  .xyz = 走 Cloudflare，缓存清不掉时只警告（加 --strict-cdn 变闸门）")

fixed_results = {}
for link, blocking in ((PRIMARY_FIXED_LINK, True), (SECONDARY_FIXED_LINK, STRICT)):
    host = link.split("/")[2]
    tag = "阻塞项" if blocking else "警告项"
    try:
        sha_fixed, size_fixed = hash_url(link)
    except urllib.error.HTTPError as exc:
        fixed_results[link] = (False, f"HTTP {exc.code}", blocking)
        print(f"  {host:20} [{tag}] 下载失败 HTTP {exc.code}")
        continue
    match = sha_fixed == expected_sha and size_fixed == expected_size
    fixed_results[link] = (match, sha_fixed, blocking)
    verdict = "OK" if match else ("FAIL（下发的是别的版本）" if blocking else "STALE（CDN 缓存旧字节）")
    print(f"  {host:20} [{tag}] size={size_fixed}  sha={sha_fixed[:16]}  → {verdict}")

blocking_ok = all(ok for ok, _, blocking in fixed_results.values() if blocking)
cdn_ok = all(ok for ok, _, blocking in fixed_results.values() if not blocking)

print()
print("=== 结果 ===")
print(f"  清单一致          : {'OK' if same else 'FAIL'}")
print(f"  清单地址合规      : {'OK' if url_ok else 'FAIL'}")
print(f"  清单字节一致      : {'OK' if ok_manifest_bytes else 'FAIL'}")
print(f"  固定链接(主域)    : {'OK' if blocking_ok else 'FAIL'}")
print(f"  固定链接(.xyz CDN): {'OK' if cdn_ok else ('FAIL' if STRICT else 'STALE（见下方处置）')}")

if same and url_ok and ok_manifest_bytes and blocking_ok and cdn_ok:
    print()
    print("  发布校验通过，用户下载到的文件是正确的。")
elif same and url_ok and ok_manifest_bytes and blocking_ok and not cdn_ok:
    print()
    print("  主域固定链接已正确 —— 用户拿到的是新版，发布可以继续。")
    print("  但 .xyz 上还有 Cloudflare 缓存的旧字节，清掉它（不阻塞发布）：")
    print()
    for link, (ok, extra, blocking) in fixed_results.items():
        if not ok and not blocking:
            print(f"  ✗ {link}")
            print(f"    期望 sha256 {expected_sha[:16]} / {expected_size} bytes")
            print(f"    实际 {extra}")
    print()
    print("  清缓存二选一：")
    print("    python -X utf8 scripts/purge_cdn.py        # 需要 CF_API_TOKEN")
    print("    Dashboard → Caching → Configuration → Purge Cache → Custom Purge")
    print("      https://aohuang888.xyz/miaoshou-downloads/latest-Setup.exe")
else:
    print()
    for link, (ok, extra, blocking) in fixed_results.items():
        if not ok and blocking:
            print(f"  ✗ {link}  [阻塞项]")
            print(f"    期望 sha256 {expected_sha[:16]} / {expected_size} bytes")
            print(f"    实际 {extra}")
    print()
    print("  主域固定链接失败时的处置（按顺序查）：")
    print("    1. nginx 精确匹配 location 是否还在：")
    print("       grep -n -A8 'location = /miaoshou-downloads/latest-Setup.exe' \\")
    print("            /etc/nginx/sites-enabled/aohuang-ai.conf")
    print("    2. 跳转 snippet 指向哪个文件（必须是带哈希的名字）：")
    print("       cat /etc/nginx/snippets/miaoshou-latest-redirect.conf")
    print("    3. 重跑发版脚本让 snippet 和 version.json 一起更新：")
    print("       python -X utf8 scripts/publish_release.py <包> <版本> \"说明\"")
    raise SystemExit("校验失败，不能发布")