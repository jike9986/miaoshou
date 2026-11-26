"""固定下载链接的契约（纯逻辑，可单测，不连服务器）。

## 为什么单独抽出来（2026-11-26 事故）

`latest-Setup.exe` 是「内容会变」的入口，但历史上它只是 `/var/www/miaoshou-downloads/`
里的一个软链接，正好落在 nginx 那段 **`Cache-Control: immutable, max-age=31536000`**
的目录级长缓存里。Cloudflare 于是把 0.1.44 的字节缓存了一整年：

    .xyz/latest-Setup.exe  →  71460377 bytes / f5295be4…（0.1.44）cf-cache-status: HIT
    源站软链接             →  指向 0.1.54（daa2c7bf…）

源站改对了也没用，用户拿到的永远是旧包。`version.json` 那条链路一直是好的，
被它掩盖了整整 6 个版本。

## 现在的规则

1. 固定链接由 nginx **精确匹配** `= /miaoshou-downloads/latest-Setup.exe` 处理，
   不进目录级长缓存，并且强制 `no-store`（连 302 本身都不许缓存）。
2. 它 302 到**带内容哈希的文件名**。URL 每次发版都变 → 缓存天然失效。
3. 跳转目标写在 `/etc/nginx/snippets/miaoshou-latest-redirect.conf`，
   由 `scripts/publish_release.py` 每次发版覆写，**绝不手改**。

这个模块只负责「生成那段 snippet」和「判断它是否安全」，好让测试能在
不接触服务器的情况下钉死契约。
"""

from __future__ import annotations

#: 固定链接所在的路径，nginx 里必须有对应的精确匹配 location。
FIXED_LINK_PATH = "/miaoshou-downloads/latest-Setup.exe"

#: 跳转目标所在的主机（走 Cloudflare 的 CDN 域名）。
CDN_ORIGIN = "https://aohuang888.xyz"

#: 服务器上的 snippet 路径，publish_release.py 每次覆写它。
SNIPPET_REMOTE_PATH = "/etc/nginx/snippets/miaoshou-latest-redirect.conf"


def artifact_name(version: str, sha256: str) -> str:
    """安装包在服务器上的文件名：**带内容哈希**，内容变了名字就变。

    不带哈希的文件名会被 Cloudflare 按 URL 缓存一年（2026-11-26 事故）。
    """
    if not version or not version.strip():
        raise ValueError("版本号不能为空")
    digest = (sha256 or "").strip().lower()
    if len(digest) < 8:
        raise ValueError(f"sha256 太短，无法生成文件名: {sha256!r}")
    return f"AohuangShangpin-{version.strip()}-{digest[:8]}-Setup.exe"


def artifact_url(version: str, sha256: str) -> str:
    """安装包的完整下载地址（CDN 域）。"""
    return f"{CDN_ORIGIN}{FIXED_LINK_PATH.rsplit('/', 1)[0]}/{artifact_name(version, sha256)}"


def redirect_snippet(url: str) -> str:
    """生成 snippet 的内容：一条 `return 302 <带哈希的地址>;`。

    这个 snippet 被 nginx 的精确匹配 location `include` 进去。写进注释里的
    事故说明不是凑字数 —— 下一个改这里的人必须知道为什么不能指回
    `latest-Setup.exe` 本身（那会形成自跳转，且重新落进长缓存）。
    """
    target = (url or "").strip()
    if not target.startswith("https://"):
        raise ValueError(f"跳转目标必须是 https 绝对地址: {url!r}")
    if "/AohuangShangpin-" not in target:
        raise ValueError(f"跳转目标必须是带版本和哈希的安装包文件名: {url!r}")
    if target.endswith("/latest-Setup.exe"):
        raise ValueError("跳转目标不能是 latest-Setup.exe 本身（自跳转 + 会被 CDN 长缓存）")
    if not target.endswith(".exe"):
        raise ValueError(f"跳转目标必须是 .exe: {url!r}")
    return (
        "# 由 scripts/publish_release.py 发版时自动生成并覆盖，不要手改。\n"
        "# 它必须指向「带内容哈希的文件名」：每次发版 URL 都变，CDN 缓存自然失效。\n"
        "# 切勿指回 latest-Setup.exe 本身 —— 2026-11-26 就是这么被 Cloudflare\n"
        "# 把 0.1.44 的字节缓存了一年，用户拿到的永远是旧包。\n"
        f"return 302 {target};\n"
    )


def parse_snippet_target(snippet: str) -> str | None:
    """从 snippet 文本里把 `return 302 <url>;` 的目标读回来。读不到返回 None。"""
    for raw in (snippet or "").splitlines():
        line = raw.strip()
        if not line.startswith("return 302 "):
            continue
        target = line[len("return 302 "):].strip().rstrip(";").strip()
        return target or None
    return None


def fixed_link_is_safe(manifest: dict) -> tuple[bool, str]:
    """判断清单里的下载地址能不能安全地当作「固定链接」的跳转目标。

    返回 `(ok, 原因)`。测试用它钉死契约，verify_release.py 用它做发布前闸门。
    """
    if not isinstance(manifest, dict):
        return False, "清单不是对象"
    url = str(manifest.get("url") or "").strip()
    version = str(manifest.get("version") or "").strip()
    digest = str(manifest.get("sha256") or "").strip().lower()
    if not url:
        return False, "清单缺少 url"
    if not version:
        return False, "清单缺少 version"
    if len(digest) != 64:
        return False, f"清单 sha256 长度异常: {len(digest)}"
    if url.endswith("/latest-Setup.exe"):
        return False, "清单 url 指向 latest-Setup.exe 自身，会被 CDN 长缓存后永久过期"
    expected = artifact_name(version, digest)
    if not url.endswith("/" + expected):
        return False, f"清单 url 未使用带哈希的文件名，期望结尾 /{expected}"
    return True, "ok"