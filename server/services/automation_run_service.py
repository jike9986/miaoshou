"""Persistent run records for the collect-box automation screen."""
from __future__ import annotations

import secrets
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy.orm import Session

from core import errors
from core.utils import now_ts
from models.account import Account
from models.automation_run import AutomationRun, AutomationRunItem
from models.image_job import ImageJob
from models.operator import Operator
from models.product import Product
from models.publish_log import PublishLog
from services import account_service, operator_service, warehouse_service

PROCESS_STATUSES = {"queued", "running", "succeeded", "failed", "needs_review"}
PUBLISH_STATUSES = {"not_requested", "queued", "running", "succeeded", "failed", "needs_review"}


def _teacher_or_admin(operator: Operator) -> bool:
    return operator_service.role_of(operator) in {
        operator_service.ROLE_ADMIN, operator_service.ROLE_TEACHER}


def _require_product_owner(db: Session, run: AutomationRun, product: Product) -> None:
    owner = operator_service.get(db, run.owner_operator_id)
    if (operator_service.role_of(owner) == operator_service.ROLE_MEMBER
            and product.operator_id != run.owner_operator_id):
        raise errors.AppError(
            errors.FORBIDDEN, "自动化运行只能使用本人处理的商品", status=403)


def _assert_account_access(db: Session, account: Account, operator: Operator) -> None:
    if not account_service.has_account_access(db, account.id, operator.id):
        raise errors.AppError(errors.FORBIDDEN, "无权访问其他学员的自动化运行", status=403)


def _recompute(run: AutomationRun, rows: list[AutomationRunItem]) -> None:
    process = [row.process_status for row in rows]
    publish = [row.publish_status for row in rows]
    images_queued = any(
        row.process_status == "succeeded"
        and isinstance((row.process_result or {}).get("images"), dict)
        and row.process_result["images"].get("queued") is True for row in rows)
    if "running" in process or "running" in publish or images_queued:
        run.status = "running"
        run.finished_at = None
    elif "queued" in process or "queued" in publish:
        run.status = "queued"
        run.finished_at = None
    elif "needs_review" in process or "needs_review" in publish:
        run.status = "needs_review"
        run.finished_at = now_ts()
    elif "failed" in process or "failed" in publish:
        run.status = "partial"
        run.finished_at = now_ts()
    else:
        run.status = "completed"
        run.finished_at = now_ts()
    run.updated_at = now_ts()


def _items(db: Session, run_id: str) -> list[AutomationRunItem]:
    return (db.query(AutomationRunItem).filter(AutomationRunItem.run_id == run_id)
            .order_by(AutomationRunItem.id.asc()).all())


def serialize_item(row: AutomationRunItem) -> dict[str, Any]:
    return {
        "id": str(row.id), "key": row.key, "detailId": row.detail_id,
        "shopId": row.source_shop_id, "title": row.title,
        "processStatus": row.process_status, "processReason": row.process_reason or "",
        "productId": str(row.product_id) if row.product_id else None,
        "processResult": row.process_result if isinstance(row.process_result, dict) else {},
        "publishStatus": row.publish_status, "publishReason": row.publish_reason or "",
        "publishResult": row.publish_result if isinstance(row.publish_result, dict) else {},
        "updatedAt": row.updated_at,
    }


def serialize_run(run: AutomationRun, rows: list[AutomationRunItem]) -> dict[str, Any]:
    return {
        "id": run.id, "ownerOperatorId": str(run.owner_operator_id),
        "accountId": str(run.account_id), "shopId": run.shop_id,
        "status": run.status, "settings": run.settings or {},
        "createdAt": run.created_at, "updatedAt": run.updated_at,
        "finishedAt": run.finished_at, "items": [serialize_item(row) for row in rows],
    }


