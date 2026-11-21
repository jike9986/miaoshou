"""发布前最终校验：清单与 CDN 上的实际字节必须完全一致。

用法：
    python -X utf8 scripts/verify_release.py

退出码 0 = 可以发布；非 0 = 不能发布。

这是唯一能证明「用户下载到的东西是对的」的方法。
只看 Content-Length 不够 —— 曾出现过 Content-Length 和 sha256 都指向旧文件、
但清单已更新的事故（CDN 缓存同名文件）。
"""
import hashlib
import json
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

MANIFEST_HOSTS = [
    "https://aohuang888.com/miaoshou-downloads/version.json",
    "https://aohuang888.xyz/miaoshou-downloads/version.json",
]


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
print()
print("=== 2. 清单地址能否下载 ===")
print(f"  url: {manifest['url']}")

print()
print("=== 3. CDN 上的实际字节 sha256 是否与清单一致 ===")
print("  （下载完整文件算 hash，这步能发现 CDN 缓存旧文件）")
actual, size = hash_url(manifest["url"])
print(f"  清单 sha256 : {manifest['sha256']}")
print(f"  实际 sha256 : {actual}")
print(f"  清单 size   : {manifest['sizeBytes']}")
print(f"  实际 size   : {size}")

ok_hash = actual == manifest["sha256"]
ok_size = size == manifest["sizeBytes"]
print()
print("=== 结果 ===")
print(f"  sha256 一致 : {'OK' if ok_hash else 'FAIL'}")
print(f"  size 一致   : {'OK' if ok_size else 'FAIL'}")
if ok_hash and ok_size and same:
    print()
    print("  发布校验通过，用户下载到的文件是正确的。")
else:
    raise SystemExit("校验失败，不能发布")