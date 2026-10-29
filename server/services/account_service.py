"""账号与店铺：连接、状态、店铺缓存、按账号取客户端。

凭据处理铁律：
  · AppSecret 只用 services.crypto 加密后进 accounts.app_secret_enc；
  · 任何响应只返回 appIdHint / secretHint（末 4 位）；
  · 上游错误文本一律经 core.utils.redact 脱敏。
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from core import errors
from core.constants import SITE_LANGUAGES, SITE_NAMES
from core.utils import clean_text, now_ts, redact
from models.account import Account
from models.shop import Shop
from services import crypto
from services.miaoshou import MiaoshouClient, MiaoshouError


def request_operator(db: Session) -> tuple[int, str] | None:
    value = db.info.get("account_request_operator")
    if (isinstance(value, tuple) and len(value) == 2
            and isinstance(value[0], int) and isinstance(value[1], str)):
        return value
    return None


def _assert_account_access(db: Session, account: Account) -> None:
    context = request_operator(db)
    if context is None or context[1] != "member":
        return
    if account.owner_operator_id != context[0]:
        raise errors.AppError(errors.FORBIDDEN, "无权访问其他学员的妙手账号", status=403)


# --- 读 -------------------------------------------------------------------
def list_accounts(db: Session) -> list[Account]:
    return db.query(Account).order_by(Account.id.asc()).all()


def get_account(db: Session, account_id: Any) -> Account | None:
    if account_id in (None, ""):
        return None
    try:
        ident = int(str(account_id))
    except (TypeError, ValueError):
        return None
    row = db.query(Account).filter(Account.id == ident).one_or_none()
    if row is not None:
        _assert_account_access(db, row)
    return row


def require_account(db: Session, account_id: Any = None) -> Account:
    """按 id 取账号；不传则取当前连接（default → 最近使用 → 最新）。"""
    if account_id not in (None, ""):
        account = get_account(db, account_id)
        if account is None:
            raise errors.not_found(f"账号 {account_id} 不存在", accountId=str(account_id))
        return account
    context = request_operator(db)
    if context is not None and context[1] == "member":
        account = (db.query(Account).filter(
            Account.owner_operator_id == context[0], Account.status == "active")
            .order_by(Account.last_used.desc().nullslast(), Account.id.desc()).first())
        if account is None:
            raise errors.not_connected()
        return account
    current = current_account(db)
    if current is None:
        raise errors.not_connected()
    return current


def current_account(db: Session) -> Account | None:
    context = request_operator(db)
    if context is not None and context[1] == "member":
        return (db.query(Account).filter(
            Account.owner_operator_id == context[0], Account.status == "active")
            .order_by(Account.last_used.desc().nullslast(), Account.id.desc()).first())
    default = (db.query(Account).filter(Account.is_default.is_(True))
               .order_by(Account.id.desc()).first())
    if default is not None:
        return default
    return (db.query(Account).filter(Account.status == "active")
            .order_by(Account.last_used.desc().nullslast(), Account.id.desc()).first())


def account_client(account: Account) -> MiaoshouClient:
    """用账号自己的凭据构造客户端。密钥解不开说明本机 secret 变了。"""
    if getattr(account, "id", None) == 0:
        # 共享服务器模式下由请求头带进来的明文密钥，不经过本地加密
        return MiaoshouClient(account.app_key or "", account.app_secret_enc or "")
    try:
        secret = crypto.decrypt(account.app_secret_enc or "")
    except crypto.CredentialError as exc:
        raise errors.AppError(
            errors.ACCOUNT_SECRET_UNREADABLE,
            f"账号「{account.name}」的凭据无法解密：{exc}",
            status=409,
        ) from None
    if not secret:
        raise errors.AppError(
            errors.ACCOUNT_SECRET_UNREADABLE,
            f"账号「{account.name}」的凭据无法解密，请重新连接该账号",
            status=409,
        )
    return MiaoshouClient(account.app_key or "", secret)


def client_for(db: Session, account_id: Any = None) -> tuple[Account, MiaoshouClient]:
    account = require_account(db, account_id)
    return account, account_client(account)


# --- 店铺 -----------------------------------------------------------------
def shop_rows(response: dict[str, Any]) -> list[dict[str, Any]]:
    """把 shop_list 的返回整理成统一店铺结构（键名带回退链）。"""
    data = response.get("data") or {}
    rows = data.get("shopList") or data.get("list") or data.get("records") or []
    shops: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        shop_id = str(row.get("shopId") or row.get("id") or "").strip()
        if not shop_id:
            continue
        site = str(row.get("site") or row.get("siteCode") or "").strip().upper()
        shops.append({
            "shopId": shop_id,
            "name": str(row.get("platformShopName") or row.get("shopName")
                        or row.get("shopNick") or row.get("name") or shop_id),
            "site": site,
            "siteName": str(row.get("siteName") or SITE_NAMES.get(site, site)),
            "languages": list(SITE_LANGUAGES.get(site, [])),
            "status": str(row.get("status") or ""),
        })
    return shops


def stash_shops(account: Account, shops: list[dict[str, Any]]) -> None:
    """店铺缓存写进 accounts.shops（离线展示用）。"""
    stamp = now_ts()
    account.shops = json.dumps(shops, ensure_ascii=False)
    account.shops_synced_at = stamp
    account.last_verified_at = stamp


def _with_site_fallback(shops: list[dict[str, Any]],
                        fallback_site: str) -> list[dict[str, Any]]:
    """店铺站点兜底：上游缺 site 时用账号级站点；仍缺则标记 siteKnown=False。"""
    fallback = str(fallback_site or "").strip().upper()
    rows: list[dict[str, Any]] = []
    for shop in shops:
        row = dict(shop)
        site = str(row.get("site") or "").strip().upper() or fallback
        row["site"] = site
        if not row.get("siteName"):
            row["siteName"] = SITE_NAMES.get(site, site)
        if not row.get("languages"):
            row["languages"] = list(SITE_LANGUAGES.get(site, []))
        row["siteKnown"] = bool(site)
        rows.append(row)
    return rows


def cached_shops(account: Account) -> list[dict[str, Any]]:
    try:
        data = json.loads(account.shops or "[]")
    except (ValueError, TypeError):
        return []
    rows: list[dict[str, Any]] = []
    for row in data if isinstance(data, list) else []:
        if not isinstance(row, dict):
            continue
        shop = dict(row)
        shop["syncedAt"] = account.shops_synced_at
        rows.append(shop)
    return _with_site_fallback(rows, str(getattr(account, "site", "") or ""))


def fetch_shops(db: Session, account: Account, *, refresh: bool = False
                ) -> tuple[list[dict[str, Any]], bool]:
    """取店铺列表；返回 (店铺, degraded)。upstream 失败时降级到缓存。"""
    if not refresh:
        cached = cached_shops(account)
        if cached:
            return cached, False
    client = account_client(account)
    try:
        response = client.shop_list(page_no=1, page_size=100)
    except MiaoshouError as exc:
        cached = cached_shops(account)
        if cached:
            return cached, True
        raise map_upstream_error(exc) from None
    finally:
        client.close()
    shops = shop_rows(response)
    shops = _with_site_fallback(shops, str(getattr(account, "site", "") or ""))
    stamp = now_ts()
    for shop in shops:
        shop["syncedAt"] = stamp
    stash_shops(account, shops)
    sync_shop_table(db, account, shops)
    db.flush()
    return shops, False


def sync_shop_table(db: Session, account: Account, shops: list[dict[str, Any]]) -> None:
    """shops 表按 (account_id, shop_id) upsert。"""
    if account.id is None:
        return
    existing = {row.shop_id: row for row in
                db.query(Shop).filter(Shop.account_id == account.id).all()}
    seen: set[str] = set()
    for shop in shops:
        shop_id = str(shop.get("shopId") or "")
        if not shop_id:
            continue
        seen.add(shop_id)
        row = existing.get(shop_id)
        if row is None:
            row = Shop(account_id=account.id, shop_id=shop_id)
            db.add(row)
        row.name = str(shop.get("name") or "")
        row.site = str(shop.get("site") or "")
        row.site_name = str(shop.get("siteName") or "")
        row.status = str(shop.get("status") or "")
        row.synced_at = now_ts()
    for shop_id, row in existing.items():
        if shop_id not in seen:
            db.delete(row)


def shop_in_account(db: Session, account: Account, shop_id: Any) -> dict[str, Any] | None:
    """校验店铺属于该账号：先看缓存，缓存为空再回源一次。"""
    wanted = str(shop_id or "").strip()
    if not wanted:
        return None
    for shop in cached_shops(account):
        if str(shop.get("shopId")) == wanted:
            return shop
    try:
        shops, _ = fetch_shops(db, account, refresh=True)
    except errors.AppError:
        return None
    for shop in shops:
        if str(shop.get("shopId")) == wanted:
            return shop
    return None


def require_shop(db: Session, account: Account, shop_id: Any) -> dict[str, Any]:
    """校验店铺属于该账号，不属于就把「现有店铺」一起报出来。

    只报「不属于」用户没法自查——他不知道这个账号现在到底有哪些店，
    只能去妙手后台一条条对。老版就是带上现有清单的。
    """
    shop = shop_in_account(db, account, shop_id)
    if shop is None:
        known = [str(row.get("name") or row.get("shopId") or "")
                 for row in cached_shops(account)]
        known_text = "、".join(name for name in known if name) or "（这个账号下没有店铺）"
        raise errors.AppError(
            errors.SHOP_NOT_IN_ACCOUNT,
            f"店铺 {shop_id} 不在账号「{account.name}」下，现有：{known_text}",
            status=422,
            details={"shopId": str(shop_id or ""), "accountId": str(account.id),
                     "availableShops": known},
        )
    return shop


# --- 连接 / 断开 ----------------------------------------------------------
def hint_name(app_id: str) -> str:
    tail = str(app_id or "")[-4:]
    return f"妙手{tail}" if tail else "未命名连接"


def name_taken(db: Session, name: str, *, exclude_id: int | None = None) -> bool:
    wanted = clean_text(name).casefold()
    if not wanted:
        return False
    for row in db.query(Account).all():
        if exclude_id is not None and row.id == exclude_id:
            continue
        if clean_text(row.name).casefold() == wanted:
            return True
    return False


def connect(db: Session, *, app_id: str | None, app_secret: str | None,
            account_id: Any = None, name: str | None = None, save: bool = True,
            auto_scan: bool = True, owner_operator_id: int | None = None,
            bound_by_operator_id: int | None = None) -> dict[str, Any]:
    """连接妙手账号：验凭据（shop_list）→ 加密存账号 → 可选预取采集箱首屏。"""
    existing: Account | None = None
    if account_id not in (None, ""):
        existing = get_account(db, account_id)
        if existing is None:
            raise errors.not_found(f"账号 {account_id} 不存在", accountId=str(account_id))
        app_id = existing.app_key
        try:
            secret = crypto.decrypt(existing.app_secret_enc or "")
        except crypto.CredentialError as exc:
            raise errors.AppError(
                errors.ACCOUNT_SECRET_UNREADABLE,
                f"账号「{existing.name}」的凭据无法解密：{exc}",
                status=409,
            ) from None
        if not secret:
            raise errors.AppError(
                errors.ACCOUNT_SECRET_UNREADABLE,
                f"账号「{existing.name}」的凭据无法解密，请重新填写 AppSecret",
                status=409,
            )
        app_secret = secret
    else:
        app_id = clean_text(app_id)
        app_secret = (app_secret or "").strip() if isinstance(app_secret, str) else ""
        if not app_id:
            raise errors.validation("APP ID 不能为空", field="appId")
        if len(app_id) > 256:
            raise errors.validation("APP ID 长度不能超过 256", field="appId")
        if not app_secret:
            raise errors.validation("App Secret 不能为空", field="appSecret")
        if len(app_secret) > 512:
            raise errors.validation("App Secret 长度不能超过 512", field="appSecret")

    label = clean_text(name, 60)
    if label and name_taken(db, label, exclude_id=existing.id if existing else None):
        raise errors.unprocessable(f"连接名「{label}」已被使用", field="name")
    if not label:
        label = existing.name if existing else hint_name(app_id or "")

    client = MiaoshouClient(app_id or "", app_secret or "")
    try:
        response = client.shop_list(page_no=1, page_size=100)
    except MiaoshouError as exc:
        if existing is not None:
            existing.status = "invalid"
            db.flush()
        raise map_upstream_error(exc, secrets=(app_id or "", app_secret or "")) from None
    finally:
        client.close()

    shops = shop_rows(response)
    site = ""
    for shop in shops:
        if shop.get("site"):
            site = str(shop["site"])
            break
    stamp = now_ts()
    for shop in shops:
        shop["syncedAt"] = stamp

    account = existing
    if account is None:
        account = Account(app_key=app_id or "", created_at=stamp)
        db.add(account)
    account.name = label
    account.app_key = app_id or ""
    account.app_secret_enc = crypto.encrypt(app_secret or "")
    key_hint, secret_hint = crypto.hints(app_id or "", app_secret or "")
    account.app_id_hint = key_hint
    account.secret_hint = secret_hint
    account.site = site or account.site or "MY"
    account.status = "active"
    if owner_operator_id is not None:
        account.owner_operator_id = owner_operator_id
        account.bound_by_operator_id = bound_by_operator_id
        account.bound_at = stamp
    account.last_verified_at = stamp
    account.last_used = stamp
    account.shops = json.dumps(shops, ensure_ascii=False)
    account.shops_synced_at = stamp
    if existing is None:
        account.is_default = db.query(Account).count() == 0
    db.flush()
    sync_shop_table(db, account, shops)

    scan: dict[str, Any] = {}
    if auto_scan:
        client = MiaoshouClient(app_id or "", app_secret or "")
        try:
            listing = client.collect_box_list(status="notPublished", page_no=1, page_size=20)
            data = listing.get("data") or {}
            rows = data.get("detailList")
            if not isinstance(rows, list):
                raise errors.upstream("采集箱返回结构异常", reason="detailList_missing")
            scan = {"available": len(rows), "total": int(data.get("total") or len(rows))}
        except MiaoshouError as exc:
            raise map_upstream_error(exc, secrets=(app_id or "", app_secret or "")) from None
        finally:
            client.close()

    db.flush()
    return {
        "connected": True,
        "accountId": str(account.id),
        "ownerOperatorId": (str(account.owner_operator_id)
                             if account.owner_operator_id else None),
        "boundByOperatorId": (str(account.bound_by_operator_id)
                               if account.bound_by_operator_id else None),
        "boundAt": account.bound_at,
        "connectionName": account.name,
        "appIdHint": account.app_id_hint,
        "secretHint": account.secret_hint,
        "site": account.site,
        "shopsCount": len(shops),
        "autoScan": bool(auto_scan),
        "lastVerifiedAt": account.last_verified_at,
        "shops": shops,
        "scan": scan,
    }


def disconnect(db: Session, account_id: Any = None) -> dict[str, Any]:
    """断开当前连接（不删除账号，除非指定 deleteAccount=true）。"""
    account = current_account(db) if account_id in (None, "") else get_account(db, account_id)
    if account is None:
        return {"connected": False, "accountId": None, "accounts": account_summaries(db)}
    account.is_default = False
    db.flush()
    nxt = (db.query(Account).filter(Account.id != account.id)
           .order_by(Account.id.desc()).first())
    if nxt is not None:
        nxt.is_default = True
        db.flush()
    return {"connected": nxt is not None,
            "accountId": str(nxt.id) if nxt else None,
            "accounts": account_summaries(db)}


def delete_account(db: Session, account_id: Any) -> dict[str, Any]:
    account = get_account(db, account_id)
    if account is None:
        raise errors.not_found(f"账号 {account_id} 不存在", accountId=str(account_id))
    was_default = bool(account.is_default)
    db.delete(account)
    db.flush()
    if was_default:
        nxt = db.query(Account).order_by(Account.id.asc()).first()
        if nxt is not None:
            nxt.is_default = True
            db.flush()
    return {"deleted": True, "id": str(account_id), "accounts": account_summaries(db)}


def touch(db: Session, account: Account) -> None:
    account.last_used = now_ts()
    db.flush()


def account_summaries(db: Session) -> list[dict[str, Any]]:
    context = request_operator(db)
    query = db.query(Account)
    if context is not None and context[1] == "member":
        query = query.filter(Account.owner_operator_id == context[0])
    rows = query.order_by(Account.id.asc()).all()
    return [
        {
            "id": str(row.id),
            "name": row.name,
            "appIdHint": row.app_id_hint or "",
            "secretHint": row.secret_hint or "",
            "site": row.site or "",
            "isDefault": bool(row.is_default),
            "status": row.status or "active",
            "lastVerifiedAt": row.last_verified_at,
            "ownerOperatorId": str(row.owner_operator_id) if row.owner_operator_id else None,
            "boundByOperatorId": (
                str(row.bound_by_operator_id) if row.bound_by_operator_id else None
            ),
            "boundAt": row.bound_at,
        }
        for row in rows
    ]


# --- 上游错误映射 ---------------------------------------------------------
def map_upstream_error(exc: MiaoshouError, *, secrets: tuple[str, ...] = ()) -> errors.AppError:
    message = redact(str(exc), secrets)
    if exc.kind == "rate_limit":
        return errors.upstream_rate_limit(f"{message}（已重试 {5} 次仍被限流）")
    if exc.kind == "timeout":
        return errors.upstream_timeout("妙手接口超时，请检查网络后重试")
    if exc.kind == "unreachable":
        # 熔断期间或根本连不上：给一句能指导行动的文案，别让用户以为是自己点错了
        return errors.AppError(
            errors.UPSTREAM_ERROR,
            f"{message}。这台机器当前连不上妙手开放接口，"
            "请检查网络 / 代理后重试；已缓存的数据仍可查看。",
            status=502,
            details={"upstream": {"code": exc.code or "upstream_unreachable",
                                  "msg": "unreachable"}},
        )
    if exc.status == 401 or exc.code == "invalid_app_key":
        return errors.AppError(
            errors.UPSTREAM_ERROR,
            "妙手拒绝了凭据：AppKey 或 AppSecret 不正确，或该应用已停用",
            status=502,
            details={"upstream": {"code": exc.code or "invalid_app_key", "msg": "unauthorized"}},
        )
    return errors.upstream(message, code=exc.code)


def rename(db: Session, account_id: str, name: str) -> dict[str, Any]:
    """重命名已保存的连接（只改显示名，不动凭据）。"""
    row = get_account(db, account_id)
    if row is None:
        raise errors.not_found("账号不存在", accountId=str(account_id))
    cleaned = str(name or "").strip()
    if not cleaned:
        raise errors.validation("连接名称不能为空", field="name")
    if len(cleaned) > 64:
        raise errors.validation("连接名称最多 64 个字符", field="name")
    dup = db.query(Account).filter(Account.name == cleaned, Account.id != row.id).first()
    if dup is not None:
        raise errors.AppError(errors.DUPLICATE_NAME, f"已有同名连接「{cleaned}」",
                              status=409, details={"field": "name"})
    row.name = cleaned
    db.flush()
    return {"id": str(row.id), "name": row.name}


def ephemeral_account(credentials: tuple[str, str] | None) -> Account | None:
    """用请求头里的凭据构造一个不落库的账号对象（共享服务器模式）。

    返回的是未 attach 到 session 的 ORM 实例：调用方只能读它的
    app_key/app_secret，不能 commit，避免把别人的密钥写进本地库。
    """
    if not credentials:
        return None
    key, secret = credentials
    return Account(id=0, name=f"AppKey …{key[-4:]}", app_key=key,
                   app_secret_enc=secret, shops="[]", site="MY")


def resolve_account(db: Session, account_id: Any = None,
                    credentials: tuple[str, str] | None = None) -> Account:
    """优先用请求自带的凭据，其次用库里保存的账号。"""
    context = request_operator(db)
    if context is not None and context[1] == "member" and credentials is not None:
        raise errors.AppError(errors.FORBIDDEN,
                              "学员请求必须使用已绑定的妙手账号", status=403)
    transient = ephemeral_account(credentials)
    if transient is not None:
        return transient
    return require_account(db, account_id)
