from __future__ import annotations

import json
import uuid
from datetime import datetime

import pytest
from sqlalchemy.orm import sessionmaker

from core import errors
from core.utils import now_ts
from db.session import SessionLocal, init_db
from models.account import Account
from models.account_binding import AccountBinding
from models.automation_run import AutomationRun, AutomationRunItem
from models.batch import Batch, BatchItem
from models.operator import Operator
from models.product import Product
from models.product_claim import ProductClaim
from models.publish_log import PublishLog
from models.schedule import Schedule
from models.schedule_log import ScheduleLog
from services import claim_job_service, operator_service, publish_service, schedule_service


@pytest.fixture
def shared_account_operators():
    init_db()
    db = SessionLocal()
    try:
        suffix = uuid.uuid4().hex[:10]
        first, _ = operator_service.create(
            db, f"shared-first-{suffix}", role=operator_service.ROLE_MEMBER)
        second, _ = operator_service.create(
            db, f"shared-second-{suffix}", role=operator_service.ROLE_MEMBER)
        teacher, _ = operator_service.create(
            db, f"shared-teacher-{suffix}", role=operator_service.ROLE_TEACHER)
        account = Account(
            name=f"shared-account-{suffix}", app_key=f"shared-key-{suffix}",
            owner_operator_id=first.id, status="active", is_default=False,
            shops=json.dumps([{"shopId": "7", "name": "Shared shop", "site": "MY"}]),
        )
        db.add(account)
        db.flush()
        db.add_all([
            AccountBinding(account_id=account.id, operator_id=first.id,
                           bound_by_operator_id=first.id, bound_at=now_ts()),
            AccountBinding(account_id=account.id, operator_id=second.id,
                           bound_by_operator_id=teacher.id, bound_at=now_ts()),
        ])
        db.commit()
        yield db, account, first, second, teacher
    finally:
        db.rollback()
        db.close()


def _schedule(ident: str, account_id: int, owner_id: int | None) -> Schedule:
    return Schedule(
        id=ident, name=ident, account_id=account_id, owner_operator_id=owner_id,
        shop_id="7", template_name="", enabled=True, frequency="daily",
        days=[], day_of_month=[], weekdays=[], start_date="", end_date="",
        time_of_day="09:00", per_run=1, claim_only=False, last_run_date="",
        attempt_date="", attempts=0, next_due_at=None, last_result={},
        created_at=now_ts(), updated_at=now_ts(),
    )


