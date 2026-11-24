# -*- coding: utf-8 -*-
"""批量图搜 + 候选标题采集：给每件商品留一份「人可复核」的候选清单。

为什么不能只取第一条：图搜按相关性排，同品类但不同款的现象很常见
（实测 #27 成品是"多用途运动鞋"，第一条候选是"纯白色板鞋"）。
所以这里把前 N 个候选的 1688 标题一起抓下来，人工看一眼就能挑对。

用法：
    # 只搜索、不写库，产出带标题的候选表
    python tools/source_match/collect_candidates.py --limit 5

    # 全量 171 件
    python tools/source_match/collect_candidates.py
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
OUT = HERE / ".candidates.json"
RESTRICTED = ("精品", "自营海外仓", "海外仓定制款")
OFFER_IN_URL = re.compile(r"[?&]offerId=(\d{9,16})")
OFFER_IN_JSON = re.compile(r'"offerId"\s*:\s*"?(\d{9,16})"?')
# 结果卡片里的标题（1688 卡片标题在 title 属性或文本里）
TITLE_ATTR = re.compile(r'title="([^"]{6,120})"')


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


def load_from_json(path: Path, limit: int) -> list[dict[str, Any]]:
    """从清单文件读目标（本机不连库时用）。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    return data[:limit] if limit else data


def load_targets(limit: int, manifest: str = "") -> list[dict[str, Any]]:
    if manifest:
        return load_from_json(Path(manifest), limit)
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
              and coalesce(group_name,'') <> all(:r)
            order by id asc
        """
        params: dict[str, Any] = {"r": list(RESTRICTED)}
        if limit:
            sql += " limit :lim"
            params["lim"] = limit
        return [{"id": r[0], "title": r[1], "detailId": r[2],
                 "group": r[3], "img": r[4]}
                for r in db.execute(text(sql), params).fetchall()]
    finally:
        db.close()


async def collect(page, img: Path, want: int) -> list[dict]:
    """上传 → 搜索 → 返回 [{offerId, title}]（按页面顺序）。"""
    await page.goto("https://air.1688.com/kapp/1688-search/pc-image-search/",
                    wait_until="domcontentloaded", timeout=120_000)
    if "login" in page.url:
        raise RuntimeError("会话失效")
    await page.wait_for_timeout(3000)
    fi = await page.query_selector("input[type=file]")
    if not fi:
        raise RuntimeError("无上传入口")
    await fi.set_input_files(str(img))
    for _ in range(12):
        await page.wait_for_timeout(1200)
        if "已上传" in await page.inner_text("body"):
            break
    try:
        await page.get_by_text("搜索图片", exact=True).click(timeout=6000)
    except Exception:
        pass
    await page.wait_for_timeout(6000)

    # 卡片：抓 offerId + 同卡片的标题
    cards = await page.evaluate("""() => {
      const out = [];
      // 所有带 offerId 的元素，往上找卡片容器再取标题
      const seen = new Set();
      document.querySelectorAll('a[href*=offerId]').forEach(a => {
        const m = a.href.match(/[?&]offerId=(\\d{9,16})/);
        if (!m) return;
        const id = m[1];
        if (seen.has(id)) return;
        let box = a;
        for (let i = 0; i < 8 && box.parentElement; i++) box = box.parentElement;
        let t = '';
        const tEl = box.querySelector('[title]') || box.querySelector('h3,h4,span');
        if (tEl) t = tEl.getAttribute('title') || tEl.textContent || '';
        seen.add(id);
        out.push({offerId: id, title: (t || '').trim().slice(0, 120)});
      });
      return out;
    }""")
    if cards:
        return cards[:want]

    # 兜底：只有 id 没有标题
    ids = await page.eval_on_selector_all(
        "a[href*=offerId]", "els => els.map(e => e.href)")
    out, seen = [], set()
    for h in ids:
        m = OFFER_IN_URL.search(h)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            out.append({"offerId": m.group(1), "title": ""})
    return out[:want]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--want", type=int, default=8, help="每件留几个候选")
    ap.add_argument("--manifest", default="",
                    help="从 JSON 清单读目标（不连库）")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--pace", type=float, default=2.0)
    args = ap.parse_args()

    targets = load_targets(args.limit, args.manifest)
    print(f"待处理 {len(targets)} 件，每件保留 {args.want} 个候选", flush=True)
    if not targets:
        return 0

    cookies = parse_cookie(COOKIE_FILE.read_text(encoding="utf-8"))
    results: list[dict[str, Any]] = []

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

        for i, item in enumerate(targets, 1):
            tag = f"[{i}/{len(targets)}] #{item['id']}"
            t0 = time.time()
            try:
                img = prepare_image(item["img"])
                cards = await collect(page, img, args.want)
            except Exception as exc:  # noqa: BLE001
                print(f"{tag} {type(exc).__name__}: {str(exc)[:70]}", flush=True)
                results.append({**item, "status": "error",
                                "error": str(exc)[:120]})
                continue
            cost = time.time() - t0
            if cards:
                print(f"{tag} {len(cards)} 候选 ({cost:.0f}s) 首选 "
                      f"{cards[0]['offerId']} {cards[0]['title'][:30]}", flush=True)
            else:
                print(f"{tag} 无候选 ({cost:.0f}s)", flush=True)
            results.append({**item,
                            "status": "ok" if cards else "no-result",
                            "candidates": cards})
            # 每 10 件落一次盘，跑长任务中途断了不白干
            if i % 10 == 0 or i == len(targets):
                OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1),
                               encoding="utf-8")
            await asyncio.sleep(args.pace)

        await browser.close()

    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    ok = sum(1 for r in results if r["status"] == "ok")
    print(f"\n有候选 {ok} / 共 {len(results)}")
    print(f"结果：{OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))