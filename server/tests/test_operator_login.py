from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from core import errors
from core.config import get_settings
from db.session import SessionLocal, init_db
from main import app
from models.email_verification_code import EmailVerificationCode
from models.login_rate_limit import LoginRateLimit
from models.operator import Operator
from services import (
    email_code_service,
    email_service,
    login_rate_limit_service,
    operator_service,
)

API = "/api/v1"


@pytest.fixture(autouse=True)
def isolate_login_ip_rate_limit():
    ip = "testclient"
    login_rate_limit_service.clear_ip(ip)
    yield
    login_rate_limit_service.clear_ip(ip)


@pytest.fixture
def restore_default_operator():
    """Keep the one-time local takeover test from polluting later tests.

    The takeover endpoint intentionally retires the built-in machine identity.
    Restore its exact pre-test state after the case so tests in other modules do
    not accidentally inherit a disabled local secret.
    """
    db = SessionLocal()
    try:
        row = operator_service.ensure_default(db)
        snapshot = {
            field: getattr(row, field)
            for field in (
                "username", "email", "email_verified", "password_hash",
                "must_change_password", "enabled", "is_admin", "role",
            )
        }
        ident = row.id
        db.commit()
    finally:
        db.close()

    yield

    db = SessionLocal()
    try:
        row = db.query(Operator).filter(Operator.id == ident).one()
        for field, value in snapshot.items():
            setattr(row, field, value)
        db.commit()
    finally:
        db.close()


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
        _admin, rows[operator_service.ROLE_ADMIN] = operator_service.create(
            db, f"login-admin-{uuid.uuid4().hex[:8]}",
            role=operator_service.ROLE_ADMIN)
        db.commit()
    finally:
        db.close()
    return rows


def auth(tokens, role):
    return {"Authorization": f"Bearer {tokens[role]}"}


def test_local_secret_is_a_business_identity_in_single_machine_mode(client):
    """单机模式（默认）：本机 secret 可直接访问业务接口（免登录，2026-10-30 起）。"""
    local = {"Authorization": f"Bearer {get_settings().local_secret()}"}
    for path in ("/warehouse", "/auth/status", "/operators"):
        response = client.get(API + path, headers=local)
        assert response.status_code == 200, f"{path}: {response.status_code} {response.text}"
    # 探活接口不需要鉴权
    assert client.get(API + "/system/health").status_code == 200
    assert client.get("/api/system/health").status_code == 200


def test_students_can_validate_their_session_with_operator_probe(client):
    db = SessionLocal()
    try:
        operator, token = operator_service.create(
            db, f"session-probe-{uuid.uuid4().hex[:8]}",
            role=operator_service.ROLE_MEMBER)
        operator_id = str(operator.id)
        db.commit()
    finally:
        db.close()

    valid = client.get(API + "/operators", headers={"Authorization": f"Bearer {token}"})
    assert valid.status_code == 200, valid.text
    payload = valid.json()
    assert payload["current"]["id"] == operator_id
    assert payload["total"] == 1
    assert payload["operators"][0]["id"] == operator_id

    revoked = client.get(API + "/operators", headers={"Authorization": "Bearer revoked-token"})
    assert revoked.status_code == 401, revoked.text


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
    changed_body = changed.json()
    assert changed_body["ok"] is True
    assert changed_body["mustChangePassword"] is False
    assert changed_body["token"] != old_token
    stale = client.get(API + "/warehouse", headers={
        "Authorization": f"Bearer {old_token}"})
    assert stale.status_code == 401, stale.text
    continued = client.get(API + "/warehouse", headers={
        "Authorization": f"Bearer {changed_body['token']}"})
    assert continued.status_code == 200, continued.text
    assert client.post(API + "/auth/login",
                       json={"username": f"student-{suffix}",
                             "password": "new-pass-456"}).json()["mustChangePassword"] is False


