"""清除 Cloudflare 上「固定链接」的缓存条目。

## 什么时候需要跑

只有一种情况：`.xyz` 域名下的 `latest-Setup.exe` 返回的字节和清单对不上。

## 为什么会这样（2026-11-26 事故）

源站 nginx 曾经给固定链接也挂了 `max-age=31536000, immutable`，Cloudflare
老老实实缓存了一整年 —— 把 0.1.44 的字节发给了 0.1.54 的用户。源站改对之后，
CF 那一层还是旧字节，只有清缓存才能立刻生效（TTL 到期前它绝不回源）。

`scripts/verify_release.py` 的第 4 步会检测到这种情况。

## 怎么取得 token

Dashboard → 右上头像 → My Profile → API Tokens → Create Token
→ 用模板「Purge Cache」（权限只需 Zone → Cache Purge → Purge）→ 复制。

Token 也可以放在 `~/.dsh/aohuang-cf-token`（单行纯文本），脚本会自动读。

用法：
    # 清固定链接（最常用）
    python -X utf8 scripts/purge_cdn.py

    # 清任意 URL（相对 CDN 域）
    python -X utf8 scripts/purge_cdn.py --url https://aohuang888.xyz/miaoshou-downloads/latest-Setup.exe

    # 整个 zone 全清（慎用：所有缓存全没了，回源压力上来）
    python -X utf8 scripts/purge_cdn.py --everything

    # 只看会发什么请求，不真发
    python -X utf8 scripts/purge_cdn.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.cloudflare.com/client/v4"
ZONE_NAME = "aohuang888.xyz"
FIXED_LINK = f"https://{ZONE_NAME}/miaoshou-downloads/latest-Setup.exe"
TOKEN_FILES = [
    Path.home() / ".dsh" / "aohuang-cf-token",
    Path.home() / ".config" / "aohuang" / "cf-token",
]
TOKEN_ENV = ["CF_API_TOKEN", "CLOUDFLARE_API_TOKEN"]


def resolve_token() -> str:
    """token 来源优先级：环境变量 → 本机 token 文件 → 报错并给获取办法。"""
    for name in TOKEN_ENV:
        value = (os.environ.get(name) or "").strip()
        if value:
            print(f"token 来源: 环境变量 {name}")
            return value
    for path in TOKEN_FILES:
        if path.is_file():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                print(f"token 来源: {path}")
                return value
    print("找不到 Cloudflare API token。")
    print()
    print("取一个：Dashboard → 头像 → My Profile → API Tokens → Create Token")
    print("        用模板「Purge Cache」（Zone → Cache Purge → Purge）")
    print()
    print("然后二选一：")
    print(f'  setx CF_API_TOKEN "你的token"        （新开终端生效）')
    print(f'  或写进 {TOKEN_FILES[0]}（单行纯文本）')
    raise SystemExit(2)


def api_get(path: str, token: str) -> dict:
    request = urllib.request.Request(
        f"{API}{path}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def api_post(path: str, token: str, body: dict) -> dict:
    request = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def find_zone_id(token: str, zone_name: str) -> str:
    data = api_get(f"/zones?name={zone_name}", token)
    if not data.get("success") or not data.get("result"):
        raise SystemExit(f"查 zone 失败: {json.dumps(data.get('errors'), ensure_ascii=False)}")
    return data["result"][0]["id"]


def main() -> None:
    parser = argparse.ArgumentParser(description="清除 Cloudflare 缓存")
    parser.add_argument("--url", action="append", default=[],
                        help="要清的完整 URL，可重复；默认清固定下载链接")
    parser.add_argument("--everything", action="store_true", help="清整个 zone（慎用）")
    parser.add_argument("--dry-run", action="store_true", help="只打印请求，不发送")
    args = parser.parse_args()

    urls = args.url or ([] if args.everything else [FIXED_LINK])

    if args.dry_run:
        print(f"POST {API}/zones/<zone_id>/purge_cache")
        if args.everything:
            print('  {"purge_everything": true}')
        else:
            print(f'  {json.dumps({"files": urls}, ensure_ascii=False)}')
        return

    token = resolve_token()
    zone_id = find_zone_id(token, ZONE_NAME)
    print(f"zone: {ZONE_NAME} → {zone_id}")

    body = {"purge_everything": True} if args.everything else {"files": urls}
    if not args.everything:
        print("清除 URL:")
        for url in urls:
            print(f"  {url}")

    try:
        data = api_post(f"/zones/{zone_id}/purge_cache", token, body)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        print(f"清缓存失败 HTTP {exc.code}: {detail}")
        raise SystemExit(1)

    if not data.get("success"):
        print(f"清缓存失败: {json.dumps(data.get('errors'), ensure_ascii=False)}")
        raise SystemExit(1)

    print("清缓存成功。")
    print()
    print("复查（应该看到 cf-cache-status: MISS 或新的 last-modified）：")
    for url in urls:
        print(f"  curl -sI {url} | grep -i 'cf-cache-status\\|last-modified\\|content-length'")
    print()
    print("然后跑: python -X utf8 scripts/verify_release.py")


if __name__ == "__main__":
    main()