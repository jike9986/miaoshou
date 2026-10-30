from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from api import publish as publish_api
from db.session import SessionLocal, init_db
from main import app
from models.account import Account
from models.automation_run import AutomationRunItem
from models.image_job import ImageJob
from models.product import Product
from models.publish_log import PublishLog
from services import operator_service, setting_service

API = "/api/v1"


@pytest.fixture(scope="module")
def client():
    init_db()
    instance = TestClient(app, raise_server_exceptions=False)
    yield instance
    instance.close()


@pytest.fixture(scope="module")
def setup(client):
    del client
    db = SessionLocal()
    try:
        teacher, teacher_token = operator_service.create(
            db, f"run-teacher-{uuid.uuid4().hex[:8]}", role=operator_service.ROLE_TEACHER)
        member_a, token_a = operator_service.create(
            db, f"run-member-a-{uuid.uuid4().hex[:8]}", role=operator_service.ROLE_MEMBER)
        member_b, token_b = operator_service.create(
            db, f"run-member-b-{uuid.uuid4().hex[:8]}", role=operator_service.ROLE_MEMBER)
        db.flush()
        account = Account(
            name="automation-run-account", app_key="run-key", app_secret_enc="",
            owner_operator_id=teacher.id, status="active", shops="[]", is_default=False,
        )
        db.add(account)
        db.commit()
        return {
            "teacher_token": teacher_token,
            "teacher_id": teacher.id,
            "member_a_token": token_a,
            "member_a_id": member_a.id,
            "member_b_token": token_b,
            "account_id": str(account.id),
        }
    finally:
        db.close()


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def create_run(client: TestClient, setup: dict[str, object], *,
               publish_enabled: bool = False,
               images_enabled: bool | None = None,
               item_count: int = 1,
               extra_settings: dict[str, object] | None = None) -> dict:
    if images_enabled is None:
        images_enabled = publish_enabled
    detail_id = str(uuid.uuid4().int % 10**12)
    source_shop_id = str(uuid.uuid4().int % 10**12)
    settings = {"textEnabled": True, "imagesEnabled": images_enabled,
                "publishEnabled": publish_enabled}
    if publish_enabled:
        settings.update({
            "priceTemplateEnabled": True,
            "template": "Test template",
        })
    if extra_settings:
        settings.update(extra_settings)
    items = []
    for _ in range(item_count):
        detail_id = str(uuid.uuid4().int % 10**12)
        source_shop_id = str(uuid.uuid4().int % 10**12)
        items.append({"key": f"{detail_id}:{source_shop_id}",
                      "detailId": detail_id, "shopId": source_shop_id,
                      "title": "Test item"})
    response = client.post(
        API + "/automation-runs",
        json={
            "accountId": setup["account_id"], "shopId": "22",
            "settings": settings,
            "items": items,
        },
        headers=headers(str(setup["teacher_token"])),
    )
    assert response.status_code == 201, response.text
    return response.json()["run"]


def create_automation_product(setup: dict[str, object], detail_id: str,
                              shop_id: str = "11", *, verified: bool = True) -> Product:
    db = SessionLocal()
    try:
        product = Product(
            warehouse_key=f"run-product-{uuid.uuid4().hex}",
            account_id=int(setup["account_id"]), detail_id=detail_id, shop_id=shop_id,
            title="Automation test", status="ready", automation_result=True,
            claimed=False, collected={}, payload={}, draft_verified_revision=1,
            quality_review={"verifiedRevision": 1, "evidence": "test"} if verified else None,
        )
        db.add(product)
        db.commit()
        db.refresh(product)
        return product
    finally:
        db.close()


def complete_automation_item(client: TestClient, setup: dict[str, object],
                             run: dict, item: dict, *, verified: bool = True) -> Product:
    auth = headers(str(setup["teacher_token"]))
    assert client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/start", headers=auth,
    ).status_code == 200
    product = create_automation_product(
        setup, item["detailId"], item["shopId"], verified=verified)
    processed = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/process-result",
        json={"status": "succeeded", "productId": str(product.id),
              "result": {"saved": True, "draftVerified": True,
                         "images": {"requested": 0, "submitted": 0, "failed": 0}}},
        headers=auth,
    )
    assert processed.status_code == 200, processed.text
    return product


