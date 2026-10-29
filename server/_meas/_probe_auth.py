"""Issue short-lived personal tokens for HTTP probes that exercise business APIs."""
from __future__ import annotations

import uuid


def teacher_headers() -> dict[str, str]:
    from db.session import Base, SessionLocal, engine
    from services import operator_service

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        _operator, token = operator_service.create(
            db, f"parity-teacher-{uuid.uuid4().hex}",
            role=operator_service.ROLE_TEACHER,
        )
        db.commit()
    finally:
        db.close()
    return {"Authorization": f"Bearer {token}"}
