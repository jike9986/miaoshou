"""核对成品仓库实际件数，以及卡片到底显示哪个价格。"""
import json
import warnings

warnings.filterwarnings("ignore")

from sqlalchemy import text

from db.session import SessionLocal

db = SessionLocal()

print("=" * 70)
print("成品仓库件数核对")
print("=" * 70)

queries = {
    "products 全部（含已删）": "select count(*) from products",
    "products deleted_at is null": "select count(*) from products where deleted_at is null",
    "products 非自动化结果": ("select count(*) from products "
                              "where deleted_at is null and automation_result = false"),
    "products 自动化结果": ("select count(*) from products "
                            "where deleted_at is null and automation_result = true"),
    "products 有 account_id": ("select count(*) from products "
                               "where deleted_at is null and account_id is not null"),
}
for label, sql in queries.items():
    try:
        n = db.execute(text(sql)).scalar()
        print(f"  {label:<28} {n}")
    except Exception as ex:  # noqa: BLE001
        print(f"  {label:<28} 查询失败 {type(ex).__name__}")

# 列表接口实际用的过滤条件
print()
print("=" * 70)
print("仓库列表接口的实际过滤（warehouse_service.list_products）")
print("=" * 70)

# 卡片显示的价格字段
print()
print("=" * 70)
print("卡片显示哪个价格")
print("=" * 70)
row = db.execute(text("""
    select id, price_min, price_max, price_currency, payload
    from products where deleted_at is null order by id limit 5
""")).fetchall()
for pid, pmin, pmax, cur, payload in row:
    pl = payload if isinstance(payload, dict) else json.loads(payload or "{}")
    sku = (pl.get("skuMap") or {})
    first = next((s for s in sku.values() if isinstance(s, dict)), {})
    print(f"  #{pid}")
    print(f"    卡片显示 price_min/price_max = {pmin} / {pmax} {cur}")
    print(f"    ← 来自 sku.originPrice = {first.get('originPrice')!r}")
    print(f"    sku.price（卖价字段）= {first.get('price')!r}  ← 卡片不显示这个")

db.close()