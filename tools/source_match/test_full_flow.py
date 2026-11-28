"""按界面完整 4 步真实流程测。

之前一直漏掉第 1、2、4 步，所以线上失败本地测不出来：
  ① POST /automation-runs                   创建批次
  ② POST /automation-runs/{run}/items/{id}/start   标记该项开始
  ③ POST /collect-box/{detailId}/process    处理
  ④ POST /automation-runs/{run}/items/{id}/process-result  回写结果
"""
import os
import time
import warnings

import httpx

warnings.filterwarnings("ignore")

TARGETS = [
    "3562906453", "3562906426", "3562906431", "3562906432", "3562906444",
    "3562906423", "3562906425", "3562906420", "3562906418", "3562906415",
]
BASE = "http://127.0.0.1:8813/api/v1"
TOKEN = os.environ.get("AOHUANG_TEST_TOKEN", "script-4bfc132a7d57feaf966f20b1643c280f")
SHOP = "19977183"
ACCOUNT = "12"


def avail_mb():
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def main():
    client = httpx.Client(headers={"Authorization": f"Bearer {TOKEN}"}, timeout=900)

    # ① 创建批次：前端按「选中商品」传给后端，商品信息从采集箱列表拿
    print("① 创建自动化批次")
    r = client.post(f"{BASE}/automation-runs", json={
        "accountId": ACCOUNT, "shopId": SHOP,
        "items": [{"detailId": d, "shopId": SHOP} for d in TARGETS],
        "settings": {"language": "ms", "processImages": True,
                     "mainImageCount": 9, "skuImageCount": 9,
                     "detailImageCount": 9, "itemConcurrency": 5},
    })
    print(f"   status={r.status_code}")
    if r.status_code >= 400:
        print(f"   失败: {r.text[:400]}")
        return
    run = (r.json() or {}).get("run") or {}
    run_id = run.get("id")
    items = run.get("items") or []
    print(f"   runId={run_id}  items={len(items)}")

    # 建立 detailId -> runItemId 映射
    item_map = {}
    for it in items:
        did = str(it.get("detailId") or it.get("key") or "")
        iid = it.get("id") or it.get("itemId")
        if did and iid is not None:
            item_map[did] = iid
    print(f"   映射 {len(item_map)} 件")

    results = []
    from concurrent.futures import ThreadPoolExecutor

    def process_one(detail_id: str) -> dict:
        item_id = item_map.get(detail_id)
        t0 = time.time()
        if item_id is None:
            return {"id": detail_id, "ok": False, "err": "无 run item"}

        # ② start
        rs = client.post(f"{BASE}/automation-runs/{run_id}/items/{item_id}/start")
        if rs.status_code >= 400:
            return {"id": detail_id, "ok": False,
                    "err": f"start {rs.status_code}: {rs.text[:160]}"}

        # ③ process
        body = {
            "shopId": SHOP, "targetShopId": SHOP, "accountId": ACCOUNT,
            "language": "ms", "template": None, "save": True,
            "processImages": True, "mainImageCount": 9, "skuImageCount": 9,
            "detailImageCount": 9, "allowImageReview": True,
            "itemConcurrency": 5, "commonCollectBoxDetailId": None,
        }
        rp = client.post(f"{BASE}/collect-box/{detail_id}/process",
                         json=body, timeout=900)
        if rp.status_code >= 400:
            err = rp.text[:200]
            client.post(f"{BASE}/automation-runs/{run_id}/items/{item_id}/process-result",
                        json={"status": "failed", "reason": f"HTTP {rp.status_code}: {err}"})
            return {"id": detail_id, "ok": False, "err": err,
                    "elapsed": time.time() - t0}

        data = rp.json()
        # ④ 回写（与前端一致）
        client.post(f"{BASE}/automation-runs/{run_id}/items/{item_id}/process-result",
                    json={"status": "succeeded", "result": data})
        return {"id": detail_id, "ok": True, "elapsed": time.time() - t0,
                "verified": data.get("draftVerified")}

    print(f"\n②③④ 处理（并发 5，当前可用内存 {avail_mb():.0f}MB）")
    for i in range(0, len(TARGETS), 5):
        group = TARGETS[i:i + 5]
        with ThreadPoolExecutor(max_workers=len(group)) as pool:
            chunk = list(pool.map(process_one, group))
        results.extend(chunk)
        for c in chunk:
            mark = "OK " if c["ok"] else "FAIL"
            print(f"  {mark} {c['id']}  {c.get('elapsed', 0):.0f}s  "
                  f"verified={c.get('verified')}  可用={avail_mb():.0f}MB")
            if not c["ok"]:
                print(f"       {c['err']}")

    print("\n" + "=" * 68)
    ok = sum(1 for r in results if r["ok"])
    print(f"结果: {ok}/{len(results)} 通过")
    client.close()


if __name__ == "__main__":
    main()