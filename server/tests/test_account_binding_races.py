from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from core.errors import AppError
from db.session import SessionLocal, init_db
from models.account import Account
from models.account_binding import AccountBinding
from services import account_service, operator_service


class FakeMiaoshouClient:
    def __init__(self, *_args):
        pass

    def shop_list(self, **_kwargs):
        return {"data": {"shopList": []}}

    def close(self):
        pass


@pytest.mark.parametrize("other_integrity_error", [False, True])
def test_connect_maps_only_operator_binding_unique_race(
        monkeypatch, other_integrity_error: bool) -> None:
    init_db()
    db = SessionLocal()
    suffix = uuid.uuid4().hex
    try:
        student, _token = operator_service.create(
            db, f"binding-race-{suffix}", role=operator_service.ROLE_MEMBER)
        existing_account = Account(
            name=f"race-winner-{suffix}", app_key=f"winner-{suffix}",
            app_secret_enc="", status="active", shops="[]")
        db.add(existing_account)
        db.commit()

        monkeypatch.setattr(account_service, "MiaoshouClient", FakeMiaoshouClient)

        def inject_competing_binding(session, _account, _shops):
            session.add(AccountBinding(
                account_id=(999_999_999 if other_integrity_error
                            else existing_account.id),
                operator_id=student.id,
            ))

        # Add a competing write after the service's availability read. With
        # autoflush disabled, connect() then attempts its own binding and the
        # database unique constraint decides the race at the final flush.
        monkeypatch.setattr(account_service, "sync_shop_table", inject_competing_binding)

        if other_integrity_error:
            with pytest.raises(IntegrityError):
                account_service.connect(
                    db, app_id=f"loser-{suffix}", app_secret="secret",
                    auto_scan=False, owner_operator_id=student.id,
                    bound_by_operator_id=student.id)
        else:
            with pytest.raises(AppError) as caught:
                account_service.connect(
                    db, app_id=f"loser-{suffix}", app_secret="secret",
                    auto_scan=False, owner_operator_id=student.id,
                    bound_by_operator_id=student.id)
            assert caught.value.status == 409
            assert caught.value.code == "CONFLICT"
            assert "secret" not in str(caught.value)
    finally:
        db.rollback()
        db.close()
