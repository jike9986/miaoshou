"""操作员与审计查询路由。

操作员名单用于记名。老师（包括旧版管理员身份）拥有全局管理能力，
学员只能访问本人范围内的业务数据。

  · `GET  /operators`          —— 谁能被记名（**永不返回 token**）
  · `POST /operators`          —— 加一个人，token 只在这里出现一次
  · `PATCH /operators/{id}`    —— 改名 / 重新启用（`{enabled: true}`）
  · `POST /operators/{id}/disable` —— 停用（老师；不能停自己 / 最后一位老师）
  · `POST /operators/{id}/enable`  —— 重新启用的显式路径（与 PATCH 等价）
  · `POST /operators/{id}/rotate`  —— 重发 token（旧 token 立即失效）
  · `GET  /audit-logs`         —— 谁、什么时候、对什么、做了什么、结果如何

这几个接口本身也留痕：加人 / 停用 / 重发 token 是「会影响别人能不能干活」的
操作，出事时同样要答得出「是谁停的」。

── 为什么 `GET /operators` 不进 `_guarded` ────────────────────────────
它是「我是谁」的探活接口：前端切换身份时拿一串 token 打它，200 说明有效、
401 说明无效（见 OperatorsPanel 的 SwitchIdentityDialog）。如果它挂在
`require_token` 后面，那么带着**别人的** token 来探测会被共享 secret.key
那道门先放行，永远返回 200，「这串 token 有没有效」就判不出来了。
写操作由全局管理权限统一鉴权，并逐个接口留下审计。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import Field

from api.deps import (
    AdminDep,
    DbSession,
    OperatorDep,
    OptionalOperatorDep,
    require_password_change_complete,
    require_permission,
    require_writable,
)
from core import errors
from schemas.common import CamelModel
from services import audit_service, operator_service

router = APIRouter(prefix="/operators", tags=["操作员"])

# 审计查询单独一个 router：它查的是全部写操作，不属于 /operators 的语义。
# 但同样必须带 prefix —— tests/test_smoke.py 里有一条断言「每个 APIRouter
# 都要有 prefix 和 tags」，没有的话 /api/docs 会把它挤进 default 分组。
audit_router = APIRouter(prefix="/audit-logs", tags=["操作员"])


class StudentCreateRequest(CamelModel):
    name: str = Field(min_length=1, max_length=64)
    username: str = Field(max_length=64)
    password: str = Field(min_length=8, max_length=128)


class PasswordResetRequest(CamelModel):
    password: str = Field(min_length=8, max_length=128)
    username: str | None = Field(default=None, max_length=64)


def _require_teacher_or_admin(operator: Any) -> None:
    if operator_service.role_of(operator) not in {
            operator_service.ROLE_ADMIN, operator_service.ROLE_TEACHER}:
        raise errors.AppError("FORBIDDEN", "无权管理学员账号", status=403)


@router.post("/students", status_code=201,
             dependencies=[Depends(require_password_change_complete)])
def create_student(payload: StudentCreateRequest, db: DbSession,
                   operator: OperatorDep) -> dict[str, Any]:
    _require_teacher_or_admin(operator)
    require_writable(db)
    row = operator_service.create_student(
        db, payload.name, payload.username, payload.password)
    db.commit()
    audit_service.record(db, operator=operator, action="operator.student_create",
                         target_kind="operator", target_id=str(row.id),
                         detail={"name": row.name, "role": operator_service.ROLE_MEMBER})
    db.commit()
    return {"operator": operator_service.serialize(row)}


@router.post("/{operator_id}/reset-password",
             dependencies=[Depends(require_password_change_complete)])
def reset_student_password(operator_id: str, payload: PasswordResetRequest,
                           db: DbSession, operator: OperatorDep) -> dict[str, bool]:
    _require_teacher_or_admin(operator)
    target = operator_service.get(db, operator_id)
    if operator_service.role_of(target) != operator_service.ROLE_MEMBER:
        raise errors.AppError("FORBIDDEN", "只能重置学员账号密码", status=403)
    require_writable(db)
    operator_service.set_password(
        db, target, payload.password, must_change=True, rotate_token=True,
        username=payload.username)
    db.commit()
    audit_service.record(db, operator=operator, action="operator.student_reset_password",
                         target_kind="operator", target_id=str(target.id),
                         detail={"role": operator_service.ROLE_MEMBER})
    db.commit()
    return {"ok": True}


class OperatorCreateRequest(CamelModel):
    name: str = ""
    # 旧客户端兼容字段；新界面通过 role 创建老师或学员。
    isAdmin: bool = False
    # 前端字段叫 admin（见 web/src/lib/types.ts 的 OperatorCreateRequest）。
    # 两个都收：老调用方按 isAdmin，界面按 admin。
    admin: bool | None = None
    # admin 是历史兼容值，与 teacher 权限相同；新界面只创建 teacher / member。
    role: str | None = None


class OperatorPatchRequest(CamelModel):
    """改名 / 重新启用 / 改角色。三个字段都可选，但至少要给一个。"""

    name: str | None = None
    # 前端「重新启用」走的就是 PATCH {enabled: true}：后端只有 /disable 一个
    # 动作接口，启用没必要再开一个语义重复的路由（/enable 保留给脚本用）。
    enabled: bool | None = None
    # 改角色：admin / teacher / member
    role: str | None = None


@router.get("", summary="操作员列表（不含 token）")
def list_operators(db: DbSession,
                   operator: OptionalOperatorDep = None) -> dict[str, Any]:
    """名单 + 当前身份。

    `current` 是**这次请求**解析出来的身份：不带任何凭据时它是「本机」
    （`ensure_default` 保证它存在），所以老客户端也能拿到一个合法的 current。
    注意这里对停用的身份不报错——界面要靠它显示「已被停用」徽标。
    """
    current = operator
    if current is None:
        current = operator_service.ensure_default(db)
        db.flush()
    rows = operator_service.list_all(db)
    can_manage_accounts = bool(
        operator is not None
        and operator_service.role_of(operator) in {
            operator_service.ROLE_ADMIN, operator_service.ROLE_TEACHER})
    current_data = operator_service.serialize(current)
    if not can_manage_accounts:
        for row in rows:
            row.pop("username", None)
            row.pop("hasLoginCredentials", None)
        current_data.pop("username", None)
        current_data.pop("hasLoginCredentials", None)
    return {
        # 两个键名都给：前端 types.ts 里是 operators，任务书写的是 items
        "operators": rows,
        "items": rows,
        "total": len(rows),
        "current": current_data,
    }


@router.post("", status_code=201, summary="新增操作员（返回的 token 只出现这一次）")
def create_operator(payload: OperatorCreateRequest, db: DbSession,
                    operator: AdminDep) -> dict[str, Any]:
    require_writable(db)
    is_admin = bool(payload.isAdmin if payload.admin is None else payload.admin)
    role = payload.role if payload.role in operator_service.VALID_ROLES else None
    # 传了角色就按角色来，没传就按 isAdmin 推
    if role is None:
        role = operator_service.ROLE_ADMIN if is_admin else operator_service.ROLE_MEMBER
    # role 优先于旧 isAdmin 字段；admin 只保留为旧客户端兼容的角色值。
    is_admin = role == operator_service.ROLE_ADMIN
    row, token = operator_service.create(db, payload.name, is_admin=is_admin,
                                          role=role)
    db.commit()
    audit_service.record(db, operator=operator, action="operator.create",
                         target_kind="operator", target_id=str(row.id),
                         detail={"name": row.name, "admin": bool(row.is_admin),
                                 "role": role})
    db.commit()
    # token 明文只在这一次响应里出现；之后（包括 GET /operators）永不再给
    return {"operator": operator_service.serialize(row), "token": token}


@router.patch("/{operator_id}", summary="改名 / 重新启用")
def patch_operator(operator_id: str, payload: OperatorPatchRequest,
                   db: DbSession, operator: AdminDep) -> dict[str, Any]:
    require_writable(db)
    if payload.name is None and payload.enabled is None and payload.role is None:
        raise errors.validation("至少提供一个要修改的字段（name / enabled / role）",
                                field="name")
    result: dict[str, Any] = {}
    if payload.role is not None:
        # 改角色：老师可管理，且不能移除最后一位全局管理者
        result = operator_service.set_role(
            db, operator_id, payload.role,
            is_admin_operator=operator_service.can(operator, "admin"))
        db.commit()
        audit_service.record(db, operator=operator, action="operator.set_role",
                             target_kind="operator", target_id=str(operator_id),
                             detail={"name": result["name"], "role": payload.role})
        db.commit()
    if payload.name is not None:
        row = operator_service.get(db, operator_id)
        old_name = row.name
        result = operator_service.rename(db, operator_id, payload.name)
        db.commit()
        # 历史审计里的 operator_name 是快照，刻意不回填（见 operator_service.rename）
        audit_service.record(db, operator=operator, action="operator.rename",
                             target_kind="operator", target_id=str(operator_id),
                             detail={"from": old_name, "to": result["name"]})
        db.commit()
    if payload.enabled is not None:
        if payload.enabled:
            result = operator_service.enable(
                db, operator_id,
                is_admin_operator=operator_service.can(operator, "admin"))
            db.commit()
            audit_service.record(db, operator=operator, action="operator.enable",
                                 target_kind="operator", target_id=str(operator_id),
                                 detail={"name": result["name"]})
            db.commit()
        else:
            # PATCH {enabled:false} 与 /disable 走同一条校验（含「不能停自己」
            # 与「不能停最后一个老师」），不另开一套宽松路径。
            result = _disable(db, operator_id, operator)
    return {"operator": result}


def _disable(db: DbSession, operator_id: str, operator: Any) -> dict[str, Any]:
    """停用的公共实现：PATCH 与 POST /disable 共用，保证两条路一样严。"""
    if str(operator.id) == str(operator_id):
        # 停自己 = 立刻把自己锁在门外，而界面上没有任何补救入口。挡的是手滑。
        raise errors.forbidden("不能停用自己的账号", reason="self")
    result = operator_service.disable(
        db, operator_id,
        is_admin_operator=operator_service.can(operator, "admin"))
    db.commit()
    audit_service.record(db, operator=operator, action="operator.disable",
                         target_kind="operator", target_id=str(operator_id),
                         detail={"name": result["name"]})
    db.commit()
    return result


@router.post("/{operator_id}/disable", summary="停用（老师；不能停自己/最后一位老师）")
def disable_operator(operator_id: str, db: DbSession,
                     operator: AdminDep) -> dict[str, Any]:
    require_writable(db)
    return {"operator": _disable(db, operator_id, operator)}


@router.post("/{operator_id}/enable", summary="重新启用（老师）")
def enable_operator(operator_id: str, db: DbSession,
                    operator: AdminDep) -> dict[str, Any]:
    require_writable(db)
    result = operator_service.enable(
        db, operator_id, is_admin_operator=operator_service.can(operator, "admin"))
    db.commit()
    audit_service.record(db, operator=operator, action="operator.enable",
                         target_kind="operator", target_id=str(operator_id),
                         detail={"name": result["name"]})
    db.commit()
    return {"operator": result}


@router.post("/{operator_id}/rotate", summary="重发 token（旧 token 立即失效）")
def rotate_operator(operator_id: str, db: DbSession,
                    operator: AdminDep) -> dict[str, Any]:
    require_writable(db)
    result, token = operator_service.rotate(db, operator_id)
    db.commit()
    # detail 里**不记 token**：审计表是给全员看的，塞进去等于公开它
    audit_service.record(db, operator=operator, action="operator.rotate",
                         target_kind="operator", target_id=str(operator_id),
                         detail={"name": result["name"]})
    db.commit()
    return {"operator": result, "token": token}


@audit_router.get("", summary="审计流水（谁做的、什么时候、结果）",
                  dependencies=[require_permission("admin")])
def list_audit_logs(
    db: DbSession,
    operator: OperatorDep,
    operatorId: str | None = None,
    action: str | None = None,
    targetKind: str | None = None,
    targetId: str | None = None,
    result: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    before: float | None = None,
) -> dict[str, Any]:
    """按 created_at 倒序，游标分页。

    `before` 传上一页响应里的 `nextBefore`：审计表只增不减，用 offset 翻页
    会漏行/重行（并发写入时偏移会漂移），而审计漏一条就是一次追溯失败。
    """
    return audit_service.query(
        db, operator_id=operatorId, action=action, target_kind=targetKind,
        target_id=targetId, result=result, limit=limit, before=before)
