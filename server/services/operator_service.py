"""操作员身份与三级角色服务：解析 token → 是谁，以及能做什么。

成员只读，老师处理商品/图片/发布，管理员额外管理操作员、账号和全局配置。
`is_admin` 为旧数据兼容保留，`role_of()` 始终优先识别它，避免老管理员被
新增的 member 默认列降级。

── 向后兼容（改这里之前先读完这段）──────────────────────────────────
0.1 版全实例只有一把 `data_dir/secret.key`，所有客户端都拿它当 Bearer。
0.2 加操作员身份时，**绝不能把旧客户端踢回 401**——用户升级后打开界面
就该继续能用，不该被要求「重新配置身份」，因为界面上根本没有那个地方。

做法是 `ensure_default()`：首次启动建一个叫「本机」的操作员，
它的 token 就是现有 `secret.key` 的值。于是：
  · 旧客户端继续发 `Bearer <secret.key>` → 解析成「本机」，一切照旧；
  · 新客户端发 `X-Operator: 张三` 或某个人的 token → 解析成张三；
  · 什么都不发 → 仍然是 401（与 0.1 一致，不放松）。
换句话说：**旧 token 不失效，只是从此有了名字**。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core import errors
from core.config import get_settings
from core.utils import clean_text, now_ts
from models.operator import Operator

logger = logging.getLogger(__name__)

# X-Operator 可以声明显示名或 token；登录凭据只认 token（见 resolve/resolve_declared）
OPERATOR_HEADER = "X-Operator"

# 默认操作员的名字。老客户端没有身份概念，历史记录里统一记成它。
DEFAULT_OPERATOR_NAME = "本机"

# last_seen_at 的写入节流（秒）。一个批量流程能打上百个请求，
# 每个请求都 UPDATE 一次就是上百次抢写锁，而这个字段的精度要求是「天」级。
TOUCH_INTERVAL_SECONDS = 60.0

# 显示名长度上限，与 models/operator.py 的 String(64) 对齐
NAME_MAX_LENGTH = 64
USERNAME_MAX_LENGTH = 64
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128


# 三级角色定义
ROLE_ADMIN = "admin"
ROLE_TEACHER = "teacher"
ROLE_MEMBER = "member"
VALID_ROLES = {ROLE_ADMIN, ROLE_TEACHER, ROLE_MEMBER}

# 权限语义：成员只读；老师处理商品、图片和发布；管理员额外管理账号、
# 操作员与全局设置。写词库用 words.update，避免成员拿只读 words 权限写库。
ROLE_PERMISSIONS: dict[str, set[str]] = {
    ROLE_ADMIN: {"*"},
    ROLE_TEACHER: {
        "process", "publish", "claim", "product_claim", "batch", "schedule",
        "warehouse", "words", "words.update", "images", "carrier", "account.bind",
    },
    ROLE_MEMBER: {"warehouse", "words", "product_claim", "account.bind"},
}


def normalize_username(value: Any) -> str:
    text = str(value or "").strip()
    normalized = text.casefold()
    if (not normalized or len(normalized) > USERNAME_MAX_LENGTH
            or any(ch.isspace() for ch in normalized)):
        raise errors.validation("用户名长度为 1-64 个字符且不能包含空白", field="username")
    return normalized


def hash_password(password: str) -> str:
    if len(password) < PASSWORD_MIN_LENGTH or len(password) > PASSWORD_MAX_LENGTH:
        raise errors.validation("密码长度必须为 8-128 个字符", field="password")
    if not password.strip():
        raise errors.validation("密码不能全为空白", field="password")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str | None) -> bool:
    if not encoded or len(password) > PASSWORD_MAX_LENGTH:
        return False
    try:
        scheme, salt_hex, digest_hex = encoded.split("$", 2)
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                                n=2**14, r=8, p=1)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, UnicodeError):
        return False


def role_of(operator: Operator) -> str:
    """操作员的角色。老数据没有 role 字段，is_admin=True 的按 admin 处理。

    注意：`role` 列的默认值是 "member"，所以 `operator.role` 永远不为 None。
    必须优先看 `is_admin`：它是老库里唯一可信的权限标记。
    """
    if operator.is_admin:
        return ROLE_ADMIN
    if operator.role and operator.role in VALID_ROLES:
        return operator.role
    return ROLE_MEMBER


def can(operator: Operator, permission: str) -> bool:
    """检查操作员是否有某个权限。"""
    role = role_of(operator)
    perms = ROLE_PERMISSIONS.get(role, set())
    return "*" in perms or permission in perms


def require_role(operator: Operator, permission: str) -> Operator:
    """要求操作员有某个权限，没有就 403。"""
    if not can(operator, permission):
        role = role_of(operator)
        raise errors.AppError(
            "FORBIDDEN",
            f"当前角色（{role}）没有「{permission}」权限，请联系管理员",
            status=403,
            details={"role": role, "permission": permission})
    return operator


# --- 默认操作员 -----------------------------------------------------------
def ensure_default(db: Session) -> Operator:
    """保证存在「本机」操作员，且它的 token 等于现有 secret.key。

    为什么复用 secret.key 而不是生成新 token：
      老用户机器上的 secret.key 已经在用（客户端存着它），换掉等于把每台
      客户端踢回未授权。而界面上没有任何「重新填 token」的入口——用户只能
      删数据目录重来，那会连 355 件商品一起删。用现有 secret 当默认操作员的
      token，老客户端一个字节都不用改，只是从此被记成「本机」。

    幂等：已存在则只做一次「token 与 secret.key 对齐」（用户删过 secret.key
    重生成的情况下，本机操作员的 token 要跟着走，否则旧 token 会永久失效）。
    """
    secret = get_settings().local_secret()
    row = (db.query(Operator)
           .filter(Operator.name == DEFAULT_OPERATOR_NAME).one_or_none())
    if row is None:
        row = Operator(name=DEFAULT_OPERATOR_NAME, token=secret,
                       enabled=True, is_admin=True, created_at=now_ts())
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            # 并发首启（两个进程同时 init_db）：另一个已经建好了，读回来即可
            db.rollback()
            row = (db.query(Operator)
                   .filter(Operator.name == DEFAULT_OPERATOR_NAME).one_or_none())
            if row is None:
                raise
        return row
    if row.token != secret:
        # secret.key 被删过/换过：跟着对齐，否则老客户端会 401
        row.token = secret
        db.flush()
    return row


# --- 解析 -----------------------------------------------------------------
def resolve(db: Session, supplied: str) -> Operator | None:
    """把**凭据**解析成操作员。解析不出返回 None（由调用方决定怎么拒）。

    这里只认 token，绝不认显示名：Authorization: Bearer / X-Local-Token
    都会经过这个函数，若接受名字，任何知道同事名字的人都能直接登录。
    X-Operator 的可读身份声明由 deps._declared_operator 单独解析。
    """
    text = (supplied or "").strip()
    if not text:
        return None
    return db.query(Operator).filter(Operator.token == text).one_or_none()


def resolve_declared(db: Session, supplied: str) -> Operator | None:
    """解析 X-Operator 的身份声明：先按 token，再按显示名。

    显示名只能用于「我是谁」的声明，调用方仍须校验请求凭据，不能拿它登录。
    """
    text = (supplied or "").strip()
    if not text:
        return None
    row = resolve(db, text)
    if row is not None:
        return row
    return db.query(Operator).filter(Operator.name == text).one_or_none()


def require_enabled(operator: Operator | None) -> Operator:
    """停用的操作员等同于没有身份：给出明确原因，别让用户对着 401 猜。"""
    if operator is None:
        raise errors.AppError(errors.TOKEN_INVALID, "访问令牌不正确", status=401)
    if not operator.enabled:
        raise errors.AppError(
            errors.TOKEN_INVALID,
            f"操作员「{operator.name}」已被停用，请联系管理员重新启用或换用其它身份",
            status=403, details={"operatorId": operator.id, "enabled": False})
    return operator


# --- 管理 -----------------------------------------------------------------
def _clean_name(name: Any) -> str:
    text = clean_text(str(name or "")).strip()
    if not text:
        raise errors.validation("操作员名字不能为空", field="name")
    if len(text) > NAME_MAX_LENGTH:
        raise errors.validation(f"操作员名字不能超过 {NAME_MAX_LENGTH} 个字符",
                                field="name", value=len(text))
    return text


def create(db: Session, name: Any, *, is_admin: bool = False,
           role: str | None = None) -> tuple[Operator, str]:
    """新建操作员，返回 (操作员, 明文 token)。

    明文 token **只在这里返回一次**，之后任何接口都不再给出（包括列表）。
    理由：它顺着日志、截图、群聊扩散出去之后，审计就白做了——
    「谁做的」变成「谁都有可能」。
    """
    text = _clean_name(name)
    exists = db.query(Operator).filter(Operator.name == text).one_or_none()
    if exists is not None:
        raise errors.duplicate_name(f"操作员「{text}」已存在", name=text)
    # 角色优先于 is_admin：传了角色就按角色来，没传就按 is_admin 推
    resolved_role = role if role in VALID_ROLES else (ROLE_ADMIN if is_admin else ROLE_MEMBER)
    token = secrets.token_urlsafe(32)
    row = Operator(name=text, token=token, enabled=True,
                   is_admin=(resolved_role == ROLE_ADMIN),
                   role=resolved_role, created_at=now_ts())
    db.add(row)
    db.flush()
    return row, token


def create_student(db: Session, name: Any, username: Any, password: str) -> Operator:
    clean_username = normalize_username(username)
    if db.query(Operator).filter(Operator.username == clean_username).one_or_none():
        raise errors.validation("用户名已存在", field="username")
    row = Operator(
        name=_clean_name(name), username=clean_username,
        password_hash=hash_password(password), token=secrets.token_urlsafe(32),
        enabled=True, is_admin=False, role=ROLE_MEMBER,
        must_change_password=True, created_at=now_ts(),
    )
    db.add(row)
    db.flush()
    return row


def authenticate_password(db: Session, username: Any, password: str) -> tuple[Operator, str] | None:
    try:
        clean_username = normalize_username(username)
    except Exception:
        return None
    row = db.query(Operator).filter(Operator.username == clean_username).one_or_none()
    if row is None or not row.enabled or not verify_password(password, row.password_hash):
        return None
    row.token = secrets.token_urlsafe(32)
    return row, row.token


def set_password(db: Session, row: Operator, password: str, *, must_change: bool,
                 rotate_token: bool = False, username: Any = None) -> None:
    if username is not None:
        clean_username = normalize_username(username)
        clash = (db.query(Operator)
                 .filter(Operator.username == clean_username, Operator.id != row.id)
                 .one_or_none())
        if clash is not None:
            raise errors.validation("用户名已存在", field="username")
        row.username = clean_username
    row.password_hash = hash_password(password)
    row.must_change_password = must_change
    if rotate_token:
        row.token = secrets.token_urlsafe(32)


def list_all(db: Session) -> list[dict[str, Any]]:
    """列表。**不含 token**——这里多一个字段，前面所有努力都白费。"""
    rows = db.query(Operator).order_by(Operator.id.asc()).all()
    return [serialize(row) for row in rows]


def serialize(row: Operator) -> dict[str, Any]:
    """序列化。**不含 token**——这里多一个字段，前面所有努力都白费。

    字段名与前端 `Operator` 类型对齐（admin / lastActiveAt / builtin），
    同时保留 `isAdmin` / `lastSeenAt` 两个同义别名：老调用方按 snake→camel
    直觉取的是后者，改名会静默变成 undefined。
    """
    return {
        "id": str(row.id),
        "name": row.name or "",
        "username": row.username,
        "hasLoginCredentials": bool(row.username and row.password_hash),
        "enabled": bool(row.enabled),
        "admin": bool(row.is_admin),
        "isAdmin": bool(row.is_admin),
        "role": role_of(row),
        "createdAt": row.created_at,
        "lastActiveAt": row.last_seen_at,
        "lastSeenAt": row.last_seen_at,
        # 内置「本机」：它代表「没设置身份时的默认身份」，界面上不该给它
        # 「停用 / 重置」按钮——停掉它等于让所有老客户端 401。
        "builtin": (row.name or "") == DEFAULT_OPERATOR_NAME,
    }


def get(db: Session, operator_id: Any) -> Operator:
    try:
        ident = int(str(operator_id))
    except (TypeError, ValueError):
        raise errors.not_found(f"操作员 {operator_id} 不存在",
                               operatorId=str(operator_id)) from None
    row = db.query(Operator).filter(Operator.id == ident).one_or_none()
    if row is None:
        raise errors.not_found(f"操作员 {operator_id} 不存在", operatorId=str(operator_id))
    return row


def rename(db: Session, operator_id: Any, name: Any) -> dict[str, Any]:
    row = get(db, operator_id)
    text = _clean_name(name)
    clash = (db.query(Operator)
             .filter(Operator.name == text, Operator.id != row.id).one_or_none())
    if clash is not None:
        raise errors.duplicate_name(f"操作员「{text}」已存在", name=text)
    row.name = text
    db.flush()
    # 注意：**不回填历史审计里的 operator_name**。审计表的 operator_name 是
    # 当时的快照，改名之后历史仍应显示当时的名字——否则「谁做的」会被后来的
    # 改名改写，审计就不成立了。
    return serialize(row)


def _require_admin(is_admin_operator: bool, action: str) -> None:
    """只有管理员能执行的操作的统一校验。"""
    if not is_admin_operator:
        raise errors.AppError(
            "FORBIDDEN", f"只有管理员可以{action}", status=403,
            details={"reason": "not_admin"})


def _check_last_admin(db: Session, row: Operator, *, action: str) -> None:
    """不能把最后一个启用中的管理员停用/降级。"""
    remaining = (db.query(Operator)
                 .filter(Operator.is_admin.is_(True),
                         Operator.enabled.is_(True),
                         Operator.id != row.id).count())
    if remaining == 0:
        raise errors.AppError(
            "FORBIDDEN", f"这是最后一个启用中的管理员，{action}后将无人能管理操作员",
            status=409, details={"reason": "last_admin"})


def disable(db: Session, operator_id: Any, *, is_admin_operator: bool) -> dict[str, Any]:
    """停用。不是删除：删了之后历史日志的 operator_id 会变 NULL。

    两道保护，都是挡手滑：
      · 只有管理员能停用别人（普通操作员互相停用会直接打架）；
      · 不能停用自己，也不能停用最后一个启用中的管理员——
        否则没人能再停用/启用任何人，也没人能改共享设置。
    """
    _require_admin(is_admin_operator, "停用操作员")
    row = get(db, operator_id)
    if row.enabled and bool(row.is_admin):
        _check_last_admin(db, row, action="停用")
    row.enabled = False
    db.flush()
    return serialize(row)


def enable(db: Session, operator_id: Any, *, is_admin_operator: bool) -> dict[str, Any]:
    """重新启用（停用的反操作，同一条路由的两个方向）。"""
    _require_admin(is_admin_operator, "启用操作员")
    row = get(db, operator_id)
    row.enabled = True
    db.flush()
    return serialize(row)


def set_role(db: Session, operator_id: Any, role: str, *,
             is_admin_operator: bool) -> dict[str, Any]:
    """改角色。只有管理员能改，且不能把最后一个管理员降成非管理员。"""
    _require_admin(is_admin_operator, "改角色")
    if role not in VALID_ROLES:
        raise errors.validation(f"角色无效：{role}，可选 admin/teacher/member",
                                field="role", value=role)
    row = get(db, operator_id)
    # 不能把最后一个启用中的管理员降成非管理员
    if role != ROLE_ADMIN and bool(row.is_admin) and row.enabled:
        _check_last_admin(db, row, action="降级")
    row.role = role
    row.is_admin = (role == ROLE_ADMIN)
    db.flush()
    return serialize(row)


def rotate(db: Session, operator_id: Any) -> tuple[dict[str, Any], str]:
    """重发 token，旧 token 立即失效（换电脑 / 怀疑泄露）。

    不在这里强制「只有管理员能重发」：运营场景里张三换电脑后让李四帮忙点
    一下是常态，卡这一下没有安全收益（两人本来就能看到同一台机器上的数据）。
    """
    row = get(db, operator_id)
    token = secrets.token_urlsafe(32)
    row.token = token
    db.flush()
    return serialize(row), token


def touch(db: Session, operator: Operator | None) -> None:
    """更新 last_seen_at，节流到 60 秒一次。

    两个刻意的选择：
      · **用自己的短会话**，不污染请求会话。挂在请求会话上的话，这个 UPDATE
        会在下一次 commit 时和业务写入一起提交——业务失败回滚会把「张三来过」
        一起回滚掉（无所谓），但反过来，这个纯统计字段写失败会连累业务提交
        （有所谓）。分开写，互不牵连。
      · **绝不抛异常**：last_seen_at 只是「这人最近还来不来」的参考，
        它写不进去不该让任何请求失败。
    """
    if operator is None or operator.id is None:
        return
    now = now_ts()
    last = operator.last_seen_at or 0.0
    if now - last < TOUCH_INTERVAL_SECONDS:
        return
    try:
        from db.session import session_scope

        with session_scope() as own:
            row = own.query(Operator).filter(Operator.id == operator.id).one_or_none()
            if row is not None:
                row.last_seen_at = now
    except Exception:  # noqa: BLE001 - 统计字段，失败不影响业务
        logger.debug("last_seen_at 更新失败 operator_id=%s", operator.id, exc_info=True)
