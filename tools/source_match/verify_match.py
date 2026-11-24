# -*- coding: utf-8 -*-
"""图搜 + 抓候选主图 + 图像比对，一条龙自核。

关键的实现细节（2026-11-24 实测）：
  · 图搜结果卡片的图在 DOM 里是 `img[src*=alicdn]`，尺寸 300x300 左右
  · 卡片和 offerId 的关联：卡片容器里同时有 `<a href="...offerId=xxx">` 和 `<img>`
  · 所以一次页面加载就能同时拿到 offerId 和它的主图，不用再逐件打详情页

比对：
  pHash（DCT 低频 64bit）+ HSV 直方图，两者都过阈值才算同款。
  阈值经验值：pHash 汉明 ≤12 且直方图距离 ≤0.25 → 视为同款。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import math
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests
from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
COOKIE_FILE = HERE / ".1688-cookie.txt"
CACHE = HERE / ".img-cache"
CANDS = HERE / ".candidates.json"
OUT = HERE / ".verified.json"
THUMBS = HERE / ".thumbs"
OFFER_IN_URL = re.compile(r"[?&]offerId=(\d{9,16})")


# ---------------------------------------------------------------- 图像特征
def phash(data: bytes, size: int = 32, hash_size: int = 8) -> list[int]:
    from PIL import Image

    im = Image.open(io.BytesIO(data)).convert("L").resize(
        (size, size), Image.Resampling.LANCZOS)
    px = list(im.getdata())

    def dct1(row: list[float]) -> list[float]:
        n = len(row)
        out = []
        for u in range(n):
            s = 0.0
            for x in range(n):
                s += row[x] * math.cos(math.pi * u * (2 * x + 1) / (2 * n))
            out.append(s)
        return out

    rows = [dct1([float(v) for v in px[i * size:(i + 1) * size]])
            for i in range(size)]
    cols = [dct1([rows[v][u] for v in range(size)]) for u in range(size)]
    low = []
    for u in range(hash_size):
        for v in range(hash_size):
            if u == 0 and v == 0:
                continue
            low.append(cols[v][u])
    avg = sum(low) / len(low)
    return [1 if x > avg else 0 for x in low]


def hist(data: bytes) -> list[float]:
    from PIL import Image

    im = Image.open(io.BytesIO(data)).convert("HSV").resize((64, 64))
    h = [0.0] * (16 * 4 * 4)
    for a, b, c in im.getdata():
        h[(a // 16) * 16 + (b // 64) * 4 + (c // 64)] += 1.0
    t = sum(h) or 1.0
    return [x / t for x in h]


def dist(a: list[float], b: list[float]) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / 2.0


def ham(a: list[int], b: list[int]) -> int:
    return sum(1 for x, y in zip(a, b) if x != y)


# ---------------------------------------------------------------- 数据
def is_valid_thumb(path: Path) -> bool:
    """判断一张缩略图是否像「商品照片」。

    排除：文件太小、纯色块、宽高比极端（logo 条 / 广告横幅）。
    """
    from PIL import Image

    try:
        if path.stat().st_size < 3000:
            return False
        im = Image.open(path).convert("RGB")
        w, h = im.size
        if w < 80 or h < 80:
            return False
        ratio = max(w / h, h / w)
        if ratio > 1.8:          # 横条/竖条 → 不是商品图
            return False
        # 纯色检测：缩到 16x16 后看颜色多样性
        small = im.resize((16, 16))
        colors = {px for px in small.getdata()}
        if len(colors) < 24:     # 纯色或近乎纯色
            return False
        return True
    except Exception:
        return False


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


def our_image(item: dict) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    key = CACHE / f"ours-{item['id']}.jpg"
    if key.exists() and key.stat().st_size > 2048:
        return key
    r = requests.get(item["img"], timeout=45)
    r.raise_for_status()
    key.write_bytes(r.content)
    return key


async def search_with_thumbs(page, img: Path, want: int) -> list[dict]:
    """上传 → 搜索 → 返回 [{offerId, title, thumb(本地路径)}]。"""
    await page.goto("https://air.1688.com/kapp/1688-search/pc-image-search/",
                    wait_until="domcontentloaded", timeout=120_000)
    if "login" in page.url:
        raise RuntimeError("会话失效")
    await page.wait_for_timeout(2500)
    fi = await page.query_selector("input[type=file]")
    if not fi:
        raise RuntimeError("无上传入口")
    await fi.set_input_files(str(img))
    for _ in range(12):
        await page.wait_for_timeout(1100)
        if "已上传" in await page.inner_text("body"):
            break
    try:
        await page.get_by_text("搜索图片", exact=True).click(timeout=6000)
    except Exception:
        pass
    await page.wait_for_timeout(6000)

    # 从 DOM 里一次拿 offerId + 主图 URL
    cards = await page.evaluate("""() => {
      const out = [];
      const seen = new Set();
      document.querySelectorAll('a[href*=offerId]').forEach(a => {
        const m = a.href.match(/[?&]offerId=(\\d{9,16})/);
        if (!m) return;
        const id = m[1];
        if (seen.has(id)) return;
        // 往上找包含图片的卡片容器
        let box = a;
        for (let i = 0; i < 8 && box.parentElement; i++) {
          box = box.parentElement;
          if (box.querySelector('img[src*=alicdn]')) break;
        }
        const im = box.querySelector('img[src*=alicdn]');
        let title = '';
        const tEl = box.querySelector('[title]');
        if (tEl) title = tEl.getAttribute('title') || '';
        seen.add(id);
        out.push({offerId: id, title: (title||'').slice(0,120),
                  thumb: im ? im.src : ''});
      });
      return out;
    }""")
    # 主图可能懒加载，滚一下再取一次
    if len(cards) < want:
        for _ in range(3):
            await page.mouse.wheel(0, 800)
            await page.wait_for_timeout(1500)
        more = await page.evaluate("""() => {
          const out = []; const seen = new Set();
          document.querySelectorAll('a[href*=offerId]').forEach(a => {
            const m = a.href.match(/[?&]offerId=(\\d{9,16})/);
            if (!m || seen.has(m[1])) return;
            let box = a;
            for (let i = 0; i < 8 && box.parentElement; i++) {
              box = box.parentElement;
              if (box.querySelector('img[src*=alicdn]')) break;
            }
            const im = box.querySelector('img[src*=alicdn]');
            seen.add(m[1]);
            out.push({offerId: m[1], title: '', thumb: im ? im.src : ''});
          });
          return out;
        }""")
        byid = {c["offerId"]: c for c in cards}
        for m in more:
            if m["offerId"] not in byid:
                byid[m["offerId"]] = m
        cards = list(byid.values())

    THUMBS.mkdir(parents=True, exist_ok=True)
    result = []
    for c in cards[:want]:
        oid = c["offerId"]
        local = THUMBS / f"{oid}.jpg"
        if not local.exists() or local.stat().st_size < 1024:
            src = c.get("thumb") or ""
            if src:
                try:
                    r = requests.get(src, timeout=25,
                                     headers={"User-Agent": "Mozilla/5.0",
                                              "Referer": "https://s.1688.com/"})
                    if r.status_code == 200 and len(r.content) > 1024:
                        local.write_bytes(r.content)
                except Exception:
                    pass
        # 只有「像商品图」的缩略图才算数：1688 卡片里常混进品牌 logo、
        # 店招、空白占位。实测把 logo 当主图会让比对彻底跑偏。
        if local.exists() and not is_valid_thumb(local):
            local.unlink(missing_ok=True)
        result.append({"offerId": oid, "title": c.get("title", ""),
                       "thumb": str(local) if local.exists() else ""})
    return result


async def run(args: argparse.Namespace) -> int:
    data = json.loads(Path(args.candidates).read_text(encoding="utf-8"))
    if args.limit:
        data = data[:args.limit]
    print(f"处理 {len(data)} 件", flush=True)

    cookies = parse_cookie(COOKIE_FILE.read_text(encoding="utf-8"))
    # 先算成品图特征
    for item in data:
        try:
            p = our_image(item)
            item["ours"] = {"pHash": phash(p.read_bytes()),
                            "hist": hist(p.read_bytes())}
        except Exception as exc:  # noqa: BLE001
            print(f"  #{item['id']} 成品取图失败 {type(exc).__name__}", flush=True)
            item["ours"] = None

    out: list[dict[str, Any]] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"])
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

        for i, item in enumerate(data, 1):
            tag = f"[{i}/{len(data)}] #{item['id']}"
            t0 = time.time()
            try:
                img = our_image(item)
                cands = await search_with_thumbs(page, img, args.want)
            except Exception as exc:  # noqa: BLE001
                print(f"{tag} {type(exc).__name__}: {str(exc)[:60]}", flush=True)
                out.append({**{k: item.get(k) for k in ("id", "title", "img")},
                            "status": "error", "error": str(exc)[:120]})
                continue

            scored = []
            ours = item.get("ours")
            for c in cands:
                s: dict[str, Any] = {"offerId": c["offerId"],
                                     "title": c["title"]}
                if ours and c.get("thumb") and Path(c["thumb"]).exists():
                    try:
                        tb = Path(c["thumb"]).read_bytes()
                        d = ham(ours["pHash"], phash(tb))
                        hd = dist(ours["hist"], hist(tb))
                        s["pHashDist"] = d
                        s["histDist"] = round(hd, 4)
                        # 判据以 pHash 为主：1688 缩略图普遍带水印、白边、
                        # 角标（实测同款图 p=0 但 h 到 0.22），直方图只用来
                        # 排除「连主色调都不对」的情况（如黑白鞋互认）。
                        s["same"] = d <= args.max_phash and hd <= args.max_hist
                    except Exception:
                        s["pHashDist"] = None
                scored.append(s)
            # 排：同款优先，其次 pHash 近
            scored.sort(key=lambda x: (
                0 if x.get("same") else 1,
                x.get("pHashDist") if x.get("pHashDist") is not None else 999))

            best = scored[0] if scored else None
            verdict = "none"
            if best and best.get("same"):
                verdict = "auto"
            elif best and best.get("pHashDist") is not None and best["pHashDist"] <= 16:
                verdict = "review"

            cost = time.time() - t0
            if verdict == "auto":
                print(f"{tag} 同款 {best['offerId']} "
                      f"(p={best['pHashDist']} h={best.get('histDist')}) {cost:.0f}s",
                      flush=True)
            elif verdict == "review":
                print(f"{tag} 疑似 {best['offerId']} "
                      f"(p={best.get('pHashDist')}) 需人工 {cost:.0f}s", flush=True)
            else:
                print(f"{tag} 未匹到 {cost:.0f}s", flush=True)

            out.append({**{k: item.get(k) for k in ("id", "title", "img")},
                        "status": "ok", "verdict": verdict,
                        "chosen": best["offerId"] if best and verdict != "none" else "",
                        "candidates": scored,
                        "oursFeatures": ours})
            if i % 5 == 0 or i == len(data):
                OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                               encoding="utf-8")
            await asyncio.sleep(args.pace)

        await browser.close()

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    auto = sum(1 for r in out if r.get("verdict") == "auto")
    rev = sum(1 for r in out if r.get("verdict") == "review")
    none = sum(1 for r in out if r.get("verdict") in ("none", "error"))
    print(f"\n同款 {auto} / 疑似 {rev} / 未匹 {none} / 共 {len(out)}")
    print(f"结果：{OUT}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", default=str(CANDS))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--want", type=int, default=8)
    ap.add_argument("--pace", type=float, default=1.5)
    ap.add_argument("--max-phash", type=int, default=10)
    ap.add_argument("--max-hist", type=float, default=0.45)
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())