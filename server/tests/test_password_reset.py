from __future__ import annotations

import logging
import time
import types
import uuid

import pytest
from fastapi.testclient import TestClient

from core import errors
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


def _student(email: str, *, verified: bool = True) -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:10]
    username = f"reset-{suffix}"
    db = SessionLocal()
    try:
        row = operator_service.create_student(
            db, f"Reset {suffix}", username, "initial-pass-456", email=email)
        row.email_verified = verified
        db.commit()
        return username, str(row.id)
    finally:
        db.close()


def _clear_email_limit(email: str, purpose: str = email_code_service.PURPOSE_PASSWORD_RESET):
    login_rate_limit_service.clear(
        "testclient", f"email-code:{purpose}:{email}",
        namespace=f"email-code:{purpose}")


@pytest.fixture
def isolated_registration_code_ip_limit():
    namespace = f"email-code:{email_code_service.PURPOSE_REGISTRATION}"
    login_rate_limit_service.clear_ip("testclient", namespace=namespace)
    yield
    login_rate_limit_service.clear_ip("testclient", namespace=namespace)


def test_password_reset_sends_hashed_one_time_code_and_rotates_login(
        client, monkeypatch):
    email = f"reset-{uuid.uuid4().hex[:10]}@example.com"
    username, ident = _student(email)
    sent: dict[str, str] = {}

    def fake_send(**kwargs):
        sent.update({key: str(value) for key, value in kwargs.items()})

    monkeypatch.setattr(email_service, "send_verification_code", fake_send)
    response = client.post(API + "/auth/password-reset/request", json={
        "email": f"  {email.upper()}  ",
    })
    assert response.status_code == 202, response.text
    assert response.json() == {"ok": True}
    assert sent["recipient"] == email
    assert sent["purpose"] == email_code_service.PURPOSE_PASSWORD_RESET
    assert sent["expires_minutes"] == "15"

    with SessionLocal() as db:
        stored = db.query(EmailVerificationCode).filter_by(
            purpose=email_code_service.PURPOSE_PASSWORD_RESET, email=email).one()
        assert stored.code_hash != sent["code"]
        assert stored.failed_attempts == 0
        assert stored.expires_at - stored.created_at == pytest.approx(900, abs=2)

    old_login = client.post(API + "/auth/login", json={
        "username": username, "password": "initial-pass-456"})
    assert old_login.status_code == 200, old_login.text
    old_token = old_login.json()["token"]
    confirmed = client.post(API + "/auth/password-reset/confirm", json={
        "email": email.upper(), "code": sent["code"],
        "newPassword": "changed-pass-456",
    })
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["operator"]["id"] == ident
    assert confirmed.json()["mustChangePassword"] is False

    stale = client.get(API + "/warehouse", headers={
        "Authorization": f"Bearer {old_token}"})
    assert stale.status_code == 401, stale.text
    repeated = client.post(API + "/auth/password-reset/confirm", json={
        "email": email, "code": sent["code"],
        "newPassword": "another-pass-456",
    })
    assert repeated.status_code == 400, repeated.text
    login = client.post(API + "/auth/login", json={
        "username": username, "password": "changed-pass-456"})
    assert login.status_code == 200, login.text

    _clear_email_limit(email)


def test_password_reset_unknown_email_has_same_public_response(client, monkeypatch):
    email = f"missing-{uuid.uuid4().hex[:10]}@example.com"
    sent = []
    monkeypatch.setattr(email_service, "send_verification_code",
                        lambda **kwargs: sent.append(kwargs))
    unknown = client.post(API + "/auth/password-reset/request", json={"email": email})
    assert unknown.status_code == 202, unknown.text
    assert unknown.json() == {"ok": True}
    assert sent == []

    existing_email = f"existing-{uuid.uuid4().hex[:10]}@example.com"
    _student(existing_email)
    known = client.post(API + "/auth/password-reset/request", json={
        "email": existing_email,
    })
    assert known.status_code == 202, known.text
    assert known.json() == unknown.json()
    assert len(sent) == 1
    _clear_email_limit(email)
    _clear_email_limit(existing_email)


