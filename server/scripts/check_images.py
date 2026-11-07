"""图片健康巡检：找出失效的图片地址并从商品里清掉。

为什么需要这个脚本：
  商品入库时图片可能是从 1688 图床直接引用的（没走自建图床），
  而 1688 的 `earth-rt.chengji-inc.com` 这类 CDN 会过期 —— 实测
  2026-11-07 有 7 张 404，前端只能显示破图占位。

  入库环节目前只在「处理入仓」时会调 persist_payload_images 转存，
  历史数据不会自动修复。所以提供一个可重复执行的巡检：

    1. 扫描所有未删除商品的 imgUrls（主图）与 notes（详情图）
    2. 逐个 HEAD 请求验证（并发 16，单张 12 秒超时）
    3. 失效的从 payload 里剔除（主图重建数组，详情图从 HTML 里删掉整段 <p><img></p>）
    4. 默认只报告不写库；加 --apply 才真改

用法（服务器上）：
    /opt/miaoshou-listing-v2/venv/bin/python scripts/check_images.py
    /opt/miaoshou-listing-v2/venv/bin/python scripts/check_images.py --apply

注意：只检查 http(s) 地址，不下载图片内容（HEAD 就够判断存活），
所以对图床几乎没有压力。
"""
from __future__ import annotations

import argparse
import re
import ssl
import sys
import urllib.error
import urllib.request
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

# 以 `python scripts/check_images.py` 方式运行时，server/ 不在 sys.path 里，
# `from db.session import ...` 会报 ModuleNotFoundError。其他运维脚本
# （ci_migration_check / init_superadmin）都显式插入了 server 根目录，这里照做。
SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

# 与 services/product_standard.py 保持同一套 URL 提取规则
URL_RE = re.compile(r"https?://[^\s\"'<>\\]+", re.I)

USER_AGENT = "Mozilla/5.0 (compatible; AohuangImageCheck/1.0)"
TIMEOUT_SECONDS = 12
MAX_WORKERS = 16


def _ssl_context() -> ssl.SSLContext:
    """部分 1688 图床证书链不完整；这里只判存活，不校验证书。"""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _is_alive(url: str, context: ssl.SSLContext) -> bool:
    """HEAD 200/206 视为存活。

    有的 CDN 不支持 HEAD 会回 405，这时退回 GET 只读首字节，
    避免把「不支持 HEAD」误判成「图片已删」。
    """
    headers = {"User-Agent": USER_AGENT, "Referer": "https://aohuang888.com/"}
    try:
        request = urllib.request.Request(url, method="HEAD", headers=headers)
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS,
                                  context=context) as response:
            return response.status in (200, 206)
    except urllib.error.HTTPError as exc:
        if exc.code in (405, 501):
            return _is_alive_via_get(url, context)
        return False
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _is_alive_via_get(url: str, context: ssl.SSLContext) -> bool:
    headers = {"User-Agent": USER_AGENT, "Referer": "https://aohuang888.com/"}
    try:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS,
                                    context=context) as response:
            response.read(1)
            return response.status in (200, 206)
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _collect(payload: dict[str, Any]) -> list[tuple[str, int, str]]:
    """返回 [(来源, 序号, url)]，来源是 main / detail。"""
    found: list[tuple[str, int, str]] = []
    for index, url in enumerate(payload.get("imgUrls") or []):
        if isinstance(url, str) and url.startswith("http"):
            found.append(("main", index, url))
    for index, url in enumerate(URL_RE.findall(str(payload.get("notes") or ""))):
        found.append(("detail", index, url))
    return found


def _strip_detail_url(notes: str, url: str) -> str:
    """从 notes HTML 里删掉这张图。

    只删图片本身会留下空 `<p></p>`，所以连包裹它的空段落一起删；
    末尾再清一遍空段，避免每次巡检都积累一层空标签。
    """
    text = notes.replace(url, "")
    text = re.sub(r"<p>\s*</p>", "", text, flags=re.I)
    text = re.sub(r"<p>\s*(<img[^>]*>\s*)*</p>", "", text, flags=re.I)
    return text.strip()


def check(apply_changes: bool = False, only_ids: Iterable[int] | None = None) -> dict[str, Any]:
    from db.session import SessionLocal
    from models.product import Product

    session = SessionLocal()
    try:
        query = session.query(Product).filter(Product.deleted_at.is_(None))
        if only_ids:
            query = query.filter(Product.id.in_(list(only_ids)))
        products = query.all()

        context = _ssl_context()
        # 先汇总所有 URL 去重后统一验证：同一张图常被多个商品引用，
        # 只验一次能省掉大量重复请求。
        unique: dict[str, tuple[str, int, str]] = {}
        for product in products:
            payload = product.payload if isinstance(product.payload, dict) else {}
            for kind, index, url in _collect(payload):
                unique.setdefault(url, (kind, index, url))

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            alive_flags = dict(zip(unique.keys(),
                                   pool.map(lambda u: _is_alive(u, context), unique.keys()),
                                   strict=True))

        dead = {url for url, alive in alive_flags.items() if not alive}
        report: dict[str, Any] = {
            "products": len(products),
            "images": len(unique),
            "dead": len(dead),
            "affected": [],
            "applied": apply_changes,
        }
        if not dead:
            return report

        for product in products:
            payload = product.payload if isinstance(product.payload, dict) else {}
            hits = [(kind, index, url) for kind, index, url in _collect(payload)
                    if url in dead]
            if not hits:
                continue
            report["affected"].append({
                "id": product.id,
                "title": str(product.title or "")[:40],
                "deadUrls": [url for _, _, url in hits],
            })
            if not apply_changes:
                continue

            new_payload = dict(payload)
            # 主图重建数组，保持原顺序、剔除失效项
            main_urls = [u for u in (new_payload.get("imgUrls") or [])
                         if isinstance(u, str) and u not in dead]
            new_payload["imgUrls"] = main_urls
            # 详情图逐张从 HTML 里删
            notes = str(new_payload.get("notes") or "")
            for _, _, url in hits:
                if url in notes:
                    notes = _strip_detail_url(notes, url)
            new_payload["notes"] = notes
            product.payload = new_payload

        if apply_changes:
            session.commit()
        return report
    finally:
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="图片健康巡检")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只报告）")
    parser.add_argument("--ids", default="", help="只检查指定商品 id，逗号分隔")
    args = parser.parse_args()
    ids = [int(x) for x in args.ids.split(",") if x.strip().isdigit()] or None

    report = check(apply_changes=args.apply, only_ids=ids)
    print(f"商品 {report['products']} 件，图片 {report['images']} 张，失效 {report['dead']} 张")
    for item in report["affected"]:
        print(f"  p{item['id']} {item['title']}")
        for url in item["deadUrls"]:
            print(f"    - {url}")
    print("已写库" if report["applied"] else "仅报告（加 --apply 才写库）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
