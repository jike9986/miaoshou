"""尺寸表库：本地记住的尺码图地址，按站点复用。

老版是 `size_chart_store.py`（JSON 文件 + `remember_size_chart` / `forget_size_chart`）。
新版落到 SQLite 的 `size_charts` 表，并成为 `settings.workflow.defaults.sizeChartUrl`
的真实消费方：`resolve_for_site()` 先查本地库，再回落到 settings 里的默认地址。

约定（与老版语义一致，不可回归）：
  · 只存书签：每条记录是一个已经上传到图床的图片 URL + 标签，不存图片本体；
  · 删除只忘掉书签，**不删图片**——早前入库的商品还在引用它；
  · name + site 唯一（同站点下不允许同名），大小写不敏感；
  · site 为空表示「通用」，任何站点都能用；
  · 列表响应带 `defaultUrl`：一个本地一条都没有时也能用的兜底地址。

表结构与项目现有模型对齐：挂在 `db.session.Base.metadata` 上，因此
`init_db()` 的 `create_all` 会一并建表；对已经存在的库则在首次访问时
`create(checkfirst=True)` 补建，不需要迁移脚本。
"""
from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import (
    JSON,
    Column,
    Float,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy import (
    delete as sql_delete,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core import errors
from core.utils import clean_text, now_ts
from db.session import Base
from db.session import engine as default_engine

TABLE_NAME = "size_charts"
MAX_CHARTS = 500
NAME_MAX_LENGTH = 128
SITE_MAX_LENGTH = 8
URL_MAX_LENGTH = 2048

size_charts = Table(
    TABLE_NAME,
    Base.metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(NAME_MAX_LENGTH), nullable=False),
    Column("site", String(SITE_MAX_LENGTH), nullable=False, default=""),
    Column("url", Text, nullable=False),
    Column("content_json", JSON, nullable=True),
    Column("created_at", Float, nullable=False, default=now_ts),
    Column("updated_at", Float, nullable=False, default=now_ts),
    UniqueConstraint("name", "site", name="uq_size_charts_name_site"),
    extend_existing=True,
)

# 已经确认过表存在的 engine（按对象身份记；同时持有引用，避免 id 被回收后复用）
_ready_engines: dict[int, Any] = {}


# --- 建表 -----------------------------------------------------------------
def ensure_table(bind: Any = None) -> None:
    """首次访问时补建 size_charts 表（幂等）。

    bind 可以是 Engine 或 Connection；为空则用全局 engine。
    """
    target = bind if bind is not None else default_engine
    owner = getattr(target, "engine", target)  # Connection -> 它的 Engine
    key = id(owner)
    if key in _ready_engines:
        return
    size_charts.create(target, checkfirst=True)
    _ready_engines[key] = owner


def _ensure(db: Session) -> None:
    """用当前会话的 engine / connection 补建表。

    会话绑定的库才是真正要写的库（测试会挂临时库），不能用全局 engine
    代替，否则测试会在真实 app.db 上建表。
    """
    ensure_table(db.get_bind())


# --- 校验 -----------------------------------------------------------------
def _validate_name(value: Any) -> str:
    text = clean_text(value)
    if not text:
        raise errors.validation("尺寸表名称不能为空", field="name")
    if len(text) > NAME_MAX_LENGTH:
        raise errors.validation(f"尺寸表名称不能超过 {NAME_MAX_LENGTH} 个字符",
                                field="name", value=len(text))
    return text


def _validate_site(value: Any) -> str:
    """站点代码：大写字母/数字，空表示通用。"""
    text = clean_text(value).upper()
    if not text:
        return ""
    if len(text) > SITE_MAX_LENGTH or not text.isalnum():
        raise errors.validation("站点代码无效（例如 MY / SG / TH）",
                                field="site", value=value)
    return text


def _url_shape(value: Any) -> tuple[str, Any]:
    """长度与 scheme 的基础校验，返回 (原文本, urlsplit 结果)。"""
    text = clean_text(value)
    if not text:
        raise errors.validation("尺寸表图片地址不能为空", field="url")
    if len(text) > URL_MAX_LENGTH:
        raise errors.validation(f"图片地址不能超过 {URL_MAX_LENGTH} 个字符",
                                field="url", value=len(text))
    parts = urlsplit(text)
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        raise errors.validation("尺寸表地址必须是 http(s) 图片链接",
                                field="url", value=text)
    return text, parts


def _validate_url(value: Any) -> str:
    """书签地址：只做形状校验。

    这里刻意不查内网：老库里可能还留着内网测试地址（都是操作者自己填的），
    读接口上再拦一次会把已有条目标成非法，反而看不出原始数据。
    新写入走 `_validate_remote_url`。
    """
    return _url_shape(value)[0]


# 明确的本地主机名（ipaddress 只能判字面 IP，这些名字得单独列）
_LOCAL_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost",
                    "metadata.google.internal", "instance-data"}