def test_recovery_marks_inflight_item_unknown_and_blocks_reprocessing(client, setup):
    run = create_run(client, setup)
    run_id = run["id"]
    item_id = run["items"][0]["id"]
    auth = headers(str(setup["teacher_token"]))

    started = client.post(
        API + f"/automation-runs/{run_id}/items/{item_id}/start", headers=auth)
    assert started.status_code == 200, started.text
    assert started.json()["run"]["items"][0]["processStatus"] == "running"

    recovered = client.post(API + f"/automation-runs/{run_id}/recover", headers=auth)
    assert recovered.status_code == 200, recovered.text
    recovered_run = recovered.json()["run"]
    assert recovered_run["accountId"] == setup["account_id"]
    assert recovered_run["shopId"] == "22"
    assert recovered_run["settings"] == {
        "textEnabled": True, "imagesEnabled": False, "publishEnabled": False,
    }
    item = recovered_run["items"][0]
    assert item["shopId"] != recovered_run["shopId"]
    assert item["processStatus"] == "needs_review"
    assert "核对" in item["processReason"]

    repeated = client.post(
        API + f"/automation-runs/{run_id}/items/{item_id}/start", headers=auth)
    assert repeated.status_code == 409, repeated.text


def test_automation_state_writes_respect_read_only_mode(client, setup):
    auth = headers(str(setup["teacher_token"]))
    publish_run = create_run(client, setup, publish_enabled=True)
    publish_item = publish_run["items"][0]
    assert client.post(
        API + f"/automation-runs/{publish_run['id']}/items/{publish_item['id']}/start",
        headers=auth,
    ).status_code == 200
    product = create_automation_product(
        setup, publish_item["detailId"], publish_item["shopId"])
    processed = client.post(
        API + f"/automation-runs/{publish_run['id']}/items/{publish_item['id']}/process-result",
        json={"status": "succeeded", "productId": str(product.id),
              "result": {"saved": True, "draftVerified": True,
                         "images": {"requested": 0, "submitted": 0, "failed": 0}}}, headers=auth,
    )
    assert processed.status_code == 200, processed.text
    assert client.post(
        API + f"/automation-runs/{publish_run['id']}/items/{publish_item['id']}/publish-start",
        headers=auth,
    ).status_code == 200

    recovery_run = create_run(client, setup)
    recovery_item = recovery_run["items"][0]
    assert client.post(
        API + f"/automation-runs/{recovery_run['id']}/items/{recovery_item['id']}/start",
        headers=auth,
    ).status_code == 200

    db = SessionLocal()
    try:
        setting_service.set_read_only(db, True)
        db.commit()
    finally:
        db.close()

    try:
        process_result = client.post(
            API + f"/automation-runs/{recovery_run['id']}/items/"
            f"{recovery_item['id']}/process-result",
            json={"status": "failed", "reason": "should not persist"}, headers=auth,
        )
        publish_result = client.post(
            API + f"/automation-runs/{publish_run['id']}/items/{publish_item['id']}/publish-result",
            json={"status": "failed", "reason": "should not persist"}, headers=auth,
        )
        recover = client.post(
            API + f"/automation-runs/{recovery_run['id']}/recover", headers=auth)
        assert process_result.status_code == 403, process_result.text
        assert publish_result.status_code == 403, publish_result.text
        assert recover.status_code == 403, recover.text
    finally:
        db = SessionLocal()
        try:
            setting_service.set_read_only(db, False)
            db.commit()
        finally:
            db.close()

    unchanged = client.get(
        API + f"/automation-runs/{recovery_run['id']}", headers=auth)
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["run"]["items"][0]["processStatus"] == "running"
    publish_unchanged = client.get(
        API + f"/automation-runs/{publish_run['id']}", headers=auth)
    assert publish_unchanged.status_code == 200, publish_unchanged.text
    publish_state = publish_unchanged.json()["run"]["items"][0]
    assert publish_state["processStatus"] == "succeeded"
    assert publish_state["publishStatus"] == "running"


