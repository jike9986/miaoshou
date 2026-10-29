"""成品仓库路由。"""
from __future__ import annotations

import json

# --- 价格试算（price-preview）所需的导入与请求模型（必须在路由之前定义） ---
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import Field
from sqlalchemy.orm import Session

from api.deps import CredentialsHeader, DbSession, OperatorDep, require_permission, require_writable
from api.helpers import parse_bool
from core import errors
from core.utils import money, to_decimal
from schemas.common import CamelModel
from schemas.requests import (
    BatchCategoryRequest,
    BatchDeleteRequest,
    CustomCategoryRequest,
    ProductImagesPatch,
    ProductPatch,
)
from services import (
    audit_service,
    operator_service,
    price_template,
    product_service,
    setting_service,
    warehouse_service,
)

PRICE_PREVIEW_MAX_ITEMS = 200


class PricePreviewItem(CamelModel):
    """一件待算价的商品：warehouseId / key 二选一，也接受 detailId + shopId。"""

    warehouseId: str | None = None
    key: str | None = None
    detailId: str | None = None
    shopId: str | None = None


class PricePreviewRequest(CamelModel):
    items: list[PricePreviewItem] = Field(default_factory=list)
    template: str | None = None
    exchangeRate: str | float | None = None


class PreSaveGateRequest(CamelModel):
    """入仓前闸门检查的入参：给 warehouseId，或 detailId + shopId。

    `allowTitleReview` / `allowImageReview` 是**放行开关**，不是复核记录：
    前端点过「我看过了」才传 true，闸门随即把 blocked 降为 false，但命中的词 /
    待复核的图仍原样返回，调用方要自己决定记不记这笔账。
    """

    warehouseId: str | None = None
    detailId: str | None = None
    shopId: str | None = None
    allowTitleReview: bool = False
    allowImageReview: bool = False
    # 默认真跑 OCR（否则这道闸门等于不存在）。批量场景嫌慢可以传 true 只查标题。
    skipImages: bool = False
    retryOcr: bool = False


class ConfirmSaveRequest(CamelModel):
    """两阶段保存的入参：预览拿到的 payloadHash 原样带回来。

    `payloadHash` 是「核对过的那一份」的凭据，不是可选项 —— 缺了就直接拒，
    因为没有它这一步就退化成普通保存，两阶段的意义全没了。
    """

    detailId: str
    shopId: str
    payloadHash: str
    confirm: bool = False
    accountId: str | None = None
    language: str | None = None
    groupId: str | None = None
    template: str | None = None
    exchangeRate: str | float | None = None
    allowTitleReview: bool = False
    allowImageReview: bool = False
    skipImageReview: bool = False




router = APIRouter(
    prefix="/warehouse",
    tags=["成品仓库"],
    # 仓库所有写接口要求老师以上；成员保留 GET 与预览类只读操作。
    dependencies=[require_permission("warehouse")],
)


@router.get("", summary="分页 + 类目 + 分组 + 搜索")
def list_warehouse(
    db: DbSession,
    operator: OperatorDep,
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=24, ge=1, le=100),
    categoryId: str | None = None,
    cid: str | None = None,
    groupId: str | None = None,
    group: str | None = None,
    q: str | None = None,
    accountId: str | None = None,
    status: str | None = None,
    claimed: str | None = None,
    sort: str = "savedDesc",
) -> dict[str, Any]:
    claimed_flag = None if claimed in (None, "") else parse_bool(claimed, "claimed")
    return warehouse_service.list_products(
        db, page=page, page_size=pageSize, category_id=categoryId, cid=cid,
        group_id=groupId, group=group, q=q, account_id=accountId, status=status,
        claimed=claimed_flag, sort=sort, operator_id=operator.id)


@router.get("/automation-results", summary="自动化处理成功记录",
            dependencies=[require_permission("process")])
def list_automation_results(
    db: DbSession,
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=24, ge=1, le=100),
) -> dict[str, Any]:
    return warehouse_service.list_automation_results(db, page=page, page_size=pageSize)


class AutomationResultsImportRequest(CamelModel):
    ids: list[str] = Field(default_factory=list)


@router.post("/automation-results/import", summary="将自动化结果加入成品仓库",
             dependencies=[require_permission("process")])
