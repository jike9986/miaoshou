"""验证 P1-4 / P1-10：手动执行返回四态摘要；PATCH 能真正清空字段。"""
import sys
import time
import uuid

sys.path.insert(0, ".")

from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402
from core.config import get_settings  # noqa: E402
from db.session import SessionLocal  # noqa: E402
from models.product import Product  # noqa: E402
from models.schedule import Schedule  # noqa: E402
from services import publish_service  # noqa: E402

token = get_settings().local_secret()
H = {"Authorization": f"Bearer {token}"}
client = TestClient(main.app, raise_server_exceptions=False)

db = SessionLocal()
product = (db.query(Product)
           .filter(Product.deleted_at.is_(None), Product.status != "published").first())
if product is None:
    print("无可用商品，跳过")
    raise SystemExit(0)

now = time.time()
task_id = f"smk{uuid.uuid4().hex[:12]}"
db.add(Schedule(
    id=task_id, name="__smoke_manual__", enabled=True,
    account_id=product.account_id, shop_id=product.shop_id,
    frequency="weekly", per_run=2, days=[1, 3], day_of_month=[],
    start_date="2026-01-01", end_date="2026-12-31",
    next_due_at=now + 3600, created_at=now, updated_at=now, last_result={},
))
db.commit()

batches = []

def fake_publish(db_, payload, **kwargs):
    batches.append(payload)
    ids = [str(item.get("warehouseId")) for item in (payload.get("items") or [])]
    return {"published": ids, "claimed": [], "failures": [], "mode": "direct",
            "batchId": f"mock-{len(batches)}", "repriced": [], "warning": None}

original = publish_service.publish
publish_service.publish = fake_publish
try:
    print("--- P1-4 手动执行摘要 ---")
    response = client.post(f"/api/v1/schedules/{task_id}/run", headers=H)
    print("HTTP:", response.status_code)
    body = response.json()
    keys = ("requested", "selected", "published", "failed", "status", "reason")
    print({k: body.get(k) for k in keys if k in body})
    print("publish 调用次数:", len(batches), "（期望 1）")

    print("\n--- P1-10 PATCH 显式 null 清空 ---")
    db2 = SessionLocal()
    row = db2.get(Schedule, task_id)
    print("改前: days=%s start_date=%r end_date=%r" %
          (row.days, row.start_date, row.end_date))
    db2.close()

    response = client.patch(f"/api/v1/schedules/{task_id}", headers=H, json={
        "days": None, "startDate": None, "endDate": None,
    })
    print("PATCH HTTP:", response.status_code)

    db3 = SessionLocal()
    row = db3.get(Schedule, task_id)
    print("改后: days=%s start_date=%r end_date=%r" %
          (row.days, row.start_date, row.end_date))
    print("清空是否生效:", row.days == [] and not row.start_date and not row.end_date)
    db3.close()
finally:
    publish_service.publish = original
    cleanup = SessionLocal()
    row = cleanup.get(Schedule, task_id)
    if row is not None:
        cleanup.delete(row)
        cleanup.commit()
    cleanup.close()
    print("\n已清理临时任务")
