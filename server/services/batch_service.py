"""有状态批次编排（老版 `AutomationController` 的 `/api/start` + `/api/control`）。

核心约束：**`run_once` 只推进一件，并且必须能被反复调用把整批推完。**
老版是后台线程 + `threading.Event` 控制暂停/停止；新版没有常驻线程，
所以「可推进」这件事必须落在单步语义上：

    while 还没跑完:
        POST /batch/{id}/step      # 一件
        （调用方随时可以插一个 POST /batch/{id}/control 暂停/停止）

写成阻塞大循环的话，pause / stop 只会在整批跑完之后才被读到，
等于没有暂停——这是刻意避开的写法。

为什么要有幂等闸（payload_hash）：
`warehouse_service.save_from_collect_box` 里的 `prepare_weight` 不幂等，
同一件重复入仓会一次次加 0.1kg 增量，重量越漂越远。老版靠
`automation-save-intent-{detailId}-{shopId}.json` 这个「意图文件」拦：
文件在就说明上一次可能已经提交过，必须先去妙手核对，不能自动重试。
新版没有那个文件，改用四道闸：

  1. `(batch_id, detail_id)` 唯一约束 —— 物理上不可能有两行同一件；
  2. `payload_hash`（详情快照的 sha256）—— 同店铺、同 detailId、同快照
     已经成功提交过就不再提交（跨批次生效，`_submitted_before`）；
  3. 条目停在 `running` 且哈希未变 —— 说明上次崩在提交中间，结果未知，
     直接 409 拒绝自动重跑，要求人工核对后 `allowUnknown=True`；
  4. 单件失败即把整批置 `failed` —— 不让「跑完剩下几件、错误埋在最后」
     这种情况发生（`_fail` 里有详细理由）。
"""
from __future__ import annotations

import contextlib
import inspect
import json
import logging
from collections.abc import Callable, Iterable, Mapping
from functools import partial
from typing import Any

from sqlalchemy.orm import Session

from core import errors
from core.locks import session_lock
from core.utils import new_batch_id, now_ts, sha256_hex
from models.account import Account
from models.batch import Batch, BatchItem

logger = logging.getLogger(__name__)

# 批次状态机允许的取值。写成常量而不是散落的字符串：
# 状态判断分布在 run_once / control / retry_failed / delete 四处，拼错一个字母
# 就会变成「永远推不动」或「什么状态都能删」。
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_STOPPED = "stopped"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
BATCH_STATUSES = (STATUS_PENDING, STATUS_RUNNING, STATUS_PAUSED,
                  STATUS_STOPPED, STATUS_DONE, STATUS_FAILED)

ITEM_PENDING = "pending"
ITEM_RUNNING = "running"
ITEM_OK = "ok"
ITEM_FAILED = "failed"
ITEM_SKIPPED = "skipped"

# 「这批已经跑完了，不能再动」的集合：done 只能删
_TERMINAL = (STATUS_DONE,)

# 未完成的批次：同一账号 × 同一店铺同时只允许一条。
# `pending` 必须算进来。任务书只写了 running/paused，但 pending 同样是「没跑完」：
# 两条 pending 批次覆盖同一批 detailId 时，两边都会从 index 0 开始提交，
# 唯一约束和幂等闸只挡得住「同批次内重复」和「同快照重复」，
# 挡不住「两个批次各自跑一遍」——那正是重量被加两次的路径。
_ACTIVE = (STATUS_PENDING, STATUS_RUNNING, STATUS_PAUSED)

# 允许删除的状态：跑着的批次不能删，删了 current_index 就丢了
_DELETABLE = (STATUS_DONE, STATUS_STOPPED, STATUS_FAILED)

# 提交结果未知时打在 message 上的标记。回读核对过之后操作者可以用
# `allowUnknown=true` 强行重跑（见 retry_failed 的说明）。
UNKNOWN_MARK = "保存可能已提交"

MAX_ITEMS = 200          # 一次批次最多多少件（与 publish 的批量上限对齐）
MAX_ATTEMPT = 5          # 同一件自动重试上限，超过就要求人工介入


