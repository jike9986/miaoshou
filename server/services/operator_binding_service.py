"""Teacher-facing summaries of each student's current Miaoshou binding."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from models.account import Account
from models.account_binding import AccountBinding
from models.operator import Operator
from services import operator_service


def student_miaoshou_bindings(db: Session) -> dict[str, dict[str, Any] | None]:
    """Return each student's masked current binding, including unbound students.

    The account binding table is the source of truth for shared connections: a
    single account may therefore appear once for each student who is bound to
    it. Credential material is deliberately not selected for serialization.
    """
    rows = (
        db.query(Operator, AccountBinding, Account)
        .outerjoin(AccountBinding, AccountBinding.operator_id == Operator.id)
        .outerjoin(Account, Account.id == AccountBinding.account_id)
        .order_by(Operator.id.asc())
        .all()
    )

    summaries: dict[str, dict[str, Any] | None] = {}
    for operator, binding, account in rows:
        if operator_service.role_of(operator) != operator_service.ROLE_MEMBER:
            continue

        bound = binding is not None and account is not None
        if not bound:
            summaries[str(operator.id)] = None
            continue

        # Derive the App ID hint from the raw ID instead of trusting a stored
        # display field. For AppSecret, only the dedicated last-four hint is
        # used; encrypted or plaintext credential columns never leave the DB.
        app_id_hint = f"…{(account.app_key or '')[-4:]}" if account.app_key else None
        secret_suffix = (account.secret_hint or "")[-4:]
        summaries[str(operator.id)] = {
            "linked": True,
            "appIdHint": app_id_hint,
            "secretHint": f"…{secret_suffix}" if secret_suffix else None,
            "status": account.status or "unknown",
            "boundAt": binding.bound_at,
        }
    return summaries
