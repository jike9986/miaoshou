"""违规词库路由。"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from api.deps import DbSession, OperatorDep, require_permission, require_writable
from core import errors
from schemas.requests import WordRequest
from services import audit_service, word_service

router = APIRouter(prefix="/violation-words", tags=["违规词库"])
# 任务书里的短路径别名（GET/POST /words），与 /violation-words 等价
alias_router = APIRouter(prefix="/words", tags=["违规词库"])


async def _body_of(request: Request) -> dict[str, Any]:
    """把请求体读成 dict。

    为什么不用 FastAPI 的自动绑定：`words` 除了数组还要接受**纯文本**（老版
    粘贴词表的用法），而 WordRequest 把它声明成 list[str]，交给它自动绑定就会
    在进路由之前被判 400/422，服务层根本没机会按分隔符切词。
    """
    try:
        raw = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise errors.AppError(errors.BAD_JSON, "请求体不是合法 JSON",
                              status=400) from None
    if not isinstance(raw, dict):
        raise errors.validation("请求体必须是 JSON 对象")
    return raw


def _payload_of(body: dict[str, Any]) -> dict[str, Any]:
    """校验除 `words` 之外的字段，然后把 `words` 原样放回去。

    单独摘出 words 是为了保留原来对 action / category / mode 的严格校验
    （写错枚举值仍是明确报错），同时让 words 可以是字符串。
    """
    raw_words = body.get("words")
    rest = {key: value for key, value in body.items() if key != "words"}
    try:
        model = WordRequest(**rest)
    except ValidationError as exc:
        first = (exc.errors() or [{}])[0]
        field = ".".join(str(part) for part in (first.get("loc") or []))
        message = str(first.get("msg") or "参数不合法")
        raise errors.validation(f"参数校验失败：{field} {message}".strip(),
                                field=field or None) from None
    data = model.model_dump(by_alias=False, exclude_none=True)
    if raw_words is not None:
        data["words"] = raw_words
    return data


@alias_router.get("", summary="词库列表（/words 别名）")
def list_words_alias(
    db: DbSession,
    action: str | None = None,
    q: str | None = None,
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=200, ge=1, le=500),
) -> dict[str, Any]:
    return word_service.list_words(db, action=action, q=q, page=page, page_size=pageSize)


@alias_router.post("", summary="增删改（/words 别名）",
                   dependencies=[require_permission("words.update")])
async def mutate_words_alias(request: Request, db: DbSession,
                             operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    # 批次运行中改词库会被服务层再挡一次；这里先挡，避免解析完大词表才报错。
    word_service.require_idle()
    body = _payload_of(await _body_of(request))
    result = word_service.apply_action(db, body)
    db.commit()
    _audit_words(db, operator, body, result)
    return result


@router.get("", summary="词库列表（分页 + 计数）")
def list_words(
    db: DbSession,
    action: str | None = None,
    q: str | None = None,
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=200, ge=1, le=500),
) -> dict[str, Any]:
    return word_service.list_words(db, action=action, q=q, page=page, page_size=pageSize)


@router.get("/export", summary="导出完整词库备份",
            dependencies=[require_permission("words.update")])
def export_words(db: DbSession) -> JSONResponse:
    return JSONResponse(
        content=word_service.export_words(db),
        headers={"Content-Disposition": 'attachment; filename="violation-words-backup.json"'},
    )


@router.post("", summary="增删改（add / remove / import / remove-many / clear）",
             dependencies=[require_permission("words.update")])
async def mutate_words(request: Request, db: DbSession,
                       operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    word_service.require_idle()
    body = _payload_of(await _body_of(request))
    with audit_service.guard(db, operator=operator, action="words.update",
                             target_kind="words",
                             target_id=str(body.get("action") or "")):
        result = word_service.apply_action(db, body)
    db.commit()
    _audit_words(db, operator, body, result)
    return result


def _audit_words(db: DbSession, operator: Any, body: dict[str, Any],
                 result: dict[str, Any]) -> None:
    """记一条词库改动。

    为什么按 action 分成 words.create / words.delete：违规词库是会直接影响
    发布结果的共享配置——某天标题全被拦下来时，第一个要查的就是「谁加了这条词」，
    而「加词」和「删词」的责任完全不同。其余动作（import / clear / remove-many）
    归到 words.update，但 detail 里带上原始 action 与词数。
    """
    verb = str(body.get("action") or "").strip()
    words = body.get("words")
    single = body.get("word")
    count = len(words) if isinstance(words, list) else (1 if (words or single) else 0)
    sample = ([str(word)[:32] for word in words[:5]] if isinstance(words, list)
              else ([str(single)[:32]] if single else []))
    action = {"add": "words.create",
              "remove": "words.delete"}.get(verb, "words.update")
    audit_service.record(
        db, operator=operator, action=action, target_kind="words",
        target_id=verb[:64],
        detail={"verb": verb,
                "category": str(body.get("category") or "")[:32],
                "count": count,
                # 只记前 5 条：审计是追溯用的，不是备份词库的
                "sample": sample,
                "result": _slim(result)})
    db.commit()


def _slim(result: Any) -> dict[str, Any]:
    """结果里只留计数类字段，别把整份词库塞进审计。"""
    if not isinstance(result, dict):
        return {}
    return {key: result[key] for key in
            ("added", "removed", "skipped", "total", "counts")
            if key in result and not isinstance(result[key], list)}


@router.post("/building", summary="标记/查询批次运行中（词库写入闸）",
             dependencies=[require_permission("words.update")])
def mark_building(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """置位/读取「有批次在跑」标志。

    GET 语义用同一路由：不带 building 字段就只读当前值。这个开关不是安全边界，
    只是让正在跑的批次和词库编辑互斥；后续把批次线程接到这里即可，不用改调用方。
    """
    body = payload or {}
    if "building" in body:
        word_service.mark_building(body.get("building") is True)
    return {"building": word_service.is_building(),
            "message": "批次运行中，暂不能修改词库，请等本轮结束"}


@router.post("/seed", summary="写入内置词（幂等）",
             dependencies=[require_permission("words.update")])
def seed_words(db: DbSession, operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    added = word_service.seed(db)
    db.commit()
    audit_service.record(db, operator=operator, action="words.update",
                         target_kind="words", target_id="seed",
                         detail={"added": int(added or 0)})
    db.commit()
    return {"added": added, "counts": word_service._counts(db)}


@router.get("/counts", summary="词库计数与上限")
def word_counts(db: DbSession) -> dict[str, Any]:
    return word_service._counts(db)


class WordEnabledRequest(BaseModel):
    enabled: bool
    words: list[str] = Field(default_factory=list)


@router.patch("/enabled", summary="启用/停用词条（不删除）",
              dependencies=[require_permission("words.update")])
def set_word_enabled(payload: WordEnabledRequest, db: DbSession,
                     operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    word_service.require_idle()
    result = word_service.set_enabled(db, enabled=payload.enabled, words=payload.words)
    db.commit()
    # 停用和删除对发布结果的影响一样（词不再生效），但可逆，所以归 update。
    audit_service.record(
        db, operator=operator,
        action="words.update" if not payload.enabled else "words.create",
        target_kind="words", target_id="enabled",
        detail={"enabled": bool(payload.enabled),
                "count": len(payload.words or []),
                "sample": [str(word)[:32] for word in (payload.words or [])[:5]]})
    db.commit()
    return result
