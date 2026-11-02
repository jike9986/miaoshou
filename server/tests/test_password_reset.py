from __future__ import annotations

import time
import types
import uuid

import pytest
from fastapi.testclient import TestClient

from core import errors
from db.session import SessionLocal, init_db
from main import app
from models.password_reset_token import PasswordResetToken
from services import email_service, login_rate_limit_service, operator_service

API = "/api/v1"


@pytest.fixture(scope="module")
def client():
    init_db()
    instance = TestClient(app, raise_server_exceptions=False)
    yield instance
    instance.close()


def _student(email: str) -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:10]
    username = f"reset-{suffix}"
    db = SessionLocal()
    try:
        row = operator_service.create_student(
            db, f"Reset {suffix}", username, "initial-pass-456", email=email)
        db.commit()
        return username, str(row.id)
    finally:
        db.close()


def test_password_reset_sends_one_time_token_and_rotates_login(
        client, monkeypatch):
    email = f"reset-{uuid.uuid4().hex[:10]}@example.com"
    username, ident = _student(email)
    sent: dict[str, str] = {}

    def fake_send(**kwargs):
        sent.update({key: str(value) for key, value in kwargs.items()})

    monkeypatch.setattr(email_service, "send_password_reset", fake_send)
    response = client.post(API + "/auth/password-reset/request", json={"email": email})
    assert response.status_code == 202, response.text
    assert response.json() == {"ok": True}
    assert sent["recipient"] == email
    assert sent["expires_minutes"] == "15"

    old_login = client.post(API + "/auth/login", json={
        "username": username, "password": "initial-pass-456"})
    assert old_login.status_code == 200, old_login.text
    old_token = old_login.json()["token"]
    confirmed = client.post(API + "/auth/password-reset/confirm", json={
        "token": sent["token"], "newPassword": "changed-pass-456"})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["operator"]["id"] == ident
    assert confirmed.json()["mustChangePassword"] is False

    stale = client.get(API + "/warehouse", headers={
        "Authorization": f"Bearer {old_token}"})
    assert stale.status_code == 401, stale.text
    repeated = client.post(API + "/auth/password-reset/confirm", json={
        "token": sent["token"], "newPassword": "another-pass-456"})
    assert repeated.status_code == 400, repeated.text
    login = client.post(API + "/auth/login", json={
        "username": username, "password": "changed-pass-456"})
    assert login.status_code == 200, login.text

    login_rate_limit_service.clear("testclient", f"password-reset:{email}")


def test_password_reset_does_not_reveal_unknown_email(client):
    email = f"missing-{uuid.uuid4().hex[:10]}@example.com"
    response = client.post(API + "/auth/password-reset/request", json={"email": email})
    assert response.status_code == 202, response.text
    assert response.json() == {"ok": True}
    login_rate_limit_service.clear("testclient", f"password-reset:{email}")


def test_password_reset_rejects_expired_token(client):
    email = f"expired-{uuid.uuid4().hex[:10]}@example.com"
    _username, ident = _student(email)
    db = SessionLocal()
    try:
        raw_token = "expired-token-123456789"
        db.add(PasswordResetToken(
            operator_id=int(ident),
            token_hash=operator_service.hash_token(raw_token),
            created_at=time.time() - 3600,
            expires_at=time.time() - 1,
            requested_ip="testclient",
        ))
        db.commit()
    finally:
        db.close()
    response = client.post(API + "/auth/password-reset/confirm", json={
        "token": raw_token, "newPassword": "expired-pass-456"})
    assert response.status_code == 400, response.text


def test_password_reset_email_service_reports_unconfigured(monkeypatch):
    settings = types.SimpleNamespace(
        mail_host="", mail_port=587, mail_username="", mail_password="",
        mail_from="", mail_reply_to="", mail_use_tls=True, mail_use_ssl=False,
        mail_timeout=1,
    )
    monkeypatch.setattr(email_service, "get_settings", lambda: settings)
    with pytest.raises(errors.AppError) as caught:
        email_service.send_password_reset(
            recipient="test@example.com", display_name="Test", token="token",
            expires_minutes=15)
    assert caught.value.code == "MAIL_NOT_CONFIGURED"

