"""Initialize the one top-level administrator from the server shell only.

This tool has no HTTP counterpart and never accepts credentials as command-line
arguments. It uses the application's configured database and normal schema
initialization checks before creating the account.
"""
from __future__ import annotations

import argparse
import contextlib
import getpass
import sys
from pathlib import Path

from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from core import errors  # noqa: E402
from core.config import get_settings  # noqa: E402
from db.session import SessionLocal, engine, init_db  # noqa: E402
from models.operator import Operator  # noqa: E402
from services import operator_service  # noqa: E402


class InitializationAlreadyComplete(RuntimeError):
    """Raised when the database already has a top-level administrator."""


def _acquire_initialization_lock(db: Session) -> None:
    """Serialize competing bootstrap attempts across supported database engines."""
    if db.bind is None:
        raise RuntimeError("数据库会话未绑定")
    dialect = db.bind.dialect.name
    if dialect == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 684_210_771})
    elif dialect == "sqlite":
        db.execute(text("BEGIN IMMEDIATE"))
    else:
        raise RuntimeError(f"不支持的数据库类型：{dialect}")


def _ensure_uninitialized(db: Session) -> None:
    operators = db.query(Operator).order_by(Operator.id.asc()).all()
    explicit_admin = any(operator_service.role_of(row) == operator_service.ROLE_ADMIN
                         for row in operators)
    implicit_admin = any(operator_service.is_superadmin(db, row) for row in operators)
    if explicit_admin or implicit_admin:
        raise InitializationAlreadyComplete(
            "数据库已存在最高权限账号；为避免覆盖现有权限，初始化已拒绝。"
        )


def create_superadmin(db: Session, username: str, email: str, password: str) -> Operator:
    """Create the sole admin account in the caller's transaction.

    The generated bearer token is deliberately discarded; password login will
    issue the user's token through the regular authentication flow.
    """
    _acquire_initialization_lock(db)
    _ensure_uninitialized(db)

    clean_username = operator_service.normalize_username(username)
    clean_email = operator_service.normalize_deliverable_email(email)
    if db.query(Operator.id).filter(Operator.username == clean_username).first():
        raise ValueError("用户名已存在")
    if db.query(Operator.id).filter(
            func.lower(func.trim(Operator.email)) == clean_email).first():
        raise ValueError("邮箱已绑定其他账号")
    if db.query(Operator.id).filter(Operator.name == clean_username).first():
        raise ValueError("用户名与现有显示名称冲突，请换一个用户名")

    row, _discarded_token = operator_service.create(
        db, clean_username, role=operator_service.ROLE_ADMIN, email=clean_email)
    row.username = clean_username
    row.password_hash = operator_service.hash_password(password)
    row.must_change_password = False
    db.flush()
    return row


def _target_label() -> str:
    if engine.dialect.name == "sqlite":
        return f"SQLite: {engine.url.database}"
    return f"{engine.dialect.name}: {engine.url.render_as_string(hide_password=True)}"


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(OSError, ValueError):
                reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="在服务器本机安全初始化唯一最高权限账号；密码仅交互输入。")
    parser.parse_args()

    print("奥皇上品最高权限账号初始化（仅限服务器本机运维终端）")
    print(f"目标数据库：{_target_label()}")
    print("此操作会按应用现有模式检查/初始化本地 SQLite，PostgreSQL 必须已迁移到当前版本。")
    try:
        confirmation = input("输入 INIT ADMIN 确认操作此数据库：").strip()
    except EOFError:
        print("当前没有交互终端，已取消。", file=sys.stderr)
        return 1
    if confirmation != "INIT ADMIN":
        print("已取消。")
        return 1

    try:
        username = input("管理员用户名：").strip()
        email = input("管理员邮箱：").strip()
        password = getpass.getpass("管理员密码（输入不可见）：")
        repeated_password = getpass.getpass("再次输入管理员密码：")
    except EOFError:
        print("交互输入未完成，已取消。", file=sys.stderr)
        return 1
    if password != repeated_password:
        print("两次输入的密码不一致。", file=sys.stderr)
        return 2

    try:
        get_settings().ensure_dirs()
        init_db()
        with SessionLocal() as db:
            try:
                row = create_superadmin(db, username, email, password)
                db.commit()
            except Exception:
                db.rollback()
                raise
    except InitializationAlreadyComplete as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except errors.AppError as exc:
        print(f"初始化失败：{exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"初始化失败：{exc}", file=sys.stderr)
        return 2
    except IntegrityError:
        print("初始化失败（数据库约束冲突）。未创建账号，请检查数据库状态。",
              file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - avoid printing DSNs or secrets
        print(f"初始化失败（{type(exc).__name__}）。请检查服务器数据库配置与迁移状态。",
              file=sys.stderr)
        return 2

    print(f"最高权限账号已创建：{row.username}。请使用登录界面登录并立即确认账号信息。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
