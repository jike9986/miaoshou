"""采集箱路由：分页查询、单件详情、认领。"""
from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from api.deps import CredentialsHeader, DbSession, OperatorDep, require_permission, require_writable
from core import errors
from core.utils import now_ts, product_key
from schemas.requests import BatchClaimRequest, ClaimRequest, ProcessRequest
from services import account_service, audit_service, collect_box_service

logger = logging.getLogger("aohuang.api")

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
    refresh: bool = False,
    accountId: str | None = None,
    credentials: CredentialsHeader = None,
) -> dict[str, Any]:
    account = account_service.resolve_account(db, accountId, credentials)
    return collect_box_service.list_items(
        db, account, page=page, page_size=pageSize, status=status,
        keyword=keyword or "", shop_id=shopId, site=site, sort=sort,
        force_refresh=refresh)


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
             dependencies=[require_permission("automation")])
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
    bg_image_job: dict[str, Any] | None = None
    if payload.processImages and payload.save:
        # 图片翻译整体放后台：文字/价格/入仓几秒就完成，用户应当立刻看到结果；
        # 图片翻译每张 AI 20-30 秒、十几张要几分钟，绝不能卡在同步请求里让界面
        # 一直「处理中 0%」。这里只记下商品 id 与各环节上限，交给后台线程跑
        # 「本地 OCR 筛选 → 去重 → 提交翻译 → 写回译文」。前端随后轮询
        # 自动化运行的 processResult.images 包含选图、翻译、草稿回写的完整状态。
        from models import Product

        row = db.query(Product).filter(Product.id == int(product["id"])).one()
        row.automation_images = {
            "queued": True, "completed": False, "requested": 0, "submitted": 0,
            "failed": 0, "jobs": [], "generation": secrets.token_hex(16),
            "revision": row.revision, "startedAt": now_ts(),
        }
        row.draft_verified_revision = None
        product["draftVerified"] = False
        bg_image_job = {
            "productId": int(row.id),
            "detailId": str(row.detail_id or ""),
            "operatorId": getattr(operator, "id", None),
            "generation": row.automation_images["generation"],
            "limits": {
                "main": int(payload.mainImageCount or 0),
                "sku": int(payload.skuImageCount or 0),
                "detail": int(payload.detailImageCount or 0),
            },
        }
        image_result = {"queued": True, "completed": False, "requested": 0,
                        "submitted": 0, "failed": 0, "jobs": []}

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
    # 文字+价格+入仓已落库，先把结果返回给前端（几秒），图片翻译后台继续。
    if bg_image_job is not None:
        _spawn_image_translation(bg_image_job)
    return {"saved": True, "product": product, "images": image_result}


def _spawn_image_translation(job: dict[str, Any]) -> None:
    import threading

    threading.Thread(target=_run_image_translation, args=(job,),
                     name=f"imgtr-{job.get('productId')}", daemon=True).start()


