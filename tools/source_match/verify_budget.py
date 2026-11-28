"""验证图片预算上限：读到的值 + 通过 set_image_limits 写入是否真的生效。

修复前：前端/接口改 `limitMilliyuan`（旧版唯一认的键）写进去，
        `budget_limit()` 读 `budgetMilliyuan` → 静默无效。
修复后：两个键都收，统一落到 `budgetMilliyuan`。
"""
import json
import warnings

warnings.filterwarnings("ignore")

from db.session import SessionLocal
from services import image_service, setting_service

db = SessionLocal()
try:
    print("=" * 70)
    print("当前状态")
    print("=" * 70)
    raw = setting_service.image_limits(db)
    print(f"  settings['image.limits'] = {json.dumps(raw, ensure_ascii=False)}")
    print(f"  budget_limit() = {image_service.budget_limit(db)} 毫央 "
          f"= {image_service.budget_limit(db)/1000:.2f} 元")
    used = image_service.reserved_total(db)
    print(f"  已预扣费 = {used} 毫央 = {used/1000:.2f} 元")
    print(f"  剩余 = {(image_service.budget_limit(db)-used)/1000:.2f} 元")

    print()
    print("=" * 70)
    print("写入路径验证：用旧键名 limitMilliyuan 提交，看是否落到 budgetMilliyuan")
    print("=" * 70)
    before = dict(raw)
    updated = setting_service.set_image_limits(
        db, {"limitMilliyuan": 100000})
    db.rollback()  # 本次只是验证，不落库
    print(f"  返回: {json.dumps(updated, ensure_ascii=False)}")
    ok = updated.get("budgetMilliyuan") == 100000
    print(f"  budgetMilliyuan = {updated.get('budgetMilliyuan')}  "
          f"{'✓ 修复生效' if ok else '✗ 仍未生效'}")
    print(f"  残留旧键 limitMilliyuan = {updated.get('limitMilliyuan')} "
          f"{'（已清理）' if 'limitMilliyuan' not in updated else '（仍存在，异常）'}")

    print()
    print("=" * 70)
    print("usage() 返回给前端的 budget 对象")
    print("=" * 70)
    u = image_service.usage(db)
    print(f"  {json.dumps(u.get('budget'), ensure_ascii=False)}")
    print(f"  budgets = {json.dumps(u.get('budgets'), ensure_ascii=False)}")
finally:
    db.close()