def create(db: Session, operator: Operator, *, account_id: Any, shop_id: Any,
           settings: Mapping[str, Any], items: Iterable[Mapping[str, Any]]) -> AutomationRun:
    account_text = str(account_id or "").strip()
    if not account_text.isdigit():
        raise errors.validation("accountId 必须是数字", field="accountId")
    account = db.query(Account).filter(Account.id == int(account_text)).one_or_none()
    if account is None:
        raise errors.not_found(f"账号 {account_text} 不存在", accountId=account_text)
    _assert_account_access(db, account, operator)
    shop_text = str(shop_id or "").strip()
    if not shop_text.isdigit():
        raise errors.validation("shopId 必须是数字", field="shopId")
    wants_publish = settings.get("publishEnabled") is True
    if wants_publish:
        if settings.get("textEnabled") is not True:
            raise errors.validation("开启正式发布前必须启用文字处理", field="settings")
        if settings.get("imagesEnabled") is not True:
            raise errors.validation("开启正式发布前必须启用图片处理", field="settings")
        template = settings.get("template")
        if (settings.get("priceTemplateEnabled") is not True
                or not isinstance(template, str) or not template.strip()):
            raise errors.validation("开启正式发布前必须启用并选择定价模板", field="settings")

    clean: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        detail_id = str(item.get("detailId") or "").strip()
        source_shop_id = str(item.get("shopId") or "").strip()
        key = str(item.get("key") or f"{detail_id}:{source_shop_id}").strip()
        if not detail_id.isdigit() or not source_shop_id.isdigit():
            raise errors.validation("商品 detailId 和 shopId 必须是数字", field="items")
        if key in seen:
            continue
        if len(key) > 80:
            raise errors.validation("商品 key 不能超过 80 个字符", field="items")
        seen.add(key)
        clean.append({"key": key, "detail_id": detail_id,
                      "shop_id": source_shop_id,
                      "title": str(item.get("title") or "")[:500]})
        if len(clean) > 200:
            raise errors.validation("一次最多运行 200 件商品", field="items")
    if not clean:
        raise errors.validation("请至少选择一件商品", field="items")

    run = AutomationRun(
        id=f"arun_{secrets.token_hex(12)}", owner_operator_id=operator.id,
        account_id=account.id, shop_id=shop_text, status="queued",
        settings={key: value for key, value in settings.items()
                  if key not in {"specificationConfirmations",
                                 "confirmUnverifiedSpecifications"}},
        created_at=now_ts(), updated_at=now_ts(),
    )
    db.add(run)
    db.flush()
    for item in clean:
        db.add(AutomationRunItem(
            run_id=run.id, key=item["key"], detail_id=item["detail_id"],
            source_shop_id=item["shop_id"], title=item["title"],
            process_status="queued", process_result={},
            publish_status="queued" if wants_publish else "not_requested",
            publish_result={}, created_at=now_ts(), updated_at=now_ts(),
        ))
    db.flush()
    return run


def get(db: Session, run_id: str, operator: Operator) -> AutomationRun:
    run = db.query(AutomationRun).filter(AutomationRun.id == str(run_id)).one_or_none()
    if run is None:
        raise errors.not_found(f"自动化运行 {run_id} 不存在", runId=str(run_id))
    if not _teacher_or_admin(operator):
        if run.owner_operator_id != operator.id:
            raise errors.AppError(errors.FORBIDDEN, "无权访问其他学员的自动化运行", status=403)
        if not account_service.has_account_access(db, run.account_id, operator.id):
            # Treat a revoked run like a missing one so the old owner cannot
            # use its identifier to confirm that another user's history exists.
            raise errors.not_found(f"自动化运行 {run_id} 不存在", runId=str(run_id))
    return run


def latest(db: Session, operator: Operator, *, account_id: Any,
           shop_id: Any = None) -> AutomationRun | None:
    text = str(account_id or "").strip()
    if not text.isdigit():
        raise errors.validation("accountId 必须是数字", field="accountId")
    account = db.query(Account).filter(Account.id == int(text)).one_or_none()
    if account is None:
        raise errors.not_found(f"账号 {text} 不存在", accountId=text)
    _assert_account_access(db, account, operator)
    query = db.query(AutomationRun).filter(AutomationRun.account_id == account.id)
    if shop_id not in (None, ""):
        query = query.filter(AutomationRun.shop_id == str(shop_id))
    if not _teacher_or_admin(operator):
        query = query.filter(AutomationRun.owner_operator_id == operator.id)
    return query.order_by(AutomationRun.created_at.desc(), AutomationRun.id.desc()).first()


