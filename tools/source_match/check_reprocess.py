"""核实用户的判断：
1. 这几件商品是否**已经处理过**（products 表有记录？collect_box 状态？）
2. 代码里是否存在「文字已处理就跳过」的幂等逻辑？
3. 草稿里的颜色是中文还是马来语（决定它被处理过几轮）
"""
import json
import os
from datetime import datetime, timezone

import psycopg

url = os.environ["AOHUANG_DATABASE_URL"].replace(
    "postgresql+psycopg://", "postgresql://")

DETAILS = ["3563357906", "3563357863", "3563353396", "3563357908",
           "3563357820", "3563357851"]


def ts(v):
    if not v:
        return None
    return datetime.fromtimestamp(float(v), timezone.utc).strftime("%m-%d %H:%M:%S")


with psycopg.connect(url) as conn:
    print("=" * 76)
    print("① 这几件在我们库里的记录（有记录=处理过）")
    print("=" * 76)
    for did in DETAILS:
        rows = conn.execute("""
            select id, revision, draft_verified_revision, automation_result,
                   status, revision, saved_at, updated_at,
                   automation_images->>'failed' as img_failed,
                   coalesce(automation_images->>'completed','-') as img_done
            from products
            where detail_id = %s and deleted_at is null
            order by id
        """, (did,)).fetchall()
        if not rows:
            print(f"  {did}  ✗ 库中无记录（从未成功入库）")
            continue
        for r in rows:
            print(f"  {did}  #{r[0]} rev={r[1]} ver={r[2]} auto={r[3]} "
                  f"{r[4]}")
            print(f"        saved={ts(r[6])} updated={ts(r[7])} "
                  f"图片 failed={r[8]} completed={r[9]}")

    print()
    print("=" * 76)
    print("② 这些商品有没有 image_jobs（有=提交过翻译）")
    print("=" * 76)
    for did in DETAILS:
        rows = conn.execute("""
            select j.status, j.source, count(*)
            from image_jobs j join products p on p.id = j.product_id
            where p.detail_id = %s
            group by 1,2
        """, (did,)).fetchall()
        print(f"  {did}: {rows if rows else '无 image_job'}")

    print()
    print("=" * 76)
    print("③ 自动化运行记录（有=走过真实流程）")
    print("=" * 76)
    for did in DETAILS:
        rows = conn.execute("""
            select i.run_id, i.process_status, i.product_id, i.process_reason
            from automation_run_items i where i.detail_id = %s
            order by i.id desc limit 3
        """, (did,)).fetchall()
        if not rows:
            print(f"  {did}: 无 run item")
        for r in rows:
            print(f"  {did}  run={r[0]} status={r[1]} pid={r[2]} "
                  f"{(r[3] or '')[:60]}")
