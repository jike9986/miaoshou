"""静态防回归：不能在 if/else 的单侧分支里定义变量、却在结构外使用。

2026-11-26 线上事故：给尺码图判定传了 `_cat_name`，而 `_cat_name` 只在
`if _is_legacy_default:` 分支里赋值。运营显式配过 packageDimensions 时
走 else 分支，变量从未定义 →

    UnboundLocalError: local variable '_cat_name' referenced before assignment
    POST /api/v1/collect-box/{id}/process HTTP/1.1" 500

采集箱「处理」按钮全部 500。

这个测试只查最典型的形态：**if 有 else，但变量只在 if 体里赋值，
之后在 if/else 结构之外被读取**。这是必然崩的，不是概率问题。
"""
from __future__ import annotations

import ast
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "services" / "warehouse_service.py"


def _function_node(source: str, name: str) -> ast.FunctionDef:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} 不存在")


def _names_assigned(body: list[ast.stmt]) -> set[str]:
    """这组语句里（含嵌套）被赋值的名字集合。"""
    names: set[str] = set()
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                if isinstance(node.target, ast.Name):
                    names.add(node.target.id)
    return names


def _unconditional_assignments(function: ast.FunctionDef) -> set[str]:
    """在最外层（不在任何 if/for/while/try 里）被赋过值的名字。

    这些变量有默认值，即使 if 分支没走到也不会 UnboundLocalError。
    """
    names: set[str] = set()

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.depth = 0

        def _enter(self, item: ast.AST) -> None:
            self.depth += 1
            self.generic_visit(item)
            self.depth -= 1

        visit_If = _enter
        visit_For = _enter
        visit_While = _enter
        visit_Try = _enter

        def visit_Assign(self, item: ast.Assign) -> None:
            if self.depth == 0:
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
            self.generic_visit(item)

        def visit_AnnAssign(self, item: ast.AnnAssign) -> None:
            if self.depth == 0 and isinstance(item.target, ast.Name):
                names.add(item.target.id)
            self.generic_visit(item)

    Visitor().visit(function)
    return names


def _find_dangerous_ifs(function: ast.FunctionDef) -> list[str]:
    """找出「只在 if 分支定义、else 没有、初始化也没有、却在外面读取」的变量。

    三个条件都要满足才算必崩：
      1. if 有 else
      2. 变量只在 if 体里赋值，else 体里没有
      3. 函数最外层没有给它默认值（有默认值就不会 UnboundLocalError）
    """
    problems: list[str] = []
    unconditional = _unconditional_assignments(function)

    for node in ast.walk(function):
        if not isinstance(node, ast.If) or not node.orelse:
            continue  # 只看有 else 的
        in_if = _names_assigned(node.body)
        in_else = _names_assigned(node.orelse)
        only_in_if = in_if - in_else - unconditional
        if not only_in_if:
            continue

        start = node.lineno
        end = getattr(node, "end_lineno", start) or start

        # 在 if/else 结构之后、函数内任何地方读取这些变量 → 必然崩。
        #
        # 但「结构之后、读取之前又重新赋过值」的情况是安全的：
        #   比如 3316 行 `_cat_name = product_service.category_label(payload)`
        #   显式赋值，3317 行再读就没问题。
        re_assign_lines: dict[str, list[int]] = {}
        for assign in ast.walk(function):
            if isinstance(assign, ast.Assign):
                for target in assign.targets:
                    if isinstance(target, ast.Name):
                        re_assign_lines.setdefault(target.id, []).append(assign.lineno)
            elif isinstance(assign, (ast.AnnAssign, ast.AugAssign)):
                if isinstance(assign.target, ast.Name):
                    re_assign_lines.setdefault(
                        assign.target.id, []).append(assign.lineno)

        for later in ast.walk(function):
            if not isinstance(later, ast.Name):
                continue
            if not isinstance(later.ctx, ast.Load):
                continue
            if later.id not in only_in_if:
                continue
            if later.lineno <= end:
                continue
            # 读取之前是否在结构之外重新赋过值
            has_default_after = any(
                end < line < later.lineno
                for line in re_assign_lines.get(later.id, []))
            if has_default_after:
                only_in_if.discard(later.id)
                continue
            problems.append(
                f"{later.id}: 只在 {start}-{end} 的 if 单侧分支赋值，"
                f"函数最外层也没给默认值，却在 {later.lineno} 行被读取")
            only_in_if.discard(later.id)  # 同一个变量只报一次

    return problems


def test_no_variable_defined_in_one_branch_used_outside():
    """if 有 else，但变量只在 if 里定义，然后在结构外被用 → UnboundLocalError。"""
    source = SERVICE.read_text(encoding="utf-8")
    function = _function_node(source, "save_from_collect_box")
    problems = _find_dangerous_ifs(function)
    assert not problems, (
        "save_from_collect_box 有单侧分支变量越界引用（线上必崩 500）：\n  "
        + "\n  ".join(problems))


def test_other_hot_functions_have_no_branch_leak():
    """采集箱处理链路上的其他关键函数也扫一遍。"""
    source = SERVICE.read_text(encoding="utf-8")
    problems: list[str] = []
    for name in ("save_from_collect_box", "admit_automation_results",
                 "_price_warehouse_at_purchase", "process_price"):
        try:
            function = _function_node(source, name)
        except AssertionError:
            continue
        for problem in _find_dangerous_ifs(function):
            problems.append(f"{name}: {problem}")
    assert not problems, "存在分支变量越界引用：\n  " + "\n  ".join(problems)


def test_size_chart_check_does_not_pass_bare_cat_name():
    """尺码图判定不能依赖分支内局部变量，要自己从 payload 取类目。

    正确写法：
        _needs_chart = _payload_needs_size_chart(
            payload, product_service.category_label(payload))
    """
    source = SERVICE.read_text(encoding="utf-8")
    function = _function_node(source, "save_from_collect_box")

    calls = [node for node in ast.walk(function)
             if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name)
             and node.func.id == "_payload_needs_size_chart"]
    assert calls, "save_from_collect_box 没有调用 _payload_needs_size_chart"

    for call in calls:
        sources = [ast.unparse(arg) for arg in call.args]
        assert "payload" in sources[0], (
            f"第一参数应该是 payload，实际 {sources[0]}")
        for arg in sources[1:]:
            assert arg != "_cat_name", (
                "_payload_needs_size_chart 不能传裸 _cat_name —— "
                "_cat_name 只在 if _is_legacy_default 分支里定义，"
                "显式配过 packageDimensions 时必然 UnboundLocalError")


def test_size_chart_cleared_for_non_footwear_in_warehouse():
    """入库路径：没有尺码轴的商品要把历史尺码图清掉。"""
    source = SERVICE.read_text(encoding="utf-8")
    function = _function_node(source, "save_from_collect_box")
    body = ast.unparse(function)

    assert "_payload_needs_size_chart" in body
    assert 'sizeChart"] = ""' in body or "sizeChart'] = ''" in body, (
        "入库时要清空无尺码轴商品的 sizeChart")