def test_email_registration_and_email_login_keep_username_compatibility(
        client, monkeypatch):
    suffix = uuid.uuid4().hex[:8]
    email = f"Signup-{suffix}@Example.com"
    sent: dict[str, str] = {}

    def fake_send(**kwargs):
        sent.update({key: str(value) for key, value in kwargs.items()})

    monkeypatch.setattr(email_service, "send_verification_code", fake_send)
    code_response = client.post(API + "/auth/registration-code/request", json={
        "email": f"  {email}  ",
    })
    assert code_response.status_code == 202, code_response.text
    registered = client.post(API + "/auth/register", json={
        "name": f"Email Student {suffix}",
        "email": f"  {email}  ",
        "password": "signup-pass-456",
        "code": sent["code"],
    })
    assert registered.status_code == 201, registered.text
    body = registered.json()
    assert body["operator"]["role"] == operator_service.ROLE_MEMBER
    assert body["operator"]["emailVerified"] is True
    assert body["mustChangePassword"] is False
    assert body["token"]

    for credentials in (
        {"identifier": email.upper(), "password": "signup-pass-456"},
        {"email": email.lower(), "password": "signup-pass-456"},
    ):
        login = client.post(API + "/auth/login", json=credentials)
        assert login.status_code == 200, login.text
        assert login.json()["operator"]["id"] == body["operator"]["id"]

    duplicate_code = client.post(API + "/auth/registration-code/request", json={
        "email": email.lower(),
    })
    assert duplicate_code.status_code == 202, duplicate_code.text
    duplicate = client.post(API + "/auth/register", json={
        "name": f"Duplicate {suffix}",
        "email": email.lower(),
        "password": "another-pass-456",
        "code": sent["code"],
    })
    invalid_code = "000000" if sent["code"] != "000000" else "000001"
    invalid = client.post(API + "/auth/register", json={
        "name": f"Invalid Code {suffix}",
        "email": email.lower(),
        "password": "another-pass-456",
        "code": invalid_code,
    })
    assert duplicate.status_code == invalid.status_code == 400
    assert {key: value for key, value in duplicate.json()["error"].items()
            if key != "requestId"} == {
        key: value for key, value in invalid.json()["error"].items()
        if key != "requestId"
    }

    # A correct code rejected for a duplicate mailbox is still consumed.
    with SessionLocal() as db:
        stored = (db.query(EmailVerificationCode)
                  .filter_by(purpose=email_code_service.PURPOSE_REGISTRATION,
                             email=email.lower())
                  .order_by(EmailVerificationCode.id.desc())
                  .first())
        assert stored is not None and stored.used_at is not None
    replay = client.post(API + "/auth/register", json={
        "name": f"Duplicate Replay {suffix}",
        "email": email.lower(),
        "password": "another-pass-456",
        "code": sent["code"],
    })
    assert replay.status_code == duplicate.status_code
    assert {key: value for key, value in replay.json()["error"].items()
            if key != "requestId"} == {
        key: value for key, value in duplicate.json()["error"].items()
        if key != "requestId"
    }


def test_registration_email_uniqueness_race_is_generic_and_consumes_code(
        client, monkeypatch):
    suffix = uuid.uuid4().hex[:8]
    email = f"race-{suffix}@example.com"
    sent: dict[str, str] = {}
    monkeypatch.setattr(email_service, "send_verification_code",
                        lambda **kwargs: sent.update(kwargs))
    requested = client.post(API + "/auth/registration-code/request", json={
        "email": email,
    })
    assert requested.status_code == 202, requested.text

    def concurrent_registration_wins(db, name, requested_email, password):
        # Simulate another request committing after the route's ownership check.
        db.rollback()
        suffix = uuid.uuid4().hex[:8]
        operator_service.create_student(
            db, f"Race Winner {suffix}", f"race-winner-{suffix}",
            "winner-pass-456", email=requested_email)
        db.commit()
        raise errors.AppError("CONFLICT", "该邮箱已注册", status=409)

    monkeypatch.setattr(operator_service, "register_email_account",
                        concurrent_registration_wins)
    payload = {"name": f"Race Loser {suffix}", "email": email,
               "password": "loser-pass-456", "code": sent["code"]}
    raced = client.post(API + "/auth/register", json=payload)
    invalid_code = "000000" if sent["code"] != "000000" else "000001"
    invalid = client.post(API + "/auth/register", json={**payload, "code": invalid_code})
    assert raced.status_code == invalid.status_code == 400
    assert {key: value for key, value in raced.json()["error"].items()
            if key != "requestId"} == {
        key: value for key, value in invalid.json()["error"].items()
        if key != "requestId"
    }
    with SessionLocal() as db:
        stored = (db.query(EmailVerificationCode)
                  .filter_by(purpose=email_code_service.PURPOSE_REGISTRATION,
                             email=email)
                  .order_by(EmailVerificationCode.id.desc())
                  .first())
        assert stored is not None and stored.used_at is not None