def test_password_reset_does_not_deliver_to_unverified_operator_email(client, monkeypatch):
    email = f"unverified-{uuid.uuid4().hex[:10]}@example.com"
    _student(email, verified=False)
    sent = []
    monkeypatch.setattr(email_service, "send_verification_code",
                        lambda **kwargs: sent.append(kwargs))

    response = client.post(API + "/auth/password-reset/request",
                           json={"email": email})

    assert response.status_code == 202, response.text
    assert response.json() == {"ok": True}
    assert sent == []
    with SessionLocal() as db:
        row = db.query(EmailVerificationCode).filter_by(
            purpose=email_code_service.PURPOSE_PASSWORD_RESET, email=email).one()
        assert row.used_at is None
        operator = db.query(operator_service.Operator).filter_by(email=email).one()
        assert operator.email_verified is False
    _clear_email_limit(email)


def test_signed_in_user_verifies_new_email_before_replacing_old_recovery_email(
        client, monkeypatch):
    old_email = f"old-{uuid.uuid4().hex[:10]}@example.com"
    new_email = f"new-{uuid.uuid4().hex[:10]}@example.com"
    username, _ident = _student(old_email, verified=True)
    login = client.post(API + "/auth/login", json={
        "username": username, "password": "initial-pass-456",
    })
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    changed = client.post(API + "/auth/change-password", json={
        "currentPassword": "initial-pass-456",
        "newPassword": "updated-pass-456",
    }, headers=headers)
    assert changed.status_code == 200, changed.text
    headers["Authorization"] = f"Bearer {changed.json()['token']}"
    sent: dict[str, str] = {}
    monkeypatch.setattr(email_service, "send_verification_code",
                        lambda **kwargs: sent.update(kwargs))

    requested = client.post(API + "/auth/email-verification/request",
                            json={"email": new_email}, headers=headers)
    assert requested.status_code == 202, requested.text
    assert sent["recipient"] == new_email
    assert sent["purpose"] == email_code_service.PURPOSE_EMAIL_VERIFICATION
    with SessionLocal() as db:
        row = db.query(operator_service.Operator).filter_by(username=username).one()
        assert row.email == old_email
        assert row.email_verified is True

    wrong_code = "000000" if sent["code"] != "000000" else "000001"
    rejected = client.post(API + "/auth/email-verification/confirm", json={
        "email": new_email, "code": wrong_code,
    }, headers=headers)
    assert rejected.status_code == 400, rejected.text
    with SessionLocal() as db:
        row = db.query(operator_service.Operator).filter_by(username=username).one()
        assert row.email == old_email
        assert row.email_verified is True

    confirmed = client.post(API + "/auth/email-verification/confirm", json={
        "email": new_email, "code": sent["code"],
    }, headers=headers)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["emailVerified"] is True
    with SessionLocal() as db:
        row = db.query(operator_service.Operator).filter_by(username=username).one()
        assert row.email == new_email
        assert row.email_verified is True

    reset_mail: dict[str, str] = {}
    monkeypatch.setattr(email_service, "send_verification_code",
                        lambda **kwargs: reset_mail.update(kwargs))
    reset_request = client.post(API + "/auth/password-reset/request",
                                json={"email": new_email})
    assert reset_request.status_code == 202, reset_request.text
    assert reset_mail["recipient"] == new_email
    reset = client.post(API + "/auth/password-reset/confirm", json={
        "email": new_email, "code": reset_mail["code"],
        "newPassword": "verified-reset-pass-456",
    })
    assert reset.status_code == 200, reset.text

    _clear_email_limit(old_email, email_code_service.PURPOSE_EMAIL_VERIFICATION)
    _clear_email_limit(new_email, email_code_service.PURPOSE_EMAIL_VERIFICATION)
    _clear_email_limit(new_email)


