"""用控制流可达性判断 `_cat_name` 是否真的存在 UnboundLocalError 风险。

第一版脚本按「行号先后」判定，得出「HEAD 安全」——**这是错的**。
行号先后不等于执行可达：`_cat_name` 在 3093 行赋值，但那行在
`if _is_legacy_default:` 分支内；3112 行的读取在分支外。
行号 3112 > 3093，可是执行路径可以跳过 3093。

本脚本做真正需要的判断：**从函数入口出发，是否存在一条到达该读取点、
却不经过任何赋值点的路径。** 这是经典的「定值-引用分析」。

实现方式：把函数体转成一个简化的 CFG（顺序 / if / 循环 / return / raise
都建边），然后：
  1. 找出所有「读取 _cat_name」的语句节点
  2. 对每个读取点，做一次反向可达性搜索：能否不经过赋值点到达函数入口
  3. 能 → 该读取点存在未定义风险

用法：cd miaoshou/server && python -X utf8 ../docs/audit/_verify_sizechart_unbound.py
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parents[2] / "server"
TARGET = SERVER / "services" / "warehouse_service.py"
VAR = "_cat_name"


class CFG:
    """极简 CFG：只关心「语句之间的可达性」与「哪条语句给 VAR 赋值」。"""

    def __init__(self) -> None:
        self.succ: dict[int, set[int]] = {}
        self.node_of_line: dict[int, ast.AST] = {}
        self.assign_nodes: set[int] = set()

    def add(self, a: int, b: int) -> None:
        self.succ.setdefault(a, set()).add(b)
        self.succ.setdefault(b, set())


def stmt_id(node: ast.AST) -> int:
    return id(node)


def build(node: ast.AST, cfg: CFG, incoming: list[int]) -> list[int]:
    """返回从该节点流出后的「出边端点」列表。"""
    if isinstance(node, list):
        current = list(incoming)
        for item in node:
            current = build(item, cfg, current)
        return current

    nid = stmt_id(node)
    cfg.node_of_line[getattr(node, "lineno", 0)] = node
    for src in incoming:
        cfg.add(src, nid)

    # 赋值语句里出现 VAR
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        else:
            targets = [node.target]
        for t in targets:
            for sub in ast.walk(t):
                if isinstance(sub, ast.Name) and sub.id == VAR:
                    cfg.assign_nodes.add(nid)
    # for 循环的 target 也算赋值
    if isinstance(node, (ast.For, ast.AsyncFor)):
        for sub in ast.walk(node.target):
            if isinstance(sub, ast.Name) and sub.id == VAR:
                cfg.assign_nodes.add(nid)

    if isinstance(node, ast.If):
        body_out = build(node.body, cfg, [nid])
        orelse_out = build(node.orelse, cfg, [nid]) if node.orelse else [nid]
        return body_out + orelse_out

    if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
        # 循环体可能执行 0 次或多次
        body_out = build(node.body, cfg, [nid])
        return [nid] + body_out

    if isinstance(node, (ast.With, ast.AsyncWith)):
        return build(node.body, cfg, [nid])

    if isinstance(node, ast.Try):
        outs: list[int] = []
        outs += build(node.body, cfg, [nid])
        for h in node.handlers:
            outs += build(h.body, cfg, [nid])
        outs += build(node.finalbody, cfg, [nid]) if node.finalbody else []
        return outs if outs else [nid]

    if isinstance(node, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
        return []          # 不往下走

    return [nid]


def find_reads(fn: ast.AST) -> list[tuple[int, ast.AST]]:
    out: list[tuple[int, ast.AST]] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and node.id == VAR and isinstance(node.ctx, ast.Load):
            out.append((node.lineno, node))
    return out


def enclosing_stmt(fn: ast.AST, target: ast.AST) -> ast.AST | None:
    """找到包含 target 的最内层**语句**节点（CFG 里有的那种）。

    第一版脚本直接用 `id(Name节点)` 去 CFG 里查前驱 —— 但 CFG 只登记语句，
    表达式节点根本不在图里，所以 `pred.get()` 永远返回空，
    每个读取点都被判成「必然经过赋值」。**这是假阴性**：
    它会给出一份「全部安全」的错误报告。

    必须把读取点映射到它所在的语句，再对该语句做可达性分析。
    """
    best: ast.AST | None = None
    for node in ast.walk(fn):
        if not isinstance(node, (ast.stmt,)):
            continue
        for sub in ast.walk(node):
            if sub is target:
                # 取最小的那个包含者
                if best is None or getattr(node, "lineno", 0) >= getattr(best, "lineno", 0):
                    best = node
                break
    return best


def reachable_without_assign(cfg: CFG, read_node: int, entry: int) -> bool:
    """从读取点反向搜，能否不经过任何赋值点到达入口。"""
    seen: set[int] = set()
    pred: dict[int, set[int]] = {}
    for a, bs in cfg.succ.items():
        for b in bs:
            pred.setdefault(b, set()).add(a)
    stack: list[int] = list(pred.get(read_node, set()))
    if not stack:
        # 该节点在 CFG 里没有前驱 —— 可能它就是入口，或者没被登记
        return read_node == entry
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        if cur == entry:
            return True
        if cur in cfg.assign_nodes:
            continue        # 遇到赋值点就剪枝（这条路径已定义）
        stack.extend(pred.get(cur, set()))
    return False


def analyse(source: str, label: str) -> bool:
    tree = ast.parse(source)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "save_from_collect_box":
            fn = node
            break
    if fn is None:
        print(f"  {label}: 找不到函数")
        return False

    cfg = CFG()
    entry = stmt_id(fn)
    build(fn.body, cfg, [entry])

    reads = find_reads(fn)
    print(f"  {label}: 赋值节点 {len(cfg.assign_nodes)} 个，读取 {len(reads)} 处")
    risky: list[int] = []
    for lineno, node in reads:
        stmt = enclosing_stmt(fn, node)
        if stmt is None:
            continue
        if reachable_without_assign(cfg, stmt_id(stmt), entry):
            risky.append(lineno)
    if risky:
        print(f"    ❌ 存在未定义风险的读取行: {sorted(set(risky))}")
        return False
    print("    ✅ 每个读取点都必然经过赋值")
    return True


def main() -> None:
    print("=== 1. HEAD 版本（已提交、已发布）===")
    head_src = subprocess.run(
        ["git", "show", "HEAD:server/services/warehouse_service.py"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(SERVER.parent)).stdout
    head_safe = analyse(head_src, "HEAD")

    print("\n=== 2. 工作区版本（未提交的修复）===")
    work_safe = analyse(TARGET.read_text(encoding="utf-8"), "工作区")

    print("\n=== 3. 崩溃条件（直接模拟那段分支）===")
    def simulate(is_legacy_default: bool) -> str:
        try:
            if is_legacy_default:
                _cat_name = "鞋"
            _ = _cat_name       # noqa: F821  ← c263cfe 的写法
            return "正常"
        except UnboundLocalError as exc:
            return f"UnboundLocalError: {exc}"

    for flag in (True, False):
        label = "未配尺寸（legacy 分支）" if flag else "显式配过 30×20×10"
        print(f"  {label:26} → {simulate(flag)}")

    print("\n=== 结论 ===")
    if not head_safe and work_safe:
        print("  ★ HEAD 有 UnboundLocalError 风险；工作区修复有效但**尚未提交**")
        print("    → 不提交的话，下次 build/deploy 打包的仍是崩溃版本")
    elif head_safe and work_safe:
        print("  HEAD 与工作区均安全")
    elif head_safe and not work_safe:
        print("  ⚠ HEAD 安全但工作区引入了风险（反了，需要人工看）")
    else:
        print("  两版均有风险")

    print("\n  注意：`_cat_name` 在同一个函数后半段（约 3300 行）还有另一处赋值/读取，")
    print("  那组是自洽的（先赋值后读）。风险只在 3112/3115 那两行。")


if __name__ == "__main__":
    main()