def test_succeeded_item_cannot_be_started_again(client, setup):
    run = create_run(client, setup)
    run_id = run["id"]
    item_id = run["items"][0]["id"]
    auth = headers(str(setup["teacher_token"]))
    assert client.post(
        API + f"/automation-runs/{run_id}/items/{item_id}/start", headers=auth
    ).status_code == 200
    item = run["items"][0]
    product = create_automation_product(setup, item["detailId"], item["shopId"])
    finished = client.post(
        API + f"/automation-runs/{run_id}/items/{item_id}/process-result",
        json={"status": "succeeded", "productId": str(product.id),
              "result": {"textDone": True}}, headers=auth,
    )
    assert finished.status_code == 200, finished.text
    assert finished.json()["run"]["items"][0]["processStatus"] == "succeeded"

    repeated = client.post(
        API + f"/automation-runs/{run_id}/items/{item_id}/start", headers=auth)
    assert repeated.status_code == 409, repeated.text


def test_process_success_rejects_client_claim_without_verified_product(client, setup):
    run = create_run(client, setup, publish_enabled=True)
    item = run["items"][0]
    auth = headers(str(setup["teacher_token"]))
    assert client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/start", headers=auth,
    ).status_code == 200

    db = SessionLocal()
    try:
        product = Product(
            warehouse_key=f"unverified-{uuid.uuid4().hex}",
            account_id=int(setup["account_id"]), detail_id=item["detailId"],
            shop_id=item["shopId"], title="Unverified", automation_result=True,
            status="ready", claimed=False, collected={}, payload={},
        )
        db.add(product)
        db.commit()
        product_id = product.id
    finally:
        db.close()

    forged = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/process-result",
        json={"status": "succeeded", "productId": str(product_id),
              "result": {"saved": True, "draftVerified": True,
                         "images": {"failed": 0}}},
        headers=auth,
    )
    assert forged.status_code == 409, forged.text
    current = client.get(API + f"/automation-runs/{run['id']}", headers=auth)
    assert current.json()["run"]["items"][0]["processStatus"] == "running"


def test_process_success_rejects_stale_verified_revision(client, setup):
    run = create_run(client, setup, publish_enabled=True)
    item = run["items"][0]
    auth = headers(str(setup["teacher_token"]))
    assert client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/start", headers=auth,
    ).status_code == 200
    product = create_automation_product(setup, item["detailId"], item["shopId"])
    db = SessionLocal()
    try:
        row = db.query(Product).filter_by(id=product.id).one()
        row.revision += 1
        db.commit()
    finally:
        db.close()

    stale = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/process-result",
        json={"status": "succeeded", "productId": str(product.id),
              "result": {"saved": True, "draftVerified": True,
                         "images": {"failed": 0}}},
        headers=auth,
    )
    assert stale.status_code == 409, stale.text


def test_student_cannot_read_another_operators_run(client, setup):
    run = create_run(client, setup)
    denied = client.get(
        API + f"/automation-runs/{run['id']}",
        headers=headers(str(setup["member_b_token"])),
    )
    assert denied.status_code == 403, denied.text

    teacher_view = client.get(
        API + f"/automation-runs/{run['id']}",
        headers=headers(str(setup["teacher_token"])),
    )
    assert teacher_view.status_code == 200, teacher_view.text


def test_failed_process_closes_queued_publish_and_run(client, setup):
    run = create_run(client, setup, publish_enabled=True)
    item = run["items"][0]
    auth = headers(str(setup["teacher_token"]))
    started = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/start", headers=auth)
    assert started.status_code == 200, started.text

    finished = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/process-result",
        json={"status": "failed", "reason": "process error"}, headers=auth,
    )
    assert finished.status_code == 200, finished.text
    result = finished.json()["run"]
    assert result["items"][0]["publishStatus"] == "failed"
    assert result["status"] == "partial"


