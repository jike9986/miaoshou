"""Member category-cache reads must stay within bound Miaoshou accounts."""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from db.session import SessionLocal, init_db
from main import app
from models.account import Account
from models.account_binding import AccountBinding
from models.category import Category
from models.operator import Operator
from services import operator_service

API = "/api/v1"


@pytest.fixture(scope="module")
def client():
    init_db()
    instance = TestClient(app, raise_server_exceptions=False)
    yield instance
    instance.close()


def test_member_cannot_read_another_accounts_category_cache(client):
    operator_id = own_id = foreign_id = None
    cid = None
    suffix = uuid.uuid4().hex[:10]
    db = SessionLocal()
    try:
        member, token = operator_service.create(
            db, f"category-member-{suffix}", role=operator_service.ROLE_MEMBER)
        own_account = Account(name=f"category-own-{suffix}", app_key=f"own-{suffix}",
                              app_secret_enc="", shops="[]", is_default=False)
        foreign_account = Account(name=f"category-foreign-{suffix}",
                                  app_key=f"foreign-{suffix}", app_secret_enc="",
                                  shops="[]", is_default=False)
        db.add_all([own_account, foreign_account])
        db.flush()
        db.add(AccountBinding(account_id=own_account.id, operator_id=member.id))
        db.add(Category(account_id=foreign_account.id, cid=f"cid-{suffix}",
                        site="MY", name="Private account category",
                        name_cn="私有账号类目", path="私有账号类目", is_leaf=True))
        operator_id = member.id
        own_id = own_account.id
        foreign_id, cid = foreign_account.id, f"cid-{suffix}"
        db.commit()
    finally:
        db.close()

    try:
        headers = {"Authorization": f"Bearer {token}"}
        foreign_tree = client.get(API + f"/categories?accountId={foreign_id}", headers=headers)
        assert foreign_tree.status_code == 403, foreign_tree.text
        foreign_path = client.get(
            API + f"/categories/{cid}/path?accountId={foreign_id}", headers=headers)
        assert foreign_path.status_code == 403, foreign_path.text
    finally:
        # These requests use the session-wide SQLite test database. Remove only
        # rows created above so the global category guard sees its original state.
        cleanup = SessionLocal()
        try:
            if foreign_id is not None and cid is not None:
                cleanup.query(Category).filter(
                    Category.account_id == foreign_id, Category.cid == cid).delete(
                        synchronize_session=False)
            if own_id is not None:
                cleanup.query(AccountBinding).filter(
                    AccountBinding.account_id == own_id).delete(synchronize_session=False)
                cleanup.query(Account).filter(Account.id == own_id).delete(
                    synchronize_session=False)
            if foreign_id is not None:
                cleanup.query(Account).filter(Account.id == foreign_id).delete(
                    synchronize_session=False)
            if operator_id is not None:
                cleanup.query(Operator).filter(Operator.id == operator_id).delete(
                    synchronize_session=False)
            cleanup.commit()
        finally:
            cleanup.close()
