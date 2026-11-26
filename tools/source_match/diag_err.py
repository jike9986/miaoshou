"""查最近的报错：服务日志 + 自动化运行 + 批次。"""
import json
import warnings

warnings.filterwarnings("ignore")

from sqlalchemy import text

from db.session import SessionLocal

db = SessionLocal()

print("=" * 78)
print("① 最近自动化运行")
print("=" * 78)
for r in db.execute(text("""
  select id, status, account_id, shop_id, created_at, updated_at,
         settings, finished_at
  from automation_runs order by created_at desc limit 5
""")).fetchall():
    rid, st, aid, sid, ca, ua, stg, fa = r
    s = stg if isinstance(stg, dict) else json.loads(stg or "{}")
    print(f"\n  {rid}")
    print(f"    status={st} acc={aid} shop={sid}")
    print(f"    settings={json.dumps(s, ensure_ascii=False)[:300]}")

print()
print("=" * 78)
print("② 最近自动化商品项（有错误的）")
print("=" * 78)
for r in db.execute(text("""
  select run_id, key, detail_id, process_status, process_reason,
         publish_status, publish_reason, process_result
  from automation_run_items
  where process_status in ('failed','needs_review')
     or publish_status in ('failed','needs_review')
  order by updated_at desc limit 10
""")).fetchall():
    print(f"\n  run={r[0]} key={r[1]} detail={r[2]}")
    print(f"    process: {r[3]}  reason={str(r[4])[:300]}")
    print(f"    publish: {r[5]}  reason={str(r[6])[:300]}")
    pr = r[7] if isinstance(r[7], dict) else json.loads(r[7] or "{}")
    if pr:
        print(f"    result={json.dumps(pr, ensure_ascii=False)[:400]}")

print()
print("=" * 78)
print("③ 最近批次")
print("=" * 78)
for r in db.execute(text("""
  select id, status, current_index,
         json_array_length(coalesce(detail_ids,'[]'::json)) as n,
         settings->>'kind' as kind, created_at, errors
  from batches order by created_at desc limit 5
""")).fetchall():
    print(f"\n  {r[0]} status={r[1]} {r[2]}/{r[3]}件 kind={r[4]}")
    errs = r[6] if isinstance(r[6], list) else json.loads(r[6] or "[]")
    for e in (errs or [])[:3]:
        print(f"    error: {str(e)[:300]}")

db.close()