def list_runs(db: Session, operator: Operator, *, account_id: Any,
              shop_id: Any = None, limit: int = 30) -> list[dict[str, Any]]:
    """执行历史：按时间倒序列出这个人能看到的运行记录。

    只给「列表页」用，所以每一条都带汇总计数而不是逐项明细 —— 明细走
    get_full(run_id)。运营要的是「哪几批跑过、成了几件、挂了几件」，
    而不是一屏几百行 SKU。
    """
    text = str(account_id or "").strip()
    if not text.isdigit():
        raise errors.validation("accountId 必须是数字", field="accountId")
    account = db.query(Account).filter(Account.id == int(text)).one_or_none()
    if account is None:
        raise errors.not_found(f"账号 {text} 不存在", accountId=text)
    _assert_account_access(db, account, operator)
    query = db.query(AutomationRun).filter(AutomationRun.account_id == account.id)
    if shop_id not in (None, ""):
        query = query.filter(AutomationRun.shop_id == str(shop_id))
    if not _teacher_or_admin(operator):
        query = query.filter(AutomationRun.owner_operator_id == operator.id)
    rows = (query.order_by(AutomationRun.created_at.desc(), AutomationRun.id.desc())
            .limit(max(1, min(100, int(limit or 30)))).all())
    result: list[dict[str, Any]] = []
    for run in rows:
        items = _items(db, run.id)
        succeeded = sum(1 for row in items if row.process_status == "succeeded")
        failed = sum(1 for row in items if row.process_status == "failed")
        review = sum(1 for row in items if row.process_status == "needs_review"
                     or row.publish_status in ("needs_review", "failed"))
        settings = run.settings or {}
        # 逐件异常明细：只给「不是成功」的那些，带上详情号 / 标题 / 原因。
        # 负责人口径 2026-11-12：「要检查到底要检查什么，哪里有问题就写出来
        # 不要哪里有问题都不清楚」—— 之前列表只给「挂 2 件」这个数字，
        # 运营点开也看不到是哪两件、为什么挂，等于没有信息。
        issues: list[dict[str, Any]] = []
        for row in items:
            if row.process_status in ("succeeded", "queued", "running"):
                continue
            problem = row.process_reason or row.publish_reason or ""
            if not problem:
                # 状态是失败的但没记原因：别让界面留空，明确说「没记到原因」。
                problem = ("处理未完成，但没有记录具体原因；"
                           "请点开商品详情核对妙手草稿")
            issues.append({
                "detailId": str(row.detail_id or ""),
                "productId": str(row.product_id) if row.product_id else "",
                "title": str(getattr(row, "title", "") or "")[:80],
                "stage": "publish" if str(row.publish_status or "") in
                         ("failed", "needs_review") else "process",
                "status": str(row.process_status or ""),
                "reason": str(problem)[:400],
            })
        result.append({
            "id": run.id,
            "status": run.status,
            "issues": issues,
            "accountId": str(run.account_id),
            "shopId": run.shop_id,
            "createdAt": run.created_at,
            "finishedAt": run.finished_at,
            "total": len(items),
            "succeeded": succeeded,
            "failed": failed,
            "needsReview": review,
            "textEnabled": settings.get("textEnabled") is True,
            "imagesEnabled": settings.get("imagesEnabled") is True,
            "publishEnabled": settings.get("publishEnabled") is True,
            "language": settings.get("language") or "",
            "template": settings.get("template") or "",
        })
    return result




def delete_run(db: Session, run: AutomationRun) -> dict[str, Any]:
    """删掉一条执行历史（连带逐件明细）。

    负责人 2026-11-12：「还有能在这个历史加上×能够删除这个」。
    只清历史记录本身：已经入库的成品商品、妙手草稿都不动 ——
    运营删的是「这批跑过的痕迹」，不是撤回处理结果。
    运行中的批次不给删（线程还在写这张表）。
    """
    if run.status == "running":
        raise errors.AppError(
            "CONFLICT", "这批还在运行，不能删除；等跑完或先停止", status=409)
    run_id = run.id
    bill = (db.query(AutomationRunItem)
            .filter(AutomationRunItem.run_id == run_id)
            .delete(synchronize_session=False))
    db.delete(run)
    db.flush()
    return {"deleted": run_id, "items": int(bill or 0)}
def get_full(db: Session, run: AutomationRun) -> dict[str, Any]:
    return serialize_run(run, _items(db, run.id))