def _is_internal_host(host: str) -> bool:
    """主机名是否是本机 / 内网。

    只认「字面 IP」和明确的本地主机名，**不做 DNS 解析**。原因很实在：装了
    代理 / 加速器的机器上公网域名普遍被解析到 fake-ip 段（198.18.0.0/15），
    那一段在 ipaddress 里算 private，按解析结果判内网会把用户正常的图床地址
    全部拒掉。域名一律放行，可达性交给真正取图的那一端。
    """
    name = host.casefold().strip("[]").rstrip(".")
    if not name:
        return True
    if name in _LOCAL_HOSTNAMES or name.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    return _is_internal_address(address)


def _is_internal_address(address: Any) -> bool:
    return bool(
        address.is_private          # 10/8、172.16/12、192.168/16、fc00::/7 …
        or address.is_loopback      # 127/8、::1
        or address.is_link_local    # 169.254/16、fe80::/10
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _validate_remote_url(value: Any) -> str:
    """外部传入的新地址：形状 + 不允许内网。

    上传接口允许直接给 URL 代替文件，等价于「服务端替用户登记一条远端图片」。
    不拦内网的话，前端只要有人填 http://127.0.0.1:8080/x.png，下游取图就会
    变成对内网服务的请求；老版上传只收文件，天然没有这个面。
    """
    text, parts = _url_shape(value)
    if parts.username or parts.password:
        raise errors.validation("尺寸表地址不能带账号密码", field="url")
    if _is_internal_host(parts.hostname or ""):
        raise errors.validation("尺寸表地址不能是本机 / 内网地址", field="url")
    return text


# --- 图片类型嗅探 ---------------------------------------------------------
# 只读前 12 字节就够判 PNG / JPEG / WebP，不引 Pillow：这里要防的是「声明
# image/png、实际传了别的东西」，不是要解析图片，魔数比解码器更便宜也更稳。
_SNIFF_BYTES = 12
MIME_LABELS = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/webp": "WebP",
}


def sniff_image_type(raw: bytes) -> str:
    """按魔数判真实 MIME；判不出来返回空串（由调用方决定怎么报错）。"""
    head = raw[:_SNIFF_BYTES]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return ""


def mime_label(mime: str) -> str:
    """给报错文案用的短名：image/png → PNG，认不出就原样返回。"""
    text = clean_text(mime).lower()
    if not text:
        return "未知"
    return MIME_LABELS.get(text, text)


def check_content_type(raw: bytes, content_type: Any) -> str:
    """声明的 content_type 与嗅探结果必须一致，返回嗅探到的真实 MIME。

    老版 `SizeChartService.upload` 就在这一步拒绝：类型声明和实际字节不符时
    说明前端传错了文件（比如把 .webp 当成 png 存），继续传上去会在图床上
    留下后缀对不上的文件，之后平台侧按扩展名取图会失败。文案按新版错误规范
    成形（422 + 「文件类型与声明不一致：声明 X，实际 Y」），断言点保持老版位置。
    """
    declared = clean_text(content_type).split(";", 1)[0].strip().lower()
    actual = sniff_image_type(raw)
    if not actual:
        raise errors.unprocessable(
            f"文件类型与声明不一致：声明 {mime_label(declared)}，实际 未知格式",
            declared=declared or None, actual=None)
    if not declared:
        # 没声明就按嗅探结果处理：老版前端总会带 Content-Type，缺了不该判失败。
        return actual
    if declared == "image/jpg":  # 历史别名，等价于 jpeg
        declared = "image/jpeg"
    if declared != actual:
        raise errors.unprocessable(
            f"文件类型与声明不一致：声明 {mime_label(declared)}，"
            f"实际 {mime_label(actual)}",
            declared=declared, actual=actual)
    return actual


