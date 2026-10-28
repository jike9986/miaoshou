"""发布路由：发布 + 发布记录。"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query

from api.deps import CredentialsHeader, DbSession, OperatorDep, require_permission, require_writable
from api.helpers import parse_bool
from schemas.requests import PublishRequest
from services import audit_service, operator_service, publish_service, setting_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/publish", tags=["发布"])

# 对外分页上限沿用全局约定（100），但平台侧 publish_records 自身只收 1..20，
# 所以回源时按这个值截断，不能把 100 直接送给上游（会直接判参数错误）。
PUBLISH_RECORDS_PAGE_SIZE = 20


@router.get("/logs", summary="本机发起的发布记录")
def logs(
    db: DbSession,
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=20, ge=1, le=100),
    status: str | None = None,
    accountId: str | None = None,
    shopId: str | None = None,
    scheduleId: str | None = None,
    since: float | None = None,
    refresh: str | None = None,
    operator: OperatorDep = None,
) -> dict[str, Any]:
    # 默认不在 GET 里打上游（列表要快）；但带 refresh=1 时必须真回源——
    # 否则用户看到的水远是本地快照，平台侧改判永远不会同步过来。
    platform_total: Any = None
    degraded = False
    refresh_flag = parse_bool(refresh, "refresh", False)
    if refresh_flag:
        operator_service.require_role(operator, "publish")
    if refresh_flag:
        try:
            # 回源沿用本次列表请求的分页与状态筛选：写死 1/20 会把翻到第 3 页
            # 的记录永远留在 processing，状态筛选也被无声丢掉。
            result = _refresh_processing(db, accountId, page_no=page,
                                         page_size=pageSize, status=status)
            platform_total = result.get("platformTotal")
            degraded = bool(result.get("degraded"))
        except Exception:  # noqa: BLE001 - 回源失败不该让列表报错
            logger.warning("发布记录回源失败", exc_info=True)
            degraded = True
    return publish_service.list_logs(
        db, page=page, page_size=pageSize, status=status, account_id=accountId,
        shop_id=shopId, schedule_id=scheduleId, since=since,
        platform_total=platform_total, degraded=degraded)


@router.get("/settings", summary="发布开关状态")
def publish_settings(db: DbSession) -> dict[str, Any]:
    return {
        "publishEnabled": setting_service.publish_enabled(db),
        "readOnly": setting_service.is_read_only(db),
    }


@router.post("/settings", summary="打开/关闭发布总开关",
             dependencies=[require_permission("admin")])
def set_publish_settings(payload: dict[str, Any], db: DbSession,
                         operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    enabled = payload.get("publishEnabled")
    if enabled is None:
        enabled = payload.get("enabled")
    enabled = parse_bool(enabled, "publishEnabled", False)
    # 发布总开关是**共享设置**：一关全员都发不出去。审计里必须留一条，
    # 否则「下午三点以后谁都没发成功」会变成一个没人答得出的问题。
    setting_service.set_publish_enabled(db, enabled)
    db.commit()
    audit_service.record(db, operator=operator, action="settings.change",
                         target_kind="setting", target_id="publishEnabled",
                         detail={"name": "发布总开关", "publishEnabled": bool(enabled)})
    db.commit()
    return {"publishEnabled": setting_service.publish_enabled(db)}


@router.post("", summary="发布（单个/批量，必须 confirm=true）",
             dependencies=[require_permission("publish")])
def publish(payload: PublishRequest, db: DbSession,
            operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    body = payload.model_dump(by_alias=False, exclude_none=True)
    # confirm 必须是字面量 true：这里再落一次，避免 Pydantic 真值转换影响判定
    body["confirm"] = payload.confirm is True
    # 发布不可撤销，失败一定要留痕：guard 在抛异常时用独立会话记一条 failed。
    # target_id 用 batchId 而不是商品键——一次发布可能几十件，逐件记会让审计
    # 表比发布台账还大；批次 id 能把整次操作串起来（发布台账里每行也带它）。
    with audit_service.guard(db, operator=operator, action="publish.submit",
                             target_kind="publish",
                             target_id=str(body.get("batchId") or ""),
                             account_id=body.get("accountId"),
                             shop_id=str(body.get("shopId") or "")):
        result = publish_service.publish(db, body,
                                         operator_id=getattr(operator, "id", None))
        db.commit()
    audit_service.record(
        db, operator=operator, action="publish.submit", target_kind="publish",
        target_id=str(result.get("batchId") or ""),
        account_id=body.get("accountId"),
        shop_id=str(result.get("targetShopId") or body.get("shopId") or ""),
        message=str(result.get("warning") or ""),
        detail={"mode": result.get("mode"),
                "batchId": result.get("batchId"),
                "published": len(result.get("published") or []),
                "claimed": len(result.get("claimed") or []),
                "failures": len(result.get("failures") or []),
                "targetShopName": result.get("targetShopName")})
    db.commit()
    return result


def _refresh_processing(db: DbSession, account_id: str | None = None, *,
                        page_no: int = 1, page_size: int = PUBLISH_RECORDS_PAGE_SIZE,
                        status: str | None = None) -> dict[str, Any]:
    """回源平台，更新 processing 行的状态（best effort，不抛错给调用方）。"""
    account, client = publish_service.account_client_for(db, account_id)
    from services.miaoshou import MiaoshouError

    updated = 0
    degraded = False
    platform_total: Any = None
    # 上游 search_move_collect_list 自身上限就是 20 行，比本机 pageSize（最大 100）
    # 小；直接透传会被上游判参数无效，所以这里夹一层。
    upstream_size = max(1, min(int(page_size or PUBLISH_RECORDS_PAGE_SIZE),
                               PUBLISH_RECORDS_PAGE_SIZE))
    upstream_page = max(1, int(page_no or 1))
    # 透传状态筛选，但「processing」不透传：回源的目的是拿改判结果，若让上游
    # 只返回仍是 processing 的行，那些已经被平台判成 success/fail 的记录永远
    # 不会出现在回执里，本地就永久卡在「已提交，等待平台回执」。
    upstream_status = (status or None)
    if str(upstream_status or "").lower() == "processing":
        upstream_status = None
    try:
        records = client.publish_records(page_no=upstream_page,
                                         page_size=upstream_size,
                                         status=upstream_status)
    except (MiaoshouError, ValueError):
        degraded = True
        records = {}
    finally:
        client.close()
    from models.publish_log import PublishLog

    rows = (db.query(PublishLog)
            .filter(PublishLog.account_id == account.id,
                    PublishLog.status == "processing").all())
    by_detail: dict[str, Any] = {}
    # 上游同一个接口（search_move_collect_list），老版读 moveCollectDetailList、
    # 新版读 detailList。两边都试：读错字段的后果是永远解析出 0 行、
    # 静默返回 updated=0，回执功能整体失效还不报错。
    data = records.get("data") or {}
    # 平台真实总数只在上游回执里；本地 count 是本机台账行数，两者不是一回事
    # （老版就是读这里的 total）。
    platform_total = data.get("total")
    platform_rows = data.get("moveCollectDetailList") or data.get("detailList") or []
    for row in platform_rows:
        if isinstance(row, dict):
            detail_id = row.get("detailId") or row.get("collectBoxDetailId")
            if detail_id not in (None, ""):
                by_detail[str(detail_id)] = row
    from core.utils import now_ts

    for row in rows:
        found = by_detail.get(str(row.detail_id))
        if not found:
            continue
        # 局部名不能叫 status：那会盖住入参 status（上游筛选条件），
        # 后面再加一轮回源就会用到错的筛选值。
        remote_status = str(found.get("status") or "").lower()
        if remote_status in ("success", "fail", "cancel", "processing"):
            row.status = remote_status
            row.reason = str(found.get("reason") or found.get("message") or "")
            row.platform_item_id = (str(found.get("itemId"))
                                    if found.get("itemId") not in (None, "") else None)
            row.finished_at = now_ts() if remote_status != "processing" else None
            updated += 1
    db.commit()
    return {"updated": updated, "degraded": degraded, "accountId": str(account.id),
            "platformTotal": platform_total}


@router.post("/logs/refresh", summary="回源平台更新发布状态（best effort）",
             dependencies=[require_permission("publish")])
def refresh_logs(db: DbSession,
                 credentials: CredentialsHeader = None,
                 accountId: str | None = None,
                 page: int = Query(default=1, ge=1),
                 pageSize: int = Query(default=PUBLISH_RECORDS_PAGE_SIZE, ge=1, le=100),
                 status: str | None = None) -> dict[str, Any]:
    require_writable(db)
    return _refresh_processing(db, accountId, page_no=page, page_size=pageSize,
                               status=status)