def test_login_accepts_email_in_legacy_username_field(client):
    suffix = uuid.uuid4().hex[:8]
    db = SessionLocal()
    try:
        row, _token = operator_service.create(
            db, f"Email Login {suffix}", role=operator_service.ROLE_MEMBER,
            email=f"legacy-{suffix}@example.com")
        operator_service.set_password(
            db, row, "legacy-email-pass-456", must_change=False)
        db.commit()
        operator_id = str(row.id)
    finally:
        db.close()

    login = client.post(API + "/auth/login", json={
        "username": f"LEGACY-{suffix}@EXAMPLE.COM",
        "password": "legacy-email-pass-456",
    })
    assert login.status_code == 200, login.text
    assert login.json()["operator"]["id"] == operator_id


def test_password_authentication_uses_dummy_hash_for_unknown_and_disabled_accounts(
        monkeypatch):
    suffix = uuid.uuid4().hex[:8]
    username = f"timing-{suffix}"
    password = "timing-pass-456"
    db = SessionLocal()
    seen_hashes: list[str | None] = []
    verify = operator_service.verify_password

    def tracked_verify(candidate: str, encoded: str | None) -> bool:
        seen_hashes.append(encoded)
        return verify(candidate, encoded)

    monkeypatch.setattr(operator_service, "verify_password", tracked_verify)
    try:
        row = operator_service.create_student(
            db, f"Timing {suffix}", username, password)
        db.commit()
        saved_hash = row.password_hash

        assert operator_service.authenticate_password(
            db, f"unknown-{suffix}", password) is None
        row.enabled = False
        db.commit()
        assert operator_service.authenticate_password(db, username, password) is None
        row.enabled = True
        db.commit()
        assert operator_service.authenticate_password(
            db, username, "incorrect-pass-456") is None
        authenticated = operator_service.authenticate_password(db, username, password)

        assert authenticated is not None
        assert authenticated[0].id == row.id
        assert seen_hashes == [
            operator_service.DUMMY_PASSWORD_HASH,
            operator_service.DUMMY_PASSWORD_HASH,
            saved_hash,
            saved_hash,
        ]
    finally:
        db.close()


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


def test_teacher_password_reset_cannot_replace_student_recovery_email(client, tokens):
    suffix = uuid.uuid4().hex[:8]
    original_email = f"student-{suffix}@example.com"
    replacement_email = f"replacement-{suffix}@example.com"
    created = client.post(
        API + "/operators/students",
        json={"name": f"Student {suffix}", "username": f"student-{suffix}",
              "password": "initial-pass-123", "email": original_email},
        headers=auth(tokens, "admin"),
    )
    assert created.status_code == 201, created.text
    ident = created.json()["operator"]["id"]
    with SessionLocal() as db:
        student = operator_service.get(db, ident)
        student.email_verified = True
        db.commit()

    reset = client.post(
        API + f"/operators/{ident}/reset-password",
        json={"password": "reset-pass-456", "email": replacement_email},
        headers=auth(tokens, "teacher"),
    )
    assert reset.status_code == 400, reset.text

    with SessionLocal() as db:
        student = operator_service.get(db, ident)
        assert student.email == original_email
        assert student.email_verified is True


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


def test_legacy_token_can_initialize_password_login(client, tokens,
                                                    restore_default_operator):
    before = client.get(API + "/auth/credentials-status")
    assert before.status_code == 200, before.text
    assert before.json() == {"initialized": False}
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
        headers={"Authorization": f"Bearer {get_settings().local_secret()}"},
    )
    assert initialized.status_code == 200, initialized.text
    assert initialized.json()["operator"]["name"] == "legacy-admin"
    after = client.get(API + "/auth/credentials-status")
    assert after.status_code == 200, after.text
    assert after.json() == {"initialized": True}
    db = SessionLocal()
    try:
        builtin = operator_service.ensure_default(db)
        assert initialized.json()["operator"]["id"] != str(builtin.id)
        assert builtin.enabled is False
        assert builtin.role == operator_service.ROLE_MEMBER
    finally:
        db.close()
    admin_headers = {"Authorization": f"Bearer {initialized.json()['token']}"}
    assert client.get(API + "/operators", headers=admin_headers).status_code == 200
    assert client.get(API + "/warehouse", headers=admin_headers).status_code == 200
    login = client.post(API + "/auth/login",
                        json={"username": "LEGACY-ADMIN", "password": "legacy-pass-456"})
    assert login.status_code == 200, login.text
    assert login.json()["operator"]["role"] == operator_service.ROLE_ADMIN
    assert login.json()["operator"]["name"] == "legacy-admin"
    login_headers = {"Authorization": f"Bearer {login.json()['token']}"}
    assert client.get(API + "/operators", headers=login_headers).json()[
        "current"]["hasLoginCredentials"] is True

    # 初始化老师后，内置「本机」记录会被停用（initialize_credentials 的既定行为），
    # 此时本机 secret 解析到已停用的记录 → 403。免登录场景不需要走这一步初始化，
    # 这里只验证「能初始化 + 初始化后本机记录被正确退休」。
    local = {"Authorization": f"Bearer {get_settings().local_secret()}"}
    assert client.get(API + "/warehouse", headers=local).status_code == 403
    # 凭据初始化是一次性的：再初始化应 409
    again = client.post(API + "/auth/initialize-credentials",
                        json={"username": "second-teacher", "password": "second-pass-456"},
                        headers=local)
    assert again.status_code == 409, again.text


