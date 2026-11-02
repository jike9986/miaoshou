from __future__ import annotations

import ssl
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from db.session import SessionLocal, init_db
from main import app
from models.email_verification_code import EmailVerificationCode
from services import email_code_service, email_service, login_rate_limit_service


def _settings(*, use_tls: bool, use_ssl: bool, port: int) -> SimpleNamespace:
    return SimpleNamespace(
        mail_host="smtp.example.test",
        mail_port=port,
        mail_username="test-only-user",
        mail_password="test-only-password",
        mail_from="no-reply@example.test",
        mail_reply_to="support@example.test",
        mail_use_tls=use_tls,
        mail_use_ssl=use_ssl,
        mail_timeout=12.5,
    )


@pytest.mark.parametrize(
    ("use_tls", "use_ssl", "port", "constructor_name"),
    [
        (True, False, 587, "SMTP"),
        (False, True, 465, "SMTP_SSL"),
    ],
)
def test_verification_email_uses_configured_smtp_transport(
        monkeypatch, use_tls, use_ssl, port, constructor_name):
    settings = _settings(use_tls=use_tls, use_ssl=use_ssl, port=port)
    monkeypatch.setattr(email_service, "get_settings", lambda: settings)

    events = []
    sent_messages = []

    class FakeSmtp:
        def __enter__(self):
            events.append(("enter",))
            return self

        def __exit__(self, *_args):
            events.append(("exit",))
            return False

        def starttls(self, *, context):
            events.append(("starttls", context))

        def login(self, username, password):
            events.append(("login", username, password))

        def send_message(self, message):
            events.append(("send_message",))
            sent_messages.append(message)

    smtp = FakeSmtp()
    connection_calls = []

    def connect(*args, **kwargs):
        connection_calls.append((args, kwargs))
        return smtp

    unused = "SMTP_SSL" if constructor_name == "SMTP" else "SMTP"
    monkeypatch.setattr(email_service.smtplib, constructor_name, connect)
    monkeypatch.setattr(
        email_service.smtplib, unused,
        lambda *_args, **_kwargs: pytest.fail(f"unexpected {unused} transport"),
    )

    email_service.send_verification_code(
        recipient="student@example.test",
        display_name="Test Student",
        code="123456",
        purpose=email_code_service.PURPOSE_REGISTRATION,
        expires_minutes=15,
    )

    assert len(connection_calls) == 1
    args, kwargs = connection_calls[0]
    assert args == (settings.mail_host, port)
    assert kwargs["timeout"] == settings.mail_timeout
    if use_ssl:
        assert isinstance(kwargs["context"], ssl.SSLContext)
    else:
        assert kwargs == {"timeout": settings.mail_timeout}

    expected_events = [("enter",)]
    if use_tls:
        assert events[1][0] == "starttls"
        assert isinstance(events[1][1], ssl.SSLContext)
        expected_events.append(events[1])
    expected_events.extend([
        ("login", settings.mail_username, settings.mail_password),
        ("send_message",),
        ("exit",),
    ])
    assert events == expected_events

    assert len(sent_messages) == 1
    message = sent_messages[0]
    assert message["To"] == "student@example.test"
    assert message["From"] == "奥皇上品 <no-reply@example.test>"
    assert message["Reply-To"] == "support@example.test"
    assert message["Subject"] == "奥皇上品邮箱验证码"
    assert "完成邮箱注册" in message.get_content()
    assert "123456" in message.get_content()


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app, raise_server_exceptions=False) as instance:
        yield instance


def test_registration_code_route_reports_smtp_delivery_failure(
        client, monkeypatch):
    email = f"smtp-failure-{uuid.uuid4().hex[:10]}@example.test"
    namespace = f"email-code:{email_code_service.PURPOSE_REGISTRATION}"
    login_rate_limit_service.clear_ip("testclient", namespace=namespace)
    settings = _settings(use_tls=True, use_ssl=False, port=587)
    monkeypatch.setattr(email_service, "get_settings", lambda: settings)

    def fail_delivery(_settings, _message):
        raise email_service.smtplib.SMTPAuthenticationError(
            535, b"test-only SMTP authentication failure")

    monkeypatch.setattr(email_service, "_deliver", fail_delivery)
    try:
        response = client.post("/api/v1/auth/registration-code/request", json={
            "email": email,
        })
    finally:
        login_rate_limit_service.clear_ip("testclient", namespace=namespace)

    assert response.status_code == 502, response.text
    assert response.json()["error"]["code"] == "MAIL_DELIVERY_FAILED"
    assert response.json()["error"]["message"] == "验证码邮件发送失败，请稍后重试"
    with SessionLocal() as db:
        assert db.query(EmailVerificationCode).filter_by(
            purpose=email_code_service.PURPOSE_REGISTRATION,
            email=email).count() == 0


def test_registration_code_route_exposes_missing_mail_configuration(
        client, monkeypatch):
    email = f"smtp-unconfigured-{uuid.uuid4().hex[:10]}@example.test"
    namespace = f"email-code:{email_code_service.PURPOSE_REGISTRATION}"
    login_rate_limit_service.clear_ip("testclient", namespace=namespace)
    settings = _settings(use_tls=True, use_ssl=False, port=587)
    settings.mail_host = ""
    settings.mail_from = ""
    monkeypatch.setattr(email_service, "get_settings", lambda: settings)

    try:
        response = client.post("/api/v1/auth/registration-code/request", json={
            "email": email,
        })
    finally:
        login_rate_limit_service.clear_ip("testclient", namespace=namespace)

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "MAIL_NOT_CONFIGURED"
    assert response.json()["error"]["message"] == "服务器尚未配置邮箱验证码邮件服务"
    with SessionLocal() as db:
        assert db.query(EmailVerificationCode).filter_by(
            purpose=email_code_service.PURPOSE_REGISTRATION,
            email=email).count() == 0