@pytest.mark.parametrize(
    "settings",
    [
        {"textEnabled": False, "imagesEnabled": True,
         "priceTemplateEnabled": True, "template": "Template A", "publishEnabled": True},
        {"textEnabled": True, "imagesEnabled": False,
         "priceTemplateEnabled": True, "template": "Template A", "publishEnabled": True},
        {"textEnabled": True, "imagesEnabled": True,
         "priceTemplateEnabled": False, "template": "Template A", "publishEnabled": True},
        {"textEnabled": True, "imagesEnabled": True,
         "priceTemplateEnabled": True, "template": "  ", "publishEnabled": True},
    ],
    ids=["text-required", "images-required", "template-required", "template-name-required"],
)
def test_publish_enabled_run_requires_complete_processing_settings(client, setup, settings):
    response = client.post(
        API + "/automation-runs",
        json={
            "accountId": setup["account_id"], "shopId": "22",
            "settings": settings,
            "items": [{"key": "101:11", "detailId": "101", "shopId": "11"}],
        },
        headers=headers(str(setup["teacher_token"])),
    )
    assert response.status_code == 400, response.text


def test_publish_enabled_run_accepts_complete_processing_settings(client, setup):
    run = create_run(client, setup, publish_enabled=True)

    assert run["settings"]["textEnabled"] is True
    assert run["settings"]["imagesEnabled"] is True
    assert run["settings"]["priceTemplateEnabled"] is True
    assert run["settings"]["template"] == "Test template"


def test_publish_requires_saved_and_verified_automation_product(client, setup):
    run = create_run(client, setup, publish_enabled=True)
    item = run["items"][0]
    auth = headers(str(setup["teacher_token"]))
    assert client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/start", headers=auth,
    ).status_code == 200

    unverified = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/process-result",
        json={"status": "succeeded", "productId": "1",
              "result": {"saved": True, "draftVerified": False}}, headers=auth,
    )
    assert unverified.status_code == 409, unverified.text

    db = SessionLocal()
    try:
        product = Product(
            warehouse_key=f"run-unverified-{uuid.uuid4().hex}",
            account_id=int(setup["account_id"]), detail_id="102", shop_id="22",
            title="Unverified test", status="ready", automation_result=True,
            claimed=False, collected={}, payload={},
        )
        db.add(product)
        db.commit()
        item_id = item["id"]
        row = db.query(AutomationRunItem).filter_by(id=int(item_id)).one()
        row.process_status = "succeeded"
        row.product_id = product.id
        row.process_result = {"saved": True, "draftVerified": False}
        db.commit()

        blocked = client.post(
            API + f"/automation-runs/{run['id']}/items/{item['id']}/publish-start",
            headers=auth,
        )
        assert blocked.status_code == 409, blocked.text
    finally:
        db.close()


@pytest.mark.parametrize(
    ("job_status", "expected_status"),
    [
        ("failed", 409),
        ("queued", 409),
        ("submitted", 409),
        ("unknown", 409),
        ("ready", 200),
    ],
)
def test_publish_requires_all_product_image_jobs_ready(
    client, setup, job_status, expected_status,
):
    run = create_run(client, setup, publish_enabled=True, images_enabled=True)
    item = run["items"][0]
    auth = headers(str(setup["teacher_token"]))

    assert client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/start", headers=auth,
    ).status_code == 200
    product = create_automation_product(setup, item["detailId"], item["shopId"])
    failed_result = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/process-result",
        json={"status": "succeeded", "productId": str(product.id),
              "result": {"saved": True, "draftVerified": True,
                         "images": {"requested": 1, "submitted": 0, "failed": 1}}},
        headers=auth,
    )
    assert failed_result.status_code == 409, failed_result.text

    processed = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/process-result",
        json={"status": "succeeded", "productId": str(product.id),
              "result": {"saved": True, "draftVerified": True,
                         "images": {"requested": 1, "submitted": 1, "failed": 0}}},
        headers=auth,
    )
    assert processed.status_code == 200, processed.text
    db = SessionLocal()
    try:
        db.add(ImageJob(
            id=uuid.uuid4().hex, product_id=product.id, detail_id=item["detailId"],
            source="main", status=job_status,
        ))
        db.commit()
    finally:
        db.close()

    blocked = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/publish-start",
        headers=auth,
    )
    assert blocked.status_code == expected_status, blocked.text