def _run_image_translation(job: dict[str, Any]) -> None:
    """Finish all image work, save the final draft upstream, and bind its readback proof."""
    import time
    from copy import deepcopy

    from core.locks import hold_for_session
    from db.session import SessionLocal
    from models import Account, ImageJob, Operator, Product
    from services import (
        automation_run_service,
        image_service,
        product_service,
        readback_check,
        warehouse_service,
    )

    db = SessionLocal()
    product_id = int(job["productId"])
    generation = job["generation"]
    try:
        row = db.get(Product, product_id)
        if (row is None or (row.automation_images or {}).get("generation") != generation
                or row.automation_images.get("queued") is not True):
            return
        base_revision = row.automation_images["revision"]

        def current_generation() -> bool:
            hold_for_session(db, [f"automation-image-result:{product_id}"])
            db.refresh(row)
            current = row.automation_images or {}
            if current.get("generation") != generation or current.get("queued") is not True:
                return False
            if row.revision != base_revision or row.operator_id != job.get("operatorId"):
                raise errors.AppError(
                    "CONFLICT", "图片处理期间商品版本发生变化，请重新核对", status=409)
            owner = (db.query(Operator).populate_existing()
                     .filter_by(id=row.operator_id).one_or_none())
            if (owner is None or not owner.enabled
                    or not account_service.has_account_access(db, row.account_id, owner.id)):
                raise errors.AppError(
                    "CONFLICT", "账号绑定或操作权限已变化，未继续提交图片", status=409)
            return True

        owner = db.get(Operator, row.operator_id)
        if (row.revision != base_revision or row.operator_id != job.get("operatorId")
                or owner is None or not owner.enabled
                or not account_service.has_account_access(db, row.account_id, owner.id)):
            raise errors.AppError(
                "CONFLICT", "商品版本或账号绑定已变化，未继续提交图片", status=409)
        saved_payload = deepcopy(row.payload) if isinstance(row.payload, dict) else {}
        limits = job.get("limits") or {}
        picked = warehouse_service.select_translatable_images(
            saved_payload, main_limit=int(limits.get("main") or 0),
            detail_limit=int(limits.get("detail") or 0),
            sku_limit=int(limits.get("sku") or 0))
        product_map = {"id": row.id, "detailId": row.detail_id}
        state = deepcopy(row.automation_images)
        job_ids: list[str] = []
        if not current_generation():
            return
        for source in ("main", "sku", "detail"):
            urls = (picked.get(source) or [])[:int(limits.get(source) or 0)]
            if not urls:
                continue
            if not current_generation():
                return
            result = warehouse_service.attach_image_translation(
                db, product_map, limit=len(urls), source_images=urls,
                source=source, operator_id=job.get("operatorId"))
            if not current_generation():
                return
            state["requested"] += int(result.get("requested") or 0)
            state["submitted"] += int(result.get("submitted") or 0)
            state["failed"] += int(result.get("failed") or 0)
            state["jobs"].extend(result.get("jobs") or [])
            job_ids.extend(str(item["id"]) for item in result.get("jobs") or []
                           if item.get("id"))
            row.automation_images = dict(state)
            db.commit()
            if state["failed"]:
                raise errors.AppError(
                    "IMAGE_FAILED", "部分图片未能提交翻译，请核对后重试", status=409)
        job_ids = list(dict.fromkeys(job_ids))
        if state["requested"] and not job_ids:
            raise errors.AppError("IMAGE_FAILED", "图片翻译没有返回可核对的任务", status=409)
        deadline = time.monotonic() + 480
        while job_ids:
            jobs = db.query(ImageJob).filter(ImageJob.id.in_(job_ids)).populate_existing().all()
            if (len(jobs) != len(job_ids)
                    or any(item.product_id != product_id
                           or item.operator_id != job.get("operatorId") for item in jobs)):
                raise errors.AppError(
                    "IMAGE_FAILED", "图片任务归属发生变化，已阻止发布", status=409)
            if any(item.status in {"failed", "unknown"} for item in jobs):
                raise errors.AppError(
                    "IMAGE_FAILED", "图片翻译失败或结果未知，请先核对", status=409)
            state["jobs"] = [image_service.serialize(item) for item in jobs]
            pending = [item for item in jobs if item.status != "ready"]
            if not pending:
                break
            if time.monotonic() >= deadline:
                raise errors.AppError(
                    "IMAGE_TIMEOUT", "图片处理超时，结果待核对，未自动重新提交", status=409)
            for item in pending:
                image_service.refresh(db, item.id)
            if not current_generation():
                return
            row.automation_images = dict(state)
            db.commit()
            time.sleep(4)

        # The same lock is taken by finish_process so neither ordering loses the proof.
        hold_for_session(db, [f"automation-image-result:{product_id}"])
        db.refresh(row)
        if ((row.automation_images or {}).get("generation") != generation
                or row.automation_images.get("queued") is not True):
            return
        if row.revision != base_revision or row.operator_id != job.get("operatorId"):
            raise errors.AppError(
                "CONFLICT", "图片处理期间商品版本发生变化，请重新核对", status=409)
        owner = db.get(Operator, row.operator_id)
        account = db.get(Account, row.account_id)
        if (owner is None or not owner.enabled or account is None
                or not account_service.has_account_access(db, account.id, owner.id)):
            raise errors.AppError(
                "CONFLICT", "账号绑定或操作权限已变化，未继续保存草稿", status=409)
        payload = deepcopy(saved_payload)
        applied = warehouse_service.apply_image_results(db, row, payload, job_ids=job_ids)
        if applied.get("error") or applied.get("missing"):
            raise errors.AppError(
                "IMAGE_UPLOAD_FAILED", "译文图片尚未完整上传，未继续保存或发布", status=409)
        client = account_service.account_client(account)
        try:
            fresh = product_service.detail_block(client.shop_detail(row.detail_id, row.shop_id))
            original = fresh["info"]
            if not original or readback_check.readback_issues(
                    saved_payload, original, saved_payload, {}, pricing_enabled=True):
                raise errors.AppError(
                    "CONFLICT", "图片处理期间妙手草稿已变化，请刷新核对", status=409)
            if applied.get("replaced"):
                warehouse_service._save_processed_draft(
                    client, detail_id=row.detail_id, shop_id=row.shop_id,
                    oss_md5=fresh["ossMd5"], payload=payload, original=original,
                    coverage=(payload.get("translation") or {}).get("coverage") or {},
                    pricing_enabled=True)
                row.payload = payload
                row.images = product_service.main_images(payload)
                row.detail_images = product_service.detail_images(payload)
                row.sku_properties = product_service.sku_properties(payload)
                if row.images:
                    row.cover = row.images[0]
                row.revision += 1
        finally:
            client.close()
        row.draft_verified_revision = row.revision
        row.automation_images = {**state, "queued": False, "completed": True, "failed": 0}
        automation_run_service.complete_image_processing(db, row)
        db.commit()
    except Exception as exc:  # noqa: BLE001 - never publish an unverified background result
        db.rollback()
        hold_for_session(db, [f"automation-image-result:{product_id}"])
        row = db.get(Product, product_id)
        if row is not None:
            db.refresh(row)
            if (row.automation_images or {}).get("generation") == generation:
                state = dict(row.automation_images)
                state.update({"queued": False, "completed": True,
                              "failed": max(1, int(state.get("failed") or 0)),
                              "reason": (exc.message if isinstance(exc, errors.AppError)
                                         else "图片处理或草稿保存失败，请刷新核对")})
                row.automation_images = state
                row.draft_verified_revision = None
                automation_run_service.complete_image_processing(db, row)
                db.commit()
        logger.warning("后台图片处理未通过 product=%s type=%s", product_id, type(exc).__name__)
    finally:
        db.close()


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
