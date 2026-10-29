"""依赖注入：DB 会话、本地 token 校验、只读守卫、当前账号、当前操作员。"""
from __future__ import annotations

import secrets
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from core import errors
from core.config import get_settings
from db.session import get_db as _get_db
from models.account import Account
from models.operator import Operator
from services import account_service, operator_service, setting_service


# --- DB -------------------------------------------------------------------
def get_db() -> Iterator[Session]:
    yield from _get_db()


DbSession = Annotated[Session, Depends(get_db)]


# --- 鉴权 -----------------------------------------------------------------
def _token_from_headers(authorization: str | None, local_token: str | None) -> str:
    if authorization:
        text = authorization.strip()
        if text.lower().startswith("bearer "):
            return text[7:].strip()
        if text:
            return text
    return (local_token or "").strip()


def _same_secret(supplied: str, expected: str) -> bool:
    """恒定时间比较凭据；非法的非 ASCII token 按「不匹配」处理而非 500。"""
    try:
        return secrets.compare_digest(supplied, expected)
    except TypeError:
        return False


def require_token(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    x_local_token: Annotated[str | None, Header(alias="X-Local-Token")] = None,
    db: Session = Depends(get_db),
) -> str:
    """校验访问令牌：Authorization: Bearer 或 X-Local-Token，二者取其一。

    认两种凭据，顺序不能颠倒：
      1. **本机 secret.key** —— 老客户端、单机用户、Electron 主进程都只带它。
         必须先判它，且判定逻辑与 0.1 逐字一致（`compare_digest`），这样
         老版本客户端升级后一个字节都不用改。
      2. **某个操作员的 token** —— 多人共用一台服务器时每人一把。表里查得到
         且未被停用就放行。

    为什么操作员 token 必须在这里也认：`api/__init__.py` 把所有业务路由挂在
    `Depends(require_token)` 这一道门上，`current_operator` 只是门后的「翻译成人」。
    如果这扇门只认 secret.key，那每人一个 token 的设计就是个摆设——发出去的
    token 连第一个业务请求都进不来（旧实现就是这个 bug，多人一用就 401）。

    停用的操作员在这里就拒：`resolve` 不看 enabled，`require_enabled` 才拦，
    否则「停用」只是界面上的一个标记，被停的人照样能改数据。
    """
    supplied = _token_from_headers(authorization, x_local_token)
    if not supplied:
        raise errors.AppError(errors.AUTH_REQUIRED,
                              "缺少访问令牌：请带上 Authorization: Bearer <token>",
                              status=401)
    expected = get_settings().local_secret()
    if _same_secret(supplied, expected):
        if get_settings().shared_mode:
            raise errors.AppError(
                errors.AUTH_REQUIRED,
                "多人共享模式必须使用个人操作员令牌",
                status=401,
            )
        return supplied
    operator = operator_service.resolve(db, supplied)
    if operator is not None:
        operator_service.require_enabled(operator)
        return supplied
    raise errors.AppError(errors.TOKEN_INVALID, "访问令牌不正确", status=401)


# --- 操作员身份 -----------------------------------------------------------
def _authenticate(db: Session, supplied: str) -> Operator:
    """把一串凭据解析成操作员；解析不出就按 401/403 拒。

    解析顺序刻意是「先查表、再认 secret.key」：
      1. 表里有这个 token → 就是那个人（每人一个 token 的正常路径）；
      2. 表里没有，但它等于 `secret.key` → 说明是「本机」操作员还没建出来
         （全新库、或用户删过 data_dir 里的库但留着 secret.key）。
         这里现建一次再解析，老客户端才不会因为「表是空的」被踢回 401。
         这条是 0.2 升级的向后兼容命门，见 operator_service.ensure_default。
      3. 都不是 → 401 TOKEN_INVALID（与 0.1 的判定完全一致）。
    """
    if _same_secret(supplied, get_settings().local_secret()) and get_settings().shared_mode:
        raise errors.AppError(
            errors.AUTH_REQUIRED,
            "多人共享模式必须使用个人操作员令牌",
            status=401,
        )
    operator = operator_service.resolve(db, supplied)
    if operator is None:
        if not _same_secret(supplied, get_settings().local_secret()):
            raise errors.AppError(errors.TOKEN_INVALID, "访问令牌不正确", status=401)
        operator_service.ensure_default(db)
        db.flush()
        operator = operator_service.resolve(db, supplied)
    if operator is None:
        raise errors.AppError(errors.TOKEN_INVALID, "访问令牌不正确", status=401)
    return operator


