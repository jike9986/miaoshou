"""定价模板：每人一套，没有共享模板（负责人 2026-11-23）。

「不是模板是每人一套默认的，可以自己去修改自己账号的定价模板，没有共享模板」
所以这里不再有「共享库」概念 —— 每人的模板互不可见、互不影响。
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
def people(db):
    suffix = uuid.uuid4().hex[:8]
    member, _ = operator_service.create(
        db, f"tpl-p-m-{suffix}", role=operator_service.ROLE_MEMBER)
    teacher, _ = operator_service.create(
        db, f"tpl-p-t-{suffix}", role=operator_service.ROLE_TEACHER)
    db.commit()
    yield member, teacher, suffix
    for operator_id in (member.id, teacher.id):
        for name in list(setting_service.price_templates(db, operator_id=operator_id)):
            setting_service.delete_template(db, name, operator_id=operator_id)
    db.commit()


def _save(db, name: str, *, operator_id=None, profit: float = 20.0) -> None:
    setting_service.save_template(
        db, price_template.normalize_template(
            {"name": name, "site": "MY", "profitPercent": profit}),
        operator_id=operator_id)
    db.commit()


def test_first_read_seeds_default_templates(db, people):
    """新人第一次打开就有一套默认模板，不是空表。"""
    member, _teacher, _suffix = people
    templates = setting_service.price_templates(db, operator_id=member.id)
    assert templates, "首次读取应该铺一套默认模板"
    # 五个站点各一套
    sites = {str(raw.get("site") or "").upper() for raw in templates.values()}
    assert {"MY", "TH", "VN", "PH", "SG"} <= sites
    # 落库了：第二次读是同一份
    again = setting_service.price_templates(db, operator_id=member.id)
    assert set(again) == set(templates)


def test_each_person_has_own_copy(db, people):
    """两个人各改各的，互不可见、互不影响。"""
    member, teacher, suffix = people
    name = f"我的模板-{suffix}"
    _save(db, name, operator_id=member.id, profit=35)

    assert name in setting_service.price_templates(db, operator_id=member.id)
    assert name not in setting_service.price_templates(db, operator_id=teacher.id)


def test_same_name_does_not_collide(db, people):
    """同名模板在两个人那里是两份不同的东西。"""
    member, teacher, suffix = people
    name = f"马来-{suffix}"
    _save(db, name, operator_id=member.id, profit=30)
    _save(db, name, operator_id=teacher.id, profit=10)

    assert setting_service.price_templates(db, operator_id=member.id)[name]["profitPercent"] == 30
    assert setting_service.price_templates(db, operator_id=teacher.id)[name]["profitPercent"] == 10


def test_delete_only_affects_self(db, people):
    """删自己的不影响别人。"""
    member, teacher, suffix = people
    name = f"删我-{suffix}"
    _save(db, name, operator_id=member.id)
    _save(db, name, operator_id=teacher.id)

    assert setting_service.delete_template(db, name, operator_id=member.id) is True
    assert name not in setting_service.price_templates(db, operator_id=member.id)
    assert name in setting_service.price_templates(db, operator_id=teacher.id)


def test_teacher_also_has_personal_templates(db, people):
    """老师也是自己那套 —— 没有「老师定基准、学员抄」这回事了。"""
    _member, teacher, _suffix = people
    templates = setting_service.price_templates(db, operator_id=teacher.id)
    assert templates, "老师首次读取也该铺默认模板"
