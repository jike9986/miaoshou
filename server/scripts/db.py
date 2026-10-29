"""Alembic 迁移便捷入口（在 server/ 下执行）。

为什么不直接敲 `alembic ...`：这个项目的数据目录受 AOHUANG_DATA_DIR 控制，
而 alembic 会按当前环境变量去连库。忘了设就会连到用户真实库上改结构。
本脚本默认把目标库钉在临时目录，只有显式加 `--real` 才碰真实库 ——
把「误改用户 355 件商品」的概率降到「得自己多敲一个参数」。

用法：
    python scripts/db.py upgrade            # 临时库：建到最新
    python scripts/db.py revision -m "加 xxx 列"   # 临时库：autogenerate
    python scripts/db.py downgrade -1       # 临时库：回退一步
    python scripts/db.py history            # 看迁移链
    python scripts/db.py check              # 自检：upgrade/downgrade/无漂移/stamp

    python scripts/db.py upgrade --real     # 真实库（谨慎，会打印目标路径并等确认）

`revision --autogenerate` 生成的空迁移（没有任何变更）会自动删掉并提示，
避免仓库里堆一堆 upgrade() 里只有 pass 的文件。
"""
from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = SERVER_DIR / "migrations"
VERSIONS_DIR = MIGRATIONS_DIR / "versions"


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(OSError, ValueError):
                reconfigure(encoding="utf-8")


def _real_data_dir() -> Path:
    """真实数据目录（与 core/config.py 的规则一致，但不导入它）。"""
    env = os.environ.get("AOHUANG_DATA_DIR") or os.environ.get("TK_AUTOMATION_DATA_DIR")
    if env:
        return Path(env)
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "aohuang-desktop"


def _run_alembic(
    args: list[str], data_dir: Path, *, allow_configured_database: bool = False,
) -> int:
    env = dict(os.environ)
    env["AOHUANG_DATA_DIR"] = str(data_dir)
    if not allow_configured_database:
        env["AOHUANG_DATABASE_URL"] = ""
    env["PYTHONPATH"] = str(SERVER_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    print(f"[db] AOHUANG_DATA_DIR={data_dir}")
    print(f"[db] alembic {' '.join(args)}")
    return subprocess.call(
        [sys.executable, "-m", "alembic", *args], cwd=str(SERVER_DIR), env=env,
    )


def _empty_revision_guard(before: set[Path], message: str) -> None:
    """autogenerate 若产出空迁移（upgrade 里只有 pass），删掉并提示。

    空迁移不是「无害」的：它会成为新的 head，之后每次 autogenerate 都接在它
    后面，历史里堆一串什么都没做的版本号，排查问题时要多看 N 个文件。
    """
    new_files = sorted(set(VERSIONS_DIR.glob("*.py")) - before)
    for path in new_files:
        text = path.read_text(encoding="utf-8")
        body = text.split("def upgrade() -> None:")[1].split("def downgrade()")[0]
        ops = [line for line in body.splitlines()
               if line.strip() and not line.strip().startswith("#")
               and line.strip() != "pass"]
        if ops:
            print(f"[db] 已生成迁移：{path.name}（{len(ops)} 条操作）")
        else:
            path.unlink(missing_ok=True)
            print(f"[db] 模型与迁移无差异，已删除空迁移 {path.name}"
                  f"（{message!r} 未产生变更）")


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    parser = argparse.ArgumentParser(
        description="Alembic 迁移入口（默认操作临时库）",
        epilog=("子命令直接透传给 alembic，如 "
                "upgrade / downgrade / revision / history / stamp / current"),
    )
    parser.add_argument("--real", action="store_true",
                        help="操作真实数据目录（默认用一次性临时目录）")
    parser.add_argument("command", nargs="?", default="upgrade", help="alembic 子命令")
    parser.add_argument("rest", nargs=argparse.REMAINDER, help="透传给 alembic 的参数")
    args = parser.parse_args(argv)

    if args.command == "check":
        env = dict(os.environ)
        env["AOHUANG_DATABASE_URL"] = ""
        return subprocess.call([sys.executable, str(MIGRATIONS_DIR / "_selftest.py")],
                               cwd=str(SERVER_DIR), env=env)

    if args.real:
        data_dir = _real_data_dir()
        configured_url = os.environ.get("AOHUANG_DATABASE_URL", "").strip()
        if configured_url:
            from sqlalchemy.engine import make_url

            target_url = make_url(configured_url)
            print(f"[db] 目标真实库：{target_url.render_as_string(hide_password=True)}")
            missing_sqlite_db = (
                target_url.get_backend_name() == "sqlite"
                and target_url.database not in (None, ":memory:")
                and not Path(target_url.database).exists()
            )
        else:
            db_path = data_dir / "app.db"
            print(f"[db] 目标真实库：{db_path}")
            missing_sqlite_db = not db_path.exists()
        if missing_sqlite_db:
            print("[db] 该库不存在，命令会新建一个空库。", file=sys.stderr)
        answer = input("确认操作真实库？输入 yes 继续：").strip().lower()
        if answer != "yes":
            print("[db] 已取消")
            return 1
    else:
        data_dir = Path(tempfile.mkdtemp(prefix="aohuang-db-"))
        print("[db] 使用一次性临时目录（不碰真实库）。要改真实库请加 --real")

    try:
        rest = list(args.rest)
        # `upgrade` / `downgrade` / `stamp` 不写目标时默认 head：
        # `alembic upgrade` 裸跑会直接报 "revision is required"，容易以为脚本坏了
        if args.command in {"upgrade", "downgrade", "stamp"} and not rest:
            rest = ["head"]

        # autogenerate 要求目标库已到 head，否则直接报
        # "Target database is not up to date." 一次性临时库天然是空的，
        # 所以先把它建到 head 再比对 —— 这正是「模型 vs 迁移」的正确对照基线。
        if args.command == "revision" and "--autogenerate" in rest and not args.real:
            code = _run_alembic(["upgrade", "head"], data_dir)
            if code != 0:
                print("[db] 临时库 upgrade head 失败，无法生成迁移", file=sys.stderr)
                return code

        before = set(VERSIONS_DIR.glob("*.py"))
        code = _run_alembic(
            [args.command, *rest], data_dir, allow_configured_database=args.real,
        )
        if args.command == "revision" and "--autogenerate" in rest:
            message = ""
            if "-m" in rest:
                message = rest[rest.index("-m") + 1]
            _empty_revision_guard(before, message)
        return code
    finally:
        if not args.real:
            shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