@pytest.mark.parametrize("email", ["a@b", "missing-at.example.com", "a..b@example.com"])
def test_password_reset_rejects_non_deliverable_email_before_issuing_code(client, email):
    response = client.post(API + "/auth/password-reset/request", json={"email": email})
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_password_reset_delivery_failure_stays_generic_and_is_logged_by_type(
        client, monkeypatch, caplog):
    email = f"delivery-{uuid.uuid4().hex[:10]}@example.com"
    _student(email)

    def fail_delivery(**_kwargs):
        raise email_service.smtplib.SMTPAuthenticationError(
            535, b"private SMTP response")

    monkeypatch.setattr(email_service, "send_verification_code", fail_delivery)
    with caplog.at_level(logging.WARNING, logger="api.auth"):
        response = client.post(API + "/auth/password-reset/request",
                               json={"email": email})

    assert response.status_code == 202, response.text
    assert response.json() == {"ok": True}
    assert "SMTPAuthenticationError" in caplog.text
    assert "private SMTP response" not in caplog.text
    assert email not in caplog.text
    _clear_email_limit(email)


def test_wrong_code_attempt_limit_is_persisted(client, monkeypatch):
    email = f"attempts-{uuid.uuid4().hex[:10]}@example.com"
    _student(email)
    sent: dict[str, str] = {}
    monkeypatch.setattr(email_service, "send_verification_code",
                        lambda **kwargs: sent.update(kwargs))
    requested = client.post(API + "/auth/password-reset/request", json={"email": email})
    assert requested.status_code == 202, requested.text

    for _ in range(email_code_service.MAX_CODE_ATTEMPTS):
        invalid = client.post(API + "/auth/password-reset/confirm", json={
            "email": email, "code": "000000" if sent["code"] != "000000" else "000001",
            "newPassword": "changed-pass-456",
        })
        assert invalid.status_code == 400, invalid.text
    correct_after_limit = client.post(API + "/auth/password-reset/confirm", json={
        "email": email, "code": sent["code"],
        "newPassword": "changed-pass-456",
    })
    assert correct_after_limit.status_code == 400
    with SessionLocal() as db:
        stored = db.query(EmailVerificationCode).filter_by(
            purpose=email_code_service.PURPOSE_PASSWORD_RESET, email=email).one()
        assert stored.failed_attempts == email_code_service.MAX_CODE_ATTEMPTS
        assert stored.used_at is not None
    _clear_email_limit(email)


def test_expired_email_code_is_rejected(client, monkeypatch):
    email = f"expired-{uuid.uuid4().hex[:10]}@example.com"
    _student(email)
    sent: dict[str, str] = {}
    monkeypatch.setattr(email_service, "send_verification_code",
                        lambda **kwargs: sent.update(kwargs))
    response = client.post(API + "/auth/password-reset/request", json={"email": email})
    assert response.status_code == 202
    with SessionLocal() as db:
        row = db.query(EmailVerificationCode).filter_by(
            purpose=email_code_service.PURPOSE_PASSWORD_RESET, email=email).one()
        row.expires_at = time.time() - 1
        db.commit()
    rejected = client.post(API + "/auth/password-reset/confirm", json={
        "email": email, "code": sent["code"],
        "newPassword": "expired-pass-456",
    })
    assert rejected.status_code == 400, rejected.text
    _clear_email_limit(email)


def test_email_code_send_limit_does_not_block_password_login(
        client, monkeypatch, isolated_registration_code_ip_limit):
    email = f"limited-{uuid.uuid4().hex[:10]}@example.com"
    namespace = f"email-code:{email_code_service.PURPOSE_REGISTRATION}"
    sent_count = 0

    def fake_send(**_kwargs):
        nonlocal sent_count
        sent_count += 1

    monkeypatch.setattr(email_service, "send_verification_code", fake_send)
    for _ in range(login_rate_limit_service.MAX_FAILURES):
        response = client.post(API + "/auth/registration-code/request", json={
            "email": email,
        })
        assert response.status_code == 202, response.text
    blocked = client.post(API + "/auth/registration-code/request", json={
        "email": email,
    })
    assert blocked.status_code == 429
    assert sent_count == login_rate_limit_service.MAX_FAILURES
    assert login_rate_limit_service.blocked_retry_after("testclient", "some-user") == 0
    login_rate_limit_service.clear(
        "testclient", f"email-code:{email_code_service.PURPOSE_REGISTRATION}:{email}",
        namespace=namespace)