def _declared_operator(db: Session, declared: str) -> Operator | None:
    """解析 `X-Operator` 头的值。

    **HTTP 头只能是 ASCII**（RFC 7230），所以中文名字没法直接放进去——
    `httpx` 会在构造请求时抛 `UnicodeEncodeError: 'ascii' codec can't encode`。
    这不只是测试的问题：任何人用 curl / Postman 声明「我是张三」都会撞上。

    因此按顺序认三种写法：
      1. **token**（ASCII，最可靠，前端实际用的就是这个）；
      2. **百分号编码的名字**（`X-Operator: %E5%BC%A0%E4%B8%89`），给需要
         用可读名字的调用方；
      3. **ASCII 名字**（`X-Operator: zhangsan`），英文名/拼音直接可用。

    中文名裸放这条路走不通，是协议限制不是实现选择，所以这里主动解码而不是
    让调用方撞 400。
    """
    from urllib.parse import unquote

    text = (declared or "").strip()
    if not text:
        return None
    operator = operator_service.resolve_declared(db, text)
    if operator is not None:
        return operator
    decoded = unquote(text).strip()
    if decoded and decoded != text:
        return operator_service.resolve_declared(db, decoded)
    return None


def _require_credential(db: Session, supplied: str, operator: Operator) -> None:
    """Require a credential belonging to the declared operator.

    The local secret may assert another identity only in single-user mode, where it
    remains the trusted machine credential. Shared mode requires per-operator tokens.
    """
    if not supplied:
        raise errors.AppError(errors.AUTH_REQUIRED,
                              "缺少访问令牌：请带上 Authorization: Bearer <token>",
                              status=401)
    authenticated = _authenticate(db, supplied)
    if _same_secret(supplied, get_settings().local_secret()):
        if get_settings().shared_mode:
            raise errors.AppError(errors.FORBIDDEN,
                                  "多人共享模式不能使用本机令牌切换身份", status=403)
        return
    if authenticated.id != operator.id:
        raise errors.AppError(errors.FORBIDDEN,
                              "操作员身份与访问令牌不匹配", status=403)


def current_operator(
    db: DbSession,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    x_local_token: Annotated[str | None, Header(alias="X-Local-Token")] = None,
    x_operator: Annotated[str | None, Header(alias="X-Operator")] = None,
) -> Operator:
    """当前操作员：把「谁在操作」解析出来，供审计留痕用。

    这个依赖把已验证的凭据翻译成「人」，并确保声明身份与令牌属于同一人：

      · `Authorization: Bearer <某人的 token>` —— 每人一个 token 的正路；
      · `X-Operator` 只作为兼容声明，必须由该操作员自己的令牌证明身份。

    三种历史形态都必须照旧能用（0.2 向后兼容的硬要求）：
      1. 旧客户端只发 `secret.key` → 解析成「本机」，不是 401；
      2. 发某个人的 token → 就是那个人；
      3. 什么都不发 → 401 AUTH_REQUIRED，与 0.1 一模一样。
    """
    auth_token = _token_from_headers(authorization, x_local_token)
    declared = (x_operator or "").strip()

    if not declared and not auth_token:
        raise errors.AppError(errors.AUTH_REQUIRED,
                              "缺少访问令牌：请带上 Authorization: Bearer <token>",
                              status=401)

    if declared:
        operator = _declared_operator(db, declared)
        if operator is None:
            # 声明了一个不存在的名字：宁可明确拒绝，也不要静默记成「本机」——
            # 那样审计里会出现「张三做的事记在本机名下」，比拒绝更糟。
            raise errors.AppError(
                errors.TOKEN_INVALID,
                f"未知的操作员「{declared[:32]}」：请先在操作员页添加，或改用操作员 token",
                status=401)
        _require_credential(db, auth_token, operator)
    else:
        operator = _authenticate(db, auth_token)

    operator_service.require_enabled(operator)
    # 节流到 60 秒一次，且用自己的会话，不影响本请求的事务
    operator_service.touch(db, operator)
    return operator


OperatorDep = Annotated[Operator, Depends(current_operator)]


def require_permission(permission: str):
    """要求当前操作员有某个权限的依赖工厂。

    用法：`@router.post("/publish", dependencies=[require_permission("publish")])`
    挂载在装饰器上可在业务校验与副作用发生前统一 403。
    """
    def checker(operator: OperatorDep) -> Operator:
        return operator_service.require_role(operator, permission)
    # 供路由结构回归测试读取实际权限，避免仅靠装饰器文本审计漏路由。
    setattr(checker, "permission", permission)
    return Depends(checker)


