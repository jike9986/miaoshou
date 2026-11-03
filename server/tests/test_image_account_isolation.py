"""Image account-facing endpoints must not reveal another member's usage or key hint."""
from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient

from api import images as images_api
from db.session import SessionLocal, init_db
from main import app
from models.image_job import ImageJob
from models.product import Product
from services import image_service, operator_service

API = "/api/v1"


@pytest.fixture(scope="module")
def client():
    init_db()
    instance = TestClient(app, raise_server_exceptions=False)
    yield instance
    instance.close()


@pytest.fixture(scope="module")
def accounts(client):
    del client  # Ensure the test database has been initialized.
    db = SessionLocal()
    try:
        first, first_token = operator_service.create(
            db, f"image-member-a-{uuid.uuid4().hex[:8]}",
            role=operator_service.ROLE_MEMBER)
        second, second_token = operator_service.create(
            db, f"image-member-b-{uuid.uuid4().hex[:8]}",
            role=operator_service.ROLE_MEMBER)
        teacher, teacher_token = operator_service.create(
            db, f"image-teacher-{uuid.uuid4().hex[:8]}",
            role=operator_service.ROLE_TEACHER)
        db.commit()
        return {
            "first": (first.id, first_token),
            "second": (second.id, second_token),
            "teacher": (teacher.id, teacher_token),
        }
    finally:
        db.close()


def _auth(accounts, name: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {accounts[name][1]}"}


def test_image_usage_and_limits_are_scoped_to_the_signed_in_member(client, accounts):
    now = time.time()
    db = SessionLocal()
    try:
        for name, reserved in (("first", 24), ("second", 240)):
            db.add(ImageJob(
                id=uuid.uuid4().hex,
                operator_id=accounts[name][0],
                reserved_milliyuan=reserved,
                created_at=now,
            ))
        db.commit()
    finally:
        db.close()

    for path, body in (("/images/usage", None), ("/images/limits", "usage")):
        response = client.get(API + path, headers=_auth(accounts, "first"))
        assert response.status_code == 200, response.text
        usage = response.json() if body is None else response.json()[body]
        assert usage["usedToday"] == 1
        assert usage["usedThisHour"] == 1
        assert usage["usedThisMinute"] == 1
        assert usage["budget"]["reservedMilliyuan"] == 24

    # Teachers retain the existing global operational view.
    response = client.get(API + "/images/usage", headers=_auth(accounts, "teacher"))
    assert response.status_code == 200, response.text
    assert response.json()["usedToday"] >= 2
    assert response.json()["budget"]["reservedMilliyuan"] >= 264

    first_jobs = client.get(API + "/images/jobs", headers=_auth(accounts, "first"))
    assert first_jobs.status_code == 200, first_jobs.text
    assert first_jobs.json()["total"] == 1
    assert first_jobs.json()["budget"]["reservedMilliyuan"] == 24

    second_jobs = client.get(API + "/images/jobs", headers=_auth(accounts, "second"))
    assert second_jobs.status_code == 200, second_jobs.text
    assert second_jobs.json()["total"] == 1
    assert second_jobs.json()["budget"]["reservedMilliyuan"] == 240

    teacher_jobs = client.get(API + "/images/jobs", headers=_auth(accounts, "teacher"))
    assert teacher_jobs.status_code == 200, teacher_jobs.text
    assert teacher_jobs.json()["total"] >= 2
    assert teacher_jobs.json()["budget"]["reservedMilliyuan"] >= 264


def test_member_image_key_check_omits_the_global_key_hint(client, accounts, monkeypatch):
    key = "test-image-key-1234567890"

    class FakeSettings:
        image_proxy = ""

        @staticmethod
        def image_key_origin():
            return {"mode": "direct", "source": "environment", "path": "masked-path"}

        @staticmethod
        def image_api_key():
            return key

        @staticmethod
        def edits_endpoint():
            return "https://images.example.test/v1/edits"

    monkeypatch.setattr(images_api, "get_settings", lambda: FakeSettings())

    response = client.get(API + "/images/verify", headers=_auth(accounts, "first"))
    assert response.status_code == 200, response.text
    assert response.json()["configured"] is True
    assert response.json()["keyHint"] == ""

    teacher_response = client.get(
        API + "/images/verify", headers=_auth(accounts, "teacher"))
    assert teacher_response.status_code == 200, teacher_response.text
    assert teacher_response.json()["keyHint"] == f"{key[:4]}...{key[-4:]}"


@pytest.mark.parametrize("different_owner", [False, True])
def test_image_deduplication_preserves_product_and_operator_ownership(
        client, accounts, monkeypatch, different_owner):
    del client
    raw = uuid.uuid4().bytes
    submitted = []
    monkeypatch.setattr(image_service, "_api_key", lambda: "configured")
    monkeypatch.setattr(image_service, "_fetch_source", lambda _url: raw)
    monkeypatch.setattr(image_service, "_shrink", lambda data: data)
    monkeypatch.setattr(image_service, "_image_type", lambda _data: "png")
    monkeypatch.setattr(image_service, "_reserve_slot", lambda _db: None)
    monkeypatch.setattr(image_service, "budget_limit", lambda _db: 10**9)

    def submit(data, size, *, prompt):
        submitted.append((data, size, prompt))
        return {"status": "submitted", "taskId": f"isolated-{len(submitted)}"}

    monkeypatch.setattr(image_service, "_submit", submit)
    db = SessionLocal()
    try:
        products = []
        for _ in range(2):
            detail_id = uuid.uuid4().hex
            product = Product(warehouse_key=f"{detail_id}:1", detail_id=detail_id,
                              shop_id="1", operator_id=accounts["first"][0])
            db.add(product)
            products.append(product)
        db.flush()
        first_operator = accounts["first"][0]
        second_operator = accounts["second"][0] if different_owner else first_operator
        first_product = products[0].id
        second_product = first_product if different_owner else products[1].id

        def translate(operator_id, product_id):
            return image_service.translate(
                db, {"items": [{"url": "https://example.test/same-image.png",
                                 "source": "main", "productId": str(product_id)}]},
                operator_id=operator_id, owner_operator_id=operator_id)["jobs"][0]

        first = translate(first_operator, first_product)
        second = translate(second_operator, second_product)
        repeated = translate(second_operator, second_product)
        assert first["id"] != second["id"]
        assert repeated["id"] == second["id"]
        assert repeated["reused"] is True
        assert len(submitted) == 2
        for result, operator_id, product_id in (
                (first, first_operator, first_product),
                (second, second_operator, second_product)):
            row = db.get(ImageJob, result["id"])
            assert row.operator_id == operator_id
            assert row.product_id == product_id
    finally:
        db.rollback()
        db.close()
