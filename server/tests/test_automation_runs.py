from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from db.session import SessionLocal, init_db
from main import app
from models.account import Account
from services import operator_service

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
               publish_enabled: bool = False) -> dict:
    response = client.post(
        API + "/automation-runs",
        json={
            "accountId": setup["account_id"], "shopId": "22",
            "settings": {"textEnabled": True, "publishEnabled": publish_enabled},
            "items": [{"key": "101:11", "detailId": "101", "shopId": "11",
                       "title": "Test item"}],
        },
        headers=headers(str(setup["teacher_token"])),
    )
    assert response.status_code == 201, response.text
    return response.json()["run"]


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
    item = recovered.json()["run"]["items"][0]
    assert item["processStatus"] == "needs_review"
    assert "核对" in item["processReason"]

    repeated = client.post(
        API + f"/automation-runs/{run_id}/items/{item_id}/start", headers=auth)
    assert repeated.status_code == 409, repeated.text


def test_succeeded_item_cannot_be_started_again(client, setup):
    run = create_run(client, setup)
    run_id = run["id"]
    item_id = run["items"][0]["id"]
    auth = headers(str(setup["teacher_token"]))
    assert client.post(
        API + f"/automation-runs/{run_id}/items/{item_id}/start", headers=auth
    ).status_code == 200
    finished = client.post(
        API + f"/automation-runs/{run_id}/items/{item_id}/process-result",
        json={"status": "succeeded", "productId": None,
              "result": {"textDone": True}}, headers=auth,
    )
    assert finished.status_code == 200, finished.text
    assert finished.json()["run"]["items"][0]["processStatus"] == "succeeded"

    repeated = client.post(
        API + f"/automation-runs/{run_id}/items/{item_id}/start", headers=auth)
    assert repeated.status_code == 409, repeated.text


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


def test_skipped_publish_can_be_recorded_as_needs_review(client, setup):
    run = create_run(client, setup, publish_enabled=True)
    item = run["items"][0]
    auth = headers(str(setup["teacher_token"]))
    client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/start", headers=auth)
    processed = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/process-result",
        json={"status": "succeeded"}, headers=auth,
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


def test_recovery_closes_unstarted_publish_after_successful_processing(client, setup):
    run = create_run(client, setup, publish_enabled=True)
    item = run["items"][0]
    auth = headers(str(setup["teacher_token"]))
    client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/start", headers=auth)
    processed = client.post(
        API + f"/automation-runs/{run['id']}/items/{item['id']}/process-result",
        json={"status": "succeeded"}, headers=auth,
    )
    assert processed.status_code == 200, processed.text

    recovered = client.post(API + f"/automation-runs/{run['id']}/recover", headers=auth)
    assert recovered.status_code == 200, recovered.text
    result = recovered.json()["run"]
    assert result["items"][0]["publishStatus"] == "needs_review"
    assert result["status"] == "needs_review"