def test_shared_account_schedules_claim_jobs_and_publish_logs_are_private(
        shared_account_operators):
    db, account, first, second, teacher = shared_account_operators
    suffix = uuid.uuid4().hex[:8]
    first_schedule = _schedule(f"first-{suffix}", account.id, first.id)
    second_schedule = _schedule(f"second-{suffix}", account.id, second.id)
    legacy_schedule = _schedule(f"legacy-{suffix}", account.id, None)
    db.add_all([first_schedule, second_schedule, legacy_schedule])

    schedule_rows = [
        ScheduleLog(task_id=task.id, task_name=task.name, account_id=account.id,
                    owner_operator_id=task.owner_operator_id, status="ok", at=now_ts())
        for task in (first_schedule, second_schedule, legacy_schedule)
    ]
    db.add_all(schedule_rows)

    first_batch = Batch(
        id=f"first-{suffix}", account_id=account.id, operator_id=first.id,
        shop_id="7", detail_ids=["101"], settings={"kind": claim_job_service.KIND},
        results=[], errors=[], status="done")
    second_batch = Batch(
        id=f"second-{suffix}", account_id=account.id, operator_id=second.id,
        shop_id="7", detail_ids=["102"], settings={"kind": claim_job_service.KIND},
        results=[], errors=[], status="done")
    legacy_batch = Batch(
        id=f"legacy-{suffix}", account_id=account.id, operator_id=None,
        shop_id="7", detail_ids=["103"], settings={"kind": claim_job_service.KIND},
        results=[], errors=[], status="done")
    db.add_all([first_batch, second_batch, legacy_batch])

    publish_rows = [
        PublishLog(account_id=account.id, detail_id=f"{suffix}-{owner or 'legacy'}",
                   shop_id="7", title=f"log-{owner or 'legacy'}", operator_id=owner)
        for owner in (first.id, second.id, None)
    ]
    db.add_all(publish_rows)
    db.commit()

    first_schedules = schedule_service.list_schedules(db, account_id=account.id,
                                                       operator=first)
    second_schedules = schedule_service.list_schedules(db, account_id=account.id,
                                                        operator=second)
    teacher_schedules = schedule_service.list_schedules(db, account_id=account.id,
                                                         operator=teacher)
    assert [row["id"] for row in first_schedules["items"]] == [first_schedule.id]
    assert [row["id"] for row in second_schedules["items"]] == [second_schedule.id]
    assert {row["id"] for row in teacher_schedules["items"]} == {
        first_schedule.id, second_schedule.id, legacy_schedule.id}
    with pytest.raises(errors.AppError) as cross_task:
        schedule_service.get_schedule(db, second_schedule.id, operator=first)
    assert cross_task.value.code == errors.NOT_FOUND
    with pytest.raises(errors.AppError) as legacy_task:
        schedule_service.get_schedule(db, legacy_schedule.id, operator=first)
    assert legacy_task.value.code == errors.NOT_FOUND

    first_history = schedule_service.list_logs(db, account_id=account.id, operator=first)
    second_history = schedule_service.list_logs(db, account_id=account.id, operator=second)
    teacher_history = schedule_service.list_logs(db, account_id=account.id, operator=teacher)
    assert [row["taskId"] for row in first_history["items"]] == [first_schedule.id]
    assert [row["taskId"] for row in second_history["items"]] == [second_schedule.id]
    assert teacher_history["total"] == 3

    assert [row["id"] for row in claim_job_service.list_jobs(
        db, account.id, 50, operator=first)["items"]] == [first_batch.id]
    assert [row["id"] for row in claim_job_service.list_jobs(
        db, account.id, 50, operator=second)["items"]] == [second_batch.id]
    assert claim_job_service.list_jobs(db, account.id, 50, operator=teacher)["total"] == 3
    with pytest.raises(errors.AppError) as cross_batch:
        claim_job_service.get(db, second_batch.id, account.id, operator=first)
    assert cross_batch.value.code == errors.NOT_FOUND
    with pytest.raises(errors.AppError) as legacy_job:
        claim_job_service.get(db, legacy_batch.id, account.id, operator=first)
    assert legacy_job.value.code == errors.NOT_FOUND

    first_publish_logs = publish_service.list_logs(
        db, account_id=account.id, operator_id=first.id)
    second_publish_logs = publish_service.list_logs(
        db, account_id=account.id, operator_id=second.id)
    teacher_publish_logs = publish_service.list_logs(db, account_id=account.id)
    assert {row["title"] for row in first_publish_logs["items"]} == {f"log-{first.id}"}
    assert {row["title"] for row in second_publish_logs["items"]} == {f"log-{second.id}"}
    assert teacher_publish_logs["total"] == 3


def test_member_claim_job_requires_owned_claim_or_automation_result(
        shared_account_operators):
    db, account, first, second, teacher = shared_account_operators
    suffix = uuid.uuid4().hex[:8]

    def add_product(label: str, *, automation_result: bool = False) -> Product:
        product = Product(
            warehouse_key=f"claim-{suffix}-{label}:7", account_id=account.id,
            detail_id=f"claim-{suffix}-{label}", shop_id="7", title=label,
            automation_result=automation_result,
        )
        db.add(product)
        db.flush()
        return product

    def add_automation_owner(product: Product, owner: Operator, label: str) -> None:
        run = AutomationRun(
            id=f"run-{suffix}-{label}", owner_operator_id=owner.id,
            account_id=account.id, shop_id="7", status="done", settings={},
        )
        db.add(run)
        db.flush()
        db.add(AutomationRunItem(
            run_id=run.id, key=product.warehouse_key, detail_id=product.detail_id,
            source_shop_id=product.shop_id, product_id=product.id,
            process_status="succeeded",
        ))
        db.flush()

    unclaimed = add_product("unclaimed")
    claimed = add_product("claimed")
    db.add(ProductClaim(product_id=claimed.id, owner_operator_id=first.id))
    own_result = add_product("own-result", automation_result=True)
    add_automation_owner(own_result, first, "own")
    other_result = add_product("other-result", automation_result=True)
    add_automation_owner(other_result, second, "other")
    db.flush()

    def body(product: Product) -> dict:
        return {"confirm": True, "shopId": "7",
                "items": [{"warehouseId": str(product.id)}]}

    with pytest.raises(errors.AppError) as denied_unclaimed:
        claim_job_service.create(db, body(unclaimed), account.id, first.id)
    assert denied_unclaimed.value.status == 403

    student_claim = claim_job_service.create(db, body(claimed), account.id, first.id)
    student_result = claim_job_service.create(db, body(own_result), account.id, first.id)
    with pytest.raises(errors.AppError) as denied_foreign_result:
        claim_job_service.create(db, body(other_result), account.id, first.id)
    assert denied_foreign_result.value.status == 403

    teacher_job = claim_job_service.create(db, body(unclaimed), account.id, teacher.id)
    assert student_claim.operator_id == first.id
    assert student_result.operator_id == first.id
    assert teacher_job.operator_id == teacher.id


