# -*- coding: utf-8 -*-
"""批量：拿成品主图去 1688 图搜，把匹配到的 offer id 写回货源链接。

## 已打通的完整链路（2026-11-24 实测）

  1. 图片必须缩到 600px 以内 —— 原图 800x800 上传后页面停在「(0/6)」且缩略图
     是灰块；缩到 600x600/84KB 立刻出现「已上传1张图片」+ 预览图。
  2. 上传后必须点「搜索图片」，不点就一直是「哎呦喂，这里空空如也」。
  3. 结果不是 `<a href="detail.1688.com/offer/...">`，要从两处取：
       · DOM 里的 `offerId=` 参数（如收藏/联系卖家链接）
       · h5api 的 relationrecommend 响应里 `"offerId":"..."`
     两者一致，取前 N 个按页面顺序（1688 相关性排序，第一条最像）。

## 用法

    # 先跑 5 件看看准确率（不写库）
    python tools/source_match/batch_match.py --limit 5

    # 确认后写库
    python tools/source_match/batch_match.py --limit 5 --write

    # 全量 171 件（约 25-40 分钟）
    python tools/source_match/batch_match.py --write
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests
from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
SERVER = (HERE / "../../server").resolve()
COOKIE_FILE = HERE / ".1688-cookie.txt"
CACHE = HERE / ".img-cache"
REPORT = HERE / ".batch-report.json"
OFFER_IN_URL = re.compile(r"[?&]offerId=(\d{9,16})")
OFFER_IN_JSON = re.compile(r'"offerId"\s*:\s*"?(\d{9,16})"?')
RESTRICTED = ("精品", "自营海外仓", "海外仓定制款")


# ---------------------------------------------------------------- 数据
def load_targets(limit: int, include_restricted: bool,
                 only_ids: list[int] | None = None) -> list[dict[str, Any]]:
    sys.path.insert(0, str(SERVER))
    import warnings

    warnings.filterwarnings("ignore")
    from sqlalchemy import text

    from db.session import SessionLocal

    db = SessionLocal()
    try:
        sql = """
            select id, title, detail_id, group_name, images->>0 as img
            from products
            where deleted_at is null
              and automation_result is false
              and coalesce(payload->>'sourceUrl','') = ''
              and json_typeof(images) = 'array'
              and json_array_length(images) > 0
        """
        params: dict[str, Any] = {}
        if not include_restricted:
            sql += " and coalesce(group_name,'') <> all(:r)"
            params["r"] = list(RESTRICTED)
        if only_ids:
            sql += " and id = any(:ids)"
            params["ids"] = only_ids
        sql += " order by id asc"
        if limit:
            sql += " limit :lim"
            params["lim"] = limit
        return [{"id": r[0], "title": r[1], "detailId": r[2],
                 "group": r[3], "img": r[4]}
                for r in db.execute(text(sql), params).fetchall()]
    finally:
        db.close()


def save_source(product_id: int, offer_id: str,
                candidates: list[str] | None = None) -> None:
    sys.path.insert(0, str(SERVER))
    import warnings

    warnings.filterwarnings("ignore")
    from sqlalchemy import text

    from db.session import SessionLocal

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
        # 规则 §4：自动化结果商品同步 draft_verified_revision
        db.execute(text("""
            update products set draft_verified_revision = revision
            where id = :pid and automation_result is true
              and draft_verified_revision is not null
        """), {"pid": product_id})
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------- 图搜
def parse_cookie(raw: str) -> list[dict]:
    raw = raw.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    out = []
    for pair in raw.split(";"):
        if "=" not in pair:
            continue
        n, v = pair.split("=", 1)
        out.append({"name": n.strip(), "value": v.strip(),
                    "domain": ".1688.com", "path": "/"})
    return out


def prepare_image(url: str) -> Path:
    """下载并缩到 600px。**必须缩**，原图尺寸大 1688 上传会静默失败。"""
    from PIL import Image

    CACHE.mkdir(parents=True, exist_ok=True)
    base = url.rstrip("/").rsplit("/", 1)[-1].split("?")[0] or "img.jpg"
    dest = CACHE / (base.rsplit(".", 1)[0] + "-small.jpg")
    if dest.exists() and dest.stat().st_size > 2048:
        return dest
    resp = requests.get(url, timeout=45)
    resp.raise_for_status()
    im = Image.open(io.BytesIO(resp.content)).convert("RGB")
    im.thumbnail((600, 600))
    im.save(dest, "JPEG", quality=90)
    return dest


class Session:
    """复用一个浏览器上下文，跑完全部商品。"""

    def __init__(self, page: Any) -> None:
        self.page = page

    async def search(self, img: Path, *, want: int = 5) -> list[str]:
        page = self.page
        await page.goto("https://air.1688.com/kapp/1688-search/pc-image-search/",
                        wait_until="domcontentloaded", timeout=120_000)
        if "login" in page.url:
            raise RuntimeError("1688 会话失效，重新取 cookie 到 .1688-cookie.txt")
        await page.wait_for_timeout(3000)

        fi = await page.query_selector("input[type=file]")
        if not fi:
            raise RuntimeError("找不到上传入口")
        await fi.set_input_files(str(img))

        # 等上传生效
        ok = False
        for _ in range(12):
            await page.wait_for_timeout(1200)
            body = await page.inner_text("body")
            if "已上传" in body or "搜索图片" in body:
                ok = True
                break
        if not ok:
            raise RuntimeError("上传未生效")

        # 点「搜索图片」
        clicked = False
        try:
            await page.get_by_text("搜索图片", exact=True).click(timeout=6000)
            clicked = True
        except Exception:
            for sel in ("[class*=search-image]", "button:has-text('搜索图片')"):
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.click()
                        clicked = True
                        break
                except Exception:
                    continue
        if not clicked:
            raise RuntimeError("找不到「搜索图片」按钮")

        # 等结果
        ids: list[str] = []
        for _ in range(18):
            await page.wait_for_timeout(1500)
            ids = await self._collect()
            if len(ids) >= want:
                break
            await page.mouse.wheel(0, 700)
        return ids[:40]

    async def _collect(self) -> list[str]:
        page = self.page
        hrefs = await page.eval_on_selector_all(
            "a[href]", "els => els.map(e => e.href)")
        ids: list[str] = []
        for h in hrefs:
            m = OFFER_IN_URL.search(h)
            if m and m.group(1) not in ids:
                ids.append(m.group(1))
        # 从页面内嵌 JSON 补
        try:
            html = await page.content()
            for m in OFFER_IN_JSON.finditer(html):
                if m.group(1) not in ids:
                    ids.append(m.group(1))
        except Exception:
            pass
        return ids


async def run(args: argparse.Namespace) -> int:
    only = [int(x) for x in args.ids.split(",") if x.strip()] if args.ids else None
    targets = load_targets(args.limit, args.include_restricted, only)
    print(f"待处理 {len(targets)} 件", flush=True)
    if not targets:
        return 0

    cookies = parse_cookie(COOKIE_FILE.read_text(encoding="utf-8"))
    results: list[dict[str, Any]] = []
    hit = miss = err = 0

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=not args.show,
            args=["--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"),
            locale="zh-CN", timezone_id="Asia/Shanghai",
            viewport={"width": 1440, "height": 900})
        await ctx.add_cookies(cookies)
        page = await ctx.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        sess = Session(page)

        for i, item in enumerate(targets, 1):
            tag = f"[{i}/{len(targets)}] #{item['id']} {str(item['title'])[:24]}"
            try:
                img = prepare_image(item["img"])
            except Exception as exc:  # noqa: BLE001
                print(f"{tag} 取图失败 {type(exc).__name__}", flush=True)
                err += 1
                results.append({**item, "status": "image-error"})
                continue

            t0 = time.time()
            try:
                ids = await sess.search(img)
            except RuntimeError as exc:
                print(f"{tag} 中止：{exc}", flush=True)
                results.append({**item, "status": "aborted", "error": str(exc)})
                break
            except Exception as exc:  # noqa: BLE001
                print(f"{tag} 异常 {type(exc).__name__}: {str(exc)[:70]}", flush=True)
                err += 1
                results.append({**item, "status": "error", "error": str(exc)[:120]})
                continue

            cost = time.time() - t0
            if not ids:
                miss += 1
                print(f"{tag} 无结果 ({cost:.0f}s)", flush=True)
                results.append({**item, "status": "no-result"})
            else:
                hit += 1
                chosen = ids[0]
                print(f"{tag} -> {chosen}  (候选 {len(ids)}, {cost:.0f}s)", flush=True)
                results.append({**item, "status": "hit", "offerId": chosen,
                                "candidates": ids[:8]})
                if args.write:
                    save_source(item["id"], chosen)

            if args.pace:
                await asyncio.sleep(args.pace)

        await browser.close()

    REPORT.write_text(json.dumps(results, ensure_ascii=False, indent=1),
                      encoding="utf-8")
    print(f"\n命中 {hit} / 无结果 {miss} / 异常 {err} / 共 {len(targets)}")
    print(f"明细：{REPORT}")
    if not args.write:
        print("（未写库。加 --write 落库）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="1688 图搜批量补货源")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--ids", default="", help="只处理这些商品 id，逗号分隔")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--pace", type=float, default=2.0)
    ap.add_argument("--include-restricted", action="store_true")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())