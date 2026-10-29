from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from db.session import SessionLocal, init_db
from main import app
from models.audit_log import AuditLog
from models.product import Product
from services import operator_service

API = "/api/v1"


@pytest.fixture(scope="module")
def client():
    init_db()
    instance = TestClient(app, raise_server_exceptions=False)
    yield instance
    instance.close()


@pytest.fixture
def quality_product():
    db = SessionLocal()
    product = Product(
        warehouse_key=f"quality-{uuid.uuid4().hex}",
        detail_id=uuid.uuid4().hex[:16], shop_id="shop-quality", site="MY",
        title="Quality review test", payload={"weight": 0.1,
                                               "packageLength": 30,
                                               "packageWidth": 20,
                                               "packageHeight": 10,
                                               "site": "MY"},
        images=[], detail_images=[], sku_properties=[], collected={},
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    result = (product.id, product.revision, product.warehouse_key)
    try:
        yield result
    finally:
        db.delete(product)
        db.commit()
        db.close()


@pytest.fixture(scope="module")
def credentials():
    db = SessionLocal()
    teacher, teacher_token = operator_service.create(
        db, f"quality-teacher-{uuid.uuid4().hex[:8]}",
        role=operator_service.ROLE_TEACHER)
    admin, admin_token = operator_service.create(
        db, f"quality-admin-{uuid.uuid4().hex[:8]}",
        role=operator_service.ROLE_ADMIN)
    member, member_token = operator_service.create(
        db, f"quality-member-{uuid.uuid4().hex[:8]}",
        role=operator_service.ROLE_MEMBER)
    db.commit()
    values = {"teacher": teacher_token, "admin": admin_token,
              "member": member_token,
              "teacher_id": teacher.id, "teacher_name": teacher.name}
    db.close()
    return values


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _review(revision: int) -> dict[str, object]:
    return {
        "weightKg": 0.42,
        "packageLength": 18.5,
        "packageWidth": 12,
        "packageHeight": 7.25,
        "source": "妙手商品资料",
        "evidence": "商品详情页包装参数一栏，已与采集源核对",
        "expectedRevision": revision,
    }


def test_teacher_can_save_review_without_putting_provenance_in_publish_payload(
        client, credentials, quality_product):
    product_id, revision, _key = quality_product
    response = client.patch(
        f"{API}/warehouse/{product_id}/quality-review",
        json=_review(revision), headers=_headers(credentials["teacher"]))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["revision"] == revision + 1
    assert result["qualityReview"]["source"] == "妙手商品资料"
    assert result["qualityReview"]["site"] == "MY"
    assert result["qualityReview"]["verifiedByOperatorId"] == credentials["teacher_id"]

    detail = client.get(
        f"{API}/warehouse/{product_id}", params={"includePayload": "true"},
        headers=_headers(credentials["teacher"]))
    assert detail.status_code == 200, detail.text
    product = detail.json()["product"]
    assert product["payload"]["weight"] == pytest.approx(0.42)
    assert product["payload"]["packageLength"] == pytest.approx(18.5)
    assert "qualityReview" not in product["payload"]
    assert product["qualityReview"]["evidence"].startswith("商品详情页")
    listing = client.get(
        f"{API}/warehouse", params={"q": "Quality review test", "pageSize": 100},
        headers=_headers(credentials["teacher"]))
    assert listing.status_code == 200, listing.text
    listed = next(row for row in listing.json()["items"] if row["id"] == str(product_id))
    assert "qualityReview" not in listed

    db = SessionLocal()
    try:
        audit = (db.query(AuditLog)
                 .filter(AuditLog.action == "warehouse.quality_review",
                         AuditLog.target_id == str(product_id)).one())
        assert audit.operator_id == credentials["teacher_id"]
        assert audit.detail["source"] == "妙手商品资料"
        assert "evidence" not in audit.detail
    finally:
        db.close()


def test_member_cannot_write_quality_review(client, credentials, quality_product):
    product_id, revision, _key = quality_product
    response = client.patch(
        f"{API}/warehouse/{product_id}/quality-review",
        json=_review(revision), headers=_headers(credentials["member"]))
    assert response.status_code == 403, response.text


@pytest.mark.parametrize("changes", [
    {"weightKg": 0}, {"packageLength": -1}, {"packageWidth": "NaN"},
    {"source": "   "}, {"evidence": "x"},
])
def test_review_rejects_unverified_or_invalid_values(
        client, credentials, quality_product, changes):
    product_id, revision, _key = quality_product
    body = _review(revision)
    body.update(changes)
    response = client.patch(
        f"{API}/warehouse/{product_id}/quality-review", json=body,
        headers=_headers(credentials["teacher"]))
    assert response.status_code == 400, response.text


def test_review_requires_current_product_revision(client, credentials, quality_product):
    product_id, revision, _key = quality_product
    body = _review(revision + 1)
    response = client.patch(
        f"{API}/warehouse/{product_id}/quality-review", json=body,
        headers=_headers(credentials["teacher"]))
    assert response.status_code == 409, response.text


def test_admin_can_save_quality_review(client, credentials, quality_product):
    product_id, revision, _key = quality_product
    response = client.patch(
        f"{API}/warehouse/{product_id}/quality-review",
        json=_review(revision), headers=_headers(credentials["admin"]))
    assert response.status_code == 200, response.text
