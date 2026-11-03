from __future__ import annotations

import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from db.session import SessionLocal, init_db
from main import app
from models.email_verification_code import EmailVerificationCode
from models.operator import Operator
from services import email_code_service, email_service, login_rate_limit_service, operator_service

API = "/api/v1"
PURPOSE = email_code_service.PURPOSE_EMAIL_LOGIN
RATE_NAMESPACE = f"email-code:{PURPOSE}"
VERIFY_RATE_NAMESPACE = f"email-code-{PURPOSE}-verify"


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app, raise_server_exceptions=False) as instance:
        yield instance


@pytest.fixture(autouse=True)
def configured_email_service(monkeypatch):
    monkeypatch.setattr(email_service, "get_settings", lambda: SimpleNamespace(
        mail_host="smtp.example.test", mail_from="no-reply@example.test",
        mail_port=587, mail_username="", mail_password="", mail_reply_to="",
        mail_use_tls=True, mail_use_ssl=False, mail_timeout=10,
    ))


@pytest.fixture(autouse=True)
def isolate_email_login_rate_limit():
    login_rate_limit_service.clear_ip("testclient", namespace=RATE_NAMESPACE)
    login_rate_limit_service.clear_ip("testclient", namespace=VERIFY_RATE_NAMESPACE)
    yield
    login_rate_limit_service.clear_ip("testclient", namespace=RATE_NAMESPACE)
    login_rate_limit_service.clear_ip("testclient", namespace=VERIFY_RATE_NAMESPACE)


def _create_account(*, verified: bool = True, enabled: bool = True,
                    must_change_password: bool = False):
    suffix = uuid.uuid4().hex
    email = f"email-login-{suffix}@example.test"
    db = SessionLocal()
    try:
        row, old_token = operator_service.create(
            db, f"Email Login {suffix}", role=operator_service.ROLE_MEMBER,
            email=email)
        row.email_verified = verified
        row.enabled = enabled
        row.must_change_password = must_change_password
        db.commit()
        return str(row.id), row.name, email, old_token
    finally:
        db.close()


def _capture_delivery(monkeypatch):
    sent: list[dict[str, str]] = []

    def fake_send(**kwargs):
        sent.append({key: str(value) for key, value in kwargs.items()})

    monkeypatch.setattr(email_service, "send_verification_code", fake_send)
    return sent


def test_email_login_request_is_generic_and_only_sends_to_verified_accounts(
        client, monkeypatch):
    _operator_id, _name, email, _old_token = _create_account(verified=True)
    _create_account(verified=False)
    _create_account(verified=True, enabled=False)
    sent = _capture_delivery(monkeypatch)

    known = client.post(API + "/auth/email-code-login/request", json={"email": email})
    unknown = client.post(API + "/auth/email-code-login/request", json={
        "email": f"missing-{uuid.uuid4().hex}@example.test",
    })

    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json() == {"ok": True}
    assert len(sent) == 1
    assert sent[0]["recipient"] == email
    assert sent[0]["purpose"] == PURPOSE
    assert sent[0]["code"].isdigit() and len(sent[0]["code"]) == 6


def test_email_login_request_fails_generically_when_mail_is_unconfigured(
        client, monkeypatch):
    _operator_id, _name, known_email, _old_token = _create_account(verified=True)
    unknown_email = f"missing-{uuid.uuid4().hex}@example.test"
    monkeypatch.setattr(email_service, "get_settings", lambda: SimpleNamespace(
        mail_host="", mail_from=""))

    responses = [client.post(API + "/auth/email-code-login/request", json={
        "email": email,
    }) for email in (known_email, unknown_email)]

    assert [response.status_code for response in responses] == [503, 503]
    assert [response.json()["error"]["code"] for response in responses] == [
        "MAIL_NOT_CONFIGURED", "MAIL_NOT_CONFIGURED"]
    assert [response.json()["error"]["message"] for response in responses][0] == \
        responses[1].json()["error"]["message"]
    assert all(response.json()["error"]["code"] == "MAIL_NOT_CONFIGURED"
               for response in responses)
    with SessionLocal() as db:
        assert db.query(EmailVerificationCode).filter(
            EmailVerificationCode.purpose == PURPOSE,
            EmailVerificationCode.email.in_([known_email, unknown_email]),
            EmailVerificationCode.used_at.is_(None),
        ).count() == 0


def test_email_login_code_creates_rotated_session_and_is_single_use(client, monkeypatch):
    operator_id, name, email, old_token = _create_account()
    sent = _capture_delivery(monkeypatch)
    requested = client.post(API + "/auth/email-code-login/request", json={"email": email})
    assert requested.status_code == 202, requested.text

    login = client.post(API + "/auth/email-code-login/confirm", json={
        "email": email,
        "code": sent[0]["code"],
    })
    assert login.status_code == 200, login.text
    body = login.json()
    assert body["operator"] == {
        "id": operator_id,
        "name": name,
        "role": operator_service.ROLE_MEMBER,
    }
    assert body["token"] and body["token"] != old_token
    assert body["mustChangePassword"] is False
    assert client.get(API + "/operators", headers={
        "Authorization": f"Bearer {body['token']}",
    }).status_code == 200
    assert client.get(API + "/operators", headers={
        "Authorization": f"Bearer {old_token}",
    }).status_code == 401

    replay = client.post(API + "/auth/email-code-login/confirm", json={
        "email": email,
        "code": sent[0]["code"],
    })
    assert replay.status_code == 401
    assert replay.json()["error"]["message"] == "邮箱或验证码错误"


