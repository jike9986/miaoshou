"""权威对账：把 automation_result=true 的每件商品精确归到唯一一个「修复动作」。

前面的 diag_all_reasons / diag_split_verified / backfill_runs 三个脚本口径不同：

  · diag_all_reasons  : 看「有没有任何 run item」
  · diag_split_verified: 看「有没有任何 run item」+ verified 状态
  · backfill_runs      : 看「有没有 succeeded 的 run item」+ verified == revision
                         ← 它比前两个更严，所以 88 ≠ 78

口径不统一会修漏一批。这里按 list_automation_results 的真实六个条件逐条判定，
给出**每件商品唯一归属**的修复动作：

  A. 无任何 run item，verified==revision      → 补 run item（①④⑤⑥ 一起满足）
  B. 无任何 run item，verified is NULL        → 先补 verified，再补 run item
  C. 无任何 run item，verified != revision    → 先修 verified，再补 run item
  D. 有 run item 但都非 succeeded             → 改那条 item 的 process_status
  E. 有 succeeded item 但账号/店铺/detail 不符 → 改 item 的 source_shop_id 等
  F. 有 succeeded item 全成立，但有未 ready 图片 → 等图片跑完
  G. ✓ 已可见

输出机器可读的 JSON，供修复脚本直接消费。
"""
import json
import os

import psycopg

url = os.environ["AOHUANG_DATABASE_URL"].replace(
    "postgresql+psycopg://", "postgresql://")

with psycopg.connect(url) as conn:
    prods = conn.execute("""
        select p.id, p.detail_id, p.shop_id, p.account_id,
               p.revision, p.draft_verified_revision
        from products p
        where p.deleted_at is null and p.automation_result = true
        order by p.account_id, p.shop_id, p.id
    """).fetchall()

    bucket = {}
    for pid, did, shop, acc, rev, ver in prods:
        items = conn.execute("""
            select i.id, i.process_status, i.detail_id, i.source_shop_id,
                   r.account_id
            from automation_run_items i
            join automation_runs r on r.id = i.run_id
            where i.detail_id = %s
            order by i.id
        """, (did,)).fetchall()

        rec = {"pid": pid, "detailId": did, "shopId": str(shop),
               "accountId": acc, "revision": rev, "verified": ver,
               "items": [{"itemId": r[0], "status": r[1], "detailId": r[2],
                          "sourceShopId": str(r[3]), "runAccountId": r[4]}
                         for r in items]}

        if not items:
            if ver is None:
                bucket.setdefault("B_无item_verified为NULL", []).append(rec)
            elif ver != rev:
                bucket.setdefault("C_无item_verified不符", []).append(rec)
            else:
                bucket.setdefault("A_无item_可直接补", []).append(rec)
            continue

        succeeded = [i for i in rec["items"] if i["status"] == "succeeded"]
        if not succeeded:
            bucket.setdefault("D_有item但无succeeded", []).append(rec)
            continue

        matched = [i for i in succeeded
                   if int(i["runAccountId"]) == int(acc)
                   and i["sourceShopId"] == str(shop)
                   and i["detailId"] == did]
        if not matched:
            bucket.setdefault("E_succeeded但账号店铺detail不符", []).append(rec)
            continue

        bad = conn.execute("""
            select count(*) from image_jobs
            where product_id = %s and status != 'ready'
        """, (pid,)).fetchone()[0]
        rec["badImageJobs"] = bad
        if bad:
            bucket.setdefault("F_有未ready图片任务", []).append(rec)
            continue
        if ver != rev:
            bucket.setdefault("C2_有成功item但verified不符", []).append(rec)
            continue
        bucket.setdefault("G_已可见", []).append(rec)

    print("=" * 78)
    print(f"automation_result=true 且未删除: {len(prods)} 件")
    print("=" * 78)
    total = 0
    for k in sorted(bucket):
        n = len(bucket[k])
        total += n
        print(f"  {k:<34} {n:>4} 件")
    print(f"  {'合计':<34} {total:>4} 件")

    print()
    print("=" * 78)
    print("每个桶的账号/店铺分布")
    print("=" * 78)
    for k in sorted(bucket):
        by = {}
        for r in bucket[k]:
            by.setdefault((r["accountId"], r["shopId"]), 0)
            by[(r["accountId"], r["shopId"])] += 1
        detail = " ".join(f"acc{a}/{s}:{n}" for (a, s), n in sorted(by.items()))
        print(f"  {k}")
        print(f"      {detail}")

    print()
    print("=" * 78)
    print("D 桶明细（有 item 但都非 succeeded）")
    print("=" * 78)
    for r in bucket.get("D_有item但无succeeded", []):
        sts = ",".join(i["status"] for i in r["items"])
        print(f"  #{r['pid']:<5} {r['detailId']:<12} acc={r['accountId']} "
              f"rev={r['revision']} ver={r['verified']} items=[{sts}]")

    print()
    print("=" * 78)
    print("E 桶明细")
    print("=" * 78)
    for r in bucket.get("E_succeeded但账号店铺detail不符", []):
        for i in r["items"]:
            if i["status"] == "succeeded":
                print(f"  #{r['pid']:<5} {r['detailId']:<12} "
                      f"product acc={r['accountId']} shop={r['shopId']} | "
                      f"item#{i['itemId']} runacc={i['runAccountId']} "
                      f"shop={i['sourceShopId']} detail={i['detailId']}")

    with open("/tmp/fix_plan.json", "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in bucket.items()}, f, ensure_ascii=False, indent=1)
    print()
    print("修复计划已写 /tmp/fix_plan.json")
