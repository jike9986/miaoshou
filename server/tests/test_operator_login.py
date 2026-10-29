from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from core.config import get_settings
from db.session import SessionLocal, init_db
from main import app
from services import operator_service

API = "/api/v1"


@pytest.fixture(scope="module")
def client():
    init_db()
    instance = TestClient(app, raise_server_exceptions=False)
    yield instance
    instance.close()


@pytest.fixture(scope="module")
def tokens(client):
    del client
    db = SessionLocal()
    rows = {}
    try:
        operator_service.ensure_default(db)
        for role in (operator_service.ROLE_TEACHER, operator_service.ROLE_MEMBER):
            _row, token = operator_service.create(
                db, f"login-{role}-{uuid.uuid4().hex[:8]}", role=role)
            rows[role] = token
        db.commit()
    finally:
        db.close()
    rows[operator_service.ROLE_ADMIN] = get_settings().local_secret()
    return rows


def auth(tokens, role):
    return {"Authorization": f"Bearer {tokens[role]}"}


def test_teacher_creates_student_and_student_changes_password(client, tokens):
    suffix = uuid.uuid4().hex[:8]
    created = client.post(
        API + "/operators/students",
        json={"name": f"Student {suffix}", "username": f"student-{suffix}",
              "password": "initial-pass-123"},
        headers=auth(tokens, "teacher"),
    )
    assert created.status_code == 201, created.text
    operator = created.json()["operator"]
    assert operator["role"] == "member"
    assert "password" not in operator and "passwordHash" not in operator

    login = client.post(API + "/auth/login",
                        json={"username": f"STUDENT-{suffix}",
                              "password": "initial-pass-123"})
    assert login.status_code == 200, login.text
    body = login.json()
    assert body["mustChangePassword"] is True
    locked = client.get(API + "/warehouse", headers={
        "Authorization": f"Bearer {body['token']}"})
    assert locked.status_code == 403, locked.text
    assert locked.json()["error"]["details"]["reason"] == "password_change_required"
    old_token = body["token"]
    changed = client.post(
        API + "/auth/change-password",
        json={"currentPassword": "initial-pass-123", "newPassword": "new-pass-456"},
        headers={"Authorization": f"Bearer {old_token}"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json() == {"ok": True}
    assert client.post(API + "/auth/login",
                       json={"username": f"student-{suffix}",
                             "password": "new-pass-456"}).json()["mustChangePassword"] is False


def test_reset_only_targets_students_and_revokes_old_token(client, tokens):
    suffix = uuid.uuid4().hex[:8]
    created = client.post(
        API + "/operators/students",
        json={"name": f"Student {suffix}", "username": f"student-{suffix}",
              "password": "initial-pass-123"},
        headers=auth(tokens, "admin"),
    )
    ident = created.json()["operator"]["id"]
    login = client.post(API + "/auth/login",
                        json={"username": f"student-{suffix}",
                              "password": "initial-pass-123"}).json()
    reset = client.post(
        API + f"/operators/{ident}/reset-password", json={"password": "reset-pass-456"},
        headers=auth(tokens, "teacher"),
    )
    assert reset.status_code == 200, reset.text
    stale = client.get(API + "/warehouse", headers={
        "Authorization": f"Bearer {login['token']}"})
    assert stale.status_code == 401, stale.text
    latest = client.post(API + "/auth/login",
                         json={"username": f"student-{suffix}",
                               "password": "reset-pass-456"})
    assert latest.status_code == 200, latest.text
    assert latest.json()["mustChangePassword"] is True


def test_login_failure_is_uniform_and_roles_cannot_manage_teachers(client, tokens):
    bad = client.post(API + "/auth/login", json={"username": "missing", "password": "wrong"})
    assert bad.status_code == 401
    suffix = uuid.uuid4().hex[:8]
    target = client.post(API + "/operators", json={"name": f"Teacher {suffix}",
                                                    "role": "teacher"},
                         headers=auth(tokens, "admin")).json()["operator"]["id"]
    denied = client.post(API + f"/operators/{target}/reset-password",
                         json={"password": "reset-pass-456"}, headers=auth(tokens, "teacher"))
    assert denied.status_code == 403, denied.text
    member_denied = client.post(API + "/operators/students",
                                json={"name": "No", "username": suffix, "password": "password-123"},
                                headers=auth(tokens, "member"))
    assert member_denied.status_code == 403, member_denied.text


def test_legacy_token_can_initialize_password_login(client, tokens):
    db = SessionLocal()
    try:
        legacy = operator_service.ensure_default(db)
        legacy.username = None
        legacy.password_hash = None
        db.commit()
    finally:
        db.close()
    initialized = client.post(
        API + "/auth/initialize-credentials",
        json={"username": "legacy-admin", "password": "legacy-pass-456"},
        headers=auth(tokens, "admin"),
    )
    assert initialized.status_code == 200, initialized.text
    login = client.post(API + "/auth/login",
                        json={"username": "LEGACY-ADMIN", "password": "legacy-pass-456"})
    assert login.status_code == 200, login.text
    assert login.json()["operator"]["role"] == "admin"
    assert client.get(API + "/operators", headers=auth(tokens, "admin")).json()[
        "current"]["hasLoginCredentials"] is True


def test_legacy_credential_initialization_rejects_duplicate_username(client):
    suffix = uuid.uuid4().hex[:8]
    db = SessionLocal()
    try:
        _legacy, legacy_token = operator_service.create(
            db, f"Legacy admin {suffix}", role=operator_service.ROLE_ADMIN)
        operator_service.create_student(
            db, f"Student {suffix}", f"taken-{suffix}", "student-pass-456")
        db.commit()
    finally:
        db.close()

    response = client.post(
        API + "/auth/initialize-credentials",
        json={"username": f"taken-{suffix}", "password": "legacy-pass-456"},
        headers={"Authorization": f"Bearer {legacy_token}"},
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_role_change_keeps_existing_operator_token_valid(client, tokens):
    suffix = uuid.uuid4().hex[:8]
    created = client.post(API + "/operators", json={"name": f"Role {suffix}",
                                                       "role": "teacher"},
                          headers=auth(tokens, "admin"))
    assert created.status_code == 201, created.text
    operator = created.json()["operator"]
    bearer = {"Authorization": f"Bearer {created.json()['token']}"}
    patched = client.patch(API + f"/operators/{operator['id']}",
                           json={"role": "member"}, headers=auth(tokens, "admin"))
    assert patched.status_code == 200, patched.text
    read = client.get(API + "/warehouse", headers=bearer)
    assert read.status_code == 200, read.text
    denied = client.post(API + "/publish", json={}, headers=bearer)
    assert denied.status_code == 403, denied.text


def test_operator_list_hides_usernames_from_anonymous_and_members(client, tokens):
    suffix = uuid.uuid4().hex[:8]
    created = client.post(
        API + "/operators/students",
        json={"name": f"Student {suffix}", "username": f"private-{suffix}",
              "password": "initial-pass-123"},
        headers=auth(tokens, "teacher"),
    )
    assert created.status_code == 201, created.text

    anonymous = client.get(API + "/operators").json()
    assert all("username" not in row for row in anonymous["operators"])
    assert "username" not in anonymous["current"]

    member = client.get(API + "/operators", headers=auth(tokens, "member")).json()
    assert all("username" not in row for row in member["operators"])
    teacher = client.get(API + "/operators", headers=auth(tokens, "teacher")).json()
    assert f"private-{suffix}" in {row.get("username") for row in teacher["operators"]}


def test_reset_legacy_member_assigns_usable_username(client, tokens):
    suffix = uuid.uuid4().hex[:8]
    db = SessionLocal()
    try:
        student, _old_token = operator_service.create(
            db, f"Legacy student {suffix}", role=operator_service.ROLE_MEMBER)
        ident = str(student.id)
        db.commit()
    finally:
        db.close()

    username = f"legacy-{suffix}"
    reset = client.post(
        API + f"/operators/{ident}/reset-password",
        json={"username": username, "password": "reset-pass-456"},
        headers=auth(tokens, "teacher"),
    )
    assert reset.status_code == 200, reset.text
    login = client.post(API + "/auth/login",
                        json={"username": username, "password": "reset-pass-456"})
    assert login.status_code == 200, login.text
    assert login.json()["mustChangePassword"] is True