def recover(db: Session, run: AutomationRun) -> dict[str, Any]:
    for row in _items(db, run.id):
        product = db.get(Product, row.product_id) if row.product_id is not None else None
        images = product.automation_images if product is not None else None
        if (isinstance(images, dict) and images.get("queued") is True
                and images.get("startedAt", now_ts()) < now_ts() - 600):
            from core.locks import hold_for_session

            hold_for_session(db, [f"automation-image-result:{product.id}"])
            db.refresh(product)
            images = product.automation_images
            if (images.get("queued") is True
                    and images.get("startedAt", now_ts()) < now_ts() - 600):
                product.automation_images = {
                    **images, "queued": False, "completed": True, "failed": 1,
                    "reason": "图片处理已中断或超时，请核对已提交任务；未自动重新扣费提交",
                }
                product.draft_verified_revision = None
                complete_image_processing(db, product)
        if row.process_status == "running":
            row.process_status = "needs_review"
            row.process_reason = "客户端在处理响应前中断；请核对妙手与商品记录后再处理。"
            row.updated_at = now_ts()
        if row.publish_status == "running":
            row.publish_status = "needs_review"
            row.publish_reason = "客户端在发布响应前中断；请先核对妙手发布记录。"
            row.updated_at = now_ts()
        elif row.process_status == "succeeded" and row.publish_status == "queued":
            process_result = row.process_result if isinstance(row.process_result, dict) else {}
            image_result = process_result.get("images")
            has_image_job = (db.query(ImageJob.id).filter(
                ImageJob.product_id == row.product_id,
            ).first() if row.product_id is not None else None)
            image_work_submitted = (
                run.settings.get("publishEnabled") is True
                and run.settings.get("imagesEnabled") is True
                and isinstance(image_result, Mapping)
                and (image_result.get("queued") is True
                     or (isinstance(image_result.get("submitted"), int)
                         and image_result["submitted"] > 0))
            )
            if image_work_submitted:
                row.publish_status = "needs_review"
                row.publish_reason = "图片处理尚需核对；请确认图片任务状态后再发布。"
                row.publish_result = (
                    {"resumeKind": "images_pending"}
                    if has_image_job is not None or images is not None else {}
                )
                row.updated_at = now_ts()
    _recompute(run, _items(db, run.id))
    db.commit()
    return get_full(db, run)


def start_item(db: Session, run: AutomationRun, item_id: Any) -> dict[str, Any]:
    row = db.query(AutomationRunItem).filter(
        AutomationRunItem.run_id == run.id,
        AutomationRunItem.id == int(item_id),
    ).one_or_none()
    if row is None:
        raise errors.not_found("自动化运行商品不存在", itemId=str(item_id))
    changed = (db.query(AutomationRunItem).filter(
        AutomationRunItem.id == row.id,
        AutomationRunItem.process_status == "queued",
    ).update({"process_status": "running", "process_reason": "",
              "updated_at": now_ts()}, synchronize_session="fetch"))
    if changed != 1:
        raise errors.AppError("CONFLICT", "该商品已开始或已有处理结果，不会重复启动",
                              status=409)
    run.status = "running"
    run.finished_at = None
    run.updated_at = now_ts()
    db.commit()
    return get_full(db, run)


def image_processing_result(product: Product) -> dict[str, Any] | None:
    state = product.automation_images
    if not isinstance(state, dict):
        return None
    return {key: value for key, value in state.items()
            if key not in {"generation", "revision", "startedAt"}}


def _require_images_complete(product: Product) -> None:
    images = image_processing_result(product)
    if images is not None and (images.get("completed") is not True
                               or images.get("failed", 0) != 0):
        raise errors.AppError(
            "CONFLICT", images.get("reason") or "图片仍在处理或尚未通过草稿核验，暂不能发布",
            status=409)