def test_legacy_credential_initialization_is_one_time(client):
    suffix = uuid.uuid4().hex[:8]
    db = SessionLocal()
    try:
        legacy, _legacy_token = operator_service.create(
            db, f"Legacy admin {suffix}", role=operator_service.ROLE_ADMIN)
        operator_service.set_password(
            db, legacy, "existing-admin-pass-456", must_change=False,
            username=f"legacy-admin-{suffix}")
        operator_service.create_student(
            db, f"Student {suffix}", f"taken-{suffix}", "student-pass-456")
        db.commit()
    finally:
        db.close()

    response = client.post(
        API + "/auth/initialize-credentials",
        json={"username": f"taken-{suffix}", "password": "legacy-pass-456"},
        headers={"Authorization": f"Bearer {get_settings().local_secret()}"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["details"]["reason"] == "credentials_initialized"


def test_credential_initialization_is_disabled_in_shared_mode(client, monkeypatch):
    from types import SimpleNamespace

    import api.auth as auth_module

    local_secret = get_settings().local_secret()
    monkeypatch.setattr(
        auth_module, "get_settings",
        lambda: SimpleNamespace(shared_mode=True, local_secret=lambda: local_secret),
    )
    response = client.post(
        API + "/auth/initialize-credentials",
        json={"username": f"shared-init-{uuid.uuid4().hex[:8]}",
              "password": "initialize-pass-456"},
        headers={"Authorization": f"Bearer {local_secret}"},
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "FORBIDDEN"


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
    publish = client.post(API + "/publish", json={}, headers=bearer)
    assert publish.status_code != 403, publish.text


def test_operator_list_requires_personal_identity_and_hides_usernames_from_members(client, tokens):
    suffix = uuid.uuid4().hex[:8]
    created = client.post(
        API + "/operators/students",
        json={"name": f"Student {suffix}", "username": f"private-{suffix}",
              "password": "initial-pass-123"},
        headers=auth(tokens, "teacher"),
    )
    assert created.status_code == 201, created.text

    anonymous = client.get(API + "/operators")
    assert anonymous.status_code == 401
    # 单机模式：本机 secret 是合法身份，能过鉴权（不再 401）。
    # 具体 200/403 取决于内置「本机」记录当前的启用/角色状态（可能被其它用例
    # 或 initialize-credentials 改成停用），这里只断言「不再是 401 未授权」。
    local = client.get(API + "/operators", headers={
        "Authorization": f"Bearer {get_settings().local_secret()}"})
    assert local.status_code != 401

    member = client.get(API + "/operators", headers=auth(tokens, "member")).json()
    assert all("username" not in row for row in member["operators"])
    assert [row["id"] for row in member["operators"]] == [member["current"]["id"]]
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


def test_login_failures_are_persistently_throttled_and_uniform(client):
    suffix = uuid.uuid4().hex[:10]
    ip = "testclient"
    login_rate_limit_service.clear_ip(ip)
    unknown = client.post(
        API + "/auth/login",
        json={"username": f"missing-{suffix}", "password": "wrong-pass"},
    )
    bad_password = client.post(
        API + "/auth/login",
        json={"username": f"missing-{suffix}", "password": "other-pass"},
    )
    assert unknown.status_code == bad_password.status_code == 401
    assert unknown.json()["error"]["message"] == bad_password.json()["error"]["message"]
    login_rate_limit_service.clear(ip, f"missing-{suffix}")

    username = f"limited-{suffix}"
    ip_key, username_key = login_rate_limit_service.bucket_keys(ip, username)
    try:
        responses = [
            client.post(API + "/auth/login", json={
                "username": username, "password": "wrong-pass"})
            for _ in range(login_rate_limit_service.MAX_FAILURES)
        ]
        assert [response.status_code for response in responses] == [401] * \
            login_rate_limit_service.MAX_FAILURES
        blocked = client.post(API + "/auth/login", json={
            "username": username, "password": "wrong-pass"})
        assert blocked.status_code == 429
        assert blocked.headers["Retry-After"].isdigit()
        assert blocked.json()["error"]["code"] == "RATE_LIMITED"
        assert blocked.json()["error"]["details"]["retryAfterSeconds"] > 0

        with SessionLocal() as db:
            rows = db.query(LoginRateLimit).filter(
                LoginRateLimit.bucket_key.in_((ip_key, username_key))
            ).all()
            assert len(rows) == 2
            assert {row.failure_count for row in rows} == {
                login_rate_limit_service.MAX_FAILURES
            }
            assert all(row.locked_until > 0 for row in rows)

        # A correct login clears only its username bucket after a prior typo.
        login_rate_limit_service.clear(ip, username)
        success_username = f"success-{suffix}"
        db = SessionLocal()
        try:
            operator_service.create_student(
                db, f"Limited {suffix}", success_username, "correct-pass-456")
            db.commit()
        finally:
            db.close()
        typo = client.post(API + "/auth/login", json={
            "username": success_username, "password": "wrong-pass"})
        assert typo.status_code == 401
        success = client.post(API + "/auth/login", json={
            "username": success_username, "password": "correct-pass-456"})
        assert success.status_code == 200, success.text
        with SessionLocal() as db:
            ip_key, username_key = login_rate_limit_service.bucket_keys(
                ip, success_username)
            assert db.query(LoginRateLimit).filter(
                LoginRateLimit.bucket_key == username_key
            ).count() == 0
            assert db.query(LoginRateLimit).filter(
                LoginRateLimit.bucket_key == ip_key
            ).one().failure_count == 1
    finally:
        login_rate_limit_service.clear(ip, username)
        login_rate_limit_service.clear(ip, f"missing-{suffix}")
        login_rate_limit_service.clear(ip, f"success-{suffix}")


def test_successful_login_does_not_clear_shared_ip_failures(client):
    suffix = uuid.uuid4().hex[:10]
    ip = "testclient"
    usernames = [f"spray-{suffix}-{index}" for index in range(
        login_rate_limit_service.MAX_FAILURES - 1)]
    valid_username = f"valid-{suffix}"
    for username in [*usernames, valid_username, f"after-{suffix}"]:
        login_rate_limit_service.clear(ip, username)

    db = SessionLocal()
    try:
        operator_service.create_student(
            db, f"Valid login {suffix}", valid_username, "correct-pass-456")
        db.commit()
    finally:
        db.close()

    try:
        for username in usernames:
            failed = client.post(API + "/auth/login", json={
                "username": username, "password": "wrong-pass"})
            assert failed.status_code == 401

        success = client.post(API + "/auth/login", json={
            "username": valid_username, "password": "correct-pass-456"})
        assert success.status_code == 200, success.text

        ip_key, username_key = login_rate_limit_service.bucket_keys(ip, valid_username)
        with SessionLocal() as db:
            assert db.query(LoginRateLimit).filter(
                LoginRateLimit.bucket_key == ip_key
            ).one().failure_count == login_rate_limit_service.MAX_FAILURES - 1
            assert db.query(LoginRateLimit).filter(
                LoginRateLimit.bucket_key == username_key
            ).count() == 0

        blocked = client.post(API + "/auth/login", json={
            "username": f"after-{suffix}", "password": "wrong-pass"})
        assert blocked.status_code == 401
        locked = client.post(API + "/auth/login", json={
            "username": f"after-{suffix}", "password": "wrong-pass"})
        assert locked.status_code == 429
        assert locked.json()["error"]["code"] == "RATE_LIMITED"
    finally:
        for username in [*usernames, valid_username, f"after-{suffix}"]:
            login_rate_limit_service.clear(ip, username)
        login_rate_limit_service.clear_ip(ip)