def _validate_content(value: Any) -> Any:
    """contentJson：可空；dict/list 原样存，字符串按 JSON 文本解析。"""
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise errors.validation("contentJson 必须是 JSON 对象 / 数组 / 文本",
                                field="contentJson")
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        import json

        try:
            return json.loads(value)
        except ValueError:
            raise errors.validation("contentJson 不是合法 JSON 文本",
                                    field="contentJson") from None
    raise errors.validation("contentJson 必须是 JSON 对象 / 数组 / 文本",
                            field="contentJson")


# --- 序列化 ---------------------------------------------------------------
def serialize(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "name": row.name or "",
        "site": row.site or "",
        "url": row.url or "",
        "contentJson": row.content_json,
        "createdAt": row.created_at,
        "updatedAt": row.updated_at,
    }


def _ordered(db: Session, site: str | None = None) -> list[Any]:
    stmt = select(size_charts).order_by(size_charts.c.updated_at.desc(),
                                        size_charts.c.id.desc())
    if site is not None:
        stmt = stmt.where(size_charts.c.site == site)
    return list(db.execute(stmt).all())


def _sites(db: Session) -> list[str]:
    rows = db.execute(
        select(size_charts.c.site).distinct().order_by(size_charts.c.site.asc())
    ).all()
    return [str(row[0] or "") for row in rows]


# --- 读 -------------------------------------------------------------------
def default_url(db: Session) -> str:
    """项目默认尺码图地址：本地一条都没上传时也能直接用的那个 URL。

    老版 `SizeChartService.list()` 返回 `{"charts": [...], "defaultUrl": ...}`，
    前端「尺码图」面板据此在没有本地记录时也能显示一张可选项。取值来源就是
    `settings.workflow.defaults.sizeChartUrl`（`models.setting.DEFAULT_SETTINGS`
    已带出厂值）；设置行缺失时回落到那个出厂常量——老版是写死的模块常量，
    「设置没写过就返回空」会让刚装好的机器上一张默认图都没有。
    读坏了不当成错误：尺码图库打不开比少一张默认图严重得多。
    """
    from services import setting_service

    url = ""
    try:
        defaults = setting_service.workflow_defaults(db)
        url = clean_text(defaults.get("sizeChartUrl"))
    except Exception:  # noqa: BLE001 - 设置读坏不该让整个尺码图库 500
        url = ""
    if not url:
        try:
            from models.setting import DEFAULT_SETTINGS, WORKFLOW_DEFAULTS_KEY

            shipped = DEFAULT_SETTINGS.get(WORKFLOW_DEFAULTS_KEY) or {}
            url = clean_text(shipped.get("sizeChartUrl"))
        except Exception:  # noqa: BLE001 - 常量缺失同样不该挡住读接口
            url = ""
    if not url:
        return ""
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return ""
    return url


def list_charts(db: Session, *, site: Any = None) -> dict[str, Any]:
    _ensure(db)
    wanted = _validate_site(site) if site not in (None, "") else None
    rows = _ordered(db, wanted)
    items = [serialize(row) for row in rows]
    return {
        "items": items,
        "charts": items,  # 老版字段名，保留兼容
        "total": len(items),
        "sites": _sites(db),
        # 老版 list() 固定回这个字段；新版之前漏了，客户端只能自己拼地址
        "defaultUrl": default_url(db),
    }


def get_chart(db: Session, chart_id: Any) -> Any:
    _ensure(db)
    text = str(chart_id or "").strip()
    if not text.isdigit():
        raise errors.validation("尺寸表 id 必须是数字", field="id", value=chart_id)
    row = db.execute(
        select(size_charts).where(size_charts.c.id == int(text))
    ).first()
    if row is None:
        raise errors.not_found(f"尺寸表 {text} 不存在", id=text)
    return row