def import_automation_results(payload: AutomationResultsImportRequest,
                              db: DbSession, operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    with audit_service.guard(db, operator=operator,
                             action="warehouse.automation_results.import",
                             target_kind="product", target_id=""):
        result = warehouse_service.admit_automation_results(db, payload.ids)
        db.commit()
    for ident in result["ids"]:
        audit_service.record(
            db, operator=operator, action="warehouse.automation_results.import",
            target_kind="product", target_id=ident,
            detail={"batchSize": result["added"]})
    db.commit()
    return result


@router.get("/stats", summary="仓库统计与筛选面板")
def warehouse_stats(db: DbSession, operator: OperatorDep,
                    accountId: str | None = None) -> dict[str, Any]:
    """传 accountId 时只数当前账号的商品（多账号共用时分得清哪些是自己的）。"""
    account_id: int | None = None
    if accountId:
        try:
            account_id = int(accountId)
        except (TypeError, ValueError):
            raise errors.validation("accountId 须为数字", field="accountId") from None
    return {"stats": warehouse_service.stats(
                db, account_id=account_id, operator_id=operator.id),
            "facets": warehouse_service.facets(db)}


# --- 自定义分类标签（老版 warehouse.custom_categories）----------------------
# 三条路由都必须在 `GET /warehouse/{ident}` **之前**注册：FastAPI 按注册顺序
# 匹配，静态段写成 /custom-categories 也一样会被 {ident} 吃掉，变成
# 「按商品标识 custom-categories 查详情」→ 404。
@router.get("/custom-categories", summary="自定义分类标签列表")
def list_custom_categories(db: DbSession) -> dict[str, Any]:
    return {"categories": warehouse_service.custom_categories(db)}


@router.post("/custom-categories", status_code=201, summary="新建自定义分类标签",
             dependencies=[require_permission("process")])
def create_custom_category(payload: CustomCategoryRequest, db: DbSession,
                           operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    result = warehouse_service.add_custom_category(db, payload.name)
    db.commit()
    # 自定义分类是共享的：有人加了个新标签，别人下拉框里就会多一个
    audit_service.record(db, operator=operator, action="warehouse.batch_category",
                         target_kind="category", target_id=str(payload.name or "")[:64],
                         detail={"name": str(payload.name or "")[:64],
                                 "verb": "create"})
    db.commit()
    return {"categories": result}


@router.patch("/custom-categories", summary="重命名自定义分类标签",
              dependencies=[require_permission("process")])
def patch_custom_category(payload: CustomCategoryRequest, db: DbSession,
                          operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    result = warehouse_service.rename_custom_category(db, payload.oldName, payload.name)
    db.commit()
    audit_service.record(db, operator=operator, action="warehouse.batch_category",
                         target_kind="category",
                         target_id=str(payload.oldName or "")[:64],
                         detail={"from": str(payload.oldName or "")[:64],
                                 "to": str(payload.name or "")[:64],
                                 "verb": "rename"})
    db.commit()
    return {"categories": result}


# 删除的名字既可能在 body（`{"name": ...}`）也可能在 query（`?name=`）：
# 老版前端两种写法都有，两种都认，省得前端改一版。
@router.delete("/custom-categories", summary="删除自定义分类标签",
               dependencies=[require_permission("process")])
async def delete_custom_category(request: Request, db: DbSession,
                                 name: str | None = None) -> dict[str, Any]:
    require_writable(db)
    target = name or request.query_params.get("name") or ""
    if not str(target).strip():
        raw = await request.body()
        if raw:
            try:
                body = json.loads(raw)
            except ValueError:
                raise errors.validation("请求体不是合法 JSON", field="name") from None
            if isinstance(body, dict):
                target = body.get("name") or ""
    if not str(target).strip():
        raise errors.validation("分类名不能为空", field="name")
    result = warehouse_service.remove_custom_category(db, target)
    db.commit()
    return {"categories": result}


# --- 货源 offer id 汇总（老版 warehouse.all_source_item_ids）---------------
# 载体重建要用这批 id 去 1688 抓数据。同样必须排在 /{ident} 之前。
@router.get("/source-item-ids", summary="仓库商品里出现过的 1688 offer id")
def source_item_ids(db: DbSession, accountId: str | None = None) -> dict[str, Any]:
    ids = warehouse_service.all_source_item_ids(db, account_id=accountId)
    return {"ids": ids, "total": len(ids)}


@router.api_route("/price-preview", methods=["GET", "PUT", "PATCH", "DELETE"],
                  include_in_schema=False)
def price_preview_wrong_method() -> None:
    """占位：price-preview 只支持 POST。

    没有这条时，`GET /warehouse/price-preview` 会掉进 `GET /warehouse/{ident}`，
    被当成商品标识而返回「商品不存在」404，很容易误导调用方（审查 P1-1）。
    """
    raise errors.AppError(errors.VALIDATION_ERROR,
                          "价格试算请用 POST /api/v1/warehouse/price-preview",
                          status=405)


@router.post("/price-preview", summary="按模板试算售价（不发布、不写库、不调上游）")
def price_preview(payload: PricePreviewRequest, db: DbSession) -> dict[str, Any]:
    if not payload.items:
        raise errors.validation("请至少选择一件要试算的商品", field="items")
    if len(payload.items) > PRICE_PREVIEW_MAX_ITEMS:
        raise errors.validation(f"一次最多试算 {PRICE_PREVIEW_MAX_ITEMS} 件商品",
                                field="items", value=len(payload.items))
    template = _preview_template(db, payload.template)
    rate = payload.exchangeRate
    if rate not in (None, ""):
        try:
            rate_value = float(str(rate))
        except (TypeError, ValueError):
            raise errors.validation("汇率必须是数字", field="exchangeRate",
                                    value=rate) from None
        if not 0 < rate_value <= 1000:
            raise errors.validation("汇率必须大于 0 且不超过 1000",
                                    field="exchangeRate", value=rate)
        rate = rate_value

    entries: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for index, item in enumerate(payload.items):
        entry, warning = _preview_one(db, index, item, template, rate)
        if entry is not None:
            entries.append(entry)
        if warning is not None:
            warnings.append(warning)

    return {
        "template": {
            "name": template.get("name"),
            "site": template.get("site"),
            "sellingCurrency": "MYR",
        },
        "exchangeRate": rate,
        "items": entries,
        "total": len(payload.items),
        "repriced": len(entries),
        "skipped": len(payload.items) - len(entries),
        "warnings": warnings,
    }


@router.get("/{ident}", summary="商品详情（id 或 warehouseKey）")
def warehouse_detail(
    ident: str,
    db: DbSession,
    operator: OperatorDep,
    credentials: CredentialsHeader = None,
    includePayload: str | None = None,
) -> dict[str, Any]:
    include = parse_bool(includePayload, "includePayload", False)
    return warehouse_service.detail(db, ident, include_payload=include,
                                    operator_id=operator.id)


@router.post("/{ident}/claim", summary="认领成品商品",
             dependencies=[require_permission("product_claim")])
def claim_product(ident: str, db: DbSession, operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    claim = warehouse_service.claim_product(db, ident, operator.id)
    db.commit()
    return {
        "id": str(claim.id),
        "productId": str(claim.product_id),
        "ownerOperatorId": str(claim.owner_operator_id),
        "status": claim.status,
        "claimedAt": claim.claimed_at,
    }


@router.patch("/{ident}", summary="改类目 / 分组 / 标题",
              dependencies=[require_permission("process")])
def patch_product(ident: str, payload: ProductPatch, db: DbSession,
                  operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    body = payload.model_dump(exclude_unset=True, by_alias=False)
    # 版本号单独拿出来：它是「这次改的是哪一版」的凭据，不是一个被修改的字段。
    # 留在 body 里会污染下面的空体校验（只带版本号也变成合法请求）
    # 和审计里的 fields 列表。
    expected_revision = body.pop("expectedRevision", None)
    if not body:
        raise errors.validation(
            "至少提供一个要修改的字段（categoryId / cid / groupId / title）")
    set_category = "categoryId" in body or "cid" in body
    set_group = "groupId" in body
    with audit_service.guard(db, operator=operator, action="warehouse.patch",
                             target_kind="product", target_id=ident):
        result = warehouse_service.update(
            db, ident,
            category_id=body.get("categoryId"),
            cid=body.get("cid"),
            name=body.get("name"),
            group_id=body.get("groupId"),
            title=body.get("title"),
            set_category=set_category,
            set_group=set_group,
            operator_id=getattr(operator, "id", None),
            expected_revision=expected_revision,
        )
    # 记「改了哪些字段」而不是新旧值：商品 payload 能有几百 KB，
    # 塞进 detail 会把审计表撑成第二份业务数据
    audit_service.record(db, operator=operator, action="warehouse.patch",
                         target_kind="product", target_id=ident,
                         detail={"fields": sorted(body)})
    db.commit()
    return result


@router.delete("/{ident}", summary="软删除商品",
               dependencies=[require_permission("process")])
def delete_product(ident: str, db: DbSession,
                   operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    with audit_service.guard(db, operator=operator, action="warehouse.delete",
                             target_kind="product", target_id=ident):
        result = warehouse_service.delete(db, ident)
    audit_service.record(db, operator=operator, action="warehouse.delete",
                         target_kind="product", target_id=ident)
    db.commit()
    return result


@router.post("/batch-category", summary="批量改类目 / 分组（单事务）",
             dependencies=[require_permission("process")])
def batch_category(payload: BatchCategoryRequest, db: DbSession,
                   operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    body = payload.model_dump(exclude_unset=True, by_alias=False)
    set_category = "categoryId" in body or "cid" in body
    set_group = "groupId" in body
    if not (set_category or set_group):
        raise errors.validation(
            "至少提供一个目标字段（categoryId / cid / groupId）")
    with audit_service.guard(db, operator=operator, action="warehouse.batch_category",
                             target_kind="product",
                             target_id=str(payload.ids[0]) if payload.ids else ""):
        result = warehouse_service.batch_category(
            db, ids=payload.ids, category_id=body.get("categoryId"),
            cid=body.get("cid"), name=body.get("name"),
            group_id=body.get("groupId"), set_category=set_category,
            set_group=set_group,
            operator_id=getattr(operator, "id", None))
    # 批量操作记一件一条：出问题时运营问的是「我这几件是被谁改的」，
    # 一条汇总日志答不了。件数上限由 BatchCategoryRequest 自己管。
    for ident in (payload.ids or []):
        audit_service.record(
            db, operator=operator, action="warehouse.batch_category", target_kind="product",
            target_id=str(ident), detail={"fields": sorted(body),
                                          "batchSize": len(payload.ids or [])})
    db.commit()
    return result


class TransformRequest(CamelModel):
    """批量变换参数（老版 transforms.transform_item 的入参）。

    全部可选：给哪个就做哪个。倍数与目标价互斥，同时给会 422。
    """

    ids: list[str] = Field(default_factory=list)
    title: str | None = None
    removeTitleWords: list[str] = Field(default_factory=list)
    useWordLibrary: bool = False
    priceMultiplier: str | float | None = None
    targetPrice: str | float | None = None
    targetPriceIncludeVat: str | float | None = None
    removeImageIndexes: list[int] = Field(default_factory=list)
    removeImageUrls: list[str] = Field(default_factory=list)
    # 只算不改：默认 True，避免手滑直接改掉一批商品的价格
    dryRun: bool = True


@router.post("/transform", summary="批量变换：改价 / 删图 / 清标题（默认只预览不写库）")
def transform(payload: TransformRequest, db: DbSession,
              operator: OperatorDep) -> dict[str, Any]:
    """对一批商品做倍数改价、目标价、删图、清标题。

    `dryRun=true`（默认）只算不改，返回每件改了什么；确认后传 `dryRun=false` 才落库。
    改价直接覆盖 SKU 的 price / priceIncludeVat，不走定价模板——
    与模板重算是两条路，同时用会互相打架。
    """
    if not payload.dryRun:
        operator_service.require_role(operator, "process")
        require_writable(db)
    if not payload.ids:
        raise errors.validation("请先选择商品", field="ids")
    if (payload.priceMultiplier is not None and payload.targetPrice is not None):
        raise errors.validation("倍数与目标价不能同时使用", field="priceMultiplier")
    wanted = [str(item).strip() for item in payload.ids if str(item).strip()]
    if not wanted:
        raise errors.validation("请先选择商品", field="ids")

    from services import transform_service, word_service

    # 用词库时取 remove/review 两组，不用就只用显式传入的词
    title_words = None
    if payload.useWordLibrary:
        library = word_service.library(db)
        title_words = {"remove": list(library.get("remove") or []),
                       "review": list(library.get("review") or [])}

    items: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    changed_total = 0
    for index, ident in enumerate(wanted):
        try:
            product = warehouse_service.resolve_product(db, ident)
        except errors.AppError as exc:
            warnings.append({"index": index, "id": ident, "code": exc.code,
                             "message": exc.message})
            continue
        current = product.payload if isinstance(product.payload, dict) else {}
        if not current:
            warnings.append({"index": index, "id": ident, "code": "EMPTY_PAYLOAD",
                             "message": "这件商品没有可用的详情数据，已跳过"})
            continue
        try:
            result, report = transform_service.transform_item(
                current,
                title=payload.title,
                remove_title_words=payload.removeTitleWords,
                title_words=title_words,
                price_multiplier=payload.priceMultiplier,
                target_price=payload.targetPrice,
                target_price_include_vat=payload.targetPriceIncludeVat,
                remove_image_indexes=payload.removeImageIndexes,
                remove_image_urls=payload.removeImageUrls,
            )
        except transform_service.TransformError as exc:
            warnings.append({"index": index, "id": ident, "code": "TRANSFORM_INVALID",
                             "message": str(exc)})
            continue

        changed_total += int(bool(report.get("changed")))
        if report.get("changed") and not payload.dryRun:
            # 走 upsert 才能保留认领状态与分组；payload 换掉，展示字段同步刷新
            warehouse_service.apply_transformed_payload(db, product, result, report)
        items.append({
            "index": index,
            "id": product.id,
            "warehouseId": product.warehouse_key,
            "warehouseKey": product.warehouse_key,
            "title": result.get("title") or "",
            "report": report,
            "applied": bool(report.get("changed")) and not payload.dryRun,
        })

    if not payload.dryRun:
        db.commit()
        # 批量变换是「一次改一批价格 / 删一批图」：dryRun=false 才落库，
        # 也只有这一支需要留痕（预览不改任何东西，记了只会淹没有效记录）。
        # 一件一条：运营问的是「我这几件是被谁改的」，汇总日志答不了。
        applied = [item for item in items if item["applied"]]
        audit_service.record(
            db, operator=operator, action="warehouse.transform",
            target_kind="product",
            target_id=str(applied[0]["warehouseKey"]) if applied else "",
            detail={"changedCount": changed_total,
                    "requested": len(wanted),
                    "fields": sorted(
                        name for name in ("title", "priceMultiplier", "targetPrice",
                                          "targetPriceIncludeVat",
                                          "removeImageIndexes", "removeImageUrls")
                        if getattr(payload, name) not in (None, "", []))})
        for item in applied:
            audit_service.record(
                db, operator=operator, action="warehouse.transform",
                target_kind="product", target_id=str(item["warehouseKey"]),
                detail={"changed": (item["report"] or {}).get("changed") or [],
                        "batchSize": len(applied)})
        db.commit()
    return {
        "dryRun": bool(payload.dryRun),
        "items": items,
        "total": len(items),
        "changedCount": changed_total,
        "warnings": warnings,
    }


@router.post("/batch-delete", summary="批量软删除",
             dependencies=[require_permission("process")])
def batch_delete(payload: BatchDeleteRequest, db: DbSession,
                 operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    with audit_service.guard(db, operator=operator, action="warehouse.delete",
                             target_kind="product",
                             target_id=str(payload.ids[0]) if payload.ids else ""):
        result = warehouse_service.batch_delete(db, payload.ids)
    db.commit()
    # 批量删除一件一条：出问题时运营问的是「我这几件是被谁删的」
    for ident in (payload.ids or []):
        audit_service.record(db, operator=operator, action="warehouse.delete",
                             target_kind="product", target_id=str(ident),
                             detail={"batchSize": len(payload.ids or [])})
    db.commit()
    return result


@router.post("/pre-save-gate", summary="入仓前闸门检查（只读：不写库、不改妙手）")
def pre_save_gate(payload: PreSaveGateRequest, db: DbSession,
                  credentials: CredentialsHeader = None) -> dict[str, Any]:
    """问一次「这件现在能不能入仓」，回答标题 review 词和主图 OCR 两道闸。

    存在的理由：老版这两项是保存前的硬阻断，新版只在回写妙手那条路有闸，
    入仓这条完全没有——含需复核词的标题、带中文或厂家宣传语的主图能直接进库。
    前端在点「入仓」之前先打这个接口，就能在写库前把话说清楚。

    **绝不写数据**：不 commit、不 flush、不改 payload。上游只在仓库里查不到
    这件商品时才会被调一次（拿 info 给闸门判），也不落任何缓存文件——
    OCR 结果缓存由 image_review_service 自己按 URL 管，不归这里管。
    """
    from services import gate_service

    info, identity = gate_service.load_info(
        db, warehouse_id=payload.warehouseId,
        detail_id=payload.detailId, shop_id=payload.shopId)
    result = gate_service.pre_save_gate(
        db, info,
        allow_title_review=payload.allowTitleReview is True,
        allow_image_review=payload.allowImageReview is True,
        skip_images=payload.skipImages is True,
        retry_ocr=payload.retryOcr is True,
    )
    return {**result, "identity": identity}


@router.post("/confirm-save", summary="两阶段保存：核对过 payloadHash 后再真正入仓",
             dependencies=[require_permission("process")])
def confirm_save(payload: ConfirmSaveRequest, db: DbSession,
                 operator: OperatorDep,
                 credentials: CredentialsHeader = None) -> dict[str, Any]:
    """老版 `POST /api/confirm-save` 的等价物。

    为什么要有这一步：入仓是不可逆的写动作，而「预览看到的」和「真存进去的」
    之间可能隔着几十秒（图片翻译、OCR、上游被别人改动）。老版靠 payloadHash
    保证两者是同一份，新版没有这道闸 —— 运营核对完点保存，中间图翻完了、
    主图换了，存进去的就是他没看过的那份。

    三道闸按顺序：
      1. `confirm` 必须为 true（防误点）
      2. `payloadHash` 必须与当前重算的一致（防「核对后被改」）
      3. 重跑 pre_save_gate（防「核对时能过、现在不能过」）
    """
    require_writable(db)
    # 审计的 target 用 warehouse_key（detailId:shopId）：它不依赖这次请求是否成功，
    # 失败记录里也得有「是哪件商品没存进去」，否则运营只会看到一条没头没尾的 failed。
    from core.utils import product_key

    audit_target = product_key(payload.detailId, payload.shopId)
    if payload.confirm is not True:
        raise errors.validation("请先核对完整商品数据并确认保存", field="confirm")
    expected = str(payload.payloadHash or "").strip()
    if not expected:
        raise errors.validation("缺少 payloadHash：请先预览并核对", field="payloadHash")

    from services import account_service, warehouse_service

    account = account_service.resolve_account(db, payload.accountId, credentials)
    account_id = account.id if account else None
    with audit_service.guard(db, operator=operator, action="warehouse.import",
                             target_kind="product", target_id=audit_target,
                             account_id=account_id, shop_id=payload.shopId):
        client = account_service.account_client(account)
        try:
            # 先跑一遍预览（save=false）：它不写库，但会算出当前 payload 的 hash。
            preview = warehouse_service.save_from_collect_box(
                db, account, client,
                detail_id=payload.detailId, shop_id=payload.shopId,
                language=payload.language or "",
                group_id=payload.groupId,
                price_template=payload.template,
                exchange_rate=payload.exchangeRate,
                enforce_pre_save=False,
                allow_title_review=payload.allowTitleReview is True,
                allow_image_review=payload.allowImageReview is True,
                skip_image_review=payload.skipImageReview is True,
            )
            actual = str((preview.get("payloadReview") or {}).get("payloadHash") or "")
            if actual != expected:
                # 明确指出「哪一步变了」，否则运营只会看到「请重新核对」而不知道为什么
                raise errors.AppError(
                    "PAYLOAD_CHANGED",
                    "待保存的商品数据已变化，请重新核对完整内容后再保存",
                    status=409,
                    details={"expected": expected[:16], "actual": actual[:16],
                             "reason": "核对之后商品内容被改动"
                                       "（图片翻译完成、上游被修改或模板变了）"},
                )
        finally:
            client.close()

        # hash 对上了才真正写库。这里会重跑全部闸门与规则，任何一项不过就拒。
        client = account_service.account_client(account)
        try:
            product = warehouse_service.save_from_collect_box(
                db, account, client,
                detail_id=payload.detailId, shop_id=payload.shopId,
                language=payload.language or "",
                group_id=payload.groupId,
                price_template=payload.template,
                exchange_rate=payload.exchangeRate,
                enforce_pre_save=True,
                allow_title_review=payload.allowTitleReview is True,
                allow_image_review=payload.allowImageReview is True,
                skip_image_review=payload.skipImageReview is True,
                operator_id=getattr(operator, "id", None),
            )
        finally:
            client.close()
    audit_service.record(db, operator=operator, action="warehouse.import",
                         target_kind="product", target_id=audit_target,
                         account_id=account_id, shop_id=payload.shopId,
                         detail={"payloadHash": expected[:16],
                                 "template": payload.template or ""})
    db.commit()
    return {"saved": True, "payloadHash": expected, "product": product}


# --- 价格预览（老版 POST /api/price-preview）--------------------------------
# 发布不可撤销，操作者需要「不发布先算价」。这里只做纯计算：
#   · 不写库（不 flush、不 commit，商品 payload 只读且 apply_template 内部 deepcopy）
#   · 不调上游、不看发布开关、不做只读校验（只读模式下也能试算）
# 定价算法 100% 复用 services.price_template.apply_template()——与
# publish_service._repricing() 走的是同一个函数，试算结果与真实发布一致。
def _preview_template(db: Session, name: Any) -> dict[str, Any]:
    """取模板并校验；错误码与 publish_service._find_template 保持一致。"""
    wanted = str(name or "").strip()
    if not wanted:
        raise errors.AppError(errors.TEMPLATE_INVALID, "请先选择定价模板",
                              status=422, details={"field": "template"})
    raw = setting_service.price_templates(db).get(wanted)
    if not raw:
        raise errors.AppError(errors.TEMPLATE_INVALID, f"定价模板「{wanted}」不存在",
                              status=422, details={"field": "template"})
    try:
        return price_template.normalize_template(raw)
    except price_template.PriceTemplateError as exc:
        raise errors.AppError(errors.TEMPLATE_INVALID, f"定价模板不可用：{exc}",
                              status=422, details={"field": "template"}) from None


def _preview_ident(item: PricePreviewItem) -> str:
    for candidate in (item.warehouseId, item.key):
        text = str(candidate or "").strip()
        if text:
            return text
    detail_id = str(item.detailId or "").strip()
    shop_id = str(item.shopId or "").strip()
    if detail_id and shop_id:
        return f"{detail_id}:{shop_id}"
    return ""


def _sku_range(info: Mapping[str, Any], field: str) -> tuple[float | None, float | None]:
    """有效 SKU 某个价格字段的最小 / 最大值（复用 product_service.active_skus）。"""
    values: list[float] = []
    for sku in product_service.active_skus(info).values():
        number = to_decimal(sku.get(field))
        if number is not None and number > 0:
            values.append(float(number))
    if not values:
        return None, None
    return min(values), max(values)


def _preview_warning(index: int, item: PricePreviewItem, code: str, message: str,
                     product: Any = None) -> dict[str, Any]:
    return {
        "index": index,
        "warehouseId": (str(product.id) if product is not None
                        else (item.warehouseId or item.key)),
        "detailId": product.detail_id if product is not None else item.detailId,
        "code": code,
        "message": message,
    }


def _preview_one(db: Session, index: int, item: PricePreviewItem,
                 template: Mapping[str, Any], rate: Any
                 ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """算一件；成功返回 (条目, None)，失败返回 (None, warning)。"""
    ident = _preview_ident(item)
    if not ident:
        return None, _preview_warning(
            index, item, "INVALID_ITEM",
            "必须给 warehouseId / key，或 detailId + shopId 配对")
    try:
        product = warehouse_service.resolve_product(db, ident)
    except errors.AppError as exc:
        if exc.code != errors.NOT_FOUND:
            raise
        return None, _preview_warning(index, item, "NOT_FOUND", exc.message)
    payload = product.payload if isinstance(product.payload, dict) else {}
    if not payload:
        return None, _preview_warning(
            index, item, "NO_PAYLOAD",
            f"商品 {ident} 没有本地详情（payload 为空），无法试算", product)
    try:
        priced, report = price_template.apply_template(payload, template, rate=rate)
    except price_template.PriceTemplateError as exc:
        return None, _preview_warning(
            index, item, "PRICE_UNAVAILABLE",
            f"商品 {ident} 无法按模板算价：{exc}", product)

    samples = report.get("samples") or []
    sample = samples[0] if samples else {}
    calculation = sample.get("calculation") or {}
    currency = str(calculation.get("currency") or "MYR")
    purchase_min, purchase_max = product_service.purchase_range(payload)
    before_min, before_max = _sku_range(payload, "price")
    after_min, after_max = _sku_range(priced, "price")
    return {
        "warehouseId": str(product.id),
        "warehouseKey": product.warehouse_key,
        "title": product.title or "",
        "detailId": product.detail_id,
        "shopId": product.shop_id,
        "purchasePrice": {
            "min": money(purchase_min),
            "max": money(purchase_max),
            "currency": "CNY",
        },
        "priceBefore": {
            "min": money(before_min),
            "max": money(before_max),
            "currency": currency,
        },
        "priceAfter": {
            "min": money(after_min),
            "max": money(after_max),
            "currency": currency,
        },
        "currency": currency,
        "templateName": template.get("name"),
        "templateApplied": True,
        "rate": report.get("rate"),
        "rateSource": report.get("rateSource") or (
            "手动填写" if rate not in (None, "") else "商品自身采购价与售价之比"),
        "weightKg": sample.get("weightKg"),
        "skuCount": report.get("skuCount"),
        "changedCount": report.get("changedCount"),
        "calculation": calculation,
    }, None


# --- 图片改写（老版 POST /api/save-images）----------------------------------
# 老版在「人工核对 → 改图 → 确认保存」这一段里，把挑好的主图/详情图写回妙手
# 草稿。新版此前只改标题和类目，图片没有任何入口——译文图翻出来了也换不上，
# 「改图」这一步整条链断了。
#
# 这里刻意只改本地 DB：
#   · 上游草稿的改动是发布环节的事（services/publish_service.py），
#     把「换一张图」变成一次不可撤销的上游提交，误点就没法回退；
#   · 上游保存还要过 confirm + reviewConfirmed 两道人工闸，
#     混进图片接口只会让闸门形同虚设。
#
# 校验上限取 20（不是 MAIN_IMAGE_LIMIT=9）：这里改的是「操作者挑出来的图」，
# 挑多了一张两张要能存进来让人自己删，而不是在接口层静默截断——
# 截断会让人以为图换上了，实际少了几张。

IMAGE_PATCH_MAX_MAIN = 20
NOTES_MAX_LENGTH = 100000


def _patch_image_urls(values: list[str]) -> list[str]:
    """校验并去重主图地址：必须 http(s)，最多 20 张。"""
    if len(values) > IMAGE_PATCH_MAX_MAIN:
        raise errors.validation(
            f"主图最多 {IMAGE_PATCH_MAX_MAIN} 张，收到 {len(values)} 张",
            field="imgUrls", value=len(values))
    cleaned: list[str] = []
    for index, value in enumerate(values):
        url = value.strip() if isinstance(value, str) else ""
        if not url:
            raise errors.validation(f"第 {index + 1} 张主图地址为空", field="imgUrls",
                                    value=value)
        if not url.lower().startswith(("http://", "https://")):
            raise errors.validation(
                f"第 {index + 1} 张主图不是 http(s) 地址：{url[:120]}",
                field="imgUrls", value=url[:120])
        if url not in cleaned:
            cleaned.append(url)
    return cleaned


def _patch_sku_props(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """结构校验：必须是对象数组，且每项带 attrName。

    不做值级清洗（颜色规范化是入仓时的事，这里改的是已入库商品的属性，
    操作者看到什么就存什么，再洗一遍会让界面和库里对不上）。
    """
    for index, prop in enumerate(values):
        if not isinstance(prop, dict):
            raise errors.validation(f"第 {index + 1} 个 SKU 属性不是对象",
                                    field="skuPropertyList")
        if not str(prop.get("attrName") or "").strip():
            raise errors.validation(f"第 {index + 1} 个 SKU 属性缺少 attrName",
                                    field="skuPropertyList")
        entries = prop.get("attrValueList")
        if entries is not None and not isinstance(entries, list):
            raise errors.validation(
                f"第 {index + 1} 个 SKU 属性的 attrValueList 必须是数组",
                field="skuPropertyList")
    return values


@router.patch("/{ident}/images", summary="改主图 / 详情图 / SKU 属性（只改本地，不推上游）",
              dependencies=[require_permission("process")])
def patch_product_images(ident: str, payload: ProductImagesPatch,
                         db: DbSession, operator: OperatorDep) -> dict[str, Any]:
    """把挑好的主图、详情 HTML、SKU 属性写回仓库行的 payload。

    `confirm: true` 是硬要求：换图之后发布出去就是另一件商品的样子，
    误操作无法撤销（上游已提交的草稿只能人工回滚）。

    派生字段（images / detail_images / sku_properties / cover）必须同步重算：
    列表页、详情页、发布流程读的都是这几个字段，只改 payload 会出现
    「详情里是新图、列表里还是旧图」这种自相矛盾的状态。
    这段同步逻辑与 services/warehouse_service.py 里 save_from_collect_box
    末尾（译文图写回后）的做法保持一致，不是重新发明一套。
    """
    require_writable(db)
    if not payload.confirm:
        raise errors.validation("改图片需要 confirm: true", field="confirm")

    from copy import deepcopy

    body = payload.model_dump(exclude_unset=True, by_alias=False)
    # confirm 不是数据字段，不能写进 payload
    body.pop("confirm", None)
    # 版本号也不是数据字段：它是「我改的是哪一版」的凭据
    expected_revision = body.pop("expectedRevision", None)
    if not body:
        raise errors.validation("至少提供一个要修改的字段（imgUrls / notes / skuPropertyList）")

    product = warehouse_service.resolve_product(db, ident)
    # 校验放在构造 updated 之前：这条路径是「整份 payload 替换」，
    # 不校验的话 A 换的图会被 B 手上那份旧 payload 完整盖回去，且看不出痕迹。
    warehouse_service.revision_guard(db, product, expected_revision, action="改图")
    current = product.payload if isinstance(product.payload, dict) else {}
    # 在深拷贝上改再整份赋值：JSON 列不感知就地 mutation，
    # 直接改 product.payload 里的字典不会产生 UPDATE 语句。
    updated = deepcopy(current)
    changed: list[str] = []

    if "imgUrls" in body and body["imgUrls"] is not None:
        urls = _patch_image_urls(list(body["imgUrls"]))
        if updated.get("imgUrls") != urls:
            updated["imgUrls"] = urls
            changed.append("imgUrls")

    if "notes" in body and body["notes"] is not None:
        notes = body["notes"]
        if not isinstance(notes, str):
            raise errors.validation("notes 必须是字符串（详情 HTML）", field="notes")
        if len(notes) > NOTES_MAX_LENGTH:
            raise errors.validation(
                f"详情 HTML 最多 {NOTES_MAX_LENGTH} 字符，收到 {len(notes)}",
                field="notes", value=len(notes))
        if updated.get("notes") != notes:
            updated["notes"] = notes
            changed.append("notes")

    if "skuPropertyList" in body and body["skuPropertyList"] is not None:
        props = _patch_sku_props(list(body["skuPropertyList"]))
        if updated.get("skuPropertyList") != props:
            updated["skuPropertyList"] = props
            changed.append("skuPropertyList")

    if not changed:
        # 幂等：传了但内容一样，不算改动，也不写库（避免无意义的 updated_at 抖动）
        return {"product": warehouse_service.serialize(
            product,
            operator_name=warehouse_service.operator_names(db).get(
                product.operator_id or 0, "")), "changed": []}

    product.payload = updated
    # 展示字段跟着走（与 save_from_collect_box 的写回块同一套推导）
    images = product_service.main_images(updated)
    product.images = images
    product.detail_images = product_service.detail_images(updated)
    product.sku_properties = product_service.sku_properties(updated)
    if images:
        product.cover = images[0]
    elif "imgUrls" in changed:
        # 主图被清空：cover 必须一起清，否则列表页还挂着一张已删掉的图
        product.cover = ""
    # 「谁最后改的」：列表页要显示「14:32 由张三修改」
    product.operator_id = getattr(operator, "id", None)
    # 版本号已由上面的 revision_guard 推到下一版（条件 UPDATE），这里不能再推一次
    db.flush()

    from core.utils import now_ts

    product.updated_at = now_ts()
    audit_service.record(db, operator=operator, action="warehouse.patch",
                         target_kind="product", target_id=ident,
                         shop_id=product.shop_id or "",
                         detail={"changed": changed})
    db.commit()
    return {"product": warehouse_service.serialize(
        product,
        operator_name=warehouse_service.operator_names(db).get(
            product.operator_id or 0, "")), "changed": changed}