def test_claim_job_rechecks_member_claim_before_upstream_call(
        shared_account_operators, monkeypatch):
    db, account, first, _second, _teacher = shared_account_operators
    suffix = uuid.uuid4().hex[:8]
    product = Product(
        warehouse_key=f"claim-run-{suffix}:7", account_id=account.id,
        detail_id=f"claim-run-{suffix}", shop_id="7", title="claimed",
    )
    db.add(product)
    db.flush()
    claim = ProductClaim(product_id=product.id, owner_operator_id=first.id)
    db.add(claim)
    db.flush()
    batch = claim_job_service.create(
        db,
        {"confirm": True, "shopId": "7",
         "items": [{"warehouseId": str(product.id)}]},
        account.id, first.id,
    )
    batch_id = batch.id
    db.delete(claim)
    db.commit()

    calls = []
    monkeypatch.setattr(
        claim_job_service, "SessionLocal",
        sessionmaker(bind=db.get_bind(), autoflush=False, autocommit=False,
                     expire_on_commit=False),
    )
    monkeypatch.setattr(
        publish_service, "publish",
        lambda *_args, **_kwargs: calls.append(True),
    )
    claim_job_service.run(batch_id)
    db.expire_all()

    assert calls == []
    batch = claim_job_service.get(db, batch_id, account.id, operator=first)
    item = db.query(BatchItem).filter(BatchItem.batch_id == batch_id).one()
    assert batch.status == "failed"
    assert item.status == "failed"
    assert "只能将自己已认领" in item.message


def test_claim_job_rejects_own_automation_result_after_source_account_rebinding(
        shared_account_operators):
    db, source, first, _second, _teacher = shared_account_operators
    suffix = uuid.uuid4().hex[:8]
    product = Product(
        warehouse_key=f"revoked-result-{suffix}:7", account_id=source.id,
        detail_id=f"revoked-result-{suffix}", shop_id="7", title="private result",
        automation_result=True,
    )
    db.add(product)
    db.flush()
    run = AutomationRun(
        id=f"revoked-result-run-{suffix}", owner_operator_id=first.id,
        account_id=source.id, shop_id="7", status="done", settings={},
    )
    db.add(run)
    db.flush()
    db.add(AutomationRunItem(
        run_id=run.id, key=product.warehouse_key, detail_id=product.detail_id,
        source_shop_id=product.shop_id, product_id=product.id,
        process_status="succeeded",
    ))
    target = Account(name=f"new-target-{suffix}", app_key=f"new-key-{suffix}")
    db.add(target)
    db.flush()
    binding = db.query(AccountBinding).filter_by(
        account_id=source.id, operator_id=first.id).one()
    binding.account_id = target.id
    db.commit()

    with pytest.raises(errors.AppError) as revoked_source:
        claim_job_service.create(
            db, {"confirm": True, "shopId": "7",
                 "items": [{"warehouseId": str(product.id)}]},
            target.id, first.id,
        )

    assert revoked_source.value.status == 403
    assert "无权访问其他学员的自动化处理结果" in revoked_source.value.message
    assert db.query(Batch).filter_by(
        account_id=target.id, operator_id=first.id).count() == 0


