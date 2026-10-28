"""自定义分组 CRUD。分组名唯一（大小写不敏感）；删除分组不删商品。"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from core import errors
from core.constants import GROUP_COLOR_DEFAULT, GROUP_COLORS
from core.utils import clean_text, now_ts
from models.group import Group
from models.product import Product


def _counts(db: Session) -> dict[int, int]:
    from services import warehouse_service

    return warehouse_service.group_counts(db)


def serialize(row: Group, *, count: int = 0) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "name": row.name,
        "color": row.color or GROUP_COLOR_DEFAULT,
        "sort": int(row.sort or 0),
        "count": count,
        "createdAt": row.created_at,
        "updatedAt": row.updated_at,
    }


def list_groups(db: Session) -> dict[str, Any]:
    counts = _counts(db)
    rows = db.query(Group).order_by(Group.sort.asc(), Group.id.asc()).all()
    items = [serialize(row, count=counts.get(row.id, 0)) for row in rows]
    return {"groups": items, "items": items, "total": len(items)}


def get_group(db: Session, group_id: Any) -> Group:
    text = str(group_id or "").strip()
    if not text.isdigit():
        raise errors.validation("groupId 必须是数字", field="id", value=group_id)
    row = db.query(Group).filter(Group.id == int(text)).one_or_none()
    if row is None:
        raise errors.not_found(f"分组 {text} 不存在", groupId=text)
    return row


def _validate_name(db: Session, name: Any, *, exclude_id: int | None = None) -> str:
    text = clean_text(name, 64)
    if not text:
        raise errors.validation("分组名不能为空", field="name")
    if len(text) > 64:
        raise errors.validation("分组名不能超过 64 个字符", field="name", value=len(text))
    wanted = text.casefold()
    for row in db.query(Group).all():
        if exclude_id is not None and row.id == exclude_id:
            continue
        if (row.name or "").casefold() == wanted:
            raise errors.duplicate_name(f"分组「{text}」已存在", name=text)
    return text


def _validate_color(color: Any) -> str:
    key = clean_text(color, 16) or GROUP_COLOR_DEFAULT
    if key not in GROUP_COLORS:
        raise errors.validation(
            "颜色只能是 " + " / ".join(GROUP_COLORS), field="color", value=key)
    return key


def create(db: Session, *, name: Any, color: Any = None, sort: Any = None) -> dict[str, Any]:
    text = _validate_name(db, name)
    palette = _validate_color(color)
    if sort in (None, ""):
        current = db.query(Group).order_by(Group.sort.desc()).first()
        order = int(current.sort or 0) + 1 if current is not None else 0
    else:
        try:
            order = int(sort)
        except (TypeError, ValueError):
            raise errors.validation("排序必须是整数", field="sort", value=sort) from None
        if not 0 <= order <= 999:
            raise errors.validation("排序必须在 0 到 999 之间", field="sort", value=order)
    row = Group(name=text, color=palette, sort=order,
                created_at=now_ts(), updated_at=now_ts())
    db.add(row)
    db.flush()
    return {"group": serialize(row, count=0), "groups": list_groups(db)["groups"]}


def update(db: Session, group_id: Any, *, name: Any = None, color: Any = None,
           sort: Any = None) -> dict[str, Any]:
    row = get_group(db, group_id)
    if name is None and color is None and sort is None:
        raise errors.validation("至少提供一个要修改的字段（name / color / sort）")
    if name is not None:
        text = _validate_name(db, name, exclude_id=row.id)
        if text != row.name:
            row.name = text
            # 商品上的 group_name 是展示缓存，名字改了要一起改
            db.query(Product).filter(Product.group_id == row.id).update(
                {Product.group_name: text}, synchronize_session=False)
            db.flush()
    if color is not None:
        row.color = _validate_color(color)
    if sort is not None:
        try:
            order = int(sort)
        except (TypeError, ValueError):
            raise errors.validation("排序必须是整数", field="sort", value=sort) from None
        if not 0 <= order <= 999:
            raise errors.validation("排序必须在 0 到 999 之间", field="sort", value=order)
        row.sort = order
    row.updated_at = now_ts()
    db.flush()
    counts = _counts(db)
    return {"group": serialize(row, count=counts.get(row.id, 0)),
            "groups": list_groups(db)["groups"]}


def delete(db: Session, group_id: Any) -> dict[str, Any]:
    row = get_group(db, group_id)
    affected = db.query(Product).filter(Product.group_id == row.id).update(
        {Product.group_id: None, Product.group_name: ""}, synchronize_session=False)
    gid = str(row.id)
    db.delete(row)
    db.flush()
    return {
        "deleted": True,
        "id": gid,
        "productsUnassigned": int(affected or 0),
        "groups": list_groups(db)["groups"],
    }


def ensure_default(db: Session, name: str, color: str = GROUP_COLOR_DEFAULT) -> Group:
    """按名字取分组，不存在则创建（批量改分组时用）。"""
    text = clean_text(name, 64)
    if not text:
        raise errors.validation("分组名不能为空", field="group")
    for row in db.query(Group).all():
        if (row.name or "").casefold() == text.casefold():
            return row
    current = db.query(Group).order_by(Group.sort.desc()).first()
    order = int(current.sort or 0) + 1 if current is not None else 0
    row = Group(name=text, color=_validate_color(color) if color else GROUP_COLOR_DEFAULT,
                sort=order, created_at=now_ts(), updated_at=now_ts())
    db.add(row)
    db.flush()
    return row