def detail(db: Session, chart_id: Any) -> dict[str, Any]:
    return {"chart": serialize(get_chart(db, chart_id))}


# --- 写 -------------------------------------------------------------------
def _assert_unique(db: Session, name: str, site: str,
                   *, exclude_id: int | None = None) -> None:
    stmt = select(size_charts.c.id).where(
        size_charts.c.site == site,
        func.lower(size_charts.c.name) == name.lower(),
    )
    if exclude_id is not None:
        stmt = stmt.where(size_charts.c.id != exclude_id)
    if db.execute(stmt).first() is not None:
        label = f"{name}（{site or '通用'}）"
        raise errors.duplicate_name(f"尺寸表「{label}」已存在", name=name, site=site)


def create(db: Session, *, name: Any, url: Any, site: Any = None,
           content_json: Any = None) -> dict[str, Any]:
    _ensure(db)
    text = _validate_name(name)
    code = _validate_site(site)
    link = _validate_url(url)
    content = _validate_content(content_json)
    total = db.execute(select(func.count()).select_from(size_charts)).scalar_one()
    if int(total or 0) >= MAX_CHARTS:
        raise errors.validation(f"尺寸表最多保存 {MAX_CHARTS} 条，请先删除不用的",
                                field="name", value=int(total))
    _assert_unique(db, text, code)
    stamp = now_ts()
    try:
        db.execute(size_charts.insert().values(
            name=text, site=code, url=link, content_json=content,
            created_at=stamp, updated_at=stamp))
        db.flush()
    except IntegrityError:
        db.rollback()
        raise errors.duplicate_name(f"尺寸表「{text}」已存在", name=text,
                                    site=code) from None
    row = db.execute(
        select(size_charts).where(size_charts.c.site == code,
                                  func.lower(size_charts.c.name) == text.lower())
    ).first()
    return {"chart": serialize(row), "items": list_charts(db)["items"]}


def remember_url(db: Session, *, url: Any, name: Any = None,
                 site: Any = None) -> dict[str, Any]:
    """上传接口的「只给 URL」分支：校验后直接记一条书签，不碰文件。

    同名（同站点）已存在时按「再传一次同一张图」处理：覆盖地址而不是 409，
    否则同一个人重传同一张默认尺码图会被自己的旧记录挡住。
    """
    _ensure(db)
    link = _validate_remote_url(url)
    raw_name = clean_text(name)
    label = _validate_name(raw_name) if raw_name else _name_from_url(link)
    code = _validate_site(site)
    existing = db.execute(
        select(size_charts).where(size_charts.c.site == code,
                                  func.lower(size_charts.c.name) == label.lower())
    ).first()
    if existing is None:
        return create(db, name=label, url=link, site=code,
                      content_json={"mime": "", "bytes": 0, "source": "url"})
    fresh = update(db, existing.id, {"url": link})
    row = db.execute(
        select(size_charts).where(size_charts.c.id == int(existing.id))
    ).first()
    return {"chart": serialize(row), "items": fresh["items"], "replaced": True}


def _name_from_url(url: str) -> str:
    """没给名称时用 URL 最后一段，去掉扩展名；取不到就用固定名。"""
    import os
    from urllib.parse import unquote

    tail = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    stem = os.path.splitext(tail)[0]
    label = clean_text(stem)[:NAME_MAX_LENGTH]
    return label or "默认尺码图"


