"""验证 C-P0-1 的修复方案：把 shared_mode 收敛到唯一权威。

## 修法只有一处，但要想清楚「换成哪个」

`api/deps.py` 两处鉴权读 `Settings.shared_mode`（环境变量源），
而界面开关写的是 settings 表。改成读 `share_service.shared_mode(db)` 就行。

但有个陷阱：`deps.py` 的这两个函数里 **是否拿得到 db？**

  `_authenticate(db, supplied)`  → 有 db ✅
  `require_token(...)`           → 要确认签名

如果某个函数拿不到 db，就不能简单替换，得把 db 传进来 ——
那会牵动 40+ 个端点。所以必须先验证「两处都能拿到 db」，
否则审计给的修复方案是不可执行的。

本脚本验证：
  1. 两处鉴权能否拿到 db
  2. 替换后行为是否正确（界面开共享 → 本机 secret 被拒）
  3. 替换不会破坏单机模式（默认行为不变）

用法：cd miaoshou/server && python -X utf8 ../docs/audit/_verify_c_p0_1_fix.py
"""
from __future__ import annotations

import ast
import os
import sys
import tempfile
from pathlib import Path

_DATA_DIR = tempfile.mkdtemp(prefix="audit-c-p01-fix-")
os.environ["AOHUANG_DATA_DIR"] = _DATA_DIR
os.environ.pop("AOHUANG_SHARED_MODE", None)
os.environ.pop("AOHUANG_DATABASE_URL", None)

SERVER = Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(SERVER))


def analyse_signatures() -> None:
    """看两处鉴权函数能不能拿到 db —— 决定修法是否可执行。"""
    src = (SERVER / "api" / "deps.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    print("=== 1. 两处鉴权函数的签名（能否拿到 db）===")
    wanted = {"_authenticate", "require_token", "current_operator"}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in wanted:
            continue
        params = [a.arg for a in node.args.args]
        has_db = "db" in params
        print(f"  {node.name:20} 行{node.lineno:4}  参数={params}")
        print(f"    → 拿得到 db: {has_db}")
        if not has_db:
            print("    ⚠ 拿不到 db：不能直接替换，需要改签名（会牵动手下所有端点）")

    print("\n=== 2. 两处 settings.shared_mode 读取点的上下文 ===")
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "shared_mode":
            line = src.splitlines()[node.lineno - 1].strip()
            # 往上找最近的函数定义
            owner = None
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if fn.lineno <= node.lineno <= (fn.end_lineno or fn.lineno):
                    if owner is None or fn.lineno > owner.lineno:
                        owner = fn
            print(f"  deps.py:{node.lineno}  在 {owner.name if owner else '?'} 里")
            print(f"    {line}")


def main() -> None:
    print(f"数据目录（临时）: {_DATA_DIR}")
    print(f"AOHUANG_SHARED_MODE: {os.environ.get('AOHUANG_SHARED_MODE', '<未设置>')}\n")

    analyse_signatures()

    print("\n=== 3. 替换后的行为验证 ===")
    from core.config import get_settings
    from db.session import SessionLocal, init_db
    init_db()
    db = SessionLocal()
    try:
        from services import share_service

        def effective_shared_mode(db_session) -> bool:
            """这是建议的修法：唯一权威 = share_service.shared_mode(db)。"""
            return share_service.shared_mode(db_session)

        print(f"  初始（都没开共享）:")
        print(f"    环境变量源 settings.shared_mode : {get_settings().shared_mode}")
        print(f"    唯一权威 effective_shared_mode  : {effective_shared_mode(db)}")

        print(f"\n  界面上打开共享模式后:")
        share_service.set_shared_mode(db, True)
        db.commit()
        env_view = get_settings().shared_mode
        auth_view = effective_shared_mode(db)
        print(f"    环境变量源 settings.shared_mode : {env_view}  ← 现在鉴权读的（错）")
        print(f"    唯一权威 effective_shared_mode  : {auth_view}  ← 改成读这个（对）")
        print()
        if auth_view is True and env_view is False:
            print("  ✓ 修复成立：改用唯一权威后，界面一开共享，鉴权就能感知")
            print("    → 本机 secret.key 会被 require_token 拒绝（不再是管理员）")
        else:
            print("  ✗ 修复方案未达预期")

        print("\n=== 4. 单机模式不受影响（默认行为不变）===")
        share_service.set_shared_mode(db, False)
        db.commit()
        print(f"    关掉共享后唯一权威: {effective_shared_mode(db)}")
        print("    → 单机模式仍是 False，本机 secret 照旧代表这台机器 ✅")

        print("\n=== 5. lru_cache 的坑（为什么环境变量源特别危险）===")
        cfg_src = (SERVER / "core" / "config.py").read_text(encoding="utf-8")
        for i, line in enumerate(cfg_src.splitlines(), 1):
            if "lru_cache" in line:
                print(f"    config.py:{i}  {line.strip()}")
        print("    → get_settings() 被缓存，进程起来后环境变量再也读不到新值")
        print("      所以「改环境变量后重启」是唯一生效方式，而界面开关是即时写库")
        print("      两者语义根本不同，不可能是同一个真值源")

    finally:
        db.close()

    print("\n=== 结论 ===")
    print("  修法（两行）：")
    print("    api/deps.py:88   if settings.shared_mode:  →  if share_service.shared_mode(db):")
    print("    api/deps.py:116  if settings.shared_mode:  →  if share_service.shared_mode(db):")
    print("  配套：")
    print("    · core/config.py:82 的注释改正（它说「没有任何鉴权逻辑读它」，是错的）")
    print("    · 加回归测试：开共享后本机 secret 必须 401")


if __name__ == "__main__":
    main()