@pytest.mark.parametrize(
    "mismatch",
    ["detail", "shop"],
    ids=["different-detail", "different-source-shop"],
)
def test_publish_rejects_automation_product_from_another_run_item(
    client, setup, mismatch,
):
    run = create_run(client, setup, publish_enabled=True)
    item = run["items"][0]
    auth = headers(str(setup["teacher_token"]))
    started = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/start", headers=auth)
    assert started.status_code == 200, started.text
    product = create_automation_product(
        setup,
        str(int(item["detailId"]) + 1) if mismatch == "detail" else item["detailId"],
        str(int(item["shopId"]) + 1) if mismatch == "shop" else item["shopId"],
    )

    result = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/process-result",
        json={"status": "succeeded", "productId": str(product.id),
              "result": {"saved": True, "draftVerified": True,
                         "images": {"requested": 0, "submitted": 0, "failed": 0}}}, headers=auth,
    )

    assert result.status_code == 409, result.text


def test_skipped_publish_can_be_recorded_as_needs_review(client, setup):
    run = create_run(client, setup, publish_enabled=True)
    item = run["items"][0]
    auth = headers(str(setup["teacher_token"]))
    client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/start", headers=auth)
    product = create_automation_product(setup, item["detailId"], item["shopId"])
    processed = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/process-result",
        json={"status": "succeeded", "productId": str(product.id),
              "result": {"saved": True, "draftVerified": True,
                         "images": {"requested": 0, "submitted": 0, "failed": 0}}}, headers=auth,
    )
    assert processed.status_code == 200, processed.text

    recorded = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/publish-result",
        json={"status": "needs_review", "reason": "images pending"}, headers=auth,
    )
    assert recorded.status_code == 200, recorded.text
    result = recorded.json()["run"]
    assert result["items"][0]["publishStatus"] == "needs_review"
    assert result["status"] == "needs_review"
    cannot_resume = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/resume-publish",
        headers=auth,
    )
    assert cannot_resume.status_code == 409, cannot_resume.text


def test_async_image_publish_can_resume_once_when_all_jobs_ready(client, setup):
    run = create_run(client, setup, publish_enabled=True, images_enabled=True)
    item = run["items"][0]
    auth = headers(str(setup["teacher_token"]))
    assert client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/start", headers=auth,
    ).status_code == 200
    product = create_automation_product(setup, item["detailId"], item["shopId"])
    processed = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/process-result",
        json={"status": "succeeded", "productId": str(product.id),
              "result": {"images": {"requested": 1, "submitted": 1, "failed": 0}}},
        headers=auth,
    )
    assert processed.status_code == 200, processed.text
    db = SessionLocal()
    try:
        job = ImageJob(
            id=uuid.uuid4().hex, product_id=product.id,
            detail_id=item["detailId"], source="main", status="submitted",
        )
        db.add(job)
        db.commit()
    finally:
        db.close()

    waiting = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/publish-result",
        json={"status": "needs_review", "reason": "images pending"}, headers=auth,
    )
    assert waiting.status_code == 200, waiting.text
    waiting_item = waiting.json()["run"]["items"][0]
    assert waiting_item["publishResult"]["resumeKind"] == "images_pending"
    blocked = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/resume-publish",
        headers=auth,
    )
    assert blocked.status_code == 409, blocked.text

    db = SessionLocal()
    try:
        db.query(ImageJob).filter_by(id=job.id).update({"status": "ready"})
        db.commit()
    finally:
        db.close()
    resumed = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/resume-publish",
        headers=auth,
    )
    assert resumed.status_code == 200, resumed.text
    resumed_item = resumed.json()["run"]["items"][0]
    assert resumed_item["processStatus"] == "succeeded"
    assert resumed_item["publishStatus"] == "running"
    assert resumed_item["publishResult"]["resumeKind"] == "images_ready"

    repeated = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/resume-publish",
        headers=auth,
    )
    assert repeated.status_code == 409, repeated.text
    generic_start = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/publish-start",
        headers=auth,
    )
    assert generic_start.status_code == 409, generic_start.text


