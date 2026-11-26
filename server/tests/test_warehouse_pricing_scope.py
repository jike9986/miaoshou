"""定价模板：自动化处理套，手动入库不套（负责人 2026-11-25）。

口径来源：
  「自动化处理是可以套模板的，都是入库是需要我手动处理选择入库，
    入库的时候价格是没有套模板的价格」

同一个 `save_from_collect_box` 服务两种入仓，靠 `automation_result` 区分：

  automation_result=True   自动化处理   → 套模板定价
  automation_result=False  手动入库     → 只记拿货价

## 2026-11-26 事故

上一版把 template 无条件设成 None，`pricing_enabled=template is not None`
于是恒为 False。但自动化处理确实改了价格（前端开着「定价模板·马来」），
pre_save_check 价格规则拿到 pricing_enabled=False 就报：

    保存前校验未通过：one or more price fields changed

采集箱「处理」按钮对每一件都失败（负责人截图：0/2 失败）。

这个测试锁住「模板启用状态必须与实际是否定价一致」。
"""
from __future__ import annotations

import ast
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "services" / "warehouse_service.py"


def _function(source: str, name: str) -> ast.FunctionDef:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} 不存在")


def test_automation_result_controls_template():
    """template 只能在 automation_result 为真时才赋值。"""
    source = SERVICE.read_text(encoding="utf-8")
    function = _function(source, "save_from_collect_box")
    body = ast.unparse(function)

    # 必须先把 template 置空
    assert "template = None" in body, "template 没有初始化为 None"

    # `template = raw` 必须在 `if automation_result:` 之内
    tree = function
    assigned_inside_automation = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = ast.unparse(node.test)
        if "automation_result" not in test:
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Assign):
                for target in child.targets:
                    if isinstance(target, ast.Name) and target.id == "template":
                        assigned_inside_automation = True
    assert assigned_inside_automation, (
        "`template = raw` 必须在 `if automation_result:` 分支里 —— "
        "否则手动入库也会套模板，仓库价格就不是拿货价了")


def test_pricing_enabled_matches_actual_pricing():
    """pricing_enabled 必须与「模板是否真的用于定价」一致。

    这是 2026-11-26 报「price fields changed」的直接原因：
    实际套了模板改价，却告诉校验 pricing_enabled=False。
    """
    source = SERVICE.read_text(encoding="utf-8")
    function = _function(source, "save_from_collect_box")

    calls = [node for node in ast.walk(function)
             if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute)
             and node.func.attr == "pre_save_issues"]
    assert calls, "没找到 pre_save_issues 调用"

    for call in calls:
        kw = {k.arg: ast.unparse(k.value) for k in call.keywords if k.arg}
        assert "pricing_enabled" in kw, "pre_save_issues 没传 pricing_enabled"
        assert kw["pricing_enabled"] == "template is not None", (
            f"pricing_enabled 应写 `template is not None`，实际 {kw['pricing_enabled']}")


def test_warehouse_price_override_only_when_no_template():
    """`_price_warehouse_at_purchase` 只在没套模板时执行（手动入库）。

    套了模板就不能再改写成拿货价，否则模板定价等于白算。
    """
    source = SERVICE.read_text(encoding="utf-8")
    function = _function(source, "save_from_collect_box")

    found = False
    for node in ast.walk(function):
        if not isinstance(node, ast.If):
            continue
        test = ast.unparse(node.test)
        if "template is None" not in test:
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Assign):
                src = ast.unparse(child.value)
                if "_price_warehouse_at_purchase" in src:
                    found = True
    assert found, (
        "`_price_warehouse_at_purchase` 必须在 `if template is None:` 里调用 —— "
        "套了模板的自动化处理不能被打回拿货价")


def test_both_paths_exist_in_signature():
    """函数签名必须有 automation_result 开关。"""
    source = SERVICE.read_text(encoding="utf-8")
    function = _function(source, "save_from_collect_box")
    args = [a.arg for a in function.args.args] + [
        a.arg for a in function.args.kwonlyargs]
    assert "automation_result" in args, "签名里没有 automation_result"
    assert "price_template" in args, "签名里没有 price_template"

# ===== 售价改写豁免（2026-11-26）=====

def test_purchase_pricing_flag_accepts_price_rewrite():
    """手动入库把手写售价改写成拿货价，校验要放行。

    不带这个豁免时，手动入库每件都报
    「one or more price fields changed」—— 因为 price 从妙手值变成了拿货价。

    实测（生产真实商品 detail=3562906673，拿货 15 CNY）：
      automation_result=True  → price 20.48（模板）
      automation_result=False → price 9.23 （拿货价）
    """
    from services import pre_save_check

    original = {"skuMap": {"k1": {"originPrice": "15", "price": "20.48"}}}
    payload = {"skuMap": {"k1": {"originPrice": "15", "price": "9.23"}}}

    # 不豁免 → 报错
    issues = pre_save_check.pre_save_issues(
        original, payload, {},
        package_dimensions=None, size_chart_url="",
        process_images_enabled=True, image_only=False,
        pricing_enabled=False,
    )
    assert "one or more price fields changed" in issues

    # 豁免 → 放行
    issues = pre_save_check.pre_save_issues(
        original, payload, {},
        package_dimensions=None, size_chart_url="",
        process_images_enabled=True, image_only=False,
        pricing_enabled=False,
        warehouse_purchase_pricing=True,
    )
    assert "one or more price fields changed" not in issues


def test_purchase_pricing_still_protects_origin_price():
    """豁免售价不等于允许改采购价。"""
    from services import pre_save_check

    original = {"skuMap": {"k1": {"originPrice": "15", "price": "20.48"}}}
    payload = {"skuMap": {"k1": {"originPrice": "99", "price": "9.23"}}}

    issues = pre_save_check.pre_save_issues(
        original, payload, {},
        package_dimensions=None, size_chart_url="",
        process_images_enabled=True, image_only=False,
        pricing_enabled=False,
        warehouse_purchase_pricing=True,
    )
    assert any("originPrice" in i for i in issues), (
        "改了采购价却没报错，豁免范围太大了")


def test_readback_accepts_price_rewrite_for_purchase_pricing():
    """读回核验也要认这个改写，否则提交成功却判成「读回不一致」。"""
    from services import readback_check

    payload = {"skuMap": {"k1": {"originPrice": "15", "price": "9.23"}},
               "title": "x", "cid": 1}
    original = {"skuMap": {"k1": {"originPrice": "15", "price": "20.48"}}}
    actual = {"skuMap": {"k1": {"originPrice": "15", "price": "9.23"}},
              "title": "x", "cid": 1}

    issues = readback_check.readback_issues(
        payload, actual, original, {}, warehouse_purchase_pricing=True)
    assert "prices" not in issues, f"读回仍报 prices 不一致: {issues}"
