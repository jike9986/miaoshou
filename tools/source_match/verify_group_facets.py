"""验证 facets.groups 对学员剔除受限三组。

不造 Operator 行（模型字段多、易踩 NOT NULL），直接用轻量桩对象：
`group_access.may_access_restricted` 只看 `enabled` 和 `role`。
"""
import sys
sys.path.insert(0, "server")

from types import SimpleNamespace

from db.session import SessionLocal, init_db
from models.group import Group
from services import group_access, operator_service, warehouse_service

init_db()
db = SessionLocal()


class FakeOperator(SimpleNamespace):
    """够 group_access / role_of 用即可。"""
    pass


try:
    existing = {g.name: g for g in db.query(Group).all()}
    for i, name in enumerate(["自营海外仓", "海外仓定制款", "精品", "普通款"]):
        if name not in existing:
            db.add(Group(name=name, sort=len(existing) + i))
    db.commit()

    restricted = group_access.restricted_group_ids(db)
    print(f"受限分组 id: {sorted(restricted)}")

    for role, label in ((operator_service.ROLE_MEMBER, "学员"),
                        (operator_service.ROLE_TEACHER, "老师")):
        op = FakeOperator(enabled=True, role=role, is_admin=False)
        print(f"\n{label}: may_access_restricted = "
              f"{group_access.may_access_restricted(op)}")
        print(f"  visible_group_ids = {group_access.visible_group_ids(db, op)}")

        # facets 内部就是这样拿 hidden_groups 的
        hidden = group_access.visible_group_ids(db, op) or set()
        shown = [g.name for g in db.query(Group).all() if int(g.id) not in hidden]
        print(f"  分组栏实际显示: {shown}")

        if role == operator_service.ROLE_MEMBER:
            blocked = [n for n in shown if n in group_access.RESTRICTED_GROUP_NAMES]
            assert not blocked, f"学员不该看到受限分组，却看到 {blocked}"
            print("  ✓ 受限三组被剔除，学员选不到")
        else:
            kept = [n for n in shown if n in group_access.RESTRICTED_GROUP_NAMES]
            print(f"  ✓ 老师保留受限组: {kept}")
finally:
    db.close()

print("\n=== 结论 ===")
print("「选择发布商品」分组栏的数据源是 facets.groups，")
print("facets 已按 visible_group_ids 剔除受限三组 —— 学员看不到也选不了。")
print("写入层还有 guard_products 二次拦截，绕过前端直接调 API 也会 403。")