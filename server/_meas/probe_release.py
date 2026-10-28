"""撤销认领探针：验 `claimed=false` 真的走释放分支，而不是照样认领。

这个字段以前定义了但没人读，传 false 也照样认领 —— 运营标错了只能去改数据库。
探针重点验三件事：
  1. `claimed=false` 不碰上游（不调 claim_to_shop / fill_carriers）
  2. 只退本账号那一条，别的账号的认领记录不受影响
  3. 还有别的账号认领时 `claimed` 保持 True，不会误退回未认领
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["AOHUANG_DATA_DIR"] = tempfile.mkdtemp(prefix="release-")
os.environ["TK_AUTOMATION_DATA_DIR"] = os.environ["AOHUANG_DATA_DIR"]

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), str(detail)))


def main() -> int:
    from core import errors
    from db.session import SessionLocal, init_db
    from models.account import Account
    from models.product import Product
    from services import collect_box_service
    from services.collect_box_service import product_key

    init_db()
    db = SessionLocal()
    acc_a = Account(id=1, name="账号甲", app_key="a", app_secret_enc="a",
                    site="MY", shops=json.dumps([]), created_at=0.0, updated_at=0.0)
    acc_b = Account(id=2, name="账号乙", app_key="b", app_secret_enc="b",
                    site="MY", shops=json.dumps([]), created_at=0.0, updated_at=0.0)
    db.add_all([acc_a, acc_b])
    db.commit()

    key = product_key("3484413012", "18077147")
    product = Product(
        warehouse_key=key, account_id=1, detail_id="3484413012", shop_id="18077147",
        title="测试商品", claimed=True, claimed_at=1000.0, claimed_shop_id="18077147",
        # 两个账号都认领过：用来验「只退本账号」
        collected={"账号甲": {"at": 1000.0, "detailId": "1"},
                   "账号乙": {"at": 2000.0, "detailId": "2"}},
        status="ready", saved_at=0.0, updated_at=0.0,
    )
    db.add(product)
    db.commit()

    # ---------- 1. 释放只退本账号 ----------
    result = collect_box_service.release(
        db, acc_a, detail_id="3484413012", shop_id="18077147")
    db.commit()
    check("released=True", result["released"] is True, str(result["released"]))
    check("本账号记录被移除", "账号甲" not in (product.collected or {}),
          str(product.collected))
    check("别的账号记录保留", "账号乙" in (product.collected or {}),
          str(product.collected))
    check("还有别的账号认领 → claimed 保持 True",
          product.claimed is True, str(product.claimed))
    check("stillClaimed=True", result["stillClaimed"] is True, str(result["stillClaimed"]))
    check("claimed_shop_id 未被清（还有认领）",
          product.claimed_shop_id == "18077147", str(product.claimed_shop_id))
    check("返回结构带 detailId", result["detailId"] == "3484413012", result["detailId"])
    check("返回结构带 count=0", result["count"] == 0, str(result["count"]))

    # ---------- 2. 最后一个账号也退 → 真的回到未认领 ----------
    result2 = collect_box_service.release(
        db, acc_b, detail_id="3484413012", shop_id="18077147")
    db.commit()
    check("最后一个也退 → collected 空", (product.collected or {}) == {},
          str(product.collected))
    check("最后一个也退 → claimed=False", product.claimed is False, str(product.claimed))
    check("claimed_at 被清", product.claimed_at is None, str(product.claimed_at))
    check("claimed_shop_id 被清", product.claimed_shop_id is None,
          str(product.claimed_shop_id))
    check("stillClaimed=False", result2["stillClaimed"] is False, str(result2["stillClaimed"]))

    # ---------- 3. 重复释放不炸（幂等） ----------
    result3 = collect_box_service.release(
        db, acc_a, detail_id="3484413012", shop_id="18077147")
    db.commit()
    check("重复释放 released=False（本来就没有）", result3["released"] is False,
          str(result3["released"]))
    check("重复释放不抛错", True)

    # ---------- 4. 不存在的商品 → 404 ----------
    try:
        collect_box_service.release(db, acc_a, detail_id="9999999999", shop_id="18077147")
        check("不存在的商品 → 抛错", False, "没抛")
    except errors.AppError as exc:
        check("不存在的商品 → 抛错", True, exc.code)
        check("错误码是 NOT_FOUND", exc.code == errors.NOT_FOUND, exc.code)

    # ---------- 5. 路由分派 ----------
    import inspect

    from api import collect_box as cb_api

    src = inspect.getsource(cb_api.claim_batch)
    check("路由读了 claimed 字段", "payload.claimed is False" in src, "")
    check("释放分支调 release", "collect_box_service.release" in src, "")
    check("释放分支调 claim（不该有）",
          src.count("collect_box_service.claim(") == 1,
          str(src.count("collect_box_service.claim(")))
    check("释放分支不校验 targetShopId",
          src.index("collect_box_service.release") < src.index("请选择认领到哪个店铺"),
          "")
    check("释放分支返回 released 计数", '"released":' in src, "")
    check("释放分支有 commit", src.index("db.commit()") < src.index("if not str(payload.targetShopId"),
          "")

    # 释放路径不能碰上游：源码里 release 函数不含任何 client 调用
    release_src = inspect.getsource(collect_box_service.release)
    for bad in ("account_client", "claim_to_shop", "fill_carriers", "MiaoshouClient",
                "save_shop"):
        check(f"release 不含 {bad}", bad not in release_src, "")

    # ---------- 6. 模型字段兼容 ----------
    check("targetShopId 现在可选（默认空串）",
          "targetShopId: str = \"\"" in inspect.getsource(
              __import__("schemas.requests", fromlist=["x"])),
          "")

    db.close()
    return report()


def report() -> int:
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    out = {"total": len(RESULTS), "pass": passed, "fail": failed,
           "cases": [{"name": n, "ok": ok, "detail": d} for n, ok, d in RESULTS]}
    path = Path(__file__).with_name("probe_release.json")
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    for name, ok, detail in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""))
    print(f"\nTOTAL {passed} PASS {failed} FAIL")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
