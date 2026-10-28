"""批次编排路由（老版 `POST /api/start` + `/api/control` + `/api/confirm-save`）。

和老版最大的差别是**不再有后台线程**：
老版 `AutomationController` 起一个线程跑整批，`/api/control` 只是给那个线程
置标志位。新版把「推进一件」暴露成 `POST /batch/{id}/step`，调用方（前端
轮询 / 定时任务 / 脚本）自己循环。这样：

  · 暂停/停止是立即生效的——下一个 step 直接返回「已暂停」，不推进；
  · 进程重启不丢进度（进度在库里，不在线程内存里）；
  · 单件失败时批次停在原地，不会「继续跑完剩下的然后把错误埋在最后」。

代价是调用方要循环调用 step。这不是偷懒：SQLite 单写者 + 同步 HTTP 请求
里塞一个跑几十分钟的循环，会让整个服务在整批期间无法响应别的请求。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from api.deps import DbSession, OperatorDep, require_permission, require_writable
from schemas.requests import (
    BatchControlRequest,
    BatchCreateRequest,
    BatchRetryRequest,
)
from services import audit_service, batch_service

router = APIRouter(prefix="/batch", tags=["批次编排"],
                   dependencies=[require_permission("batch")])


@router.post("", status_code=201, summary="建批次（同账号同店铺只允许一条未完成的）")
def create_batch(payload: BatchCreateRequest, db: DbSession,
                 operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    with audit_service.guard(db, operator=operator, action="batch.create",
                             target_kind="batch", target_id="",
                             account_id=payload.accountId,
                             shop_id=str(payload.shopId or "")):
        batch = batch_service.create(
            db,
            account_id=payload.accountId,
            shop_id=payload.shopId,
            detail_ids=payload.detailIds,
            settings=payload.settings,
            operator_id=getattr(operator, "id", None),
        )
        db.commit()
        # 回读一次：commit 之后 expire_on_commit 会重查，serialize 需要活着的行
        batch = batch_service.get(db, batch.id)
    audit_service.record(db, operator=operator, action="batch.create",
                         target_kind="batch", target_id=batch.id,
                         account_id=batch.account_id, shop_id=batch.shop_id or "",
                         detail={"items": len(batch.detail_ids or [])})
    db.commit()
    return {"batch": batch_service.serialize(
                batch, operator_name=getattr(operator, "name", "")),
            "items": [batch_service.serialize_item(item)
                      for item in batch_service.items_of(db, batch.id)]}


@router.get("", summary="批次列表")
def list_batches(
    db: DbSession,
    accountId: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    return batch_service.list_batches(db, account_id=accountId, limit=limit)


@router.get("/{batch_id}", summary="批次详情（含逐件状态）")
def batch_detail(batch_id: str, db: DbSession) -> dict[str, Any]:
    batch = batch_service.get(db, batch_id)
    return {"batch": batch_service.serialize(
                batch, operator_name=batch_service.operator_names(db).get(
                    batch.operator_id or 0, "")),
            "items": [batch_service.serialize_item(item)
                      for item in batch_service.items_of(db, batch.id)]}


@router.post("/{batch_id}/step", summary="推进一件（反复调用可跑完整批）")
def step_batch(batch_id: str, db: DbSession,
               operator: OperatorDep) -> dict[str, Any]:
    """推进一件。返回 `{batch, item, advanced, message}`。

    不在这里吞异常：`run_once` 内部的单件失败已经落成 `item.status=failed` +
    `batch.status=failed`，正常返回；只有「不该推进」（暂停/停止/已完成）和
    「幂等闸拒绝」（RETRY_UNSAFE 409）会抛出去，让调用方明确看到原因。

    成功时要 commit：单件入仓是真的写了库（Product 行），
    失败时同样要 commit——失败记录本身也是进度，回滚掉就会无限重试同一件。
    """
    require_writable(db)
    batch = batch_service.get(db, batch_id)
    # 审计的粒度是「一次 step」，不是「一件商品」：一批 200 件逐件记会让审计表
    # 涨得比业务表还快，而运营要问的是「这批是谁在推、什么时候推进的」。
    # 具体哪一件失败了，BatchItem.message 里有，审计 detail 里也带了一份。
    with audit_service.guard(db, operator=operator, action="batch.step",
                             target_kind="batch", target_id=batch_id,
                             account_id=batch.account_id,
                             shop_id=batch.shop_id or ""):
        result = batch_service.run_once(db, batch_id)
        db.commit()
    item = result.get("item") if isinstance(result, dict) else None
    audit_service.record(
        db, operator=operator, action="batch.step", target_kind="batch",
        target_id=batch_id, account_id=batch.account_id,
        shop_id=batch.shop_id or "",
        # 「没推进」不是失败：暂停中的 step 返回 advanced=false 是正常语义，
        # 记成 failed 会让审计里全是假故障。三种结果值与前端一致：
        # success / failed / blocked（见 web/src/features/settings/AuditLogPanel.tsx）
        result="success" if (result.get("advanced") or not item) else "blocked",
        message=str((item or {}).get("message") or "")[:500],
        detail={"advanced": bool(result.get("advanced")),
                "detailId": str((item or {}).get("detailId") or ""),
                "itemStatus": str((item or {}).get("status") or "")})
    db.commit()
    return result


@router.post("/{batch_id}/control", summary="暂停 / 继续 / 停止")
def control_batch(batch_id: str, payload: BatchControlRequest,
                  db: DbSession, operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    batch = batch_service.get(db, batch_id)
    with audit_service.guard(db, operator=operator, action="batch.control",
                             target_kind="batch", target_id=batch_id,
                             account_id=batch.account_id,
                             shop_id=batch.shop_id or "",
                             detail={"action": payload.action}):
        result = batch_service.control(db, batch_id, payload.action)
        db.commit()
    audit_service.record(db, operator=operator, action="batch.control",
                         target_kind="batch", target_id=batch_id,
                         account_id=batch.account_id, shop_id=batch.shop_id or "",
                         detail={"action": payload.action,
                                 "status": str((result.get("batch") or {}).get("status")
                                               or "")})
    db.commit()
    return result


@router.post("/{batch_id}/retry-failed", summary="把失败件改回待处理并重跑")
def retry_failed(batch_id: str, db: DbSession, operator: OperatorDep,
                 payload: BatchRetryRequest | None = None) -> dict[str, Any]:
    """重试失败件。

    `allowUnknown=true` 是操作者「已在妙手核对过、确认没保存」的声明。
    不带这个声明而又有结果未知的件时直接 409 RETRY_UNSAFE：
    盲目重跑会让重量被加第二次增量（prepare_weight 不幂等）。
    """
    require_writable(db)
    batch = batch_service.get(db, batch_id)
    allow = bool(payload.allowUnknown) if payload is not None else False
    with audit_service.guard(db, operator=operator, action="batch.retry",
                             target_kind="batch", target_id=batch_id,
                             account_id=batch.account_id,
                             shop_id=batch.shop_id or "",
                             detail={"allowUnknown": allow}):
        result = batch_service.retry_failed(db, batch_id, allow_unknown=allow)
        db.commit()
    audit_service.record(db, operator=operator, action="batch.retry",
                         target_kind="batch", target_id=batch_id,
                         account_id=batch.account_id, shop_id=batch.shop_id or "",
                         detail={"allowUnknown": allow,
                                 "retried": int(result.get("retried") or 0)})
    db.commit()
    return result


@router.delete("/{batch_id}", summary="删除批次（只有 done / stopped / failed 可删）")
def delete_batch(batch_id: str, db: DbSession,
                 operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    batch = batch_service.get(db, batch_id)
    with audit_service.guard(db, operator=operator, action="batch.delete",
                             target_kind="batch", target_id=batch_id,
                             account_id=batch.account_id,
                             shop_id=batch.shop_id or ""):
        result = batch_service.delete(db, batch_id)
        db.commit()
    audit_service.record(db, operator=operator, action="batch.delete",
                         target_kind="batch", target_id=batch_id,
                         account_id=batch.account_id, shop_id=batch.shop_id or "")
    db.commit()
    return result