def test_registration_code_ip_limit_allows_six_distinct_emails(
        client, monkeypatch, isolated_registration_code_ip_limit):
    sent: list[str] = []
    monkeypatch.setattr(email_service, "send_verification_code",
                        lambda **kwargs: sent.append(kwargs["recipient"]))

    for index in range(login_rate_limit_service.MAX_FAILURES + 1):
        response = client.post(API + "/auth/registration-code/request", json={
            "email": f"shared-ip-{index}-{uuid.uuid4().hex[:8]}@example.com",
        })
        assert response.status_code == 202, response.text

    assert len(sent) == login_rate_limit_service.MAX_FAILURES + 1


def test_registration_code_ip_limit_blocks_after_thirty_requests(
        client, monkeypatch, isolated_registration_code_ip_limit):
    sent: list[str] = []
    monkeypatch.setattr(email_service, "send_verification_code",
                        lambda **kwargs: sent.append(kwargs["recipient"]))

    for index in range(email_code_service.REGISTRATION_IP_SEND_LIMIT):
        response = client.post(API + "/auth/registration-code/request", json={
            "email": f"ip-cap-{index}-{uuid.uuid4().hex[:8]}@example.com",
        })
        assert response.status_code == 202, response.text

    blocked = client.post(API + "/auth/registration-code/request", json={
        "email": f"ip-cap-blocked-{uuid.uuid4().hex[:8]}@example.com",
    })
    assert blocked.status_code == 429
    assert len(sent) == email_code_service.REGISTRATION_IP_SEND_LIMIT


def test_password_reset_keeps_five_request_ip_limit(client):
    namespace = f"email-code:{email_code_service.PURPOSE_PASSWORD_RESET}"
    login_rate_limit_service.clear_ip("testclient", namespace=namespace)
    try:
        for index in range(login_rate_limit_service.MAX_FAILURES):
            response = client.post(API + "/auth/password-reset/request", json={
                "email": f"reset-ip-{index}-{uuid.uuid4().hex[:8]}@example.com",
            })
            assert response.status_code == 202, response.text

        blocked = client.post(API + "/auth/password-reset/request", json={
            "email": f"reset-ip-blocked-{uuid.uuid4().hex[:8]}@example.com",
        })
        assert blocked.status_code == 429
    finally:
        login_rate_limit_service.clear_ip("testclient", namespace=namespace)


def test_registration_code_cannot_be_reused_for_password_reset(
        client, monkeypatch, isolated_registration_code_ip_limit):
    email = f"purpose-{uuid.uuid4().hex[:10]}@example.com"
    sent: dict[str, str] = {}
    monkeypatch.setattr(email_service, "send_verification_code",
                        lambda **kwargs: sent.update(kwargs))

    requested = client.post(API + "/auth/registration-code/request", json={
        "email": email,
    })
    assert requested.status_code == 202, requested.text

    wrong_purpose = client.post(API + "/auth/password-reset/confirm", json={
        "email": email,
        "code": sent["code"],
        "newPassword": "changed-pass-456",
    })
    assert wrong_purpose.status_code == 400, wrong_purpose.text

    registered = client.post(API + "/auth/register", json={
        "name": "Purpose Boundary",
        "email": email,
        "password": "signup-pass-456",
        "code": sent["code"],
    })
    assert registered.status_code == 201, registered.text
    _clear_email_limit(email, email_code_service.PURPOSE_REGISTRATION)


def test_password_reset_email_service_reports_unconfigured(monkeypatch):
    settings = types.SimpleNamespace(
        mail_host="", mail_port=587, mail_username="", mail_password="",
        mail_from="", mail_reply_to="", mail_use_tls=True, mail_use_ssl=False,
        mail_timeout=1,
    )
    monkeypatch.setattr(email_service, "get_settings", lambda: settings)
    with pytest.raises(errors.AppError) as caught:
        email_service.send_verification_code(
            recipient="test@example.com", display_name="Test", code="012345",
            purpose="password_reset", expires_minutes=15)
    assert caught.value.code == "MAIL_NOT_CONFIGURED"


