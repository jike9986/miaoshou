"""独立复现 C-P0-2：受限分组的闸门只装在列表页，详情/认领/发布全通。

审计 C 声称：
  group_access.guard_group（services/group_access.py:106）全仓零调用
  guard_products 只在 claim_job / publish_job / schedule 三处接入
  publish_service.py 一处都没有
  → 学员能按 id 直接读「精品」组商品全文、能认领它、能把它发出去

业务口径（docs/features/schedules 里记的负责人原话）：
  受限三组（自营海外仓 / 海外仓定制款 / 精品）拿货价是标高的，
  学员按它发出去是卖一件亏一件。所以必须挡住学员。

用法：cd miaoshou/server && python -X utf8 ../docs/audit/_verify_c_p0_2.py
"""
from __future__ import annotations

import ast
import os
import sys
import tempfile
from pathlib import Path

_DATA_DIR = tempfile.mkdtemp(prefix="audit-c-p02-")
os.environ["AOHUANG_DATA_DIR"] = _DATA_DIR
os.environ.pop("AOHUANG_DATABASE_URL", None)

SERVER = Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(SERVER))


def scan_calls(func_name: str, search_dirs: list[Path]) -> list[str]:
    """用 AST 扫真实调用点（不是 grep 注释里提到的）。"""
    hits: list[str] = []
    for base in search_dirs:
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", None)
                if name == func_name:
                    rel = path.relative_to(SERVER.parent).as_posix()
                    hits.append(f"{rel}:{node.lineno}")
    return sorted(hits)


def main() -> None:
    print(f"数据目录（临时）: {_DATA_DIR}\n")

    scan_targets = [SERVER / "api", SERVER / "services", SERVER / "core"]

    print("=== 1. guard_group 的 AST 调用点（排除定义本身）===")
    guard_group_calls = [
        h for h in scan_calls("guard_group", scan_targets)
    ]
    print(f"  调用点数量: {len(guard_group_calls)}")
    for h in guard_group_calls:
        print(f"    {h}")
    if not guard_group_calls:
        print("  ★ 零调用 —— 写了闸门但没接上（C-P0-2 的根因）")

    print("\n=== 2. guard_products 的 AST 调用点 ===")
    gp_calls = scan_calls("guard_products", scan_targets)
    print(f"  调用点数量: {len(gp_calls)}")
    for h in gp_calls:
        print(f"    {h}")

    print("\n=== 3. publish_service.py 里有没有分组闸门 ===")
    pub = SERVER / "services" / "publish_service.py"
    src = pub.read_text(encoding="utf-8")
    has_group_access = "group_access" in src
    has_guard = "guard_group" in src or "guard_products" in src
    print(f"  提到 group_access : {has_group_access}")
    print(f"  调用任一分组闸门  : {has_guard}")
    if not has_guard:
        print("  ★ 同步发布路径完全没有分组闸门（C-P0-2 成立）")

    print("\n=== 4. 详情 / 认领端点是否有分组闸门 ===")
    api_wh = (SERVER / "api" / "warehouse.py").read_text(encoding="utf-8")
    tree = ast.parse(api_wh)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in ("warehouse_detail", "claim_product"):
            continue
        body_src = ast.get_source_segment(api_wh, node) or ""
        has_guard_in_body = ("guard_group" in body_src
                             or "guard_products" in body_src
                             or "group_access" in body_src)
        print(f"  {node.name:20} 行 {node.lineno:4}  含分组闸门: {has_guard_in_body}")
        if not has_guard_in_body:
            print(f"      ★ 无闸门 —— 学员可直接按 id 访问")

    print("\n=== 5. 列表页（对比组：闸门装在这里）===")
    ws = (SERVER / "services" / "warehouse_service.py").read_text(encoding="utf-8")
    print(f"  warehouse_service 提到 visible_group_ids: {'visible_group_ids' in ws}")
    print("  → 列表按 visible_group_ids 过滤，但详情/认领/发布没有 → 用前端隐藏当权限")

    print("\n=== 结论 ===")
    if not guard_group_calls and not has_guard:
        print("  ★ C-P0-2 成立：闸门函数写了零调用，同步发布路径完全无分组校验")
        print("    影响：学员能读、能认领、能发布「精品」组商品（拿货价标高的那三组）")
        print("    修复：guard_group 接到 warehouse_detail / claim_product；")
        print("          guard_products 接到 publish_service 的两条分支")


if __name__ == "__main__":
    main()