def require_admin(operator: OperatorDep) -> Operator:
    """管理员专属：账号、操作员与全局配置写接口统一走这一入口。"""
    return operator_service.require_role(operator, "admin")


AdminDep = Annotated[Operator, Depends(require_admin)]


def optional_operator(
    db: DbSession,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    x_local_token: Annotated[str | None, Header(alias="X-Local-Token")] = None,
    x_operator: Annotated[str | None, Header(alias="X-Operator")] = None,
) -> Operator | None:
    """「有就解析、没有就算了」的版本，给免鉴权接口（GET /operators）用。

    两种情况必须分清，否则前端的「探活」会误判：

      · **什么都没带** → 返回 None（调用方回落到「本机」）。老客户端、首次
        启动、界面还没选身份，都是这个形态，不能因此 401。
      · **带了凭据但解析不出** → 仍然 401。前端切换身份时就是拿一串 token
        打 GET /operators，靠 401 判定「这串无效」（见 OperatorsPanel 的
        SwitchIdentityDialog）。这里若静默返回 None，界面会把任意乱码当成
        有效 token 存下来，之后每个请求都 401，人却不知道为什么。
    """
    auth_token = _token_from_headers(authorization, x_local_token)
    declared = (x_operator or "").strip()
    if not declared and not auth_token:
        return None
    if declared:
        operator = _declared_operator(db, declared)
        if operator is None:
            raise errors.AppError(
                errors.TOKEN_INVALID,
                f"未知的操作员「{declared[:32]}」：请先在操作员页添加，或改用操作员 token",
                status=401)
        _require_credential(db, auth_token, operator)
    else:
        operator = _authenticate(db, auth_token)
    # 停用的身份也要能读出来（前端要显示「已被停用」徽标并提示去启用），
    # 所以这里**不**调 require_enabled；写操作走 OperatorDep，那里才拦。
    return operator


OptionalOperatorDep = Annotated[Operator | None, Depends(optional_operator)]


# --- 只读守卫 -------------------------------------------------------------
def require_writable(db: Session) -> None:
    if setting_service.is_read_only(db):
        raise errors.read_only()


# --- 当前账号 -------------------------------------------------------------
def optional_account(db: Session) -> Account | None:
    return account_service.current_account(db)


def require_account(db: Session) -> Account:
    account = account_service.current_account(db)
    if account is None:
        raise errors.not_connected()
    return account


def account_by_id(db: Session, account_id: str | None) -> Account:
    return account_service.require_account(db, account_id)


# --- 请求元信息 -----------------------------------------------------------
def request_id_of(request: Request) -> str:
    return getattr(request.state, "request_id", "") or ""


# --- 共享部署：在线客户端心跳 ---------------------------------------------
def track_client(request: Request) -> None:
    """记一次「这台机器在连我」。

    挂在 router 级依赖上而不是中间件：`main.py` 归另一处改动所有，
    而 `api/__init__.py` 是我们能加东西的地方。router 依赖对所有经过它的
    请求都执行，效果与中间件一致，只是不含 404（未匹配路径本来也不算客户端）。

    只记 clientId / IP / X-Client-Version，**不记 Authorization**：
    共享服务器上任何人打开运维页都不该看到别人的 token。
    """
    from services import share_service

    authenticated = bool(
        (request.headers.get("Authorization") or "").strip()
        or (request.headers.get("X-Local-Token") or "").strip()
    )
    share_service.note_client(request, authenticated=authenticated)


def client_version(request: Request) -> str:
    return request.headers.get("X-Client-Version", "")


# --- 共享服务器模式：每个请求自带妙手凭据 ---------------------------------
# 桌面端是单机单账号；共享部署时多个人连同一台服务器，各自填自己的 AppKey，
# 服务器不落库任何人的密钥，只用请求头里的凭据构造一个临时客户端。
def request_credentials(
    x_miaoshou_app_key: Annotated[str | None, Header(alias="X-Miaoshou-AppKey")] = None,
    x_miaoshou_app_secret: Annotated[str | None, Header(alias="X-Miaoshou-AppSecret")] = None,
) -> tuple[str, str] | None:
    key = (x_miaoshou_app_key or "").strip()
    secret = (x_miaoshou_app_secret or "").strip()
    if not key or not secret:
        return None
    return key, secret


# 请求头里的妙手凭据（共享服务器模式）；桌面端单机时为空
CredentialsHeader = Annotated[tuple[str, str] | None, Depends(request_credentials)]
