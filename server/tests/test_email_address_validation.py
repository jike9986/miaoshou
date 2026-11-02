from __future__ import annotations

import types

import pytest
from fastapi.testclient import TestClient

from core import errors
from db.session import init_db
from main import app
from services import email_service, operator_service

API = "/api/v1"


@pytest.fixture(scope="module")
def client():
    init_db()
    instance = TestClient(app, raise_server_exceptions=False)
    yield instance
    instance.close()


@pytest.mark.parametrize("value", [
    "victim@example.com,alias",
    "victim@example.com,second@example.com",
    "victim@example.com alias",
    "alias",
    "victim@example.com\nBcc: alias@example.com",
])
def test_normalize_deliverable_email_rejects_recipient_list_and_alias(value):
    with pytest.raises(errors.AppError) as caught:
        operator_service.normalize_deliverable_email(value)
    assert caught.value.code == errors.VALIDATION_ERROR


def test_normalize_email_accepts_one_mailbox():
    assert operator_service.normalize_deliverable_email(" Victim@Example.com ") == (
        "victim@example.com"
    )


@pytest.mark.parametrize("path", [
    "/auth/registration-code/request",
    "/auth/password-reset/request",
])
def test_email_code_routes_reject_recipient_lists_before_delivery(client, monkeypatch, path):
    called = False

    def fail_delivery(**_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(email_service, "send_verification_code", fail_delivery)
    response = client.post(API + path, json={"email": "victim@example.com,alias"})
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == errors.VALIDATION_ERROR
    assert called is False


def test_register_route_rejects_recipient_list(client):
    response = client.post(API + "/auth/register", json={
        "name": "Alias Test",
        "email": "victim@example.com,alias",
        "password": "alias-pass-456",
        "code": "123456",
    })
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == errors.VALIDATION_ERROR


def test_smtp_delivery_rejects_recipient_list_before_loading_settings(monkeypatch):
    monkeypatch.setattr(email_service, "get_settings", lambda: types.SimpleNamespace())
    with pytest.raises(errors.AppError) as caught:
        email_service.send_verification_code(
            recipient="victim@example.com,alias", display_name="Test",
            code="012345", purpose="registration", expires_minutes=15)
    assert caught.value.code == errors.VALIDATION_ERROR


def test_smtp_configuration_rejects_recipient_list_in_sender(monkeypatch):
    monkeypatch.setattr(email_service, "get_settings", lambda: types.SimpleNamespace(
        mail_host="smtp.example.com", mail_port=587, mail_username="",
        mail_password="", mail_from="sender@example.com,alias", mail_reply_to="",
        mail_use_tls=True, mail_use_ssl=False, mail_timeout=10,
    ))
    with pytest.raises(errors.AppError) as caught:
        email_service.verify_connection()
    assert caught.value.code == "MAIL_CONFIG_INVALID"
