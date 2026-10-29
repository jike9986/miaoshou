"""采集箱路由：分页查询、单件详情、认领。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from api.deps import CredentialsHeader, DbSession, OperatorDep, require_permission, require_writable
from core import errors
from core.utils import product_key
from schemas.requests import BatchClaimRequest, ClaimRequest, ProcessRequest
from services import account_service, audit_service, collect_box_service

router = APIRouter(prefix="/collect-box", tags=["采集箱"])


@router.get("", summary="分页查询采集箱（扫描即查询）")
def list_collect_box(
    db: DbSession,
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=20, ge=1, le=100),
    status: str = "notPublished",
    keyword: str = "",
    shopId: str | None = None,
    site: str | None = None,
    sort: str = "default",
    accountId: str | None = None,
    credentials: CredentialsHeader = None,
) -> dict[str, Any]:
    account = account_service.resolve_account(db, accountId, credentials)
    return collect_box_service.list_items(
        db, account, page=page, page_size=pageSize, status=status,
        keyword=keyword or "", shop_id=shopId, site=site, sort=sort)


@router.get("/{detail_id}", summary="采集箱单件详情（含 ossMd5 / SKU / 诊断）")
def collect_box_detail(
    detail_id: str,
    db: DbSession,
    shopId: str = Query(description="明细只在店铺维度唯一"),
    mode: str = "shop",
    accountId: str | None = None,
    credentials: CredentialsHeader = None,
) -> dict[str, Any]:
    account = account_service.resolve_account(db, accountId, credentials)
    if not str(shopId or "").strip():
        raise errors.validation("缺少 shopId 参数", field="shopId")
    return collect_box_service.detail(db, account, detail_id=detail_id,
                                      shop_id=shopId, mode=mode)


@router.post("/{detail_id}/claim", summary="认领进目标店铺采集箱（不发布）",
             dependencies=[require_permission("claim")])
def claim(
    detail_id: str,
    payload: ClaimRequest,
    db: DbSession,
    operator: OperatorDep,
    shopId: str = Query(description="来源店铺"),
    credentials: CredentialsHeader = None,
) -> dict[str, Any]:
    require_writable(db)
    account = account_service.resolve_account(db, payload.accountId, credentials)
    if not str(shopId or "").strip():
        raise errors.validation("缺少 shopId 参数", field="shopId")
    key = product_key(detail_id, shopId)
    with audit_service.guard(db, operator=operator, action="warehouse.claim",
                             target_kind="product", target_id=key,
                             account_id=account.id if account else None,
                             shop_id=shopId):
        result = collect_box_service.claim(
            db, account, detail_id=detail_id, shop_id=shopId,
            target_shop_id=payload.targetShopId,
            mark_collected=payload.markCollected)
    audit_service.record(
        db, operator=operator, action="warehouse.claim", target_kind="product",
        target_id=key, account_id=account.id if account else None, shop_id=shopId,
        detail={"targetShopId": payload.targetShopId or "",
                "claimed": bool(result.get("claimed"))})
    db.commit()
    return result


@router.post("/{detail_id}/process", summary="处理一件商品并入仓（拉详情→定价→入库）",
             dependencies=[require_permission("process")])
def process(
    detail_id: str,
    payload: ProcessRequest,
    db: DbSession,
    operator: OperatorDep,
    credentials: CredentialsHeader = None,
    accountId: str | None = None,
) -> dict[str, Any]:
    """拉详情 → 定价 → 入仓（可选连带图片翻译）。

    这是「上品」最核心的一步：一件商品从采集箱进到仓库，就是这条路径干的。
    `save=false` 是试算（会 rollback），不算写过东西，不记审计；
    `save=true` 落库了才记——否则审计表会被「我点了一百次试算」淹掉。
    """
    require_writable(db)
    account = account_service.resolve_account(db, payload.accountId or accountId, credentials)
    from services import warehouse_service
    from services.account_service import account_client, map_upstream_error
    from services.miaoshou import MiaoshouError

    key = product_key(detail_id, payload.shopId)
    client = account_client(account)
    try:
        with audit_service.guard(db, operator=operator, action="warehouse.import",
                                 target_kind="product", target_id=key,
                                 account_id=account.id if account else None,
                                 shop_id=payload.shopId):
            product = warehouse_service.save_from_collect_box(
                db, account, client, detail_id=detail_id, shop_id=payload.shopId,
                language=payload.language or "", group_id=payload.groupId,
                price_template=payload.template, exchange_rate=payload.exchangeRate,
                enforce_pre_save=bool(payload.save),
                allow_title_review=bool(payload.allowTitleReview),
                allow_image_review=bool(payload.allowImageReview),
                skip_image_review=bool(payload.skipImageReview),
                operator_id=getattr(operator, "id", None),
                automation_result=True)
    except MiaoshouError as exc:
        raise map_upstream_error(exc) from None
    finally:
        client.close()
    image_result: dict[str, Any] | None = None
    if payload.processImages and payload.save:
        # 主图、SKU 图、详情图共用同一图片任务管线，分别按用户设置提交前 N 张。
        # 从刚入库的 payload 取详情/SKU 原图，避免只凭列表封面漏掉另外两类。
        from models import Product
        from services import product_service

        row = db.query(Product).filter(Product.id == int(product["id"])).one()
        saved_payload = row.payload if isinstance(row.payload, dict) else {}
        sources = {
            "main": [str(url) for url in (product.get("images") or [])],
            "sku": [str(item["url"]) for item in warehouse_service.sku_image_sources(saved_payload)],
            "detail": product_service.detail_images(saved_payload),
        }
        limits = {
            "main": payload.mainImageCount,
            "sku": payload.skuImageCount,
            "detail": payload.detailImageCount,
        }
        image_result = {"requested": 0, "submitted": 0, "failed": 0, "jobs": []}
        for source in ("main", "sku", "detail"):
            if not limits[source]:
                continue
            result = warehouse_service.attach_image_translation(
                db, product,
                limit=int(limits[source]),
                source_images=sources[source],
                source=source,
                operator_id=getattr(operator, "id", None),
            )
            for field in ("requested", "submitted", "failed"):
                image_result[field] += int(result.get(field) or 0)
            image_result["jobs"].extend(result.get("jobs") or [])
            if result.get("batchId") and not image_result.get("batchId"):
                image_result["batchId"] = result["batchId"]
            if result.get("reason") and not image_result.get("reason"):
                image_result["reason"] = result["reason"]
    if not payload.save:
        db.rollback()
        return {"saved": False, "product": product}
    db.commit()
    audit_service.record(
        db, operator=operator, action="warehouse.import",
        target_kind="product", target_id=key,
        account_id=account.id if account else None, shop_id=payload.shopId,
        detail={"title": str(product.get("title") or "")[:80],
                "language": str(payload.language or "")[:16],
                "template": str(payload.template or "")[:64],
                "images": (image_result or {}).get("submitted") or 0
                if isinstance(image_result, dict) else 0})
    db.commit()
    return {"saved": True, "product": product, "images": image_result}


class DeleteCollectRequest(BaseModel):
    accountId: str | int | None = None
    detailIds: list[str | int] = Field(default_factory=list)


@router.post("/claim", summary="批量认领（按仓库键）；claimed=false 为撤销认领",
             dependencies=[require_permission("claim")])
def claim_batch(payload: BatchClaimRequest, db: DbSession,
    operator: OperatorDep,
    credentials: CredentialsHeader = None,
) -> dict[str, Any]:
    require_writable(db)
    account = account_service.resolve_account(db, payload.accountId, credentials)
    if not payload.keys:
        raise errors.validation("请选择要认领的商品", field="keys")
    account_id = account.id if account else None
    # claimed=false 走撤销分支：以前这个字段定义了但没人读，传 false 也照样认领，
    # 运营标错了只能去改数据库。两条路径的返回结构保持一致，前端不用分支处理。
    releasing = payload.claimed is False
    if releasing:
        results = []
        failures: list[dict[str, Any]] = []
        for key in payload.keys[:50]:
            parts = str(key).split(":")
            if len(parts) != 2:
                failures.append({"key": key, "reason": "仓库键格式应为 detailId:shopId"})
                # 连键都解析不出，也要留痕：运营看到「失败 3 件」时得知道是哪 3 件
                audit_service.record(
                    db, operator=operator, action="warehouse.release", target_kind="product",
                    target_id=str(key)[:64], account_id=account_id, result="failed",
                    message="仓库键格式应为 detailId:shopId")
                continue
            try:
                results.append(collect_box_service.release(
                    db, account, detail_id=parts[0], shop_id=parts[1],
                    target_shop_id=payload.targetShopId))
            except errors.AppError as exc:
                failures.append({"key": key, "code": exc.code, "reason": exc.message})
                audit_service.record(
                    db, operator=operator, action="warehouse.release", target_kind="product",
                    target_id=str(key)[:64], account_id=account_id,
                    shop_id=parts[1], result="failed",
                    message=f"{exc.code}: {exc.message}")
            else:
                audit_service.record(
                    db, operator=operator, action="warehouse.release", target_kind="product",
                    target_id=str(key)[:64], account_id=account_id,
                    shop_id=parts[1], detail={"targetShopId": payload.targetShopId or ""})
        db.commit()
        return {
            "claimed": 0,
            "released": sum(1 for item in results if item.get("released")),
            "requested": len(payload.keys),
            "targetShopId": payload.targetShopId,
            "results": results,
            "failures": failures,
        }
    if not str(payload.targetShopId or "").strip():
        raise errors.validation("请选择认领到哪个店铺", field="targetShopId")
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for key in payload.keys[:50]:
        parts = str(key).split(":")
        if len(parts) != 2:
            failures.append({"key": key, "reason": "仓库键格式应为 detailId:shopId"})
            audit_service.record(
                db, operator=operator, action="warehouse.claim", target_kind="product",
                target_id=str(key)[:64], account_id=account_id, result="failed",
                message="仓库键格式应为 detailId:shopId")
            continue
        try:
            results.append(collect_box_service.claim(
                db, account, detail_id=parts[0], shop_id=parts[1],
                target_shop_id=payload.targetShopId, mark_collected=True))
        except errors.AppError as exc:
            failures.append({"key": key, "code": exc.code, "reason": exc.message})
            audit_service.record(
                db, operator=operator, action="warehouse.claim", target_kind="product",
                target_id=str(key)[:64], account_id=account_id, shop_id=parts[1],
                result="failed", message=f"{exc.code}: {exc.message}")
        else:
            audit_service.record(
                db, operator=operator, action="warehouse.claim", target_kind="product",
                target_id=str(key)[:64], account_id=account_id, shop_id=parts[1],
                detail={"targetShopId": payload.targetShopId})
    db.commit()
    return {
        "claimed": sum(1 for item in results if item["claimed"]),
        "requested": len(payload.keys),
        "targetShopId": payload.targetShopId,
        "results": results,
        "failures": failures,
    }


@router.post("/delete", summary="从采集箱删除商品（如带水印的废品）",
             dependencies=[require_permission("process")])
def delete_from_collect_box(payload: DeleteCollectRequest, db: DbSession,
    operator: OperatorDep,
    credentials: CredentialsHeader = None,
) -> dict[str, Any]:
    require_writable(db)
    account = account_service.resolve_account(db, payload.accountId, credentials)
    if not payload.detailIds:
        raise errors.validation("请选择要删除的商品", field="detailIds")
    ids = [str(i) for i in payload.detailIds]
    with audit_service.guard(db, operator=operator, action="warehouse.delete",
                             target_kind="product", target_id=ids[0],
                             account_id=account.id if account else None):
        result = collect_box_service.delete(db, account, ids)
    db.commit()
    # 一件一条：采集箱里的删除在上游是**真删**，出了问题（比如顺手删掉了
    # 带水印的废品之外的正常商品）必须能逐件追到人。
    for ident in ids:
        audit_service.record(db, operator=operator, action="warehouse.delete",
                             target_kind="product", target_id=ident,
                             account_id=account.id if account else None,
                             detail={"verb": "collect_box",
                                     "batchSize": len(ids)})
    db.commit()
    return result