def test_recovery_marks_saved_async_images_resumable_after_client_crash(client, setup):
    run = create_run(client, setup, publish_enabled=True, images_enabled=True)
    item = run["items"][0]
    auth = headers(str(setup["teacher_token"]))
    assert client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/start", headers=auth,
    ).status_code == 200
    product = create_automation_product(setup, item["detailId"], item["shopId"])
    processed = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/process-result",
        json={"status": "succeeded", "productId": str(product.id),
              "result": {"images": {"requested": 1, "submitted": 1, "failed": 0}}},
        headers=auth,
    )
    assert processed.status_code == 200, processed.text
    db = SessionLocal()
    try:
        db.add(ImageJob(
            id=uuid.uuid4().hex, product_id=product.id,
            detail_id=item["detailId"], source="main", status="ready",
        ))
        db.commit()
    finally:
        db.close()

    recovered = client.post(API + f"/automation-runs/{run['id']}/recover", headers=auth)
    assert recovered.status_code == 200, recovered.text
    recovered_item = recovered.json()["run"]["items"][0]
    assert recovered_item["publishStatus"] == "needs_review"
    assert recovered_item["publishResult"]["resumeKind"] == "images_pending"

    resumed = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/resume-publish",
        headers=auth,
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["run"]["items"][0]["publishStatus"] == "running"


def test_async_image_resume_rejects_stale_verified_draft(client, setup):
    run = create_run(client, setup, publish_enabled=True, images_enabled=True)
    item = run["items"][0]
    auth = headers(str(setup["teacher_token"]))
    assert client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/start", headers=auth,
    ).status_code == 200
    product = create_automation_product(setup, item["detailId"], item["shopId"])
    assert client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/process-result",
        json={"status": "succeeded", "productId": str(product.id),
              "result": {"images": {"requested": 1, "submitted": 1, "failed": 0}}},
        headers=auth,
    ).status_code == 200
    db = SessionLocal()
    try:
        job = ImageJob(
            id=uuid.uuid4().hex, product_id=product.id,
            detail_id=item["detailId"], source="main", status="ready",
        )
        row = db.query(Product).filter_by(id=product.id).one()
        row.revision += 1
        db.add(job)
        db.commit()
    finally:
        db.close()
    waiting = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/publish-result",
        json={"status": "needs_review", "reason": "images pending"}, headers=auth,
    )
    assert waiting.status_code == 200, waiting.text
    resumed = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/resume-publish",
        headers=auth,
    )
    assert resumed.status_code == 409, resumed.text


def test_client_cannot_finish_automation_publish_as_succeeded(client, setup):
    run = create_run(client, setup, publish_enabled=True)
    item = run["items"][0]
    auth = headers(str(setup["teacher_token"]))
    assert client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/start", headers=auth,
    ).status_code == 200
    product = create_automation_product(setup, item["detailId"], item["shopId"])
    processed = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/process-result",
        json={"status": "succeeded", "productId": str(product.id),
              "result": {"saved": False, "draftVerified": False,
                         "images": {"failed": 0}}},
        headers=auth,
    )
    assert processed.status_code == 200, processed.text
    recorded_result = processed.json()["run"]["items"][0]["processResult"]
    assert recorded_result["saved"] is True
    assert recorded_result["draftVerified"] is True
    assert client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/publish-start",
        headers=auth,
    ).status_code == 200

    forged = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/publish-result",
        json={"status": "succeeded", "result": {"batchId": "forged"}},
        headers=auth,
    )
    assert forged.status_code == 409, forged.text
    current = client.get(API + f"/automation-runs/{run['id']}", headers=auth)
    assert current.json()["run"]["items"][0]["publishStatus"] == "running"


def test_successful_automation_results_require_current_verified_revision(client, setup):
    run = create_run(client, setup)
    item = run["items"][0]
    auth = headers(str(setup["teacher_token"]))
    assert client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/start", headers=auth,
    ).status_code == 200
    product = create_automation_product(setup, item["detailId"], item["shopId"])
    db = SessionLocal()
    try:
        row = db.query(Product).filter_by(id=product.id).one()
        row.revision += 1
        db.commit()
    finally:
        db.close()

    forged = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/process-result",
        json={"status": "succeeded", "productId": str(product.id),
              "result": {"saved": True, "draftVerified": True}},
        headers=auth,
    )
    assert forged.status_code == 409, forged.text


