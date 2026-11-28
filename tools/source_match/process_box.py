"""批量处理采集箱：走真实四步 API，串行防冲突。

四步（前端 AutomationPage.tsx 的真实调用顺序）：
  1) POST /automation-runs                              建批次（≤200）
  2) POST /automation-runs/{run}/items/{item}/start     标记开始
  3) POST /collect-box/{detailId}/process               处理（文字+图片）
  4) POST /automation-runs/{run}/items/{item}/process-result  回写结果

用法：
    python process_box.py <accountId> <shopId> [--limit N] [--apply]
"""
from __future__ import annotations

import json
import os
import sys
import time
import warnings

warnings.filterwarnings("ignore")

import requests

BASE = "http://127.0.0.1:8813/api/v1"
TOKEN = os.environ.get("MK_TOKEN", "")
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def api(method, path, **kw):
    r = requests.request(method, BASE + path, headers=H, timeout=kw.pop("timeout", 600), **kw)
    if r.status_code >= 400:
        try:
            body = r.json()
        except Exception:  # noqa: BLE001
            body = r.text[:300]
        raise RuntimeError(f"{r.status_code} {json.dumps(body, ensure_ascii=False)[:300]}")
    return r.json()


def box_items(account_id):
    """翻页拿全（pageSize 上限 100）。"""
    out: list[dict] = []
    page = 1
    while True:
        data = api("GET", "/collect-box", params={
            "accountId": str(account_id), "page": page, "pageSize": 100})
        batch = data.get("items") or []
        out.extend(batch)
        if not data.get("hasMore") or not batch:
            break
        page += 1
        if page > 20:
            break
    return out


def main():
    account_id = sys.argv[1]
    shop_id = sys.argv[2]
    nums = [a for a in sys.argv[3:] if a.isdigit()]
    limit = int(nums[0]) if nums else 0
    dry = "--apply" not in sys.argv

    items = box_items(account_id)
    todo = [it for it in items if str(it.get("shopId") or "") == str(shop_id)]
    print(f"采集箱 {len(items)} 件，目标店铺 {shop_id} 的 {len(todo)} 件", flush=True)
    if limit:
        todo = todo[:limit]

    if dry:
        for it in todo[:10]:
            print(f"  {it.get('detailId')}  {str(it.get('title') or '')[:40]}")
        print(f"\n共 {len(todo)} 件；加 --apply 才真跑")
        return

    # ① 建批次
    run_items = [{"detailId": str(it.get("detailId")),
                  "title": str(it.get("title") or "")} for it in todo[:200]]
    run = api("POST", "/automation-runs", json={
        "accountId": str(account_id), "shopId": str(shop_id),
        "settings": {"language": "ms", "processImages": True},
        "items": run_items})["run"]
    run_id = run["id"]
    print(f"批次 {run_id}  共 {len(run_items)} 件", flush=True)

    by_detail = {str(i.get("detailId")): i for i in run.get("items") or []}

    ok = fail = 0
    failures = []
    for idx, it in enumerate(todo[:200], 1):
        did = str(it.get("detailId"))
        item = by_detail.get(did)
        if not item:
            continue
        item_id = str(item.get("id"))
        t0 = time.time()
        try:
            # ② 标记开始
            api("POST", f"/automation-runs/{run_id}/items/{item_id}/start",
                json={}, timeout=120)
            # ③ 处理
            res = api("POST", f"/collect-box/{did}/process", json={
                "shopId": str(shop_id),
                "targetShopId": str(shop_id),
                "commonCollectBoxDetailId": it.get("commonCollectBoxDetailId"),
                "accountId": str(account_id),
                "language": "ms",
                "template": "马来",
                "save": True,
                "processImages": True,
                "mainImageCount": 9,
                "skuImageCount": 9,
                "detailImageCount": 9,
                "allowImageReview": True,
                "itemConcurrency": 4,
            })
            product = res.get("product") or {}
            # ④ 回写结果
            api("POST", f"/automation-runs/{run_id}/items/{item_id}/process-result",
                json={"status": "succeeded", "reason": "",
                      "productId": str(product.get("id") or ""),
                      "result": {"product": product}}, timeout=180)
            ok += 1
            print(f"  [{idx}/{len(todo)}] {did} ✓ {time.time()-t0:.0f}s", flush=True)
        except Exception as exc:  # noqa: BLE001
            fail += 1
            msg = str(exc)[:220]
            failures.append((did, msg))
            print(f"  [{idx}/{len(todo)}] {did} ✗ {msg}", flush=True)
            try:
                api("POST", f"/automation-runs/{run_id}/items/{item_id}/process-result",
                    json={"status": "failed", "reason": msg,
                          "productId": None, "result": {}}, timeout=120)
            except Exception:  # noqa: BLE001
                pass

    print(f"\n成功 {ok} / 失败 {fail}", flush=True)
    if failures:
        print("失败清单:")
        for did, msg in failures:
            print(f"  {did}  {msg[:150]}")


main()