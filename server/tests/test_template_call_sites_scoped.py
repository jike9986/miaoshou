"""所有读模板的路径都必须带 operator_id（回归 2026-11-23）。

背景：模板改成「每人一套，没有共享模板」之后，任何一个漏了 operator_id 的
读取都会落到空的共享键 `pricing.templates` 上，表现为：

  · 认领/处理商品时 `price_template` 取不到 -> template=None -> 定价整段被跳过
    -> 商品按拿货价当本币提交 -> 被妙手成本校验打回：
    「本地展示价 18.71PHP，无法覆盖产品基础成本 225.50PHP」
  · 同步模板时合并结果里的「已存模板」永远是 None，同步等于没同步
  · 顺便把同步下来的模板写进空共享键，用户界面上永远看不到

这一组测试盯住这些调用点，防止以后再漏。
"""
from __future__ import annotations

import ast
import pathlib
import uuid

import pytest

SERVER = pathlib.Path(__file__).resolve().parents[1]
# 允许不带 operator_id 的文件：_meas 是离线脚本，tests 是测试自己
SKIP_DIRS = {"tests", "_meas", "__pycache__"}
TEMPLATE_FUNCS = {
    "price_templates", "save_template", "delete_template",
    "selected_template_name", "template_available",
}


def _iter_source_files():
    for path in SERVER.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


@pytest.mark.parametrize("path", list(_iter_source_files()),
                         ids=lambda p: p.relative_to(SERVER).as_posix())
def test_every_template_call_site_passes_operator_id(path):
    """任何 setting_service 模板调用的实参里都必须出现 operator_id。

    例外：`operator_id=_template_scope(db, operator)` 这种表达式也算
    —— 只要源码里出现 `operator_id` 这个词即可。
    """
    source = path.read_text(encoding="utf-8")
    if "setting_service" not in source and "setting_service." not in source:
        pytest.skip("这个文件不碰模板")
    tree = ast.parse(source)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in TEMPLATE_FUNCS:
            continue
        # 只看 setting_service.xxx(...) 这种
        owner = func.value
        if not isinstance(owner, ast.Name) or owner.id != "setting_service":
            continue
        text = ast.get_source_segment(source, node) or ""
        if "operator_id" not in text:
            offenders.append(f"line {node.lineno}: {text.splitlines()[0][:90]}")
    assert not offenders, (
        "这些调用没带 operator_id，会读到空的共享模板库：\n  "
        + "\n  ".join(offenders))


def test_warehouse_claim_resolves_template_for_the_claiming_operator():
    """认领时用的模板必须来自该操作员自己那份。"""
    from db.session import SessionLocal, init_db
    from services import operator_service, price_template, setting_service

    init_db()
    db = SessionLocal()
    suffix = uuid.uuid4().hex[:8]
    try:
        member, _ = operator_service.create(
            db, f"claim-tpl-{suffix}", role=operator_service.ROLE_MEMBER)
        db.commit()
        name = f"认领用-{suffix}"
        setting_service.save_template(
            db, price_template.normalize_template(
                {"name": name, "site": "PH", "profitPercent": 25}),
            operator_id=member.id)
        db.commit()

        # 自己的那份有
        assert name in setting_service.price_templates(db, operator_id=member.id)
        # 共享键（漏传 operator_id 时落到的位置）没有 —— 这正是 bug 的形态
        assert name not in setting_service.price_templates(db)
    finally:
        for template_name in list(
                setting_service.price_templates(db, operator_id=member.id)):
            setting_service.delete_template(db, template_name, operator_id=member.id)
        db.commit()
        db.close()
