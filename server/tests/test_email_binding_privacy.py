from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from db.session import SessionLocal, init_db
from main import app
from models.email_verification_code import EmailVerificationCode
from services import (
    email_code_service,
    email_service,
    login_rate_limit_service,
    operator_service,
)

API = "/api/v1"


@pytest.fixture(scope="module")
def client():
    init_db()
    instance = TestClient(app, raise_server_exceptions=False)
    yield instance
    instance.close()


def _identity(*, email: str | None = None) -> tuple[int, str]:
    suffix = uuid.uuid4().hex[:10]
    db = SessionLocal()
    try:
        row, token = operator_service.create(
            db, f"email-privacy-{suffix}",
            role=operator_service.ROLE_MEMBER, email=email)
        if email is not None:
            row.email_verified = True
        db.commit()
        return row.id, token
    finally:
        db.close()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _clear_limit(email: str) -> None:
    namespace = f"email-code:{email_code_service.PURPOSE_EMAIL_VERIFICATION}"
    login_rate_limit_service.clear(
        "testclient", f"email-code:{email_code_service.PURPOSE_EMAIL_VERIFICATION}:{email}",
        namespace=namespace)
    login_rate_limit_service.clear_ip("testclient", namespace=namespace)


def test_email_binding_request_requires_an_authenticated_operator(client):
    email = f"unauthenticated-{uuid.uuid4().hex[:10]}@example.com"

    response = client.post(
        API + "/auth/email-verification/request", json={"email": email})

    assert response.status_code == 401, response.text
    with SessionLocal() as db:
        assert db.query(EmailVerificationCode).filter_by(
            purpose=email_code_service.PURPOSE_EMAIL_VERIFICATION,
            email=email).count() == 0


def test_email_binding_request_does_not_disclose_another_accounts_email(
        client, monkeypatch):
    owner_email = f"owned-{uuid.uuid4().hex[:10]}@example.com"
    available_email = f"available-{uuid.uuid4().hex[:10]}@example.com"
    _identity(email=owner_email)
    _actor_id, token = _identity()
    sent: list[str] = []
    monkeypatch.setattr(
        email_service, "send_verification_code",
        lambda **kwargs: sent.append(kwargs["recipient"]))
    headers = _auth(token)

    occupied = client.post(
        API + "/auth/email-verification/request",
        json={"email": owner_email}, headers=headers)
    available = client.post(
        API + "/auth/email-verification/request",
        json={"email": available_email}, headers=headers)

    assert occupied.status_code == available.status_code == 202
    assert occupied.json() == available.json() == {"ok": True}
    assert sent == [available_email]
    with SessionLocal() as db:
        assert db.query(EmailVerificationCode).filter_by(
            purpose=email_code_service.PURPOSE_EMAIL_VERIFICATION,
            email=owner_email).count() == 0
        assert db.query(EmailVerificationCode).filter_by(
            purpose=email_code_service.PURPOSE_EMAIL_VERIFICATION,
            email=available_email).count() == 1

    _clear_limit(owner_email)
    _clear_limit(available_email)


def test_email_binding_confirm_uses_same_error_for_wrong_code_and_owned_email(client):
    owner_email = f"owned-{uuid.uuid4().hex[:10]}@example.com"
    available_email = f"available-{uuid.uuid4().hex[:10]}@example.com"
    _identity(email=owner_email)
    _actor_id, token = _identity()

    db = SessionLocal()
    try:
        occupied_code = email_code_service.issue(
            db, purpose=email_code_service.PURPOSE_EMAIL_VERIFICATION,
            email=owner_email)
        available_code = email_code_service.issue(
            db, purpose=email_code_service.PURPOSE_EMAIL_VERIFICATION,
            email=available_email)
        db.commit()
    finally:
        db.close()

    wrong_code = "000000" if available_code != "000000" else "000001"
    headers = _auth(token)
    occupied = client.post(
        API + "/auth/email-verification/confirm",
        json={"email": owner_email, "code": occupied_code}, headers=headers)
    wrong = client.post(
        API + "/auth/email-verification/confirm",
        json={"email": available_email, "code": wrong_code}, headers=headers)

    assert occupied.status_code == wrong.status_code == 400
    assert occupied.json()["error"]["code"] == wrong.json()["error"]["code"]
    assert occupied.json()["error"]["message"] == wrong.json()["error"]["message"]
    with SessionLocal() as db:
        consumed = db.query(EmailVerificationCode).filter_by(
            purpose=email_code_service.PURPOSE_EMAIL_VERIFICATION,
            email=owner_email).one()
        assert consumed.used_at is not None
