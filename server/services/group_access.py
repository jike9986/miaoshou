"""受限分组：只有老师 / 管理员能看、能发、能排进定时任务。

负责人 2026-11-19 明确：
  「成品仓库的这三个分组里面的鞋不能随便给用户上都是精品，其他的可以批量上，
    学员账号都看不到这三个分组也上不了这三个分组的鞋子，
    新建定时任务里面也不能选择这些鞋」

背景（为什么这三个组要单独关起来）：
  这三个组的成品走**合作厂家**，不走采购。所以拿货价是标高的
  （不是真实采购成本），售价按正常利润算下来反而低于这个标高价。
  实测生产库：这三组 18 件商品 **100% 倒挂**（售价 < 拿货价）。
  这属于正常业务结构 —— 一旦让学员按这个价发出去，看起来就是「卖一件亏一件」，
  而且合作厂家的货不能走普通学员的铺货渠道。

实现要点：
  · 判定集中在 `is_restricted_group` / `restricted_group_ids`，
    任何地方要判断都调这里，不要各自写死 group_id。
  · 列表查询在 SQL 层过滤（学员根本查不到这些行），
    接口层在写入/发布/定时任务处二次校验（防直接调 API 绕过前端）。
  · 老师与管理员等价（ROLE_PERMISSIONS 里两者都是 {"*"}）。
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from core import errors
from models.group import Group
from models.operator import Operator
from services.operator_service import ROLE_ADMIN, ROLE_MEMBER, ROLE_TEACHER, role_of

# 受限分组名（负责人 2026-11-19 指认，界面上这三组共 18 件成品）
RESTRICTED_GROUP_NAMES: tuple[str, ...] = ("自营海外仓", "海外仓定制款", "精品")

# 兜底：名字匹配不上时用的分组 id（生产库实测 id=1/2/3 就是这三组）。
# 按 id 兜底是为了兼容分组被改名的情况 —— 改名不该意外放开权限。
RESTRICTED_GROUP_IDS: frozenset[int] = frozenset({1, 2, 3})


def _normalize(name: Any) -> str:
    """分组名归一：去空白、去全角空格，便于稳定比对。"""
    return re.sub(r"[\s\u3000]+", "", str(name or ""))


def restricted_group_ids(db: Session) -> set[int]:
    """当前库里属于「受限分组」的 id 集合（名字命中或 id 兜底）。"""
    wanted = {_normalize(name) for name in RESTRICTED_GROUP_NAMES}
    found: set[int] = set()
    for group in db.query(Group).all():
        if _normalize(group.name) in wanted or int(group.id) in RESTRICTED_GROUP_IDS:
            found.add(int(group.id))
    # 即使库里还没有这些分组，也把兜底 id 带上：新建同 id 分组不会漏权限
    return found or set(RESTRICTED_GROUP_IDS)


def is_restricted_group(db: Session, group_id: Any) -> bool:
    """这个分组是不是受限分组。group_id 为空时返回 False（未分组不算受限）。"""
    if group_id in (None, ""):
        return False
    try:
        value = int(str(group_id).strip())
    except (TypeError, ValueError):
        return False
    return value in restricted_group_ids(db)


def may_access_restricted(operator: Operator | None) -> bool:
    """当前操作员能不能碰受限分组。

    老师和管理员可以；学员（member）和未登录一律不行。
    """
    if operator is None:
        return False
    if not getattr(operator, "enabled", True):
        return False
    return role_of(operator) in {ROLE_ADMIN, ROLE_TEACHER}


def visible_group_ids(db: Session, operator: Operator | None) -> set[int] | None:
    """该操作员能看见的受限分组 id；None 表示不受限（能看全部）。

    老师/管理员 → None（不加过滤）
    学员        → 受限 id 集合（SQL 里 `Product.group_id.notin_(...)`）
    """
    if may_access_restricted(operator):
        return None
    return restricted_group_ids(db)


def guard_group(db: Session, operator: Operator | None, group_id: Any, *,
                action: str = "使用") -> None:
    """写入/发布/排任务前的闸门：学员碰受限分组直接 403。

    这是**接口层**的拦截，不能只靠前端藏起来 ——
    绕过前端直接调 API 是最常见的越权路径。
    """
    if not is_restricted_group(db, group_id):
        return
    if may_access_restricted(operator):
        return
    raise errors.AppError(
        errors.FORBIDDEN,
        f"这个分组是受限精品组，学员不能{action}，请联系老师处理",
        status=403, details={"groupId": str(group_id),
                            "groups": list(RESTRICTED_GROUP_NAMES)})


def guard_products(db: Session, operator: Operator | None,
                   product_group_ids: Any, *, action: str = "使用") -> None:
    """批量闸门：任何一件落在受限分组就整体拒绝，并在错误里点名是哪几件。"""
    if may_access_restricted(operator):
        return
    restricted = restricted_group_ids(db)
    if not restricted:
        return
    blocked: list[str] = []
    for group_id in product_group_ids:
        if group_id in (None, ""):
            continue
        try:
            value = int(str(group_id).strip())
        except (TypeError, ValueError):
            continue
        if value in restricted:
            blocked.append(str(value))
    if not blocked:
        return
    raise errors.AppError(
        errors.FORBIDDEN,
        f"这批商品里有 {len(set(blocked))} 个受限精品分组，学员不能{action}，"
        "请联系老师处理",
        status=403, details={"groupId": sorted(set(blocked)),
                            "groups": list(RESTRICTED_GROUP_NAMES)})


def filter_group_options(groups: list[dict[str, Any]],
                         db: Session, operator: Operator | None
                         ) -> list[dict[str, Any]]:
    """给前端的分组筛选栏也做过滤：学员看不到受限分组这个选项。

    留着选项但列表永远空，用户只会以为系统坏了 —— 直接不显示更清楚。
    """
    if may_access_restricted(operator):
        return groups
    restricted = restricted_group_ids(db)
    kept = []
    for group in groups:
        try:
            gid = int(str(group.get("id") or group.get("groupId") or "").strip())
        except (TypeError, ValueError):
            kept.append(group)
            continue
        if gid in restricted:
            continue
        kept.append(group)
    return kept


__all__ = [
    "RESTRICTED_GROUP_NAMES",
    "RESTRICTED_GROUP_IDS",
    "filter_group_options",
    "guard_group",
    "guard_products",
    "is_restricted_group",
    "may_access_restricted",
    "restricted_group_ids",
    "visible_group_ids",
    "ROLE_MEMBER",
]
