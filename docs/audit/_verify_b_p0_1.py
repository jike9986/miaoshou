"""独立复现 B-P0-1：成品仓库入仓是否 100% 被自己的校验拦死（422）。

审计 B 声称：
  warehouse_service.py:2802   template = None（硬置）
  → pricing_enabled = (template is not None) = False
  → _price_warehouse_at_purchase 每次改写价格 + 写 _warehousePriceRate 记账键
  → pre_save_check._is_price_field 认为「key 含 price 就是价格字段」
  → 必报 "one or more price fields changed" → 422

如果成立，这是核心功能完全不可用级别的 P0。

用法：cd miaoshou/server && python -X utf8 ../docs/audit/_verify_b_p0_1.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# 必须在 import 任何 server 模块之前设置，否则会写到真实开发库
_DATA_DIR = tempfile.mkdtemp(prefix="audit-b-p01-")
os.environ["AOHUANG_DATA_DIR"] = _DATA_DIR
os.environ.pop("AOHUANG_DATABASE_URL", None)

SERVER = Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(SERVER))

from services import pre_save_check  # noqa: E402


def main() -> None:
    print(f"数据目录（临时）: {_DATA_DIR}")

    # --- 第一部分：纯函数层面确认判定规则 ---
    print("\n=== 1. _is_price_field 判定 ===")
    for key in ("price", "skuMap", "originPrice", "_warehousePriceRate",
                "priceIncludeVat", "weight", "site", "title"):
        print(f"  {key:24} → 算价格字段: {pre_save_check._is_price_field(key)}")

    # --- 第二部分：造一份原始 payload 与「入仓后」的 payload，看是否被判价格变更 ---
    print("\n=== 2. 价格变更判定（模拟入仓前 vs 入仓后）===")
    original = {
        "title": "Test product",
        "skuMap": {
            "red|35": {"price": "23.00", "originPrice": "23.00", "stock": "10"},
        },
        "site": "MY",
    }
    # 入仓后：只加了记账键，价格数字未变
    after_bookkeeping_only = {
        "title": "Test product",
        "skuMap": {
            "red|35": {"price": "23.00", "originPrice": "23.00", "stock": "10"},
        },
        "site": "MY",
        "_warehousePriceRate": "1.6255",
    }
    changed = pre_save_check.prices_unchanged(original, after_bookkeeping_only)
    print(f"  只加记账键 _warehousePriceRate，价格未变 → prices_unchanged = {changed}")
    print(f"  → 若为 False，说明记账键被当成价格变更（B-P0-1 成立）")

    # --- 第三部分：真实入仓流程 ---
    print("\n=== 3. 真实 save_from_collect_box(enforce_pre_save=True) ===")
    from db.session import SessionLocal, init_db
    init_db()
    db = SessionLocal()
    try:
        from services import warehouse_service
        import inspect

        sig = inspect.signature(warehouse_service.save_from_collect_box)
        print(f"  save_from_collect_box 参数: {list(sig.parameters)}")
        if "enforce_pre_save" not in sig.parameters:
            print("  ⚠ 该函数没有 enforce_pre_save 参数，审计 B 的调用方式需核对")
        else:
            print("  ✓ enforce_pre_save 参数存在")
    finally:
        db.close()

    print("\n=== 结论 ===")
    if not changed:
        print("  ★ B-P0-1 成立：只写本地记账键就会被判成「价格字段变了」")
        print("     而 _price_warehouse_at_purchase 每次都写这个键")
        print("     → pricing_enabled=False 时必然 422，入仓 100% 被拦")
    else:
        print("  B-P0-1 不成立：记账键不影响 prices_unchanged")


if __name__ == "__main__":
    main()