# --- 序列化 ---------------------------------------------------------------
def serialize(batch: Batch, *, operator_name: str = "") -> dict[str, Any]:
    items = batch.results if isinstance(batch.results, list) else []
    return {
        "id": batch.id,
        "status": batch.status or STATUS_PENDING,
        "accountId": str(batch.account_id) if batch.account_id else None,
        "shopId": batch.shop_id or "",
        "detailIds": [str(value) for value in (batch.detail_ids or [])],
        "currentIndex": int(batch.current_index or 0),
        "settings": batch.settings if isinstance(batch.settings, dict) else {},
        "results": items,
        "errors": batch.errors if isinstance(batch.errors, list) else [],
        "createdAt": batch.created_at,
        "updatedAt": batch.updated_at,
        "finishedAt": batch.finished_at,
        # 建这批的人。空串 = 没有操作员上下文（老数据 / 脚本建的），
        # 前端判空串即可，不用区分 null。
        "operatorName": operator_name or "",
        # 进度：done+skipped 之外还剩几件，前端不用自己数
        "remaining": _remaining(batch),
    }


def operator_names(db: Session) -> dict[int, str]:
    """operators.id → 显示名。批次列表一次取全，不逐行查。"""
    from models.operator import Operator

    return {row.id: (row.name or "") for row in db.query(Operator).all()}


def serialize_item(item: BatchItem) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "batchId": item.batch_id,
        "detailId": item.detail_id,
        "status": item.status or ITEM_PENDING,
        "message": item.message or "",
        "payloadHash": item.payload_hash or "",
        "attempt": int(item.attempt or 0),
        "createdAt": item.created_at,
        "updatedAt": item.updated_at,
    }


def _remaining(batch: Batch) -> int:
    """还剩几件没处理（待跑 + 失败待重试）。

    以 `detail_ids` 为总数、从 `results` 里扣掉已完成（ok / skipped）的条数，
    而不是去数 results 里的 pending：列表接口一次返回几十条批次，
    按批次查一次 batch_items 就是 N+1 查询，而这个数字只是给人看的进度。
    """
    total = len(batch.detail_ids or [])
    rows = batch.results if isinstance(batch.results, list) else []
    done = sum(1 for row in rows
               if isinstance(row, Mapping) and row.get("status") in (ITEM_OK, ITEM_SKIPPED))
    return max(0, total - done)


# --- 查询 -----------------------------------------------------------------
def get(db: Session, batch_id: str) -> Batch:
    text = str(batch_id or "").strip()
    if not text:
        raise errors.validation("批次 id 不能为空", field="batchId")
    batch = db.query(Batch).filter(Batch.id == text).one_or_none()
    if batch is None:
        raise errors.not_found(f"批次 {text} 不存在", batchId=text)
    return batch


def items_of(db: Session, batch_id: str) -> list[BatchItem]:
    """按插入顺序返回子项。

    不按 status 排序：`current_index` 是「原始选择顺序里的位置」，
    一排序就和 detail_ids 对不上，进度会跳。
    """
    return (db.query(BatchItem)
            .filter(BatchItem.batch_id == batch_id)
            .order_by(BatchItem.id.asc())
            .all())


def list_batches(db: Session, account_id: Any = None, limit: int = 50) -> dict[str, Any]:
    query = db.query(Batch)
    if account_id not in (None, ""):
        text = str(account_id)
        if not text.isdigit():
            raise errors.validation("accountId 必须是数字", field="accountId", value=account_id)
        query = query.filter(Batch.account_id == int(text))
    total = query.count()
    count = max(1, min(int(limit or 50), 200))
    rows = query.order_by(Batch.created_at.desc(), Batch.id.desc()).limit(count).all()
    operators = operator_names(db)
    return {"items": [serialize(row, operator_name=operators.get(row.operator_id or 0, ""))
                      for row in rows],
            "total": total}


