"""全库排查：为什么 automation_result=true 的商品一件都不出现在自动化结果页。

list_automation_results 的完整条件（warehouse_service.py:1214-1236）：

    Product.automation_result == True                  (a)
    AND Product.id IN (_verified_automation_product_ids)

_verified_automation_product_ids 逐个条件：
    ① AutomationRunItem.process_status == "succeeded"
    ② Product.draft_verified_revision == Product.revision
    ③ 不存在 ImageJob(status != "ready") 指向该商品
    ④ AutomationRun.account_id == Product.account_id
    ⑤ AutomationRunItem.detail_id == Product.detail_id
    ⑥ AutomationRunItem.source_shop_id == Product.shop_id

这个脚本对每一件 automation_result=true 的商品，逐条列出**哪一条**没成立，
这样才知道该补数据（缺 run item）还是该修数据（revision 不同步）。
"""
import os
from collections import Counter

import psycopg

url = os.environ["AOHUANG_DATABASE_URL"].replace(
    "postgresql+psycopg://", "postgresql://")

with psycopg.connect(url) as conn:
    rows = conn.execute("""
        select p.id, p.detail_id, p.shop_id, p.account_id,
               p.revision, p.draft_verified_revision, p.deleted_at is not null as del
        from products p
        where p.automation_result = true
        order by p.id
    """).fetchall()

    print("=" * 78)
    print(f"automation_result=true 的商品: {len(rows)} 件")
    print("=" * 78)

    reasons = Counter()
    detail = {}

    for pid, did, shop, acc, rev, verified, deleted in rows:
        if deleted:
            reasons["已软删除 deleted_at"] += 1
            detail.setdefault("已软删除 deleted_at", []).append((pid, did, rev, verified))
            continue

        items = conn.execute("""
            select i.process_status, i.product_id, i.source_shop_id,
                   r.account_id, i.detail_id
            from automation_run_items i
            join automation_runs r on r.id = i.run_id
            where i.detail_id = %s
        """, (did,)).fetchall()

        if not items:
            reasons["①无 run item 记录（detail_id 查不到）"] += 1
            detail.setdefault("①无 run item 记录（detail_id 查不到）", []).append(
                (pid, did, rev, verified))
            continue

        # 逐条判断，记录「最接近成立」的那条 item 卡在哪
        best = None
        for st, ipid, ishop, racc, idid in items:
            if st != "succeeded":
                best = best or f"①process_status={st}"
            elif verified != rev:
                best = "②draft_verified != revision"
            elif racc != acc:
                best = f"④账号 run={racc} product={acc}"
            elif str(ishop) != str(shop):
                best = f"⑥店铺 run={ishop} product={shop}"
            elif idid != did:
                best = "⑤detail 不符"
            else:
                best = "OK-run"
                break

        if best == "OK-run":
            bad = conn.execute("""
                select count(*) from image_jobs
                where product_id = %s and status != 'ready'
            """, (pid,)).fetchone()[0]
            if bad:
                reasons["③有未 ready 的图片任务"] += 1
                detail.setdefault("③有未 ready 的图片任务", []).append(
                    (pid, did, rev, verified))
            else:
                reasons["✓ 应显示"] += 1
        else:
            key = best.split("=")[0][:2]
            label = {"①": "①run item 不是 succeeded",
                     "②": "②draft_verified != revision",
                     "④": "④run.account_id != product.account_id",
                     "⑤": "⑤detail_id 不符",
                     "⑥": "⑥run 店铺 != product 店铺"}.get(key, best)
            reasons[label] += 1
            detail.setdefault(label, []).append((pid, did, rev, verified))

    print()
    print("不显示的原因分布（按商品计）:")
    for k, v in reasons.most_common():
        print(f"  {k:<44} {v:>4} 件")

    print()
    print("=" * 78)
    print("明细（每类最多 5 件）")
    print("=" * 78)
    for k, samples in detail.items():
        if k.startswith("✓"):
            continue
        print()
        print(f"【{k}】共 {len(samples)} 件")
        for pid, did, rev, verified in samples[:5]:
            print(f"  #{pid} detail={did} rev={rev} verified={verified}")

    # run item 总数：库里有任何自动化记录吗
    print()
    print("=" * 78)
    print("automation_runs / automation_run_items 总量")
    print("=" * 78)
    for tbl in ("automation_runs", "automation_run_items"):
        n = conn.execute(f"select count(*) from {tbl}").fetchone()[0]
        print(f"  {tbl:<22} {n} 行")
    print()
    print("  run item 状态分布:")
    for st, n in conn.execute("""
        select process_status, count(*) from automation_run_items
        group by process_status order by 2 desc
    """).fetchall():
        print(f"    {st:<16} {n} 行")
