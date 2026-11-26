"""独立复现 C-P0-1：shared_mode 双源分裂 → 共享部署下本机 secret.key 仍是管理员。

审计 C 声称：
  share_service.shared_mode(db)  优先读 settings 表（界面开关写这里）
  Settings.shared_mode           只读环境变量 AOHUANG_SHARED_MODE
  api/deps.py:86 / :116          鉴权读的是后者
  → 界面打开共享模式后，鉴权仍认为「单机」→ 本机 secret.key 被当管理员

用法：cd miaoshou/server && python -X utf8 ../docs/audit/_verify_c_p0_1.py
"""
from __future__ import annotations

import ast
import os
import sys
import tempfile
from pathlib import Path

_DATA_DIR = tempfile.mkdtemp(prefix="audit-c-p01-")
os.environ["AOHUANG_DATA_DIR"] = _DATA_DIR
# 关键：显式不设 AOHUANG_SHARED_MODE（模拟运维只点了界面开关）
os.environ.pop("AOHUANG_SHARED_MODE", None)
os.environ.pop("AOHUANG_DATABASE_URL", None)

SERVER = Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(SERVER))


def main() -> None:
    print(f"数据目录（临时）: {_DATA_DIR}")
    print(f"AOHUANG_SHARED_MODE 环境变量: "
          f"{os.environ.get('AOHUANG_SHARED_MODE', '<未设置>')}\n")

    from core.config import get_settings
    from db.session import SessionLocal, init_db
    init_db()
    db = SessionLocal()

    try:
        from services import setting_service, share_service

        print("=== 1. 初始状态（都没开共享）===")
        print(f"  settings.shared_mode（环境变量源，鉴权读它）: {get_settings().shared_mode}")
        print(f"  share_service.shared_mode(db)（表源）      : {share_service.shared_mode(db)}")

        print("\n=== 2. 模拟运维在界面上打开共享模式 ===")
        # 界面走 POST /system/share -> share_service.set_shared_mode
        share_service.set_shared_mode(db, True)
        db.commit()

        env_view = get_settings().shared_mode
        table_view = share_service.shared_mode(db)
        print(f"  settings.shared_mode（环境变量源）: {env_view}")
        print(f"  share_service.shared_mode(db)（表源）: {table_view}")

        print("\n=== 3. 两个真值源是否一致 ===")
        consistent = (env_view == table_view)
        print(f"  一致: {consistent}")
        if not consistent:
            print("  ★ 双源分裂成立（C-P0-1）")
            print("     界面写 settings 表 → 表源 True（监听 0.0.0.0，别人能连）")
            print("     鉴权读环境变量 → 仍是 False → 认为「单机」→ 本机 secret = 管理员")

        print("\n=== 4. 鉴权实际读的是哪个（AST 定位）===")
        deps_src = (SERVER / "api" / "deps.py").read_text(encoding="utf-8")
        tree = ast.parse(deps_src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if node.attr == "shared_mode":
                src_line = deps_src.splitlines()[node.lineno - 1].strip()
                print(f"  deps.py:{node.lineno}  {src_line}")
        print("  → 全部是 settings.shared_mode（环境变量），没有一处调 share_service")

        print("\n=== 5. core/config.py 的注释怎么说的 ===")
        cfg_src = (SERVER / "core" / "config.py").read_text(encoding="utf-8")
        for i, line in enumerate(cfg_src.splitlines(), 1):
            if "不是" in line and "权限开关" in line:
                print(f"  config.py:{i}  {line.strip()}")
                print("  ★ 注释说「没有任何鉴权逻辑读它」，实测有 2 处鉴权读它 → 注释与代码互相打脸")

    finally:
        db.close()

    print("\n=== 结论 ===")
    print("  修复：api/deps.py 的 2 处改为调用 share_service.shared_mode(db)（唯一权威）")
    print("        并同步改正 core/config.py:82 的注释")


if __name__ == "__main__":
    main()