# --- 创建 -----------------------------------------------------------------
def _clean_detail_ids(values: Iterable[Any]) -> list[str]:
    """去重 + 保序 + 校验。

    去重不只是「省一次请求」：同一件在一批里出现两次，`(batch_id, detail_id)`
    唯一约束会直接拒掉整条插入语句，批次根本建不出来。所以必须在建之前收敛，
    而不是指望数据库报错。
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value).strip()
        if not text:
            continue
        if not text.isdigit():
            raise errors.validation(f"detailId 必须是数字：{text}", field="detailIds",
                                    value=text)
        if text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    return cleaned


def active_batch(db: Session, *, account_id: Any, shop_id: Any) -> Batch | None:
    """同账号同店铺是否已有未完成的批次。"""
    query = (db.query(Batch)
             .filter(Batch.status.in_(_ACTIVE), Batch.shop_id == str(shop_id or "")))
    if account_id in (None, ""):
        query = query.filter(Batch.account_id.is_(None))
    else:
        query = query.filter(Batch.account_id == int(account_id))
    return query.order_by(Batch.created_at.desc()).first()


def create(db: Session, *, account_id: Any, shop_id: Any,
           detail_ids: Iterable[Any], settings: Mapping[str, Any] | None = None,
           operator_id: int | None = None) -> Batch:
    ids = _clean_detail_ids(detail_ids)
    if not ids:
        raise errors.validation("请至少选择一件商品", field="detailIds")
    if len(ids) > MAX_ITEMS:
        raise errors.validation(f"一个批次最多 {MAX_ITEMS} 件", field="detailIds",
                                value=len(ids))
    shop_text = str(shop_id or "").strip()
    if not shop_text:
        raise errors.validation("缺少 shopId", field="shopId")
    if not shop_text.isdigit():
        raise errors.validation("shopId 必须是数字", field="shopId", value=shop_id)

    account_pk: int | None = None
    if account_id not in (None, ""):
        text = str(account_id)
        if not text.isdigit():
            raise errors.validation("accountId 必须是数字", field="accountId",
                                    value=account_id)
        account_pk = int(text)
        # 先查账号存在性：SQLite 开了 PRAGMA foreign_keys，指向不存在的账号
        # 会在 commit 时抛 IntegrityError（500），而这是明确的参数错误。
        if db.query(Account).filter(Account.id == account_pk).one_or_none() is None:
            raise errors.not_found(f"账号 {account_pk} 不存在", accountId=account_pk)

    # 从「查有没有未完成批次」到「插入这一行」必须原子，否则两人同时建批次
    # 会各自查到「没有」然后各插一条 —— 两条 pending 覆盖同一批商品，
    # 唯一约束和幂等闸都只挡得住批次内重复，挡不住两个批次各跑一遍
    # （重量被加两次的路径，见文件头的说明）。
    #
    # 为什么用进程内锁而不是数据库约束：能加的只有「部分唯一索引」
    # （status in ('pending','running','paused') 上建 UNIQUE），而
    # `create_all` 不会给已存在的表补索引，`_add_missing_columns` 也只做
    # ADD COLUMN —— 加了也只在全新库上存在，老库和新库两种结构，比没有更难排查。
    # 后端是单进程 uvicorn（run_server.py 里没开 workers），进程内锁就是全部写入方。
    # 锁键 = 账号 × 店铺：不同店铺建批次互不相关，不该互相排队。
    # 跨进程的场景（有人另外起一个实例）由 busy_timeout + 这条 SELECT 兜底，
    # 不是完美，但比现在的「完全不设防」强。
    #
    # 锁必须是**会话级**的（session_lock）：这把锁要活到路由里的 commit 之后。
    # 用函数级的锁实测会漏 —— A 建完还没提交时锁就放了，B 拿锁后
    # 查不到 A 那条未提交的批次，于是也插一条，库里有两条 pending。
    # 这个漏放不是理论问题：`api/batch.py` 的 commit 就在 create() 之外。
    lock_key = f"batch:create:{account_pk if account_pk is not None else '-'}:{shop_text}"
    with session_lock(db, [lock_key],
                      busy_message="正在为这个店铺建批次，请稍后重试"):
        existing = active_batch(db, account_id=account_pk, shop_id=shop_text)
        if existing is not None:
            # 409 而不是 400：这不是参数错误，是「资源状态冲突」，前端据此提示
            # 「先去停掉那一批」而不是「检查你的输入」。
            raise errors.busy(
                "已有未完成的批次，请先停止或等待完成",
                batchId=existing.id, status=existing.status,
                remaining=_remaining(existing))

        batch = Batch(
            id=new_batch_id("bat"),
            status=STATUS_PENDING,
            account_id=account_pk,
            shop_id=shop_text,
            detail_ids=ids,
            current_index=0,
            settings=dict(settings or {}),
            results=[],
            errors=[],
            # 建这批的人：批次列表要显示「谁建的」。不参与任何判定。
            operator_id=operator_id,
        )
        db.add(batch)
        db.flush()
        for detail_id in ids:
            db.add(BatchItem(batch_id=batch.id, detail_id=detail_id, status=ITEM_PENDING))
        db.flush()
    return batch


# --- 幂等闸 ---------------------------------------------------------------
def snapshot_hash(response: Mapping[str, Any] | None) -> str:
    """详情快照的 sha256。

    只取 `data` 块：外层信封里可能有时间戳/请求 id 之类每次都变的字段，
    把它们算进哈希会让幂等闸永远失效（哈希永远不同 = 永远不跳过）。
    """
    block = (response or {}).get("data")
    if block is None:
        block = response or {}
    try:
        canonical = json.dumps(block, sort_keys=True, ensure_ascii=False,
                               separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        canonical = repr(block)
    return sha256_hex(canonical.encode("utf-8"))


def _default_loader(client: Any, detail_id: str, shop_id: str) -> dict[str, Any]:
    """默认的详情加载器：走妙手 shop_detail（与单件入仓同一个上游入口）。"""
    return client.shop_detail(detail_id, shop_id)


def _default_processor(db: Session, account: Account, client: Any, detail_id: str,
                       shop_id: str, settings: Mapping[str, Any],
                       operator_id: int | None = None) -> dict[str, Any]:
    """真正干活的单件入口。

    直接复用 `warehouse_service.save_from_collect_box`，不在这里重写
    「拉详情 → 翻译 → 定价 → 重量 → 诊断 → 入库」那条链：那套业务规则
    （重量不幂等、pre_save_check 十条、译文图回写顺序）已经在
    api/collect_box.py 的 process 里跑通了，复制一份必然漂移。

    `operator_id` 由 run_once 用 functools.partial 绑上批次创建人，
    不进 settings —— settings 是用户给的参数，会被原样回显给前端，
    往里塞内部字段会让「批次设置」变得不可信。
    """
    from services import warehouse_service

    product = warehouse_service.save_from_collect_box(
        db, account, client,
        detail_id=detail_id, shop_id=shop_id,
        language=str(settings.get("language") or ""),
        group_id=settings.get("groupId"),
        price_template=settings.get("template"),
        exchange_rate=settings.get("exchangeRate"),
        enforce_pre_save=bool(settings.get("save", True)),
        operator_id=operator_id,
        automation_result=bool(settings.get("save", True)),
    )
    images: dict[str, Any] | None = None
    if settings.get("processImages") and settings.get("save", True):
        # 图片翻译是附加步骤：失败只降级，不能让整件算失败（老版 process_images 同理）
        try:
            images = warehouse_service.attach_image_translation(
                db, product,
                limit=int(settings.get("mainImageCount") or 0),
                source_images=[str(url) for url in (product.get("images") or [])],
            )
        except errors.AppError as exc:
            images = {"submitted": 0, "failed": 0, "reason": exc.message}
    return {"product": product, "images": images}


# --- 推进 -----------------------------------------------------------------
def _advance(batch: Batch, rows: list[BatchItem]) -> None:
    """把 current_index 推到第一条还需要跑的子项；没有就收尾。"""
    index = max(0, min(int(batch.current_index or 0), len(rows)))
    while index < len(rows) and rows[index].status in (ITEM_OK, ITEM_SKIPPED):
        index += 1
    batch.current_index = index
    if index >= len(rows) and rows and batch.status != STATUS_STOPPED:
        batch.status = STATUS_DONE
        batch.finished_at = now_ts()


def _record(batch: Batch, item: BatchItem) -> None:
    """把子项结果同步进 batch.results（人工核对看的就是这份快照）。

    必须**新建列表再整份赋值**：JSON 列不感知就地 mutation，
    `rows.append(...)` 之后即使把同一个 list 对象赋回去，SQLAlchemy 也认为
    「值没变」，不会产生 UPDATE——results 永远是空的。
    """
    rows = batch.results if isinstance(batch.results, list) else []
    entry = {"detailId": item.detail_id, "status": item.status,
             "message": item.message or "", "attempt": int(item.attempt or 0),
             "at": now_ts()}
    updated: list[Any] = []
    replaced = False
    for row in rows:
        if isinstance(row, Mapping) and str(row.get("detailId")) == item.detail_id:
            updated.append(entry)
            replaced = True
        else:
            updated.append(row)
    if not replaced:
        updated.append(entry)
    batch.results = updated


def _fail(db: Session, batch: Batch, item: BatchItem, message: str, *,
          unknown: bool = False) -> None:
    """记一次失败。

    失败**就地刹住整批**（status=failed），不往后跳：
      · `save_from_collect_box` 里 `prepare_weight` 不幂等，失败可能发生在
        「重量已加增量、payload 还没落库」之间；自动跳到下一件会把这件事
        埋掉，操作者以为只有一件失败。
      · 老版 AutomationController 是「记错误继续下一件」，但它的继续有个前提：
        有 `automation-save-intent-*.json` 记账，重跑前能核对。新版没有那份
        记账文件，继续跑等于放弃核对机会。
    恢复入口只有一个：`retry_failed`（可带 allowUnknown 越过结果未知的件）。
    """
    item.status = ITEM_FAILED
    # 结果未知的件在 message 上留可见标记：操作者一眼能看出「这件要先去妙手
    # 核对」，而不是去翻 errors 数组里那个 unknownResult 布尔值。
    item.message = f"{UNKNOWN_MARK}：{message}" if unknown else message
    item.attempt = int(item.attempt or 0) + 1
    item.updated_at = now_ts()
    batch.updated_at = now_ts()
    batch.status = STATUS_FAILED
    _record(batch, item)
    errors_log = batch.errors if isinstance(batch.errors, list) else []
    # 同样新建列表再赋值（理由见 _record）
    batch.errors = (list(errors_log) + [{"detailId": item.detail_id,
                                         "message": message,
                                         "attempt": item.attempt, "at": now_ts(),
                                         "unknownResult": bool(unknown)}])[-50:]
    # 这是本批最后一件（后面没有待跑的了）→ 盖 finishedAt，前端才不会一直转圈
    _seal_if_exhausted(batch, rows_of(db, batch))


def rows_of(db: Session, batch: Batch) -> list[BatchItem]:
    """`db.query` 直取当前已 flush 的子项（含本次会话内刚改的状态）。"""
    db.flush()
    return (db.query(BatchItem)
            .filter(BatchItem.batch_id == batch.id)
            .order_by(BatchItem.id.asc())
            .all())


def _seal_if_exhausted(batch: Batch, rows: list[BatchItem]) -> None:
    """没有待跑子项了就盖完成时间戳。

    `failed` 也盖：一批里最后一件失败后就没有任何能推进的东西了，
    finishedAt 空着会让前端一直显示「进行中」。retry_failed 会把它清回 None。
    """
    if not any(row.status in (ITEM_PENDING, ITEM_RUNNING) for row in rows):
        batch.finished_at = batch.finished_at or now_ts()


def _bind_operator_id(processor: Callable[..., dict[str, Any]],
                      operator_id: int | None) -> Callable[..., dict[str, Any]]:
    """给默认处理器绑上批次创建人；处理器不收这个关键字就原样返回。

    为什么要探一下形参而不是直接 ``partial``：``_default_processor`` 这个模块
    级名字是可替换的注入点（``run_once(processor=...)`` 之外的第二条注入路径，
    验证脚本和 ``_meas/probe_batch.py`` 都在用）。替换实现按公开契约只收 6 个
    形参，硬绑 ``operator_id`` 会让它在第一件就 TypeError，整批直接 failed。

    ``**kwargs`` 也算收：那类实现本来就声明了「什么关键字都能接」。
    """
    try:
        params = inspect.signature(processor).parameters
    except (TypeError, ValueError):
        # 内建函数 / C 扩展拿不到签名，保守起见按老行为不绑
        return processor
    accepts = ("operator_id" in params
               or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()))
    return partial(processor, operator_id=operator_id) if accepts else processor


def run_once(db: Session, batch_id: str, *,
             loader: Callable[..., dict[str, Any]] | None = None,
             processor: Callable[..., dict[str, Any]] | None = None,
             account: Account | None = None,
             client: Any = None) -> dict[str, Any]:
    """执行 current_index 指向的那一件，返回这一件的结果。

    `loader` / `processor` 可注入是为了让验证脚本不联网、不碰上游；
    生产路径用默认实现（shop_detail + save_from_collect_box）。
    """
    batch = get(db, batch_id)
    rows = items_of(db, batch.id)
    if not rows:
        raise errors.not_found(f"批次 {batch.id} 没有子项", batchId=batch.id)

    # --- 闸门：状态不允许推进 -------------------------------------------------
    # 必须「明确不推进并说清原因」，而不是静默返回空：
    # 前端靠 message 决定是提示「先 resume」还是「已经跑完了」。
    if batch.status == STATUS_PAUSED:
        return _outcome(db, batch, None, message="批次已暂停，resume 后才能继续推进")
    if batch.status == STATUS_STOPPED:
        return _outcome(db, batch, None, message="批次已停止，请 retry-failed 或新建批次")
    if batch.status == STATUS_FAILED:
        return _outcome(db, batch, None,
                        message="批次已失败，请先调用 retry-failed 重试失败商品")
    if batch.status in _TERMINAL:
        return _outcome(db, batch, None, message="批次已完成，没有可推进的条目")

    _advance(batch, rows)
    if batch.current_index >= len(rows):
        batch.status = STATUS_DONE
        batch.finished_at = batch.finished_at or now_ts()
        db.flush()
        return _outcome(db, batch, None, message="批次已完成，没有可推进的条目")

    item = rows[batch.current_index]

    detail_id = item.detail_id
    shop_id = batch.shop_id or ""
    settings = batch.settings if isinstance(batch.settings, dict) else {}

    # --- 客户端（可注入；生产路径按账号建、用完关） ---------------------------
    load = loader or _default_loader
    owned_client = False
    target = account
    if client is None:
        from services import account_service

        target = target or account_service.require_account(db, batch.account_id)
        client = account_service.account_client(target)
        owned_client = True
    elif target is None:
        from services import account_service

        target = account_service.require_account(db, batch.account_id)

    try:
        # --- 取详情快照并算哈希（幂等闸的判据） -------------------------------
        try:
            snapshot = load(client, detail_id, shop_id)
        except errors.AppError:
            raise
        except Exception as exc:  # noqa: BLE001 - 上游异常统一算这件失败
            _fail(db, batch, item, _message_of(exc), unknown=True)
            db.flush()
            return _outcome(db, batch, item, advanced=False, message=item.message)
        digest = snapshot_hash(snapshot)

        # 闸 1：跨批次幂等。同一店铺里这件商品、这份快照已经提交成功过，
        # 就不再提交第二次。`prepare_weight` 不幂等，重复入仓会让重量一次次
        # 加增量。这是老版 `automation-save-intent-*.json` 那份记账的替代品：
        # 老版靠「意图文件存在就先去妙手核对」，新版靠「上次成功的哈希还在」。
        previous = _submitted_before(db, batch, detail_id, digest)
        if previous is not None:
            item.status = ITEM_OK
            item.payload_hash = digest
            item.message = f"同店铺已有同快照的成功记录（批次 {previous.batch_id}），跳过重复提交"
            item.updated_at = now_ts()
            _record(batch, item)
            batch.current_index = min(int(batch.current_index or 0) + 1, len(rows))
            batch.updated_at = now_ts()
            _advance(batch, rows_of(db, batch))
            db.flush()
            return _outcome(db, batch, item, advanced=True, message=item.message)

        # 闸 2：本条目上次停在进行中（进程崩在提交中间），结果未知 → 拒绝自动重跑。
        # 必须先在妙手核对，确认没保存过再 retry-failed(allowUnknown=true)。
        if item.status == ITEM_RUNNING and item.payload_hash == digest:
            raise errors.AppError(
                errors.RETRY_UNSAFE,
                f"商品 {detail_id} 上一次保存可能已提交，请先在妙手核对，"
                "确认未保存后再调用 retry-failed（带 allowUnknown=true）",
                status=409,
                details={"detailId": detail_id, "batchId": batch.id,
                         "payloadHash": digest})

        if int(item.attempt or 0) >= MAX_ATTEMPT:
            raise errors.AppError(
                errors.RETRY_UNSAFE,
                f"商品 {detail_id} 已失败 {item.attempt} 次，请人工核对该商品后再重试",
                status=409,
                details={"detailId": detail_id, "attempt": int(item.attempt or 0)})

        # 先落「意图」再动手：把 payload_hash 和 running 写下去，是崩机后
        # 唯一能判定「这件可能已经提交过」的依据。顺序不能反——先跑再记，
        # 进程正好死在提交与记录之间，重启后就完全无从判断了。
        item.status = ITEM_RUNNING
        item.payload_hash = digest
        item.message = ""
        item.updated_at = now_ts()
        batch.status = STATUS_RUNNING
        batch.updated_at = now_ts()
        # The intent must survive a process crash while the processor is running.
        # A flush alone is rolled back when the request session closes after a crash.
        db.commit()

        run = processor
        if run is None:
            # 默认实现带上「建这批的人」：批次里逐件入仓的 operator_id 应该
            # 跟批次创建人一致，而不是空着——否则批次跑完后商品列表里
            # 全是「谁也没改」，明明有人点了「开始」。
            #
            # 但**不能无条件绑**：`_default_processor` 是可替换的（验证脚本与
            # _meas/probe_batch.py 会整个换掉它，用来不联网跑通整条编排）。
            # 那些替换实现只有 6 个形参，硬塞 operator_id 会直接 TypeError，
            # 让批次第一件就 failed。所以先看它收不收这个关键字。
            run = _bind_operator_id(_default_processor, batch.operator_id)
        try:
            result = run(db, target, client, detail_id, shop_id, settings)
        except errors.AppError as exc:
            _fail(db, batch, item, _message_of(exc),
                  unknown=exc.code in (errors.UPSTREAM_UNKNOWN_RESULT,
                                       errors.UPSTREAM_TIMEOUT))
            db.flush()
            return _outcome(db, batch, item, advanced=False, message=item.message)
        except Exception as exc:  # noqa: BLE001 - 任何异常都只影响这一件
            logger.exception("批次 %s 处理商品 %s 失败", batch.id, detail_id)
            _fail(db, batch, item, _message_of(exc), unknown=True)
            db.flush()
            return _outcome(db, batch, item, advanced=False, message=item.message)
    finally:
        if owned_client and client is not None:
            # 关连接失败不该影响结果（原来就是 except: pass）
            with contextlib.suppress(Exception):  # noqa: BLE001
                client.close()

    # 成功：advance 一件。current_index 用「当前位置 +1」而不是「重新搜索」，
    # 保证顺序推进；跳过的 ok/skipped 交给 _advance 再越过。
    item.status = ITEM_OK
    item.message = ""
    item.updated_at = now_ts()
    batch.current_index = min(int(batch.current_index or 0) + 1, len(rows))
    batch.updated_at = now_ts()
    _record(batch, item)
    _advance(batch, rows_of(db, batch))
    db.flush()
    body = result if isinstance(result, Mapping) else {}
    product = body.get("product") if isinstance(body.get("product"), Mapping) else None
    outcome = _outcome(db, batch, item, advanced=True,
                       message=f"商品 {detail_id} 处理完成", product=product)
    if body.get("images") is not None:
        outcome["images"] = body["images"]
    return outcome


def _submitted_before(db: Session, batch: Batch, detail_id: str,
                      digest: str) -> BatchItem | None:
    """同店铺、同 detailId、同快照，是否已经有一条成功记录（别的批次里）。"""
    return (db.query(BatchItem)
            .join(Batch, Batch.id == BatchItem.batch_id)
            .filter(Batch.shop_id == (batch.shop_id or ""),
                    BatchItem.detail_id == detail_id,
                    BatchItem.payload_hash == digest,
                    BatchItem.status == ITEM_OK,
                    BatchItem.batch_id != batch.id)
            .order_by(BatchItem.id.desc())
            .first())


def _outcome(db: Session, batch: Batch, item: BatchItem | None, *,
             advanced: bool = False,
             message: str = "", product: Mapping[str, Any] | None = None,
             operator_name: str = "") -> dict[str, Any]:
    """组装 step 的返回体。

    带 `db` 只是为了查一次「建这批的人」的名字（operators 表就几个人，
    一次查询），不是为了写库。`operator_name` 显式传入时优先用传入的。
    """
    if not operator_name:
        operator_name = operator_names(db).get(batch.operator_id or 0, "")
    body: dict[str, Any] = {
        "batch": serialize(batch, operator_name=operator_name),
        "item": serialize_item(item) if item is not None else None,
        "advanced": bool(advanced),
        "message": message,
    }
    if product is not None:
        body["product"] = dict(product)
    return body


def _message_of(exc: BaseException) -> str:
    text = getattr(exc, "message", None) or str(exc) or type(exc).__name__
    return text[:500]


# --- 控制 -----------------------------------------------------------------
def control(db: Session, batch_id: str, action: Any) -> dict[str, Any]:
    """pause / resume / stop。"""
    command = str(action or "").strip().lower()
    if command not in ("pause", "resume", "stop"):
        raise errors.validation("action 只能是 pause / resume / stop", field="action",
                                value=action)
    batch = get(db, batch_id)
    rows = items_of(db, batch.id)

    if command == "pause":
        if batch.status not in (STATUS_RUNNING, STATUS_PENDING):
            raise errors.validation(f"批次当前状态是 {batch.status}，不能暂停",
                                    field="action", value=command)
        batch.status = STATUS_PAUSED
        message = "批次已暂停"
    elif command == "resume":
        if batch.status != STATUS_PAUSED:
            raise errors.validation(f"批次当前状态是 {batch.status}，不在暂停状态",
                                    field="action", value=command)
        batch.status = STATUS_RUNNING
        message = "批次已继续"
    else:
        if batch.status in _TERMINAL:
            raise errors.validation("批次已完成，不能停止", field="action", value=command)
        # 未跑的一律标 skipped：停止之后这些件既不欠跑、也不该被 retry_failed
        # 顺手带回队列（retry_failed 只捞 failed，这是有意的）。
        skipped = 0
        for row in rows:
            if row.status in (ITEM_PENDING, ITEM_RUNNING):
                row.status = ITEM_SKIPPED
                row.message = "批次已停止，未处理"
                row.updated_at = now_ts()
                _record(batch, row)
                skipped += 1
        batch.status = STATUS_STOPPED
        batch.finished_at = now_ts()
        message = f"批次已停止，{skipped} 件标为跳过"

    batch.updated_at = now_ts()
    db.flush()
    return {"batch": serialize(batch, operator_name=operator_names(db).get(
                batch.operator_id or 0, "")), "message": message}


def retry_failed(db: Session, batch_id: str, *, allow_unknown: bool = False) -> dict[str, Any]:
    """把 failed 改回 pending，并把 current_index 拨回第一个 pending 的位置。"""
    batch = get(db, batch_id)
    if batch.status in _TERMINAL:
        raise errors.validation("批次已完成，没有可重试的商品", field="batchId",
                                value=batch.id)
    rows = items_of(db, batch.id)
    targets = [row for row in rows if row.status == ITEM_FAILED]
    if not targets:
        raise errors.validation("当前批次没有失败的商品", field="batchId", value=batch.id)

    if not allow_unknown:
        unknown = [row.detail_id for row in targets
                   if UNKNOWN_MARK in (row.message or "")]
        if unknown:
            # 结果未知的件不允许一键重跑：重跑 = 可能第二次入仓（重量再加一次）。
            # 必须先在妙手核对，再显式带 allowUnknown=true。
            raise errors.AppError(
                errors.RETRY_UNSAFE,
                f"有 {len(unknown)} 件保存结果未知，请先在妙手核对："
                + "、".join(unknown[:5]),
                status=409,
                details={"detailIds": unknown[:20], "batchId": batch.id})

    for row in targets:
        row.status = ITEM_PENDING
        # 结果未知的件是「人工核对过、确认没保存」才走到这里的，
        # 计数必须清零：否则 attempt 已经撞上 MAX_ATTEMPT，人核完了也推不动。
        if allow_unknown and UNKNOWN_MARK in (row.message or ""):
            row.attempt = 0
        row.message = ""
        row.updated_at = now_ts()

    # current_index 拨回第一条 pending：中间跳过的 ok 件靠 _advance 再越过，
    # 这样「重试」不会把已经成功的件重跑一遍。
    pending_indexes = [index for index, row in enumerate(rows)
                       if row.status in (ITEM_PENDING, ITEM_RUNNING)]
    batch.current_index = pending_indexes[0] if pending_indexes else 0
    batch.status = STATUS_RUNNING
    batch.finished_at = None
    batch.updated_at = now_ts()
    db.flush()
    return {"batch": serialize(batch, operator_name=operator_names(db).get(
                batch.operator_id or 0, "")), "retried": len(targets),
            "message": f"{len(targets)} 件已改回待处理"}


def delete(db: Session, batch_id: str) -> dict[str, Any]:
    """删除批次。跑着的不许删——删了 current_index 就丢了，等于丢进度。"""
    batch = get(db, batch_id)
    if batch.status not in _DELETABLE:
        raise errors.validation(
            f"批次状态是 {batch.status}，只有 done / stopped / failed 可以删除",
            field="batchId", value=batch.id)
    count = (db.query(BatchItem).filter(BatchItem.batch_id == batch.id).count())
    db.query(BatchItem).filter(BatchItem.batch_id == batch.id).delete(synchronize_session=False)
    db.delete(batch)
    db.flush()
    return {"deleted": True, "id": batch_id, "items": count}