def test_claim_job_rechecks_automation_result_owner_before_upstream_call(
        shared_account_operators, monkeypatch):
    db, account, first, second, _teacher = shared_account_operators
    suffix = uuid.uuid4().hex[:8]
    product = Product(
        warehouse_key=f"claim-result-{suffix}:7", account_id=account.id,
        detail_id=f"claim-result-{suffix}", shop_id="7", title="automation result",
        automation_result=True,
    )
    db.add(product)
    db.flush()
    run = AutomationRun(
        id=f"claim-result-run-{suffix}", owner_operator_id=first.id,
        account_id=account.id, shop_id="7", status="done", settings={},
    )
    db.add(run)
    db.flush()
    db.add(AutomationRunItem(
        run_id=run.id, key=product.warehouse_key, detail_id=product.detail_id,
        source_shop_id=product.shop_id, product_id=product.id,
        process_status="succeeded",
    ))
    db.flush()
    batch = claim_job_service.create(
        db,
        {"confirm": True, "shopId": "7",
         "items": [{"warehouseId": str(product.id)}]},
        account.id, first.id,
    )
    batch_id = batch.id
    run.owner_operator_id = second.id
    db.commit()

    calls = []
    monkeypatch.setattr(
        claim_job_service, "SessionLocal",
        sessionmaker(bind=db.get_bind(), autoflush=False, autocommit=False,
                     expire_on_commit=False),
    )
    monkeypatch.setattr(
        publish_service, "publish",
        lambda *_args, **_kwargs: calls.append(True),
    )
    claim_job_service.run(batch_id)
    db.expire_all()

    assert calls == []
    batch = claim_job_service.get(db, batch_id, account.id, operator=first)
    item = db.query(BatchItem).filter(BatchItem.batch_id == batch_id).one()
    assert batch.status == "failed"
    assert item.status == "failed"
    assert "无权访问其他学员的自动化处理结果" in item.message


def test_background_schedule_run_copies_task_owner_to_both_histories(
        shared_account_operators, monkeypatch):
    db, account, first, second, teacher = shared_account_operators
    task = _schedule(f"due-{uuid.uuid4().hex[:8]}", account.id, first.id)
    db.add(task)
    db.commit()
    calls = []
    picker_calls = []

    def pick_products(*_args, **kwargs):
        picker_calls.append(kwargs)
        return [{"id": 1, "detailId": "8001", "shopId": "7", "title": "test"}]

    monkeypatch.setattr(schedule_service, "pick_products", pick_products)
    # The shared test database can contain due tasks from other suites; keep
    # this regression focused on the task whose ownership we are checking.
    monkeypatch.setattr(schedule_service, "due_tasks", lambda *_args, **_kwargs: [task])

    def publish(db_arg, payload, **kwargs):
        calls.append(kwargs)
        publish_service._upsert_log(
            db_arg, account=account, product=None, detail_id="8001", shop_id="7",
            shop_name="Shared shop", title="scheduled publish", mode="direct",
            batch_id=payload["batchId"], schedule_id=payload["scheduleId"],
            operator_id=kwargs.get("_log_operator_id"),
        )
        return {"published": ["8001"], "failures": []}

    monkeypatch.setattr(publish_service, "publish", publish)
    result = schedule_service.run_due(
        db, now=datetime(2026, 11, 2, 10, 0))
    db.expire_all()

    assert len(result) == 1
    assert calls == [{"operator_id": first.id, "_log_operator_id": first.id}]
    assert picker_calls == [{
        "account_id": account.id,
        "count": 1,
        "template": "",
        "claim_only": False,
        "operator_id": first.id,
    }]
    history = db.query(ScheduleLog).filter(ScheduleLog.task_id == task.id).one()
    assert history.owner_operator_id == first.id
    log = db.query(PublishLog).filter(PublishLog.schedule_id == task.id).one()
    assert log.operator_id == first.id

    assert schedule_service.list_logs(db, task_id=task.id, operator=first)["total"] == 1
    with pytest.raises(errors.AppError) as cross_history:
        schedule_service.list_logs(db, task_id=task.id, operator=second)
    assert cross_history.value.code == errors.NOT_FOUND
    assert schedule_service.list_logs(db, task_id=task.id, operator=teacher)["total"] == 1
    assert publish_service.list_logs(
        db, account_id=account.id, operator_id=first.id)["total"] == 1
    assert publish_service.list_logs(
        db, account_id=account.id, operator_id=second.id)["total"] == 0


