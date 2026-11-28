"""B 桶 16 件（auto=true、无 run item、draft_verified_revision is NULL）到底是什么。

`draft_verified_revision = None` 在代码里只有两个来源，都不是「还没做」：
  · collect_box.py:718  后台图片处理/草稿保存抛异常
  · automation_run_service.py:367  图片任务超时/中断

所以这 16 件是**草稿读回核验失败**留下的。它们能不能直接改成 revision，
取决于图片是否真的处理成功：

  · automation_images.completed == True 且 failed == 0  → 图确实好了，可以重核验
  · automation_images.completed == False / failed > 0   → 图没成，不能放行
  · automation_images 为 NULL                            → 根本没进图片流程
"""
import json
import os

import psycopg

url = os.environ["AOHUANG_DATABASE_URL"].replace(
    "postgresql+psycopg://", "postgresql://")

with psycopg.connect(url) as conn:
    rows = conn.execute("""
        select p.id, p.detail_id, p.shop_id, p.account_id, p.revision,
               p.automation_images, p.images, p.payload, p.updated_at
        from products p
        where p.deleted_at is null and p.automation_result = true
          and p.draft_verified_revision is null
          and not exists (select 1 from automation_run_items i
                          where i.detail_id = p.detail_id)
        order by p.account_id, p.id
    """).fetchall()

    print("=" * 78)
    print(f"B 桶: {len(rows)} 件")
    print("=" * 78)

    groups = {}
    for (pid, did, shop, acc, rev, aimg, imgs, payload, upd) in rows:
        if aimg is None:
            key = "aimg=NULL 未进图片流程"
        else:
            a = aimg if isinstance(aimg, dict) else json.loads(aimg or "{}")
            completed = a.get("completed")
            failed = a.get("failed")
            queued = a.get("queued")
            key = f"aimg: completed={completed} failed={failed} queued={queued}"
        groups.setdefault(key, []).append((pid, did, shop, acc, rev, aimg, imgs, upd))

    for k in sorted(groups):
        print(f"\n【{k}】{len(groups[k])} 件")
        for (pid, did, shop, acc, rev, aimg, imgs, upd) in groups[k]:
            a = aimg if isinstance(aimg, dict) else (json.loads(aimg) if aimg else {})
            reason = str(a.get("reason") or "")[:60]
            gen = a.get("generation")
            print(f"  #{pid:<5} {did:<12} acc={acc} shop={shop} rev={rev} "
                  f"gen={gen} reason={reason!r}")
            if imgs:
                n = len(imgs) if isinstance(imgs, list) else "?"
                print(f"        images={n} 张")

    # 这些商品的 payload 里图片地址是不是活链
    print()
    print("=" * 78)
    print("payload 图片地址抽样（看是不是 auto-temp 临时区）")
    print("=" * 78)
    for (pid, did, shop, acc, rev, aimg, imgs, upd) in rows[:16]:
        p = payload if isinstance(payload, dict) else json.loads(payload or "{}")
        notes = str(p.get("notes") or "")
        temp = notes.count("auto-temp")
        prod = notes.count("tk-product-automation/products/")
        print(f"  #{pid:<5} {did:<12} notes: auto-temp={temp} products={prod} "
              f"len={len(notes)}")