def complete_image_processing(db: Session, product: Product) -> None:
    """Bind the final image draft proof only to the run which queued this work."""
    images = image_processing_result(product)
    if images is None:
        return
    db.flush()  # updated_at must describe the exact payload and readback proof.
    # 只查 succeeded 会漏掉「图片线程上一轮把它标成 needs_review」的项：
    # 那次失败的图片最终跑成功了，但 process_status 永远停在 needs_review，
    # 于是成品仓库的自动化结果列表（要求 succeeded）一直看不到这件商品
    # （2026-11-11 实测：负责人问「都处理好了为什么成品仓库没有」）。
    # 这里把 needs_review 也查出来，核验通过后恢复成 succeeded。
    rows = (db.query(AutomationRunItem, AutomationRun)
            .join(AutomationRun, AutomationRun.id == AutomationRunItem.run_id)
            .filter(AutomationRunItem.product_id == product.id,
                    AutomationRun.owner_operator_id == product.operator_id,
                    AutomationRunItem.process_status.in_(
                        ["succeeded", "needs_review"])).all())
    for row, run in rows:
        previous = dict(row.process_result or {})
        if previous.get("_imageGeneration") != product.automation_images.get("generation"):
            continue
        previous["images"] = images
        # 判定「图片处理成功」= 翻译跑完 + 没有失败 + 本地核验版本一致。
        #
        # 最后一项（draft_verified_revision == revision）看着像内部记账，
        # 但它同时挡住了一个真实风险：图片写回后主流程又改过商品，
        # 此时草稿里的图和本地记录已经对不上，不能直接放行发布。
        # tests/test_automation_image_lifecycle 的 upstream-change 用例
        # 就是守这条，所以保留。
        #
        # 「处理完就待核对、要人点一下」的根因已经在上游修掉了：
        #   1. 图片线程不再因为「图片换 URL」误判草稿被改；
        #   2. 主流程写完会同步把 draft_verified_revision 推到 revision，
        #      两者正常情况下是一致的。
        verified = (images.get("completed") is True and images.get("failed", 0) == 0
                    and product.draft_verified_revision == product.revision)
        previous["draftVerified"] = verified
        if verified:
            previous["_serverVerification"] = {
                "productId": str(product.id), "productUpdatedAt": product.updated_at,
                "productRevision": product.revision,
            }
            # 上一轮因图片问题被标成 needs_review 的，核验通过后必须恢复
            # succeeded：否则成品仓库的「自动化结果」列表永远过滤掉它
            # （那个列表要求 process_status == succeeded）。
            if row.process_status == "needs_review":
                row.process_status = "succeeded"
                row.process_reason = ""
        else:
            previous.pop("_serverVerification", None)
            row.process_status = "needs_review"
            row.process_reason = str(images.get("reason") or "图片处理未通过核验")[:2000]
        row.process_result = previous
        if (run.settings.get("publishEnabled") is True
                and row.publish_status in {"queued", "needs_review"}):
            row.publish_status = "needs_review"
            row.publish_reason = ("图片已完成，请核对后继续发布" if verified
                                  else row.process_reason)
            row.publish_result = {"resumeKind": "images_pending"} if verified else {}
        row.updated_at = now_ts()
        _recompute(run, _items(db, run.id))


