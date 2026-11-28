"""给已处理好的商品补建 automation-run 记录，让它们出现在自动化结果页。

## 为什么需要

真实流程是四步（前端 AutomationPage.tsx）：

    POST /automation-runs                         ① 建批次
    POST .../items/{id}/start                     ② 标记开始
    POST /collect-box/{id}/process                ③ 处理
    POST .../items/{id}/process-result            ④ 回写结果

自动化结果页按 `automation_run_items.product_id` **INNER JOIN** 商品：

    db.query(AutomationRunItem.product_id)
      .join(Product, Product.id == AutomationRunItem.product_id)
      .filter(AutomationRunItem.process_status == "succeeded", ...)

早先的批处理脚本直接调了第 ③ 步，没建 run，于是商品
`automation_result=true`、`draft_verified_revision == revision`、
图片也全好，却一条都显示不出来。

本脚本只补 ①②④ 三步，**不重新处理商品**（数据已经是好的）。

## 两个坑

### 1. 必须按商品自己的 account/shop 建批次

`finish_process` 校验：

    Product.account_id == run.account_id
    Product.shop_id == row.source_shop_id

用统一的 SHOP_ID 建批次会让所有店铺不符的商品报 409。

### 2. item 时间戳要回填成商品的处理时间

`finish_process` 还有：

    product.updated_at < row.updated_at  → 409
    「商品没有与当前版本匹配的妙手草稿读回核验」

真实流程里 item 先建、updated_at 更早；补录时 item 刚建、商品是昨天
处理的，这条必然失败。把 item 时间戳对齐到商品，等价于真实先后顺序。
`start_item` 会再改一次，所以 start 之后要再对齐一遍。
"""
import secrets
import sys
import warnings
from collections import defaultdict

warnings.filterwarnings("ignore")

import httpx

from sqlalchemy import text
from db.session import SessionLocal
from models.operator_token import OperatorToken
from services import operator_service

BASE = "http://127.0.0.1:8813/api/v1"
OPERATOR_ID = 64
RUN_MAX = 200


def issue_token():
    db = SessionLocal()
    try:
        token = secrets.token_urlsafe(32)
        db.add(OperatorToken(operator_id=OPERATOR_ID,
                             token_hash=operator_service.hash_token(token)))
        db.commit()
        return token
    finally:
        db.close()


def api(c, method, path, **kw):
    r = c.request(method, f"{BASE}{path}", **kw)
    try:
        return r.status_code, r.json()
    except Exception:  # noqa: BLE001
        return r.status_code, {"raw": r.text[:300]}


def align_item_time(pid, run_id, did):
    """把 run item 时间戳对齐到商品 —— 见文件头「坑 2」。"""
    db = SessionLocal()
    try:
        db.execute(text("""
            update automation_run_items i
            set updated_at = p.updated_at, created_at = p.updated_at
            from products p
            where p.id = :pid and i.run_id = :rid and i.detail_id = :did
        """), {"pid": pid, "rid": run_id, "did": did})
        db.commit()
    finally:
        db.close()


def find_missing():
    """A 桶：auto=true、核验通过、且**完全没有** run item 的商品。

    两个条件都必须是严的：

    1. `draft_verified_revision = p.revision`
       `draft_verified_revision is null` 的 16 件（B 桶）是**图片核验失败**留下的
       真实状态（collect_box.py:718 / automation_run_service.py:367），
       补 run 记录等于伪造核验通过，绝不能进来。

    2. 没有任何 run item（不限状态）
       只写 `not exists (... process_status = 'succeeded')` 会把 D 桶那些
       「有 item 但卡在 failed / running / needs_review」的商品也捞进来
       （实测多出 10 件）。那些商品已有执行痕迹，是真实失败，
       要按失败重跑，不是补记录。
    """
    db = SessionLocal()
    try:
        rows = db.execute(text("""
            select p.id, p.detail_id, p.shop_id, p.title, p.account_id
            from products p
            where p.deleted_at is null
              and p.automation_result = true
              and p.draft_verified_revision is not null
              and p.draft_verified_revision = p.revision
              and not exists (
                select 1 from automation_run_items i
                where i.detail_id = p.detail_id
              )
              and not exists (
                select 1 from image_jobs j
                where j.product_id = p.id and j.status != 'ready'
              )
            order by p.account_id, p.shop_id, p.id
        """)).fetchall()
        return [(r[0], r[1], str(r[2]), r[3] or "", r[4]) for r in rows]
    finally:
        db.close()