@pytest.mark.parametrize(("purpose", "expected"), [
    ("registration", "完成邮箱注册"),
    ("email_verification", "验证并绑定邮箱"),
    ("password_reset", "重置登录密码"),
])
def test_verification_email_explains_its_purpose(monkeypatch, purpose, expected):
    settings = types.SimpleNamespace(
        mail_host="smtp.example.com", mail_port=587,
        mail_username="", mail_password="",
        mail_from="no-reply@example.com", mail_reply_to="",
        mail_use_tls=True, mail_use_ssl=False, mail_timeout=10,
    )
    sent = []
    monkeypatch.setattr(email_service, "get_settings", lambda: settings)
    monkeypatch.setattr(email_service, "_deliver", lambda _settings, message: sent.append(message))

    email_service.send_verification_code(
        recipient="test@example.com", display_name="Test", code="012345",
        purpose=purpose, expires_minutes=15)

    assert expected in sent[0].get_content()


@pytest.mark.parametrize("overrides", [
    {"mail_username": "smtp-user", "mail_password": "smtp-secret",
     "mail_use_tls": False},
    {"mail_username": "smtp-user", "mail_password": ""},
    {"mail_reply_to": "support@example.com\r\nBcc: victim@example.com"},
    {"mail_from": "not-an-address"},
    {"mail_timeout": float("inf")},
])
def test_email_service_rejects_unsafe_or_invalid_settings(monkeypatch, overrides):
    values = {
        "mail_host": "smtp.example.com", "mail_port": 587,
        "mail_username": "", "mail_password": "",
        "mail_from": "no-reply@example.com", "mail_reply_to": "",
        "mail_use_tls": True, "mail_use_ssl": False, "mail_timeout": 10,
    }
    values.update(overrides)
    monkeypatch.setattr(email_service, "get_settings",
                        lambda: types.SimpleNamespace(**values))

    with pytest.raises(errors.AppError) as caught:
        email_service.send_verification_code(
            recipient="test@example.com", display_name="Test", code="012345",
            purpose="registration", expires_minutes=15)

    assert caught.value.code == "MAIL_CONFIG_INVALID"


def test_email_service_does_not_log_smtp_response_or_credentials(monkeypatch, caplog):
    settings = types.SimpleNamespace(
        mail_host="smtp.example.com", mail_port=587,
        mail_username="private-user", mail_password="private-password",
        mail_from="no-reply@example.com", mail_reply_to="",
        mail_use_tls=True, mail_use_ssl=False, mail_timeout=10,
    )
    monkeypatch.setattr(email_service, "get_settings", lambda: settings)

    class BrokenSmtp:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def starttls(self, **_kwargs):
            raise email_service.smtplib.SMTPAuthenticationError(
                535, b"private-user private-password smtp.example.com")

    monkeypatch.setattr(email_service, "_smtp_connection", lambda _settings: BrokenSmtp())
    with (caplog.at_level(logging.WARNING, logger="services.email_service"),
          pytest.raises(errors.AppError) as caught):
        email_service.send_verification_code(
            recipient="test@example.com", display_name="Test", code="012345",
            purpose="registration", expires_minutes=15)

    assert caught.value.code == "MAIL_DELIVERY_FAILED"
    assert "private-user" not in caplog.text
    assert "private-password" not in caplog.text
    assert "smtp.example.com" not in caplog.text
    assert "SMTPAuthenticationError" in caplog.text


def test_email_service_rejects_multiple_recipients_before_sending(monkeypatch):
    monkeypatch.setattr(email_service, "get_settings", lambda: types.SimpleNamespace())
    with pytest.raises(errors.AppError) as caught:
        email_service.send_verification_code(
            recipient="first@example.com,second@example.com", display_name="Test",
            code="012345", purpose="registration", expires_minutes=15)
    assert caught.value.code == errors.VALIDATION_ERROR