def test_publish_receipt_endpoint_rejects_client_success_status(client, setup):
    run = create_run(client, setup, publish_enabled=True)
    item = run["items"][0]
    auth = headers(str(setup["teacher_token"]))
    assert client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/start", headers=auth,
    ).status_code == 200
    product = create_automation_product(setup, item["detailId"], item["shopId"])
    assert client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/process-result",
        json={"status": "succeeded", "productId": str(product.id),
              "result": {"images": {"failed": 0}}},
        headers=auth,
    ).status_code == 200
    assert client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/publish-start",
        headers=auth,
    ).status_code == 200

    response = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/publish-result",
        json={"status": "succeeded", "result": {"batchId": "fake"}},
        headers=auth,
    )
    assert response.status_code == 409, response.text


def test_recovery_keeps_unstarted_publish_available_after_processing(client, setup):
    run = create_run(client, setup, publish_enabled=True)
    item = run["items"][0]
    auth = headers(str(setup["teacher_token"]))
    client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/start", headers=auth)
    product = create_automation_product(setup, item["detailId"], item["shopId"])
    processed = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/process-result",
        json={"status": "succeeded", "productId": str(product.id),
              "result": {"saved": True, "draftVerified": True,
                         "images": {"requested": 0, "submitted": 0, "failed": 0}}}, headers=auth,
    )
    assert processed.status_code == 200, processed.text

    recovered = client.post(API + f"/automation-runs/{run['id']}/recover", headers=auth)
    assert recovered.status_code == 200, recovered.text
    result = recovered.json()["run"]
    assert result["items"][0]["publishStatus"] == "queued"
    assert result["status"] == "queued"

    started = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/publish-start",
        headers=auth,
    )
    assert started.status_code == 200, started.text
    assert started.json()["run"]["items"][0]["publishStatus"] == "running"

    repeated = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/publish-start",
        headers=auth,
    )
    assert repeated.status_code == 409, repeated.text


def test_automation_publish_confirmation_is_bound_to_product_and_revision(client, setup):
    run = create_run(client, setup, publish_enabled=True)
    item = run["items"][0]
    auth = headers(str(setup["teacher_token"]))
    assert client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/start", headers=auth,
    ).status_code == 200
    product = create_automation_product(
        setup, item["detailId"], item["shopId"], verified=False)
    processed = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/process-result",
        json={"status": "succeeded", "productId": str(product.id),
              "result": {"images": {"requested": 0, "submitted": 0, "failed": 0}}},
        headers=auth,
    )
    assert processed.status_code == 200, processed.text

    missing = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/publish-start",
        headers=auth,
    )
    assert missing.status_code == 409, missing.text
    missing_error = missing.json()["error"]
    assert missing_error["code"] == "QUALITY_REVIEW_REQUIRED"
    assert missing_error["details"]["confirmationStale"] is False
    assert missing_error["details"]["items"][0]["warehouseId"] == str(product.id)

    stale = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/publish-start",
        json={"confirmedProductId": str(product.id), "confirmedRevision": 0},
        headers=auth,
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["details"]["confirmationStale"] is True

    confirmed = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/publish-start",
        json={"confirmedProductId": str(product.id), "confirmedRevision": product.revision},
        headers=auth,
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["run"]["items"][0]["publishStatus"] == "running"

    repeated = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/publish-start",
        json={"confirmedProductId": str(product.id), "confirmedRevision": product.revision},
        headers=auth,
    )
    assert repeated.status_code == 409, repeated.text


