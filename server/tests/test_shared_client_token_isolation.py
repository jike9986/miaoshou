from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from core import errors
from core.config import get_settings
from core.utils import now_ts
from db.session import SessionLocal, init_db
from main import app
from models.account import Account
from models.account_binding import AccountBinding
from models.batch import Batch
from models.operator import Operator
from services import account_service, claim_job_service, operator_service

API = "/api/v1"


@pytest.fixture
def client():
    init_db()
    instance = TestClient(app, raise_server_exceptions=False)
    yield instance
    instance.close()


def test_shared_client_token_cannot_read_or_write_user_data(client, monkeypatch):
    settings = get_settings()
    shared_token = f"shared-client-{uuid.uuid4().hex}"
    monkeypatch.setattr(settings, "shared_mode", True)
    monkeypatch.setattr(settings, "client_token", shared_token)

    db = SessionLocal()
    try:
        # Keep a legacy stored client identity to cover deployments where an
        # earlier request already materialized the shared token as an operator.
        client_operator = operator_service.ensure_client(db)
        owner, _ = operator_service.create(
            db, f"token-owner-{uuid.uuid4().hex[:8]}",
            role=operator_service.ROLE_MEMBER)
        account = Account(name=f"token-account-{uuid.uuid4().hex[:8]}",
                          app_key="test-key", owner_operator_id=owner.id,
                          status="active", is_default=False)
        db.add(account)
        db.flush()
        db.add(AccountBinding(account_id=account.id, operator_id=owner.id,
                              bound_by_operator_id=owner.id, bound_at=now_ts()))
        batch = Batch(id=f"clm_{uuid.uuid4().hex[:12]}", status="done",
                      account_id=account.id, shop_id="7", detail_ids=["123"],
                      current_index=1, settings={"kind": claim_job_service.KIND},
                      results=[], errors=[], operator_id=owner.id)
        db.add(batch)
        db.commit()
        account_id = account.id
        batch_id = batch.id
        assert client_operator.id is not None
    finally:
        db.close()

    try:
        # The old shared credential used to expose accounts and claim jobs and could
        # reach any publish route. All business routes now require a personal token.
        paths = [
            ("GET", f"{API}/warehouse"),
            ("GET", f"{API}/auth/status"),
            ("GET", f"{API}/auth/accounts"),
            ("GET", f"{API}/claim-jobs?accountId={account_id}"),
            ("GET", f"{API}/claim-jobs/{batch_id}?accountId={account_id}"),
            ("GET", f"{API}/operators"),
        ]
        for method, path in paths:
            response = client.request(method, path,
                                      headers={"Authorization": f"Bearer {shared_token}"})
            assert response.status_code == 401, (
                f"{path}: {response.status_code} {response.text}")

        publish = client.post(
            f"{API}/publish",
            headers={"Authorization": f"Bearer {shared_token}"},
            json={"confirm": True, "accountId": str(account_id), "shopId": "7",
                  "items": [{"warehouseId": "123"}]},
        )
        assert publish.status_code == 401, publish.text

        # A previously stored client token is rejected even after configuration
        # changes, instead of being resolved as the legacy global client operator.
        monkeypatch.setattr(settings, "client_token", f"rotated-{uuid.uuid4().hex}")
        legacy = client.get(f"{API}/claim-jobs?accountId={account_id}",
                            headers={"Authorization": f"Bearer {shared_token}"})
        assert legacy.status_code == 401, legacy.text
    finally:
        cleanup = SessionLocal()
        try:
            row = operator_service.get(cleanup, client_operator.id)
            cleanup.delete(row)
            cleanup.commit()
        finally:
            cleanup.close()


def test_client_operator_has_no_account_or_claim_job_scope_bypass(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "client_token", f"client-{uuid.uuid4().hex}")
    db = SessionLocal()
    try:
        client_operator = operator_service.ensure_client(db)
        owner, _ = operator_service.create(
            db, f"scope-owner-{uuid.uuid4().hex[:8]}",
            role=operator_service.ROLE_MEMBER)
        account = Account(name=f"scope-account-{uuid.uuid4().hex[:8]}",
                          app_key=f"test-key-{uuid.uuid4().hex}",
                          owner_operator_id=owner.id,
                          status="active", is_default=False)
        db.add(account)
        db.flush()
        # Seed an existing owner's job directly. Creating through the claim-job
        # service now correctly validates that the member owns the source product.
        job = Batch(id=f"clm_{uuid.uuid4().hex[:12]}", status="done",
                    account_id=account.id, shop_id="7", detail_ids=["123"],
                    current_index=1,
                    settings={"kind": claim_job_service.KIND}, results=[], errors=[],
                    operator_id=owner.id)
        db.add(job)
        db.commit()

        db.info["account_request_operator"] = (
            client_operator.id, operator_service.ROLE_MEMBER)
        with pytest.raises(errors.AppError) as account_error:
            account_service.require_account(db, str(account.id))
        assert getattr(account_error.value, "status", None) == 403

        with pytest.raises(errors.AppError) as job_error:
            claim_job_service.get(db, job.id, account.id, operator=client_operator)
        assert job_error.value.code == errors.NOT_FOUND
        assert claim_job_service.list_jobs(
            db, account.id, 50, operator=client_operator)["total"] == 0
    finally:
        row = db.query(Operator).filter_by(
            id=locals().get("client_operator").id
        ).one_or_none() if locals().get("client_operator") is not None else None
        if row is not None:
            db.delete(row)
            db.commit()
        else:
            db.rollback()
        db.close()
