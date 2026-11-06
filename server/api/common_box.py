"""公共采集箱路由：把仓库成品推送到妙手公共采集箱。

与 /claim-jobs（认领到店铺采集箱）的区别：
  认领到采集箱 = 从公共箱复制载体再覆盖，目标某个店铺；
  推送到公共箱 = add_common_collect_box_detail，把成品放进公共货源池。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from api.deps import DbSession, OperatorDep, require_permission, require_writable
from core import errors
from schemas.requests import CommonBoxPushRequest
from services import account_service, common_box_service, warehouse_service

router = APIRouter(prefix="/common-box", tags=["公共采集箱"])


@router.post("/push", summary="推送仓库成品到公共采集箱",
             dependencies=[require_permission("product_claim")])
def push_to_common_box(payload: CommonBoxPushRequest, db: DbSession,
                       operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    if not payload.accountId:
        raise errors.validation("请选择妙手账号", field="accountId")
    account = account_service.require_account(db, payload.accountId)
    if not payload.items:
        raise errors.validation("请选择要推送的商品", field="items")

    client = account_service.account_client(account)

    results: list[dict[str, Any]] = []
    ok = fail = 0
    for item in payload.items:
        ident = str(item.warehouseId or "")
        if not ident:
            fail += 1
            results.append({"productId": "", "status": "fail",
                            "reason": "缺少商品 id"})
            continue
        try:
            product = warehouse_service.resolve_product(db, ident)
        except Exception as exc:  # noqa: BLE001
            fail += 1
            results.append({"productId": ident, "status": "fail",
                            "reason": str(exc)[:100]})
            continue
        if product is None:
            fail += 1
            results.append({"productId": ident, "status": "fail",
                            "reason": "仓库查无此商品"})
            continue
        push_payload = dict(product.payload or {})
        push_payload.setdefault("title", (product.payload or {}).get("title") or "")
        try:
            report = common_box_service.push_products_to_common_box(
                client, [push_payload])
        except Exception as exc:  # noqa: BLE001
            fail += 1
            results.append({"productId": ident, "status": "fail",
                            "reason": str(exc)[:120]})
            continue
        entry = (report.get("results") or [{}])[0]
        if entry.get("status") == "ok":
            ok += 1
        else:
            fail += 1
        results.append({
            "productId": ident,
            "title": entry.get("title") or (product.payload or {}).get("title", "")[:40],
            "detailId": entry.get("detailId", ""),
            "status": entry.get("status"),
            "reason": entry.get("reason", ""),
        })

    return {"ok": ok, "fail": fail, "results": results}
