from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from db.session import SessionLocal, init_db
from main import app
from models.account import Account
from models.account_binding import AccountBinding
from services import account_service, operator_service

API = "/api/v1"


@pytest.fixture(scope="module")
def client():
    init_db()
    instance = TestClient(app, raise_server_exceptions=False)
    yield instance
    instance.close()


@pytest.fixture(scope="module")
def bindings_setup(client):
    del client
    suffix = uuid.uuid4().hex[:8]
    db = SessionLocal()
    try:
        teacher, teacher_token = operator_service.create(
            db, f"binding-teacher-{suffix}", role=operator_service.ROLE_TEACHER)
        first, first_token = operator_service.create(
            db, f"binding-first-{suffix}", role=operator_service.ROLE_MEMBER)
        second, second_token = operator_service.create(
            db, f"binding-second-{suffix}", role=operator_service.ROLE_MEMBER)
        unbound, unbound_token = operator_service.create(
            db, f"binding-unbound-{suffix}", role=operator_service.ROLE_MEMBER)

        account = Account(
            name=f"shared-{suffix}",
            app_key=f"private-app-key-{suffix}",
            app_secret_enc=account_service.crypto.encrypt(f"private-secret-{suffix}"),
            app_id_hint="should-not-be-trusted",
            secret_hint=f"…{suffix[-4:]}",
            status="active",
            last_verified_at=1_800_000_000.0,
            shops="[]",
        )
        db.add(account)
        db.flush()
        second_account = Account(
            name=f"shared-second-{suffix}",
            app_key=f"second-private-app-key-{suffix}",
            app_secret_enc=account_service.crypto.encrypt(f"second-private-secret-{suffix}"),
            secret_hint=f"…{suffix[:4]}",
            status="invalid",
            shops="[]",
        )
        db.add(second_account)
        db.flush()
        db.add_all([
            AccountBinding(account_id=account.id, operator_id=first.id,
                           bound_by_operator_id=teacher.id, bound_at=1_800_000_010.0),
            AccountBinding(account_id=second_account.id, operator_id=first.id,
                           bound_by_operator_id=teacher.id, bound_at=1_800_000_015.0),
            AccountBinding(account_id=account.id, operator_id=second.id,
                           bound_by_operator_id=teacher.id, bound_at=1_800_000_020.0),
        ])
        db.commit()
        return {
            "teacher_token": teacher_token,
            "member_token": first_token,
            "other_member_token": second_token,
            "unbound_token": unbound_token,
            "first_id": str(first.id),
            "second_id": str(second.id),
            "second_name": second.name,
            "unbound_id": str(unbound.id),
            "app_key": account.app_key,
            "secret": f"private-secret-{suffix}",
            "second_app_key": second_account.app_key,
            "second_secret": f"second-private-secret-{suffix}",
            "app_id_hint": f"…{account.app_key[-4:]}",
            "secret_hint": f"…{suffix[-4:]}",
            "second_app_id_hint": f"…{second_account.app_key[-4:]}",
            "second_secret_hint": f"…{suffix[:4]}",
        }
    finally:
        db.close()


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_teacher_can_see_shared_bindings_and_unbound_students(client, bindings_setup):
    response = client.get(
        API + "/operators",
        headers=headers(bindings_setup["teacher_token"]),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    by_id = {row["id"]: row for row in body["operators"]}

    first = by_id[bindings_setup["first_id"]]["miaoshouBindings"]
    second = by_id[bindings_setup["second_id"]]["miaoshouBinding"]
    unbound = by_id[bindings_setup["unbound_id"]]["miaoshouBindings"]
    assert len(first) == 2
    assert first[0]["appIdHint"] == bindings_setup["app_id_hint"]
    assert first[0]["secretHint"] == bindings_setup["secret_hint"]
    assert first[0]["status"] == "active"
    assert first[0]["boundAt"] == 1_800_000_010.0
    assert first[1]["appIdHint"] == bindings_setup["second_app_id_hint"]
    assert first[1]["secretHint"] == bindings_setup["second_secret_hint"]
    assert first[1]["status"] == "invalid"
    assert first[1]["boundAt"] == 1_800_000_015.0
    for row in (second,):
        assert row["linked"] is True
        assert row["appIdHint"] == bindings_setup["app_id_hint"]
        assert row["secretHint"] == bindings_setup["secret_hint"]
        assert row["status"] == "active"
    assert unbound == []

    serialized = response.text
    assert bindings_setup["app_key"] not in serialized
    assert bindings_setup["secret"] not in serialized
    assert bindings_setup["second_app_key"] not in serialized
    assert bindings_setup["second_secret"] not in serialized
    assert "appSecretEnc" not in serialized


def test_student_response_does_not_include_other_students_bindings(client, bindings_setup):
    response = client.get(
        API + "/operators",
        headers=headers(bindings_setup["member_token"]),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [row["id"] for row in body["operators"]] == [bindings_setup["first_id"]]
    assert "miaoshouBinding" not in body["operators"][0]
    assert bindings_setup["second_name"] not in response.text
