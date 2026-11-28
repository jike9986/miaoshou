"""按界面真实路径测这批 10 件：走 /collect-box/{id}/process 接口。

之前一直直接调 save_from_collect_box()，绕过了接口层的并发控制、
图片队列、标记逻辑 —— 所以线上失败在本地测不出来。

这次用 httpx 打真实 HTTP 接口，跟桌面端完全一致，并全程采样内存。
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
CONCURRENCY = 5


def mem_mb() -> float:
    """读当前进程 RSS。"""
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def sys_mem():
    """整机可用内存 MB。"""
    try:
        with open("/proc/meminfo") as fh:
            data = {}
            for line in fh:
                k, v = line.split(":", 1)
                data[k] = int(v.split()[0]) / 1024
            return data.get("MemAvailable", 0), data.get("MemTotal", 0)
    except Exception:  # noqa: BLE001
        return 0, 0


def process_one(client: httpx.Client, detail_id: str) -> dict:
    body = {
        "shopId": SHOP,
        "targetShopId": SHOP,
        "accountId": "12",
        "language": "ms",
        "template": None,
        "save": True,
        "processImages": True,
        "mainImageCount": 9,
        "skuImageCount": 9,
        "detailImageCount": 9,
        "allowImageReview": True,
        "itemConcurrency": CONCURRENCY,
        "commonCollectBoxDetailId": None,
    }
    t0 = time.time()
    try:
        r = client.post(f"{BASE}/collect-box/{detail_id}/process", json=body, timeout=900)
        elapsed = time.time() - t0
        if r.status_code >= 400:
            try:
                detail = r.json()
            except Exception:  # noqa: BLE001
                detail = r.text[:200]
            return {"id": detail_id, "ok": False, "status": r.status_code,
                    "elapsed": elapsed, "detail": detail}
        data = r.json()
        return {"id": detail_id, "ok": True, "status": r.status_code,
                "elapsed": elapsed, "verified": data.get("draftVerified"),
                "data": data}
    except Exception as ex:  # noqa: BLE001
        return {"id": detail_id, "ok": False, "status": None,
                "elapsed": time.time() - t0,
                "detail": f"{type(ex).__name__}: {str(ex)[:200]}"}


def main():
    avail0, total = sys_mem()
    print(f"起始内存: 可用 {avail0:.0f} / 总 {total:.0f} MB\n")

    results = []
    with httpx.Client(headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        # 按并发 5 分组，模拟界面行为
        for i in range(0, len(TARGETS), CONCURRENCY):
            group = TARGETS[i:i + CONCURRENCY]
            print(f"--- 第 {i//CONCURRENCY + 1} 组（{len(group)} 件）---")
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=len(group)) as pool:
                chunk = list(pool.map(lambda d: process_one(client, d), group))
            results.extend(chunk)
            for c in chunk:
                avail, _ = sys_mem()
                mark = "OK " if c["ok"] else "FAIL"
                print(f"  {mark} {c['id']}  {c['elapsed']:.0f}s  "
                      f"verified={c.get('verified')}  可用内存={avail:.0f}MB")
                if not c["ok"]:
                    print(f"        {c['detail']}")
            print()

    avail1, _ = sys_mem()
    print("=" * 70)
    ok = sum(1 for r in results if r["ok"] and r.get("verified"))
    print(f"结果: {ok}/{len(results)} 通过")
    print(f"内存: {avail0:.0f} MB → {avail1:.0f} MB")


if __name__ == "__main__":
    main()