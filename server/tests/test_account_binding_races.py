from __future__ import annotations

import uuid

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


def test_connect_allows_multiple_account_bindings(monkeypatch) -> None:
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
                account_id=existing_account.id,
                operator_id=student.id,
            ))

        # Students may bind multiple accounts now. This binding is a second
        # valid account relation rather than a uniqueness race.
        monkeypatch.setattr(account_service, "sync_shop_table", inject_competing_binding)

        result = account_service.connect(
            db, app_id=f"loser-{suffix}", app_secret="secret",
            auto_scan=False, owner_operator_id=student.id,
            bound_by_operator_id=student.id)
        db.commit()
        bindings = db.query(AccountBinding).filter(
            AccountBinding.operator_id == student.id).all()
        assert {row.account_id for row in bindings} == {
            existing_account.id, int(result["accountId"])}
    finally:
        db.rollback()
        db.close()
