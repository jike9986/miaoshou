"""按 diag_reconcile 的分桶生成可执行修复方案，并区分「能自动修」与「不能自动修」。

## 三桶的正确处理

### A 桶 78 件：无 run item，verified == revision
商品数据完好（图核验通过、版本一致），纯粹缺 `automation_run_items` 那条记录。
自动化结果页 INNER JOIN 这条记录，所以一件都看不到。
→ 补建 run item（走真实 API 四步：建批次 / start / process-result），不重跑处理。

### B 桶 16 件：无 run item，verified is NULL
`draft_verified_revision = None` 在代码里**只有两个来源，都是核验失败**：
    collect_box.py:718             后台图片处理/草稿保存抛异常
    automation_run_service.py:367  图片任务超时/中断
细看 `automation_images`，这 16 件里：
    · 10 件 failed > 0（图片翻译失败 / 预扣费超限 / 下载原图 404）
    ·  3 件 aimg 全为 None（根本没进图片流程）
    ·  2 件 aimg 正常（completed=True failed=0）← 这 2 件可以重核验
    ·  1 件 aimg 为 NULL
→ **不能**直接把 verified 改成 revision，那等于伪造「草稿读回核验通过」，
   会把图片没处理好的商品放行到发布。必须按图片实际状态分流。

### D 桶 11 件：有 run item 但都非 succeeded
item 卡在 running / failed / needs_review。running 的是服务重启留下的僵尸，
按 automation_run_service.py:369-379 的负责人口径（「不要待核对，要么成功要么失败」）
应该判 failed 让人重试，而不是硬掰成 succeeded。

## 结论
只有 A 桶能自动修。B/D 桶需要先重跑图片处理或人工核对，
本脚本只把它们列出来，不做假。
"""
import json
import os

import psycopg

url = os.environ["AOHUANG_DATABASE_URL"].replace(
    "postgresql+psycopg://", "postgresql://")

with psycopg.connect(url) as conn:
    print("=" * 78)
    print("B 桶 16 件：按「能不能自动重核验」分流")
    print("=" * 78)

    rows = conn.execute("""
        select p.id, p.detail_id, p.shop_id, p.account_id, p.revision,
               p.automation_images
        from products p
        where p.deleted_at is null and p.automation_result = true
          and p.draft_verified_revision is null
          and not exists (select 1 from automation_run_items i
                          where i.detail_id = p.detail_id)
        order by p.id
    """).fetchall()

    salvage, rerun, manual = [], [], []
    for pid, did, shop, acc, rev, aimg in rows:
        a = aimg if isinstance(aimg, dict) else (
            json.loads(aimg) if isinstance(aimg, str) and aimg else None)
        if not a:
            manual.append((pid, did, shop, acc, rev, "automation_images 为空"))
            continue
        completed = a.get("completed")
        failed = a.get("failed")
        if completed is True and failed == 0:
            salvage.append((pid, did, shop, acc, rev, "图片已完成且无失败"))
        elif completed is None and failed is None:
            manual.append((pid, did, shop, acc, rev,
                           "aimg 各字段为空，未进图片流程"))
        else:
            rerun.append((pid, did, shop, acc, rev,
                          str(a.get("reason") or "")[:70]))

    print(f"\n【可重核验 → 补 verified 后补 run item】{len(salvage)} 件")
    for pid, did, shop, acc, rev, why in salvage:
        print(f"  #{pid:<5} {did:<12} acc={acc} rev={rev}  {why}")

    print(f"\n【图片未成 → 必须先重跑图片处理】{len(rerun)} 件")
    for pid, did, shop, acc, rev, why in rerun:
        print(f"  #{pid:<5} {did:<12} acc={acc} rev={rev}  {why}")

    print(f"\n【无图片记录 → 需人工确认】{len(manual)} 件")
    for pid, did, shop, acc, rev, why in manual:
        print(f"  #{pid:<5} {did:<12} acc={acc} rev={rev}  {why}")

    # A 桶：可自动补
    print()
    print("=" * 78)
    print("A 桶 78 件：可自动补 run item（按账号/店铺）")
    print("=" * 78)
    a_rows = conn.execute("""
        select p.account_id, p.shop_id, count(*)
        from products p
        where p.deleted_at is null and p.automation_result = true
          and p.draft_verified_revision = p.revision
          and not exists (select 1 from automation_run_items i
                          where i.detail_id = p.detail_id)
        group by 1,2 order by 1,2
    """).fetchall()
    tot = 0
    for acc, shop, n in a_rows:
        print(f"  account={acc} shop={shop}: {n} 件")
        tot += n
    print(f"  合计: {tot} 件")

    # D 桶：僵尸 running
    print()
    print("=" * 78)
    print("D 桶 11 件：为何不能硬改 succeeded")
    print("=" * 78)
    d_rows = conn.execute("""
        select p.id, p.detail_id, p.account_id, i.id, i.process_status,
               i.process_reason, i.updated_at
        from products p
        join automation_run_items i on i.detail_id = p.detail_id
        where p.deleted_at is null and p.automation_result = true
          and not exists (
            select 1 from automation_run_items i2
            where i2.detail_id = p.detail_id and i2.process_status = 'succeeded')
        order by p.id, i.id
    """).fetchall()
    for pid, did, acc, iid, st, reason, upd in d_rows:
        print(f"  #{pid:<5} {did:<12} acc={acc} item#{iid} "
              f"status={st:<12} {str(reason or '')[:50]}")