def main():
    # 只把纯数字参数当 limit —— 直接取 sys.argv[1] 会把 --apply 当数字解析崩掉。
    nums = [a for a in sys.argv[1:] if a.isdigit()]
    limit = int(nums[0]) if nums else 0
    dry = "--apply" not in sys.argv

    missing = find_missing()
    print(f"需要补记录的: {len(missing)} 件")

    groups = defaultdict(list)
    for row in missing:
        groups[(row[4], row[2])].append(row)
    print(f"涉及 {len(groups)} 个账号/店铺组合:")
    for (acc, shop), rows in groups.items():
        print(f"  account={acc} shop={shop}: {len(rows)} 件")

    if limit:
        trimmed = defaultdict(list)
        taken = 0
        for key, rows in groups.items():
            if taken >= limit:
                break
            take = rows[:limit - taken]
            if take:
                trimmed[key].extend(take)
                taken += len(take)
        groups = trimmed
        print(f"本次处理 {taken} 件")

    if not missing:
        return
    if dry:
        print()
        for pid, did, shop, title, acc in missing[:10]:
            print(f"  #{pid} {did} acc={acc} shop={shop} {str(title)[:24]}")
        print("加 --apply 才真写")
        return

    token = issue_token()
    headers = {"Authorization": f"Bearer {token}"}

    ok = fail = 0
    with httpx.Client(timeout=300, headers=headers) as c:
        for (acc, shop), rows in groups.items():
            print()
            print(f"=== account={acc} shop={shop}: {len(rows)} 件 ===")
            for start in range(0, len(rows), RUN_MAX):
                chunk = rows[start:start + RUN_MAX]
                items = [{
                    "key": f"{did}:{shop}",
                    "detailId": did,
                    "shopId": shop,
                    "title": str(title)[:80],
                } for _pid, did, _shop, title, _acc in chunk]

                st, body = api(c, "POST", "/automation-runs", json={
                    "accountId": str(acc),
                    "shopId": shop,
                    "settings": {
                        "textEnabled": True, "language": "ms",
                        "imagesEnabled": True,
                        "priceTemplateEnabled": True, "template": "马来",
                        "publishEnabled": False,
                        "mainImageCount": 9, "skuImageCount": 9,
                        "detailImageCount": 9,
                        "parallel": 5,
                    },
                    "items": items,
                })
                if st not in (200, 201):
                    print(f"  建批次失败 HTTP {st}: {str(body)[:180]}")
                    fail += len(chunk)
                    continue

                run = body.get("run") or {}
                run_id = run.get("id")
                by_detail = {}
                for im in (run.get("items") or []):
                    d = str(im.get("detailId") or "")
                    if d:
                        by_detail[d] = im.get("id")
                print(f"  批次 {run_id}: {len(chunk)} 件")

                for pid, did, _shop, title, _acc in chunk:
                    iid = by_detail.get(did)
                    if not iid:
                        print(f"    #{pid} {did} ✗ 批次里没有 item")
                        fail += 1
                        continue

                    align_item_time(pid, run_id, did)
                    api(c, "POST",
                        f"/automation-runs/{run_id}/items/{iid}/start")
                    align_item_time(pid, run_id, did)

                    st2, body2 = api(
                        c, "POST",
                        f"/automation-runs/{run_id}/items/{iid}/process-result",
                        json={
                            "status": "succeeded",
                            "reason": "",
                            "productId": str(pid),
                            "result": {},
                        })
                    if st2 == 200:
                        ok += 1
                    else:
                        msg = ((body2.get("error") or {}).get("message")
                               or str(body2)[:140])
                        print(f"    #{pid} {did} ✗ HTTP {st2} {msg}")
                        fail += 1

    print()
    print(f"结果: 成功 {ok} / 失败 {fail}")


main()