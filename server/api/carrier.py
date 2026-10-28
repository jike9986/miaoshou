"""载体链接池路由：跨店铺发布所需的 1688 货源链接管理。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from api.deps import DbSession, OperatorDep, require_permission, require_writable
from core import errors
from services import audit_service, carrier_service

router = APIRouter(prefix="/carrier", tags=["载体链接池"],
                   dependencies=[require_permission("carrier")])


class LinkPayload(BaseModel):
    urls: list[str] = Field(default_factory=list)


@router.get("/links", summary="链接池列表")
def list_links(db: DbSession, limit: int = Query(default=200, ge=1, le=500)) -> dict[str, Any]:
    pool = carrier_service.list_links(db)
    links = pool.get("links") if isinstance(pool.get("links"), list) else []
    rows = [dict(item) for item in links if isinstance(item, dict)]
    usable = [row for row in rows
              if int(row.get("failed") or 0) < carrier_service.MAX_FAILURES]
    return {
        "total": len(rows),
        "usable": len(usable),
        "items": rows[:limit],
        "limit": limit,
    }


@router.post("/links", summary="添加 1688 货源链接")
def add_links(payload: LinkPayload, db: DbSession,
              operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    urls = [str(url or "").strip() for url in payload.urls if str(url or "").strip()]
    if not urls:
        raise errors.validation("请至少提供一条链接", field="urls")
    if len(urls) > 200:
        raise errors.validation("一次最多添加 200 条链接", field="urls")
    # 裸 1688 offer id 也要收：上游同步回来的常常只有 id。
    # 先统一转成商品页再判定，否则能用的 id 会被当成非法链接丢掉。
    normalized = [(url, carrier_service.offer_url(url)) for url in urls]
    invalid = [raw for raw, url in normalized if not carrier_service.valid_source_link(url)]
    candidates = [url for raw, url in normalized if carrier_service.valid_source_link(url)]
    added = carrier_service.add_links(db, candidates)
    db.commit()
    # 链接池是**共享货源**：谁往里塞了 200 条错链，别人的载体重建就全废了。
    # 只记条数不记 URL 列表（200 条地址塞进审计会把那条记录撑成几 KB）。
    audit_service.record(db, operator=operator, action="system.update",
                         target_kind="setting", target_id="carrier",
                         detail={"verb": "carrier.add", "added": added,
                                 "requested": len(urls),
                                 "invalid": len(invalid)})
    db.commit()
    return {
        "added": added,
        "skipped": len(candidates) - added,
        "invalid": invalid,
        "requested": len(urls),
        "total": len(carrier_service.list_links(db).get("links") or []),
    }


@router.post("/links/harvest", summary="从采集箱行里回收 1688 货源链接")
def harvest_links(payload: dict[str, Any], db: DbSession,
                  operator: OperatorDep) -> dict[str, Any]:
    """把公共采集箱里的货源链接塞进池子。

    老版靠这个把「已经采集过的商品」的货源再收集一遍，载体重建才有料。
    请求：`{"rows": [...]}`，每行就是 common_box_list 返回的原始行。
    """
    require_writable(db)
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise errors.validation("rows 必须是非空数组", field="rows")
    added = carrier_service.harvest_from_common_box(db, rows)
    db.commit()
    audit_service.record(db, operator=operator, action="system.update",
                         target_kind="setting", target_id="carrier",
                         detail={"verb": "carrier.harvest", "added": added,
                                 "rows": len(rows)})
    db.commit()
    return {"added": added, "total": len(carrier_service.list_links(db).get("links") or [])}


@router.delete("/links", summary="删除链接池中的链接")
def remove_links(payload: LinkPayload, db: DbSession,
                 operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    urls = [str(url or "").strip() for url in payload.urls if str(url or "").strip()]
    if not urls:
        raise errors.validation("请至少提供一条链接", field="urls")
    removed = carrier_service.remove_links(db, urls)
    db.commit()
    audit_service.record(db, operator=operator, action="system.update",
                         target_kind="setting", target_id="carrier",
                         detail={"verb": "carrier.remove", "removed": removed,
                                 "requested": len(urls)})
    db.commit()
    return {
        "removed": removed,
        "requested": len(urls),
        "total": len(carrier_service.list_links(db).get("links") or []),
    }
