"""逐条对照自动化结果页的筛选条件，找出为什么商品不显示。

list_automation_results 的查询树（warehouse_service.py:1214）：
    Product.automation_result == True
    AND (verified_ids 条件全部成立)

verified_ids 条件（_verified_automation_product_ids）：
    ① AutomationRunItem.process_status == "succeeded"
    ② Product.draft_verified_revision == Product.revision
    ③ 不存在 ImageJob(status != "ready") 指向该商品
    ④ AutomationRun.account_id == Product.account_id
    ⑤ AutomationRunItem.detail_id == Product.detail_id
    ⑥ AutomationRunItem.source_shop_id == Product.shop_id
"""
import json
import os

import psycopg

url = os.environ["AOHUANG_DATABASE_URL"].replace(
    "postgresql+psycopg://", "postgresql://")

SHOP = "19977183"
ACC = 12

with psycopg.connect(url) as conn:
    # 我这批处理的商品（账号 12 / 店铺 19977183）
    rows = conn.execute("""
        select id, detail_id, revision, draft_verified_revision,
               automation_result, account_id, shop_id,
               coalesce(automation_images::text, '{}') as ai
        from products
        where deleted_at is null and account_id = %s and shop_id = %s
        order by id desc
    """, (ACC, SHOP)).fetchall()

    print("=" * 78)
    print(f"账号 {ACC} / 店铺 {SHOP} 的商品: {len(rows)} 件")
    print("=" * 78)

    fail_counts = {"①进程未成功": 0, "②核验版本不符": 0, "③有未完成图片": 0,
                   "④账号不符": 0, "⑤detail不符": 0, "⑥店铺不符": 0,
                   "无run记录": 0}
    visible = 0
    samples = []

    for (pid, did, rev, verified, auto, acc_id, shop,
         ai_raw) in rows:
        # 查这条商品有没有成功的 run item
        items = conn.execute("""
            select i.process_status, i.product_id, i.source_shop_id,
                   r.account_id, i.detail_id, i.process_reason
            from automation_run_items i
            join automation_runs r on r.id = i.run_id
            where i.detail_id = %s
        """, (did,)).fetchall()

        if not items:
            fail_counts["无run记录"] += 1
            if len(samples) < 3:
                samples.append((pid, did, "无 automation_run_items 记录",
                                rev, verified, auto))
            continue

        ok_item = None
        reason = None
        for (st, ipid, ishop, racc, idid, ireason) in items:
            if st != "succeeded":
                reason = reason or f"①process_status={st}"
                continue
            if verified != rev:
                reason = "②draft_verified != revision"
                continue
            if racc != acc_id:
                reason = f"④账号 {racc} != {acc_id}"
                continue
            if idid != did:
                reason = "⑤detail 不符"
                continue
            if str(ishop) != str(shop):
                reason = f"⑥店铺 {ishop} != {shop}"
                continue
            ok_item = items
            break

        if ok_item is None:
            key = reason.split("=")[0][:2] if reason else "?"
            if reason and reason.startswith("①"):
                fail_counts["①进程未成功"] += 1
            elif reason and reason.startswith("②"):
                fail_counts["②核验版本不符"] += 1
            elif reason and reason.startswith("④"):
                fail_counts["④账号不符"] += 1
            elif reason and reason.startswith("⑤"):
                fail_counts["⑤detail不符"] += 1
            elif reason and reason.startswith("⑥"):
                fail_counts["⑥店铺不符"] += 1
            else:
                fail_counts["①进程未成功"] += 1
            if len(samples) < 6:
                samples.append((pid, did, reason, rev, verified, auto))
            continue

        # 再查图片任务
        bad = conn.execute("""
            select count(*) from image_jobs
            where product_id = %s and status != 'ready'
        """, (pid,)).fetchone()[0]
        if bad:
            fail_counts["③有未完成图片"] += 1
            if len(samples) < 6:
                samples.append((pid, did, f"③{bad} 个图片任务未 ready",
                                rev, verified, auto))
            continue
        if not auto:
            fail_counts["①进程未成功"] += 1
            continue

        visible += 1

    print()
    print(f"应显示在自动化结果页: {visible} 件")
    print()
    print("不显示的原因分布:")
    for k, v in fail_counts.items():
        if v:
            print(f"  {k:<14} {v} 件")
    print()
    if samples:
        print("样例:")
        for pid, did, why, rev, verified, auto in samples:
            print(f"  #{pid} {did}")
            print(f"     原因: {why}")
            print(f"     auto={auto} rev={rev} verified={verified}")

    # 直接调真实查询对比
    print()
    print("=" * 78)
    print("对照：真实 list_automation_results 返回什么")
    print("=" * 78)
    r = conn.execute("""
        select count(*) from products p
        where p.deleted_at is null
          and p.automation_result = true
          and exists (
            select 1 from automation_run_items i
            join automation_runs r on r.id = i.run_id
            where i.process_status = 'succeeded'
              and i.detail_id = p.detail_id
              and r.account_id = p.account_id
              and i.source_shop_id = p.shop_id
              and p.draft_verified_revision = p.revision
          )
          and not exists (
            select 1 from image_jobs j
            where j.product_id = p.id and j.status != 'ready'
          )
    """).fetchone()[0]
    print(f"  符合条件（全库）: {r} 件")

    # 自动化结果页总数
    tot = conn.execute("""
        select count(*) from products
        where deleted_at is null and automation_result = true
    """).fetchone()[0]
    print(f"  automation_result=true 全库: {tot} 件")