def finish_process(db: Session, run: AutomationRun, item_id: Any, *, status: str,
                   reason: str = "", product_id: Any = None,
                   result: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if status not in {"succeeded", "failed", "needs_review"}:
        raise errors.validation("处理结果状态无效", field="status")
    row = _get_item(db, run, item_id)
    if row.process_status not in {"running", "needs_review"}:
        raise errors.AppError("CONFLICT", "该商品不处于可记录处理结果的状态",
                              status=409)
    product_pk = None
    if product_id not in (None, ""):
        text = str(product_id)
        if not text.isdigit():
            raise errors.validation("productId 必须是数字", field="productId")
        product_pk = int(text)
    server_verification: dict[str, Any] | None = None
    images = None
    if status == "succeeded":
        if product_pk is None:
            raise errors.AppError(
                "CONFLICT", "处理成功必须关联已保存并核验的商品", status=409)
        from core.locks import hold_for_session

        hold_for_session(db, [f"automation-image-result:{product_pk}"])
        product = db.query(Product).populate_existing().filter(
            Product.id == product_pk,
            Product.account_id == run.account_id,
            Product.automation_result.is_(True),
            Product.detail_id == row.detail_id,
            Product.shop_id == row.source_shop_id,
        ).one_or_none()
        images = image_processing_result(product) if product is not None else None
        pending_images = images is not None and images.get("queued") is True
        failed_images = images is not None and images.get("failed", 0) != 0
        if (product is None or product.updated_at < row.updated_at
                or (not pending_images and not failed_images
                    and (product.draft_verified_revision is None
                    or product.draft_verified_revision != product.revision))):
            raise errors.AppError(
                "CONFLICT", "商品没有与当前版本匹配的妙手草稿读回核验", status=409)
        _require_product_owner(db, run, product)
        if images is not None and images.get("failed", 0) != 0:
            status = "needs_review"
            reason = str(images.get("reason") or "图片处理未通过核验")
        if (images is None and run.settings.get("publishEnabled") is True
                and run.settings.get("imagesEnabled") is True
                and (not isinstance(result, Mapping)
                     or not isinstance(result.get("images"), Mapping)
                     or result["images"].get("failed") != 0)):
            raise errors.AppError(
                "CONFLICT", "发布前需确认图片处理无失败", status=409)
        server_verification = None if pending_images or status != "succeeded" else {
            "productId": str(product.id),
            "productUpdatedAt": product.updated_at,
            "productRevision": product.revision,
        }
    row.process_status = status
    row.process_reason = str(reason or "")[:2000]
    row.product_id = product_pk
    row.process_result = {
        key: value for key, value in (result or {}).items()
        if key not in {"_serverVerification", "_imageGeneration", "saved", "draftVerified"}
    }
    if images is not None:
        row.process_result["images"] = images
        row.process_result["_imageGeneration"] = product.automation_images.get("generation")
    if status == "succeeded":
        row.process_result.update({"saved": True, "draftVerified": server_verification is not None})
    if server_verification is not None:
        row.process_result["_serverVerification"] = server_verification
    if row.publish_status == "queued" and status in {"failed", "needs_review"}:
        row.publish_status = status
        row.publish_reason = (
            "商品处理失败，未进入发布。" if status == "failed"
            else "商品处理状态待核对，暂不发布。"
        )
    row.updated_at = now_ts()
    _recompute(run, _items(db, run.id))
    db.commit()
    return get_full(db, run)


def _confirm_automation_specifications(
        db: Session, run: AutomationRun, product: Product,
        confirmed_product_id: Any, confirmed_revision: Any) -> None:
    quality_status, reasons = warehouse_service._quality_review_summary(product)
    if quality_status == "verified":
        return
    product_id = str(product.id)
    revision = int(product.revision or 0)
    if (str(confirmed_product_id or "") == product_id
            and type(confirmed_revision) is int
            and confirmed_revision == revision):
        settings = dict(run.settings or {})
        confirmations = settings.get("specificationConfirmations")
        confirmations = dict(confirmations) if isinstance(confirmations, dict) else {}
        confirmations[product_id] = revision
        settings["specificationConfirmations"] = confirmations
        settings.pop("confirmUnverifiedSpecifications", None)
        run.settings = settings
        return
    raise errors.AppError(
        "QUALITY_REVIEW_REQUIRED",
        "商品重量或包裹尺寸尚未由老师按当前版本核验；明确确认后才能自动发布",
        status=409,
        details={"items": [{"warehouseId": product_id, "revision": revision,
                             "reasons": reasons}],
                 "field": "confirmedProductId,confirmedRevision",
                 "confirmationStale": confirmed_product_id is not None
                 or confirmed_revision is not None},
    )


def start_publish(db: Session, run: AutomationRun, item_id: Any, *,
                  confirmed_product_id: Any = None,
                  confirmed_revision: Any = None) -> dict[str, Any]:
    row = _get_item(db, run, item_id)
    if row.process_status != "succeeded" or row.product_id is None:
        raise errors.AppError("CONFLICT", "商品处理完成后才能发布", status=409)
    process_result = row.process_result if isinstance(row.process_result, dict) else {}
    server_verification = process_result.get("_serverVerification")
    if (run.settings.get("publishEnabled") is not True
            or process_result.get("saved") is not True
            or process_result.get("draftVerified") is not True
            or not isinstance(server_verification, dict)
            or server_verification.get("productId") != str(row.product_id)):
        raise errors.AppError(
            "CONFLICT", "商品草稿未通过保存与读回核验，不能发布", status=409)
    product = db.query(Product).filter(
        Product.id == row.product_id,
        Product.account_id == run.account_id,
        Product.automation_result.is_(True),
    ).one_or_none()
    if product is not None:
        _require_images_complete(product)
    if (product is None
            or product.updated_at != server_verification.get("productUpdatedAt")
            or product.revision != server_verification.get("productRevision")
            or product.draft_verified_revision != product.revision):
        raise errors.AppError(
            "CONFLICT", "商品自处理核验后发生变化，请重新处理并核对", status=409)
    _require_product_owner(db, run, product)
    _confirm_automation_specifications(
        db, run, product, confirmed_product_id, confirmed_revision)
    if run.settings.get("imagesEnabled") is True:
        unfinished_query = db.query(ImageJob.id).filter(
            ImageJob.product_id == product.id,
            ImageJob.status != "ready",
        )
        if product.automation_images is not None:
            unfinished_query = unfinished_query.filter(ImageJob.id.in_(
                [job["id"] for job in product.automation_images.get("jobs", [])
                 if job.get("id")]))
        unfinished_images = unfinished_query.first()
        if unfinished_images:
            raise errors.AppError(
                "CONFLICT", "图片翻译任务尚未全部完成，请完成并核对后再发布", status=409)
    changed = (db.query(AutomationRunItem).filter(
        AutomationRunItem.id == row.id,
        AutomationRunItem.publish_status == "queued",
    ).update({"publish_status": "running", "publish_reason": "",
              "updated_at": now_ts()}, synchronize_session="fetch"))
    if changed != 1:
        raise errors.AppError("CONFLICT", "该商品发布已开始或已有结果，不会重复启动",
                              status=409)
    run.status = "running"
    run.finished_at = None
    run.updated_at = now_ts()
    db.commit()
    return get_full(db, run)


def finish_publish(db: Session, run: AutomationRun, item_id: Any, *, status: str,
                    reason: str = "", result: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if status not in {"succeeded", "failed", "needs_review"}:
        raise errors.validation("发布结果状态无效", field="status")
    if status == "succeeded":
        raise errors.AppError(
            "CONFLICT", "发布成功状态只能由妙手发布回执确认", status=409)
    row = _get_item(db, run, item_id)
    if row.publish_status not in {"running", "needs_review"} and not (
        row.publish_status == "queued" and status == "needs_review"
    ):
        raise errors.AppError("CONFLICT", "该商品不处于可记录发布结果的状态",
                              status=409)
    previous_status = row.publish_status
    previous_result = row.publish_result if isinstance(row.publish_result, dict) else {}
    row.publish_status = status
    row.publish_reason = str(reason or "")[:2000]
    row.publish_result = dict(result or {})
    # A server-bound publish identity survives a missing response and cannot be
    # replaced by the client's subsequently reported (possibly empty) result.
    if previous_result.get("publishLogId"):
        for key in ("publishLogId", "batchId"):
            row.publish_result[key] = previous_result.get(key)
    process_result = row.process_result if isinstance(row.process_result, dict) else {}
    image_result = process_result.get("images")
    has_image_job = (db.query(ImageJob.id).filter(
        ImageJob.product_id == row.product_id,
    ).first() if row.product_id is not None else None)
    if (previous_status == "queued" and status == "needs_review"
            and row.process_status == "succeeded"
            and run.settings.get("publishEnabled") is True
            and run.settings.get("imagesEnabled") is True
            and isinstance(image_result, Mapping)
            and isinstance(image_result.get("submitted"), int)
            and image_result["submitted"] > 0
            and has_image_job is not None):
        row.publish_result["resumeKind"] = "images_pending"
    product = db.get(Product, row.product_id) if row.product_id is not None else None
    images = image_processing_result(product) if product is not None else None
    if (status == "needs_review" and previous_status in {"queued", "needs_review"}
            and row.process_status == "succeeded"
            and run.settings.get("publishEnabled") is True
            and run.settings.get("imagesEnabled") is True
            and images is not None and images.get("failed", 0) == 0):
        row.publish_result["resumeKind"] = "images_pending"
    row.updated_at = now_ts()
    _recompute(run, _items(db, run.id))
    db.commit()
    return get_full(db, run)


def resume_publish_after_images(db: Session, run: AutomationRun,
                                item_id: Any, *,
                                confirmed_product_id: Any = None,
                                confirmed_revision: Any = None) -> dict[str, Any]:
    row = _get_item(db, run, item_id)
    result = row.publish_result if isinstance(row.publish_result, dict) else {}
    if (row.publish_status != "needs_review"
            or result.get("resumeKind") != "images_pending"
            or row.process_status != "succeeded"
            or row.product_id is None
            or run.settings.get("publishEnabled") is not True
            or run.settings.get("imagesEnabled") is not True):
        raise errors.AppError("CONFLICT", "该商品不符合图片完成后的续发条件", status=409)

    process_result = row.process_result if isinstance(row.process_result, dict) else {}
    server_verification = process_result.get("_serverVerification")
    product = db.query(Product).filter(
        Product.id == row.product_id,
        Product.account_id == run.account_id,
        Product.automation_result.is_(True),
    ).one_or_none()
    if product is not None:
        _require_images_complete(product)
    if (product is None
            or not isinstance(server_verification, dict)
            or server_verification.get("productId") != str(product.id)
            or product.updated_at != server_verification.get("productUpdatedAt")
            or product.revision != server_verification.get("productRevision")
            or product.draft_verified_revision != product.revision):
        raise errors.AppError("CONFLICT", "商品草稿已变化或核验已失效，请重新核对", status=409)
    _require_product_owner(db, run, product)

    _confirm_automation_specifications(
        db, run, product, confirmed_product_id, confirmed_revision)

    unfinished_query = db.query(ImageJob.id).filter(
        ImageJob.product_id == product.id,
        ImageJob.status != "ready",
    )
    if product.automation_images is not None:
        unfinished_query = unfinished_query.filter(ImageJob.id.in_(
            [job["id"] for job in product.automation_images.get("jobs", []) if job.get("id")]))
    unfinished_images = unfinished_query.first()
    if unfinished_images:
        raise errors.AppError("CONFLICT", "图片翻译任务尚未全部完成", status=409)

    changed = (db.query(AutomationRunItem).filter(
        AutomationRunItem.id == row.id,
        AutomationRunItem.publish_status == "needs_review",
    ).update({
        "publish_status": "running",
        "publish_reason": "",
        "publish_result": {"resumeKind": "images_ready"},
        "updated_at": now_ts(),
    }, synchronize_session="fetch"))
    if changed != 1:
        raise errors.AppError("CONFLICT", "该商品发布已开始或已有结果，不会重复续发", status=409)
    run.status = "running"
    run.finished_at = None
    run.updated_at = now_ts()
    db.commit()
    return get_full(db, run)


def retry_publish(db: Session, run: AutomationRun, item_id: Any) -> dict[str, Any]:
    row = _get_item(db, run, item_id)
    result = row.publish_result if isinstance(row.publish_result, dict) else {}
    log_id = result.get("publishLogId")
    platform_status = result.get("platformStatus")
    if (row.process_status != "succeeded" or row.publish_status != "failed"
            or platform_status not in {"fail", "cancel"}
            or not str(log_id or "").isdigit()):
        raise errors.AppError(
            "CONFLICT", "只有妙手回执确认失败或取消的商品才可以重试", status=409)

    log = db.query(PublishLog).filter(
        PublishLog.id == int(log_id),
        PublishLog.account_id == run.account_id,
        PublishLog.product_id == row.product_id,
        PublishLog.shop_id == run.shop_id,
        PublishLog.batch_id == str(result.get("batchId") or ""),
        PublishLog.status.in_(("fail", "cancel")),
    ).one_or_none()
    if (log is None or log.status != platform_status
            or (log.operator_id is not None and log.operator_id != run.owner_operator_id)):
        raise errors.AppError(
            "CONFLICT", "发布失败记录已变化或无法匹配，请刷新发布记录后再试", status=409)

    row.publish_status = "queued"
    row.publish_reason = ""
    row.publish_result = {
        "retryOfBatchId": log.batch_id,
        "retryOfPublishLogId": str(log.id),
    }
    row.updated_at = now_ts()
    _recompute(run, _items(db, run.id))
    db.commit()
    return get_full(db, run)


def reconcile_publish_log(db: Session, log: PublishLog) -> bool:
    """Copy a final publish receipt to the automation item that submitted it."""
    if log.status not in {"success", "fail", "cancel"} or not log.batch_id:
        return False
    query = (db.query(AutomationRunItem)
             .join(AutomationRun, AutomationRun.id == AutomationRunItem.run_id)
             .filter(AutomationRun.account_id == log.account_id,
                     AutomationRun.shop_id == log.shop_id,
                     AutomationRunItem.product_id == log.product_id,
                     AutomationRunItem.publish_status.in_(("running", "needs_review", "failed"))))
    if log.operator_id is not None:
        query = query.filter(AutomationRun.owner_operator_id == log.operator_id)
    matched = []
    identity = log.detail if isinstance(log.detail, dict) else {}
    for item in query.all():
        result = item.publish_result if isinstance(item.publish_result, dict) else {}
        if result.get("batchId") != log.batch_id:
            continue
        if identity.get("automationRunItemId"):
            if (str(item.id) != str(identity["automationRunItemId"])
                    or item.run_id != identity.get("automationRunId")
                    or str(result.get("publishLogId")) != str(log.id)):
                continue
        elif item.publish_status != "needs_review":
            # Legacy records without a server-bound identity remain conservative.
            continue
        if result.get("publishLogId") not in (None, str(log.id)):
            continue
        matched.append(item)
    if len(matched) != 1:
        return False

    item = matched[0]
    item.publish_status = "succeeded" if log.status == "success" else "failed"
    item.publish_reason = str(log.reason or "")[:2000]
    item.publish_result = {
        **(item.publish_result if isinstance(item.publish_result, dict) else {}),
        "publishLogId": str(log.id),
        "platformStatus": log.status,
        "platformItemId": log.platform_item_id,
    }
    item.updated_at = now_ts()
    run = db.query(AutomationRun).filter(AutomationRun.id == item.run_id).one()
    _recompute(run, _items(db, run.id))
    return True


def _get_item(db: Session, run: AutomationRun, item_id: Any) -> AutomationRunItem:
    try:
        ident = int(str(item_id))
    except (TypeError, ValueError):
        raise errors.not_found("自动化运行商品不存在", itemId=str(item_id)) from None
    row = db.query(AutomationRunItem).filter(
        AutomationRunItem.run_id == run.id, AutomationRunItem.id == ident,
    ).one_or_none()
    if row is None:
        raise errors.not_found("自动化运行商品不存在", itemId=str(item_id))
    return row
