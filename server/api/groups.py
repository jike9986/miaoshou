"""自定义分组路由。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from api.deps import DbSession, OperatorDep, require_writable
from schemas.requests import GroupCreate, GroupPatch
from services import audit_service, group_service

router = APIRouter(prefix="/groups", tags=["分组"])


@router.get("", summary="分组列表（含商品计数）")
def list_groups(db: DbSession) -> dict[str, Any]:
    return group_service.list_groups(db)


@router.post("", status_code=201, summary="新建分组")
def create_group(payload: GroupCreate, db: DbSession,
                 operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    with audit_service.guard(db, operator=operator, action="warehouse.batch_group",
                             target_kind="group",
                             target_id=str(payload.name or "")[:64]):
        result = group_service.create(db, name=payload.name, color=payload.color,
                                      sort=payload.sort)
    db.commit()
    group = result.get("group") or result
    audit_service.record(db, operator=operator, action="warehouse.batch_group",
                         target_kind="group",
                         target_id=str((group or {}).get("id") or ""),
                         detail={"verb": "create",
                                 "name": str(payload.name or "")[:64]})
    db.commit()
    return result


@router.patch("/{group_id}", summary="改名 / 改颜色 / 改排序")
def patch_group(group_id: str, payload: GroupPatch, db: DbSession,
                operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    body = payload.model_dump(exclude_unset=True, by_alias=False)
    with audit_service.guard(db, operator=operator, action="warehouse.batch_group",
                             target_kind="group", target_id=str(group_id)):
        result = group_service.update(db, group_id, name=body.get("name"),
                                      color=body.get("color"), sort=body.get("sort"))
    db.commit()
    audit_service.record(db, operator=operator, action="warehouse.batch_group",
                         target_kind="group", target_id=str(group_id),
                         detail={"verb": "patch", "fields": sorted(body)})
    db.commit()
    return result


@router.delete("/{group_id}", summary="删除分组（组内商品解除分组）")
def delete_group(group_id: str, db: DbSession,
                 operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    # 删分组会把组内商品全部「解除分组」——商品本身还在，但谁把它们
    # 归到一起的那层信息没了，所以这条要留痕。
    with audit_service.guard(db, operator=operator, action="warehouse.batch_group",
                             target_kind="group", target_id=str(group_id)):
        result = group_service.delete(db, group_id)
    db.commit()
    audit_service.record(db, operator=operator, action="warehouse.batch_group",
                         target_kind="group", target_id=str(group_id),
                         detail={"verb": "delete",
                                 "released": result.get("released") or 0})
    db.commit()
    return result
