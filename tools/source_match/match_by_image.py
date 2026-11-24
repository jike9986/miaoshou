# -*- coding: utf-8 -*-
"""以图搜货：拿成品主图去 1688 找同源商品，把 offer id 写回货源链接。

## 前提

必须先跑过 `login1688.py` 扫码登录（会话落在同目录 `.profile-1688/`）。
未登录时 1688 会把图搜请求 302 到 login.taobao.com 或丢进 baxia 滑块页，
脚本拖动滑块会被升级成「访问被拒绝」，所以这里不做滑块对抗，只复用已登录会话。

## 做法

成品主图虽然被转存到自家 CDN，但**画面和 1688 上的一致**（只是标题翻译了），
所以图搜能找回原商品。流程：

  1. 从库里取缺货源的商品（默认排除精品/自营海外仓/海外仓定制款）
  2. 打开图搜页，把主图文件塞进 `input[type=file]`
  3. 解析结果里的 `detail.1688.com/offer/<id>.html`
  4. 按相似度取第一条（可选人工确认），写回 `payload.sourceUrl`

## 用法

    # 先看会命中多少（不写库）
    python tools/source_match/match_by_image.py --dry-run --limit 10

    # 确认无误后写库
    python tools/source_match/match_by_image.py --limit 10 --write

    # 全量（171 件，按 6 秒/件约 17 分钟）
    python tools/source_match/match_by_image.py --write
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
PROFILE = HERE / ".profile-1688"
CACHE = HERE / ".img-cache"
REPORT = HERE / ".match-report.json"

OFFER_RE = re.compile(r"detail\.1688\.com/offer/(\d+)\.html")
RESTRICTED_GROUPS = ("精品", "自营海外仓", "海外仓定制款")


# --------------------------------------------------------------------------
# 取数据：直连生产库（只读），避免为了这个工具再起一套服务
# --------------------------------------------------------------------------
def load_targets(limit: int, include_restricted: bool) -> list[dict[str, Any]]:
    """从生产库取缺货源商品。"""
    sys.path.insert(0, str((HERE / "../../server").resolve()))
    import warnings

    warnings.filterwarnings("ignore")
    from sqlalchemy import text

    from db.session import SessionLocal  # noqa: E402

    db = SessionLocal()
    try:
        sql = """
            select id, title, detail_id, group_name,
                   images->>0 as img,
                   payload->>'sourceUrl' as src
            from products
            where deleted_at is null
              and automation_result is false
              and coalesce(payload->>'sourceUrl','') = ''
              and json_typeof(images) = 'array'
              and json_array_length(images) > 0
        """
        if not include_restricted:
            sql += " and coalesce(group_name,'') not in :restricted"
        sql += " order by id asc"
        if limit:
            sql += " limit :lim"
        params: dict[str, Any] = {}
        if not include_restricted:
            params["restricted"] = RESTRICTED_GROUPS
        if limit:
            params["lim"] = limit
        rows = db.execute(text(sql), params).fetchall()
        return [
            {"id": r[0], "title": r[1], "detailId": r[2],
             "group": r[3], "img": r[4], "hasSource": bool(r[5])}
            for r in rows
        ]
    finally:
        db.close()


def save_source(product_id: int, offer_id: str, title: str) -> None:
    """把货源写回 payload（保留原有其它字段）。"""
    sys.path.insert(0, str((HERE / "../../server").resolve()))
    import warnings

    warnings.filterwarnings("ignore")
    from sqlalchemy import text

    from db.session import SessionLocal  # noqa: E402

    url = f"https://detail.1688.com/offer/{offer_id}.html"
    db = SessionLocal()
    try:
        db.execute(text("""
            update products
            set payload = jsonb_set(
                    jsonb_set(
                      jsonb_set(coalesce(payload,'{}'::jsonb),
                                '{sourceUrl}', to_jsonb(:url::text), true),
                      '{sourceItemId}', to_jsonb(:oid::text), true),
                    '{sourceMatchedBy}', to_jsonb('image-search'::text), true),
                revision = coalesce(revision, 0) + 1,
                updated_at = now()
            where id = :pid
        """), {"pid": product_id, "url": url, "oid": offer_id})

        # 规则 §4：自动化结果商品必须同步 draft_verified_revision
        db.execute(text("""
            update products
            set draft_verified_revision = revision
            where id = :pid and automation_result is true
              and draft_verified_revision is not null
        """), {"pid": product_id})
        db.commit()
    finally:
        db.close()


# --------------------------------------------------------------------------
# 图搜
# --------------------------------------------------------------------------
def fetch_image(url: str) -> Path:
    """下载主图到本地缓存（1688 上传需要本地文件）。"""
    CACHE.mkdir(parents=True, exist_ok=True)
    digest = url.rstrip("/").rsplit("/", 1)[-1]
    dest = CACHE / digest
    if dest.exists() and dest.stat().st_size > 1024:
        return dest
    resp = requests.get(url, timeout=45)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    return dest


async def search_one(page: Any, img_path: Path, *, wait_s: float = 6.0) -> list[str]:
    """上传一张图，返回结果里的 offer id 列表（去重、保持顺序）。"""
    await page.goto("https://s.1688.com/youyuan/index.htm?tab=imageSearch",
                    wait_until="domcontentloaded", timeout=90_000)
    if "login" in page.url:
        raise RuntimeError("1688 会话已失效，请重新跑 login1688.py 扫码")
    if "punish" in page.url or "x5sec" in page.url:
        raise RuntimeError("触发 1688 风控（滑块页）。等待一段时间或换网络后再试")

    # 等上传控件出现
    inputs = await page.query_selector_all("input[type=file]")
    if not inputs:
        await page.wait_for_timeout(2000)
        inputs = await page.query_selector_all("input[type=file]")
    if not inputs:
        raise RuntimeError("页面上找不到图搜上传入口（1688 可能改版了）")

    await inputs[0].set_input_files(str(img_path))
    await page.wait_for_timeout(int(wait_s * 1000))

    # 结果可能异步加载，多等几轮
    for _ in range(6):
        hrefs = await page.eval_on_selector_all(
            "a[href*='detail.1688.com/offer/']",
            "els => els.map(e => e.getAttribute('href') || '')",
        )
        ids: list[str] = []
        for raw in hrefs:
            m = OFFER_RE.search(raw)
            if m and m.group(1) not in ids:
                ids.append(m.group(1))
        if ids:
            return ids
        await page.wait_for_timeout(1500)
    return []


async def run(args: argparse.Namespace) -> int:
    targets = load_targets(args.limit, args.include_restricted)
    print(f"待处理商品 {len(targets)} 件", flush=True)
    if not targets:
        return 0

    results: list[dict[str, Any]] = []
    hit = miss = err = 0

    async with async_playwright() as pw:
        if not PROFILE.exists():
            print(f"缺少登录会话 {PROFILE}，先跑 login1688.py", flush=True)
            return 1
        ctx = await pw.chromium.launch_persistent_context(
            str(PROFILE), headless=not args.show, channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1440, "height": 900},
            locale="zh-CN", timezone_id="Asia/Shanghai")
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")

        for i, item in enumerate(targets, 1):
            tag = f"[{i}/{len(targets)}] {item['id']} {str(item['title'])[:26]}"
            try:
                img = fetch_image(item["img"])
            except Exception as exc:  # noqa: BLE001
                print(f"{tag} 图片下载失败 {type(exc).__name__}", flush=True)
                err += 1
                results.append({**item, "status": "image-error", "error": str(exc)[:120]})
                continue

            try:
                ids = await search_one(page, img)
            except RuntimeError as exc:
                print(f"{tag} 中止：{exc}", flush=True)
                results.append({**item, "status": "aborted", "error": str(exc)})
                break
            except Exception as exc:  # noqa: BLE001
                print(f"{tag} 查询异常 {type(exc).__name__}: {str(exc)[:80]}", flush=True)
                err += 1
                results.append({**item, "status": "error", "error": str(exc)[:120]})
                continue

            if not ids:
                miss += 1
                print(f"{tag} 无结果", flush=True)
                results.append({**item, "status": "no-result"})
            else:
                hit += 1
                chosen = ids[0]
                print(f"{tag} 命中 {chosen}（共 {len(ids)} 个候选）", flush=True)
                results.append({**item, "status": "hit",
                                "offerId": chosen, "candidates": ids[:10]})
                if args.write:
                    save_source(item["id"], chosen, item["title"])

            # 节流：别把账号打太快
            await asyncio.sleep(args.pace)

        await ctx.close()

    REPORT.write_text(json.dumps(results, ensure_ascii=False, indent=1),
                      encoding="utf-8")
    print(f"\n完成：命中 {hit}  无结果 {miss}  异常 {err}")
    print(f"明细：{REPORT}")
    if not args.write:
        print("（dry-run，未写库。加 --write 落库）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="1688 图搜补货源")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 件（0=全部）")
    ap.add_argument("--write", action="store_true", help="写入数据库（默认只看）")
    ap.add_argument("--show", action="store_true", help="显示浏览器窗口")
    ap.add_argument("--pace", type=float, default=3.0, help="每件间隔秒数")
    ap.add_argument("--include-restricted", action="store_true",
                    help="含精品/自营海外仓/海外仓定制款")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())