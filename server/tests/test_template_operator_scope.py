"""定价模板按操作员隔离（负责人 2026-11-23：学员也要能自己改模板）。

模板直接决定售价。让学员改共享库会把所有账号（含别的学员和老师）的价
一起改掉，所以学员写自己的私有库，读「共享 ∪ 私有」，私有同名优先。
"""
from __future__ import annotations

import uuid

import pytest

from db.session import SessionLocal, init_db
from services import operator_service, price_template, setting_service


@pytest.fixture
def db():
    init_db()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def actors(db):
    suffix = uuid.uuid4().hex[:8]
    member, _ = operator_service.create(
        db, f"tpl-scope-m-{suffix}", role=operator_service.ROLE_MEMBER)
    teacher, _ = operator_service.create(
        db, f"tpl-scope-t-{suffix}", role=operator_service.ROLE_TEACHER)
    db.commit()
    yield member, teacher, suffix
    # 清掉本用例写进去的模板，免得污染共享库
    for operator_id in (None, member.id, teacher.id):
        for name in list(setting_service.price_templates(db, operator_id=operator_id)):
            if name.endswith(suffix):
                setting_service.delete_template(db, name, operator_id=operator_id)
    db.commit()


def _save(db, name: str, *, operator_id=None, profit: float = 20.0) -> None:
    setting_service.save_template(
        db, price_template.normalize_template(
            {"name": name, "site": "MY", "profitPercent": profit}),
        operator_id=operator_id)
    db.commit()


def test_member_template_stays_private(db, actors):
    """学员存模板只写自己的私有库，共享库看不到。"""
    member, _teacher, suffix = actors
    name = f"学员自建-{suffix}"
    _save(db, name, operator_id=member.id, profit=35)

    assert name in setting_service.price_templates(db, operator_id=member.id)
    assert name not in setting_service.price_templates(db), "共享库不该被学员写污染"
    # 另一个学员也看不到
    other, _ = operator_service.create(
        db, f"tpl-scope-o-{suffix}", role=operator_service.ROLE_MEMBER)
    db.commit()
    assert name not in setting_service.price_templates(db, operator_id=other.id)


def test_member_sees_shared_templates(db, actors):
    """共享库的模板学员看得到、能拿来用。"""
    member, _teacher, suffix = actors
    name = f"共享基准-{suffix}"
    _save(db, name, operator_id=None)

    assert name in setting_service.price_templates(db, operator_id=member.id)


def test_member_override_wins(db, actors):
    """同名时学员自己那份优先 —— 他改的是「我这套怎么算」。"""
    member, _teacher, suffix = actors
    name = f"同覆盖-{suffix}"
    _save(db, name, operator_id=None, profit=10)
    _save(db, name, operator_id=member.id, profit=45)

    merged = setting_service.price_templates(db, operator_id=member.id)
    assert merged[name]["profitPercent"] == 45
    # 共享那份没被动过
    assert setting_service.price_templates(db)[name]["profitPercent"] == 10


def test_member_cannot_delete_shared_template(db, actors):
    """学员删不掉共享模板，否则他一个人能让所有人少一套模板。"""
    member, _teacher, suffix = actors
    name = f"不许删-{suffix}"
    _save(db, name, operator_id=None)

    assert setting_service.delete_template(db, name, operator_id=member.id) is False
    assert name in setting_service.price_templates(db)


def test_teacher_writes_shared_library(db):
    """老师/管理员（operator_id=None）照旧读写共享库。"""
    suffix = uuid.uuid4().hex[:8]
    name = f"老师共享-{suffix}"
    _save(db, name, operator_id=None, profit=25)
    try:
        assert name in setting_service.price_templates(db)
        assert setting_service.delete_template(db, name, operator_id=None) is True
    finally:
        for leftover in list(setting_service.price_templates(db)):
            if leftover.endswith(suffix):
                setting_service.delete_template(db, leftover)
        db.commit()