def update(db: Session, chart_id: Any, body: Mapping[str, Any]) -> dict[str, Any]:
    """PATCH：只改 body 里出现的字段；contentJson 传 null 表示清空。"""
    _ensure(db)
    row = get_chart(db, chart_id)
    if not body:
        raise errors.validation("至少提供一个要修改的字段（name / site / url / contentJson）")
    values: dict[str, Any] = {}
    if "name" in body:
        values["name"] = _validate_name(body.get("name"))
    if "site" in body:
        values["site"] = _validate_site(body.get("site"))
    if "url" in body:
        values["url"] = _validate_url(body.get("url"))
    if "contentJson" in body:
        values["content_json"] = _validate_content(body.get("contentJson"))
    new_name = str(values.get("name", row.name))
    new_site = str(values.get("site", row.site or ""))
    _assert_unique(db, new_name, new_site, exclude_id=int(row.id))
    values["updated_at"] = now_ts()
    try:
        db.execute(size_charts.update()
                   .where(size_charts.c.id == int(row.id))
                   .values(**values))
        db.flush()
    except IntegrityError:
        db.rollback()
        raise errors.duplicate_name(f"尺寸表「{new_name}」已存在", name=new_name,
                                    site=new_site) from None
    fresh = get_chart(db, chart_id)
    return {"chart": serialize(fresh), "items": list_charts(db)["items"]}


def delete(db: Session, chart_id: Any) -> dict[str, Any]:
    """忘掉书签；图片本体保留（早前入库的商品还在引用）。"""
    _ensure(db)
    row = get_chart(db, chart_id)
    chart = serialize(row)
    db.execute(sql_delete(size_charts).where(size_charts.c.id == int(row.id)))
    db.flush()
    return {
        "deleted": True,
        "id": chart["id"],
        "name": chart["name"],
        "site": chart["site"],
        "url": chart["url"],
        "imageKept": True,
        "items": list_charts(db)["items"],
    }


def delete_by_url(db: Session, url: Any) -> dict[str, Any]:
    """按地址删：老版 `delete-size-chart` 的入参只有 url，没有 id。

    前端拿到的可能是旧接口返回的 `{charts: [{url, label}]}`（没有 id），所以
    必须留一条按地址删除的路径。三条口径：
      · 地址为空 → 400，和老版「请选择要删除的尺码图」同一个位置；
      · 项目默认尺码图（settings 里那个）→ 拒绝，删掉会让所有人失去兜底图；
      · 地址不存在 → 404，不要静默成功（前端会以为删掉了）。
    """
    _ensure(db)
    link = clean_text(url)
    if not link:
        raise errors.validation("请选择要删除的尺码图", field="url")
    fallback = default_url(db)
    if fallback and link == fallback:
        raise errors.validation("项目默认尺码图不能删除", field="url", value=link)
    rows = list(db.execute(
        select(size_charts).where(size_charts.c.url == link)
        .order_by(size_charts.c.id.asc())
    ).all())
    if not rows:
        raise errors.not_found("尺码图库里没有这条地址", url=link)
    # 同一条地址可能被登记在两个站点（唯一约束是 name + site，不是 url），
    # 老版是「整条忘掉」语义，这里跟着全删，并如实报条数。
    db.execute(sql_delete(size_charts).where(size_charts.c.url == link))
    db.flush()
    return {
        "deleted": True,
        "id": str(rows[0].id) if len(rows) == 1 else None,
        "name": rows[0].name or "",
        "site": rows[0].site or "",
        "url": link,
        "count": len(rows),
        "imageKept": True,
        "items": list_charts(db)["items"],
    }


# --- 站点解析 -------------------------------------------------------------
def _pick_default(rows: list[Any]) -> Any:
    """默认条目：contentJson.default == true 优先，否则最近更新的一条。"""
    for row in rows:
        content = row.content_json
        if isinstance(content, dict) and content.get("default") is True:
            return row
    return rows[0] if rows else None


def resolve_for_site(db: Session, site: Any = None) -> dict[str, Any]:
    """该站点该用的尺寸表地址：本地库（站点 → 通用）> settings 默认 > 空。"""
    _ensure(db)
    wanted = _validate_site(site)
    rows = _ordered(db, wanted) if wanted else []
    if not rows:
        rows = _ordered(db, "")
    chart = _pick_default(rows)
    if chart is not None:
        return {
            "site": wanted,
            "url": chart.url or "",
            "name": chart.name or "",
            "chartId": str(chart.id),
            "source": "local",
        }
    fallback = default_url(db)
    if fallback:
        return {"site": wanted, "url": fallback, "name": "", "chartId": None,
                "source": "settings"}
    return {"site": wanted, "url": "", "name": "", "chartId": None, "source": "none"}
