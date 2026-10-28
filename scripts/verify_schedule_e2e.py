"""端到端验证定时任务：造一个到期任务 → mock publish → run_due → 检查摘要 → 清理。"""
import sys
import time
import uuid

sys.path.insert(0, ".")

from db.session import SessionLocal  # noqa: E402
from models.product import Product  # noqa: E402
from models.schedule import Schedule  # noqa: E402
from services import publish_service, schedule_service  # noqa: E402

db = SessionLocal()

products = (db.query(Product)
            .filter(Product.deleted_at.is_(None), Product.status != "published")
            .limit(3).all())
if not products:
    print("没有可用商品，跳过")
    raise SystemExit(0)
account_id = products[0].account_id
shop_id = products[0].shop_id
print(f"用 {len(products)} 件商品，账号 {account_id}，店铺 {shop_id}")

now = time.time()
task = Schedule(
    id=f"smk{uuid.uuid4().hex[:12]}",
    name="__smoke_run_due__",
    enabled=True,
    account_id=account_id,
    shop_id=shop_id,
    frequency="daily",
    per_run=2,
    next_due_at=now - 60,
    days=[],
    day_of_month=[],
    created_at=now,
    updated_at=now,
    last_result={},
)
db.add(task)
db.commit()
print("任务 id:", task.id)

batches = []

def fake_publish(db_, payload, **kwargs):
    batches.append(payload)
    ids = [str(item.get("warehouseId")) for item in (payload.get("items") or [])]
    ok = ids[:1]
    bad = ids[1:]
    return {
        "published": ok,
        "claimed": [],
        "failures": [{"detailId": bad[0], "error": "mock 失败"}] if bad else [],
        "mode": "direct",
        "batchId": f"mock-{len(batches)}",
        "repriced": [],
        "warning": None,
    }

original = publish_service.publish
publish_service.publish = fake_publish
try:
    result = schedule_service.run_due(db)
    print("run_due 返回条数:", len(result))
    for item in result:
        print("摘要:", {k: item.get(k) for k in
                       ("requested", "selected", "published", "failed", "status", "reason")
                       if k in item})
    db.refresh(task)
    last = task.last_result or {}
    print("任务 lastResult:", {k: last.get(k) for k in
                            ("status", "selected", "published", "failed", "errorType", "reason")
                            if k in last})
    print("next_due_at 已推进:", (task.next_due_at or 0) > now)
    print("publish 调用次数:", len(batches), "（期望 1，旧 bug 会是 3）")
finally:
    publish_service.publish = original
    db.delete(task)
    db.commit()
    db.close()
    print("已清理临时任务")
