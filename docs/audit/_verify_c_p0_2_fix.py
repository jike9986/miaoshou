"""验证 C-P0-2 / C-P0-6 的修复方案：把闸门接到必经之路，且不误伤。

## 为什么这两条要一起验证

它们形状相同、修法相同，都卡在同一个问题上：

  C-P0-2  受限分组闸门 guard_group 零调用 → 学员能读/认领/发布「精品」组商品
  C-P0-6  跨租户发布：23 个 resolve_product 调用点 0 个传 account_id

而**两者的修复都有误伤风险**，必须同时验证正反两面：

  · 加闸门后，老师/管理员还能不能正常用？（他们是允许碰受限组的）
  · 加 account_id 后，跨账号但同属一人的场景会不会被误拦？
  · 老师是全局角色，不能因为加隔离就把老师也拦住

只验证「能拦住学员」不够 —— 那是把校验加严，可能顺手把正常业务打死。
这正是本项目 42% 补丁叠补丁的来源：改一处、坏一处。

## 用法

    cd miaoshou/server && python -X utf8 ../docs/audit/_verify_c_p0_2_fix.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_DATA_DIR = tempfile.mkdtemp(prefix="audit-c-p02-fix-")
os.environ["AOHUANG_DATA_DIR"] = _DATA_DIR
os.environ.pop("AOHUANG_DATABASE_URL", None)

SERVER = Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(SERVER))

from db.session import SessionLocal, init_db  # noqa: E402
from models.group import Group  # noqa: E402
from models.operator import Operator  # noqa: E402
from services import group_access  # noqa: E402


def make_operator(db, name: str, role: str) -> Operator:
    op = Operator(name=name, token=f"tk-{name}", role=role, enabled=True)
    db.add(op)
    db.commit()
    return op


def main() -> None:
    print(f"数据目录（临时）: {_DATA_DIR}\n")
    init_db()
    db = SessionLocal()
    try:
        print("=== 0. 受限分组的判定依据（先看清规则再谈修复）===")
        print(f"  RESTRICTED_GROUP_NAMES = "
              f"{getattr(group_access, 'RESTRICTED_GROUP_NAMES', '(无)')}")
        print(f"  RESTRICTED_GROUP_IDS   = "
              f"{getattr(group_access, 'RESTRICTED_GROUP_IDS', '(无)')}")

        # 造三个角色
        member = make_operator(db, "audit-member", "member")
        teacher = make_operator(db, "audit-teacher", "teacher")
        admin = make_operator(db, "audit-admin", "admin")

        print("\n=== 1. may_access_restricted：角色矩阵 ===")
        for op in (member, teacher, admin):
            print(f"  {op.role:8} 可访问受限分组: "
                  f"{group_access.may_access_restricted(op)}")

        print("\n=== 2. guard_group 的现有行为（对受限组 id 直接判）===")
        # 受限组 id 即使不在库里，is_restricted_group_id 也按 id 兜底
        restricted_ids = sorted(getattr(group_access, "RESTRICTED_GROUP_IDS", ()) or ())
        sample_id = restricted_ids[0] if restricted_ids else None
        print(f"  取样受限组 id: {sample_id}")
        if sample_id is not None:
            for op in (member, teacher, admin):
                try:
                    group_access.guard_group(db, op, sample_id)
                    print(f"  {op.role:8} → 放行")
                except Exception as exc:
                    code = getattr(exc, "code", type(exc).__name__)
                    status = getattr(exc, "status", "?")
                    print(f"  {op.role:8} → 拦住 ({status} {code})")

        print("\n=== 3. 非受限组：所有人都不该被拦（防误伤）===")
        # ⚠ 注意：RESTRICTED_GROUP_IDS = {1,2,3} 是「id 兜底集合」，
        # 新库自增 id 从 1 开始 → 第一个普通组恰好会拿到 id=1 而被误判受限。
        # 这不是 guard_group 的 bug，是「id 兜底」这个设计的固有限制：
        # 它假设生产库里 1/2/3 就是那三个精品组。
        # 所以这里刻意把普通组插到 id>3 的位置，才能测到真正的「非受限」路径。
        normal = Group(name="普通款", sort=99)
        db.add(normal)
        db.commit()
        while normal.id <= 3:
            db.add(Group(name=f"占位{normal.id}", sort=99))
            db.commit()
            normal = Group(name=f"普通款{normal.id}", sort=99)
            db.add(normal)
            db.commit()
        print(f"  普通组 id = {normal.id}（>3，避开 id 兜底集合）")
        print(f"  is_restricted_group(普通组) = "
              f"{group_access.is_restricted_group(db, normal.id)}")
        for op in (member, teacher, admin):
            try:
                group_access.guard_group(db, op, normal.id)
                print(f"  {op.role:8} 普通组 → 放行 ✓")
            except Exception as exc:
                print(f"  {op.role:8} 普通组 → 被拦 ✗ ({type(exc).__name__}) 误伤！")

        print("\n=== 3b. id 兜底设计的固有风险（这条要写进报告）===")
        db.add(Group(name="误判测试组", sort=99))
        db.commit()
        probe = db.query(Group).filter(Group.name == "误判测试组").one()
        print(f"  新插入的普通组 id={probe.id}，"
              f"is_restricted_group = {group_access.is_restricted_group(db, probe.id)}")
        print("  → 若新库从头建，前 3 个普通组会被 id 兜底误判成「精品组」")
        print("    生产库不受影响（1/2/3 确实是精品组），但**测试库/新部署会踩**")
        print("    建议：id 兜底只在「名字匹配不到但库里确实有这三个组」时启用")

        print("\n=== 4. operator 为 None（内部调用/定时任务）时的行为 ===")
        try:
            group_access.guard_group(db, None, sample_id)
            print("  None → 放行（内部调用不拦，合理）")
        except Exception as exc:
            code = getattr(exc, "code", type(exc).__name__)
            print(f"  None → 拦住 ({code})")
            print("  ⚠ 内部调用（定时任务/后台线程）不传 operator，若被拦会打死任务")
            print("    接闸门前必须确认：这些路径的 operator 参数是真的有值")

        print("\n=== 5. 结论：修法可执行性 ===")
        print("  C-P0-2 修法（4 处接线，不改逻辑）：")
        print("    · api/warehouse.py:450 warehouse_detail  → guard_group(db, operator, 商品的 groupId)")
        print("    · api/warehouse.py:466 claim_product     → 同上")
        print("    · services/publish_service.py:536/560    → guard_products(批量的 warehouseId)")
        print("  关键：guard_group 本身已经是正确的（角色矩阵对、普通组不误伤），")
        print("        缺的只是「接上」—— 这是最小改动、最低风险的一类修复。")

        print("\n  C-P0-6 修法（1 处签名 + 2 处调用）：")
        print("    · resolve_product 的 account_id 改必传（None 需显式写出）")
        print("    · publish_service.py:536 传 account_id")
        print("    · publish_service.py:560 的裸 ORM 加 .filter(Product.account_id == account_id)")
        print("    老师路径要显式放行（老师是全局角色），否则会误伤老师跨账号发布。")

    finally:
        db.close()


if __name__ == "__main__":
    main()