@pytest.mark.parametrize(
    ("remote_status", "expected_status", "expected_run_status"),
    [("success", "succeeded", "completed"), ("fail", "failed", "partial")],
)
def test_publish_receipt_updates_only_matching_automation_item(
    client, setup, monkeypatch, remote_status, expected_status, expected_run_status,
):
    run = create_run(client, setup, publish_enabled=True)
    item = run["items"][0]
    auth = headers(str(setup["teacher_token"]))
    db = SessionLocal()
    try:
        detail_id = item["detailId"]
        assert client.post(
            API + f"/automation-runs/{run['id']}/items/{item['id']}/start",
            headers=auth,
        ).status_code == 200

        product = Product(
            warehouse_key=f"run-receipt-{uuid.uuid4().hex}",
            account_id=int(setup["account_id"]), detail_id=detail_id,
            shop_id=item["shopId"],
            title="Receipt test", status="ready", automation_result=True, claimed=False,
            collected={}, payload={}, draft_verified_revision=1,
        )
        db.add(product)
        db.commit()
        processed = client.post(
            API + f"/automation-runs/{run['id']}/items/{item['id']}/process-result",
            json={"status": "succeeded", "productId": str(product.id),
                  "result": {"saved": True, "draftVerified": True,
                             "images": {"requested": 0, "submitted": 0,
                                        "failed": 0}}},
            headers=auth,
        )
        assert processed.status_code == 200, processed.text
        assert client.post(
            API + f"/automation-runs/{run['id']}/items/{item['id']}/publish-start",
            json={"confirmedProductId": str(product.id), "confirmedRevision": 1},
            headers=auth,
        ).status_code == 200
        submitted = client.post(
            API + f"/automation-runs/{run['id']}/items/{item['id']}/publish-result",
            json={"status": "needs_review", "reason": "waiting receipt",
                  "result": {"batchId": "receipt-batch-1", "published": [detail_id]}},
            headers=auth,
        )
        assert submitted.status_code == 200, submitted.text

        log = PublishLog(
            account_id=int(setup["account_id"]), product_id=product.id,
            detail_id=detail_id, shop_id="22", batch_id="receipt-batch-1",
            status="processing", reason="", title="Receipt test",
        )
        db.add(log)
        db.commit()

        class FakeClient:
            def publish_records(self, **_kwargs):
                return {"data": {"detailList": [{
                    "detailId": detail_id, "status": remote_status,
                    "itemId": "50001", "reason": "platform rejected",
                }]}}

            def close(self):
                pass

        monkeypatch.setattr(
            publish_api.publish_service, "account_client_for",
            lambda *_args: (db.query(Account).filter_by(id=int(setup["account_id"])).one(),
                            FakeClient()),
        )
        refreshed = publish_api._refresh_processing(db, str(setup["account_id"]))
        assert refreshed["updated"] == 1

        saved = client.get(API + f"/automation-runs/{run['id']}", headers=auth)
        assert saved.status_code == 200, saved.text
        saved_run = saved.json()["run"]
        saved_item = saved_run["items"][0]
        assert saved_item["publishStatus"] == expected_status
        assert saved_item["publishReason"] == "platform rejected"
        assert saved_run["status"] == expected_run_status
        assert saved_item["publishResult"]["platformStatus"] == remote_status
        assert saved_item["publishResult"]["publishLogId"] == str(log.id)
    finally:
        db.close()


@pytest.mark.parametrize(("remote_shop_id", "expected_updated"), [("33", 1), (None, 0)])
def test_publish_receipt_does_not_cross_match_same_detail_across_shops(
    client, setup, monkeypatch, remote_shop_id, expected_updated,
):
    del client
    db = SessionLocal()
    try:
        account = db.query(Account).filter_by(id=int(setup["account_id"])).one()
        detail_id = f"cross-shop-{uuid.uuid4().hex[:8]}"
        logs = [PublishLog(
            account_id=account.id, detail_id=detail_id, shop_id=shop_id,
            batch_id=f"cross-shop-{shop_id}-{uuid.uuid4().hex[:6]}",
            status="processing", title="Cross-shop receipt",
        ) for shop_id in ("22", "33")]
        db.add_all(logs)
        db.commit()

        platform_row = {"detailId": detail_id, "status": "success"}
        if remote_shop_id is not None:
            platform_row["shopId"] = remote_shop_id

        class FakeClient:
            def publish_records(self, **_kwargs):
                return {"data": {"detailList": [platform_row]}}

            def close(self):
                pass

        monkeypatch.setattr(
            publish_api.publish_service, "account_client_for",
            lambda *_args: (account, FakeClient()),
        )
        refreshed = publish_api._refresh_processing(db, str(account.id))
        assert refreshed["updated"] == expected_updated
        db.refresh(logs[0])
        db.refresh(logs[1])
        if remote_shop_id == "33":
            assert (logs[0].status, logs[1].status) == ("processing", "success")
        else:
            assert (logs[0].status, logs[1].status) == ("processing", "processing")
    finally:
        db.close()
