"""把指定软件账号提升为老师（teacher）。

为什么要脚本而不是调接口：
  `PATCH /operators/{id}` 内部要求调用者是 superadmin（is_superadmin），
  而生产库里当前没有显式 admin —— 唯一老师的判定见 operator_service
  的「首个启用老师兜底」逻辑。用运维脚本直接改库最直接，也便于留痕。

teacher 与 admin 权限等价（ROLE_PERMISSIONS 里都是 {"*"}），
所以提升为 teacher 就拥有：改重量尺寸、批量改类目、入库、看自动化结果等
全部管理能力。

默认只报告；--apply 才写库。
"""
from __future__ import annotations

import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="提升账号为老师")
    parser.add_argument("--id", type=int, default=0, help="账号 id")
    parser.add_argument("--email", default="", help="按邮箱定位账号")
    parser.add_argument("--apply", action="store_true", help="真正写库")
    args = parser.parse_args(argv)

    if not args.id and not args.email:
        print("需要 --id 或 --email 指定账号")
        return 2

    from db.session import SessionLocal
    from models.operator import Operator

    session = SessionLocal()
    try:
        query = session.query(Operator)
        target = (query.filter(Operator.id == args.id).one_or_none()
                  if args.id else
                  query.filter(Operator.email == args.email).one_or_none())
        if target is None:
            print("找不到账号")
            return 1

        print("账号: id=%s name=%s email=%s" % (
            target.id, target.name, getattr(target, "email", "")))
        print("当前角色: %s | enabled=%s" % (target.role, target.enabled))

        if target.role == "teacher":
            print("已经是老师，无需修改")
            return 0

        if not args.apply:
            print("将改为: teacher（加 --apply 才写库）")
            return 0

        target.role = "teacher"
        session.commit()
        print("已改为 teacher")

        # 复核
        session.expire_all()
        fresh = session.get(Operator, target.id)
        print("复核: role=%s enabled=%s" % (fresh.role, fresh.enabled))
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