def test_email_login_preserves_forced_password_setup(client, monkeypatch):
    _operator_id, _name, email, _old_token = _create_account(
        must_change_password=True)
    sent = _capture_delivery(monkeypatch)
    requested = client.post(API + "/auth/email-code-login/request", json={"email": email})
    assert requested.status_code == 202, requested.text
    login = client.post(API + "/auth/email-code-login/confirm", json={
        "email": email,
        "code": sent[0]["code"],
    })
    assert login.status_code == 200, login.text
    session = login.json()
    assert session["mustChangePassword"] is True

    blocked = client.get(API + "/warehouse", headers={
        "Authorization": f"Bearer {session['token']}",
    })
    assert blocked.status_code == 403
    completed = client.post(API + "/auth/complete-password-setup", json={
        "newPassword": "new-password-456",
    }, headers={"Authorization": f"Bearer {session['token']}"})
    assert completed.status_code == 200, completed.text
    assert completed.json()["mustChangePassword"] is False
    assert completed.json()["token"] != session["token"]


def test_unverified_email_cannot_complete_email_login_even_with_valid_code(client):
    _operator_id, _name, email, _old_token = _create_account(verified=False)
    with SessionLocal() as db:
        code = email_code_service.issue(db, purpose=PURPOSE, email=email)
        db.commit()

    response = client.post(API + "/auth/email-code-login/confirm", json={
        "email": email,
        "code": code,
    })
    assert response.status_code == 401, response.text
    assert response.json()["error"]["message"] == "邮箱或验证码错误"


def test_disabled_account_cannot_complete_email_login_with_valid_code(client):
    _operator_id, _name, email, _old_token = _create_account(enabled=False)
    with SessionLocal() as db:
        code = email_code_service.issue(db, purpose=PURPOSE, email=email)
        db.commit()

    response = client.post(API + "/auth/email-code-login/confirm", json={
        "email": email,
        "code": code,
    })
    assert response.status_code == 401, response.text
    assert response.json()["error"]["message"] == "邮箱或验证码错误"


def test_expired_email_login_code_is_rejected(client, monkeypatch):
    _operator_id, _name, email, old_token = _create_account()
    sent = _capture_delivery(monkeypatch)
    requested = client.post(API + "/auth/email-code-login/request", json={"email": email})
    assert requested.status_code == 202, requested.text

    with SessionLocal() as db:
        row = (db.query(EmailVerificationCode)
               .filter(EmailVerificationCode.purpose == PURPOSE,
                       EmailVerificationCode.email == email,
                       EmailVerificationCode.used_at.is_(None))
               .one())
        row.expires_at = time.time() - 1
        db.commit()

    response = client.post(API + "/auth/email-code-login/confirm", json={
        "email": email,
        "code": sent[0]["code"],
    })
    assert response.status_code == 401, response.text
    assert response.json()["error"]["message"] == "邮箱或验证码错误"
    # A stale code must not rotate or otherwise invalidate the previous session.
    assert client.get(API + "/operators", headers={
        "Authorization": f"Bearer {old_token}",
    }).status_code == 200


def test_email_login_code_requests_allow_multiple_mailboxes_from_one_ip(client):
    for index in range(6):
        response = client.post(API + "/auth/email-code-login/request", json={
            "email": f"rate-limit-{index}-{uuid.uuid4().hex}@example.test",
        })
        assert response.status_code == 202, response.text

def test_email_login_code_requests_remain_limited_per_mailbox(client):
    email = f"rate-limit-{uuid.uuid4().hex}@example.test"
    for _ in range(email_code_service.MAX_CODE_ATTEMPTS):
        response = client.post(API + "/auth/email-code-login/request", json={
            "email": email,
        })
        assert response.status_code == 202, response.text

    limited = client.post(API + "/auth/email-code-login/request", json={"email": email})
    assert limited.status_code == 429


def test_email_login_invalid_codes_are_rate_limited_across_challenges(
        client, monkeypatch):
    _operator_id, _name, email, _old_token = _create_account()
    sent = _capture_delivery(monkeypatch)
    requested = client.post(API + "/auth/email-code-login/request", json={"email": email})
    assert requested.status_code == 202, requested.text
    wrong_code = "000000" if sent[0]["code"] != "000000" else "000001"

    for _ in range(email_code_service.MAX_CODE_ATTEMPTS):
        invalid = client.post(API + "/auth/email-code-login/confirm", json={
            "email": email,
            "code": wrong_code,
        })
        assert invalid.status_code == 401

    limited = client.post(API + "/auth/email-code-login/confirm", json={
        "email": email,
        "code": sent[0]["code"],
    })
    assert limited.status_code == 429