def test_schedule_product_deduplication_is_scoped_to_schedule_owner(
        shared_account_operators):
    db, account, first, second, _teacher = shared_account_operators
    suffix = uuid.uuid4().hex[:8]
    products = {}
    for owner in (first, second, None):
        owner_label = str(owner.id) if owner else "legacy"
        detail_id = f"dedup-{suffix}-{owner_label}"
        product = Product(
            warehouse_key=f"{detail_id}:7", account_id=account.id,
            detail_id=detail_id, shop_id="7", title="shared product",
        )
        products[owner_label] = product
        db.add(product)
    db.flush()
    # Each learner has a separate ledger. An ownerless legacy log remains
    # visible only to ownerless legacy tasks.
    db.add_all([
        PublishLog(account_id=account.id, product_id=product.id,
                   detail_id=product.detail_id, warehouse_key=product.warehouse_key,
                   shop_id="7", status="success", operator_id=owner.id if owner else None)
        for owner, product in zip((first, second, None), products.values(), strict=True)
    ])
    # Both learners have individually claimed all three shared finished goods.
    # The picker can now verify the publish-history ledger remains owner-scoped.
    db.add_all([
        ProductClaim(product_id=product.id, owner_operator_id=owner.id)
        for owner in (first, second)
        for product in products.values()
    ])
    db.commit()

    first_items = schedule_service.pick_products(
        db, account_id=account.id, count=10, operator_id=first.id)
    second_items = schedule_service.pick_products(
        db, account_id=account.id, count=10, operator_id=second.id)
    legacy_items = schedule_service.pick_products(
        db, account_id=account.id, count=10, operator_id=None)
    assert {row["id"] for row in first_items} == {
        products[str(second.id)].id, products["legacy"].id}
    assert {row["id"] for row in second_items} == {
        products[str(first.id)].id, products["legacy"].id}
    assert {row["id"] for row in legacy_items} == {
        products[str(first.id)].id, products[str(second.id)].id}


def test_member_empty_schedule_does_not_leak_global_warehouse_state(
        shared_account_operators, monkeypatch):
    db, shared_account, first, _second, _teacher = shared_account_operators
    suffix = uuid.uuid4().hex[:8]
    unclaimed_member, _ = operator_service.create(
        db, f"shared-unclaimed-{suffix}", role=operator_service.ROLE_MEMBER)
    db.add(AccountBinding(account_id=shared_account.id,
                          operator_id=unclaimed_member.id,
                          bound_by_operator_id=first.id, bound_at=now_ts()))
    task = _schedule(f"member-empty-{suffix}", shared_account.id,
                     unclaimed_member.id)
    db.add(task)
    db.commit()

    monkeypatch.setattr("services.setting_service.is_read_only", lambda _db: False)
    result = schedule_service.execute(db, task)

    assert result["status"] == schedule_service.RUN_EMPTY
    assert result["reason"] == "这个账号没有已认领且可发布的新商品"
    # Other members have claims in the same account, but that fact must not change
    # this member's empty-state result.


def test_teacher_empty_schedule_keeps_global_warehouse_diagnostic(
        shared_account_operators, monkeypatch):
    db, _shared_account, _first, _second, teacher = shared_account_operators
    suffix = uuid.uuid4().hex[:8]
    empty_account = Account(
        name=f"teacher-empty-account-{suffix}",
        app_key=f"teacher-empty-key-{suffix}",
        owner_operator_id=teacher.id, status="active", is_default=False,
        shops=json.dumps([{"shopId": "9", "name": "Empty shop", "site": "MY"}]),
    )
    db.add(empty_account)
    db.flush()
    task = _schedule(f"teacher-empty-{suffix}", empty_account.id, teacher.id)
    task.shop_id = "9"
    db.add(task)
    db.commit()

    monkeypatch.setattr("services.setting_service.is_read_only", lambda _db: False)
    result = schedule_service.execute(db, task)

    assert result["status"] == schedule_service.RUN_EMPTY
    # Products from the shared account make the global warehouse non-empty, while
    # the selected account itself has no products.
    assert result["reason"] == "仓库里没有属于这个账号的商品，检查任务的账号设置"
