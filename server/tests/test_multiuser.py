"""多人共用的四块地基：身份解析、审计留痕、乐观锁、认领互斥。

这四块东西有一个共同点：**平时一条用例都不会触发，出事的代价却最大**。

  · 身份解析错了，审计里把张三做的事记到「本机」名下——数据全对，追责全错；
  · 审计写失败把业务请求带崩，或者反过来业务回滚把审计一起回滚掉——
    日志看着正常，实际丢了一条；
  · 乐观锁只在「两个人真的同时点保存」时才生效，用手点永远测不出来；
  · 认领互斥漏一次，妙手那边就多一条采集箱记录，本工具没有接口能退回来。

所以这里全部用**真线程 + 各自独立的 SessionLocal** 来验，而不是顺序调用两次：
顺序调用证明不了任何并发性质，那恰恰是这几个机制唯一存在的理由。

数据目录由 ``tests/conftest.py`` 在 import 期钉到临时目录（``AOHUANG_DATA_DIR``），
``db.session`` 的 engine 就是在那一刻按它建好的。因此这个文件**绝不自己设**
``AOHUANG_DATA_DIR``——那会让 engine 指向另一个库，而真实 ``app.db`` 由
conftest 的会话级守门 fixture 事后校验。
"""
from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from api import deps
from core import errors
from core.config import get_settings
from db.session import SessionLocal, init_db
from models.group import Group
from models.operator import Operator
from models.product import Product
from services import audit_service, collect_box_service, operator_service, warehouse_service
from services.account_service import Account


# --- 基础设施 -------------------------------------------------------------
@pytest.fixture(scope="module")
def schema() -> None:
    """建表 + 补列 + 种子设置。

    平时由 lifespan 里的启动钩子调用；这里直接调服务层，没有 lifespan 可依赖。
    ``init_db()`` 幂等（create_all + _add_missing_columns + 只补缺失的种子键），
    与其它测试模块重复调用没有副作用。
    """
    init_db()


@pytest.fixture()
def db(schema):
    """每条用例一个会话，结束无条件回滚。

    显式请求 ``schema`` 而不是靠 autouse 的隐式顺序：表必须在使用会话之前建好。
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _suffix() -> str:
    """每用例唯一的后缀：所有造出来的名字/键都带上它，用例之间互不干扰。"""
    return uuid.uuid4().hex[:8]


def _digits() -> str:
    """一个随机的纯数字标识（认领与仓库键的校验都要求纯数字）。"""
    return str(uuid.uuid4().int % 10**10)


# --- 一、身份解析 ---------------------------------------------------------
def test_only_secret_key_resolves_to_builtin_operator(db):
    """向后兼容命门：只带 secret.key 的老客户端必须被解析成「本机」，不是 401。"""
    local = operator_service.ensure_default(db)
    db.commit()

    operator = deps.current_operator(
        db, authorization=f"Bearer {get_settings().local_secret()}")

    assert operator.name == operator_service.DEFAULT_OPERATOR_NAME
    # token 与 secret.key 对齐，是老客户端一个字节都不用改的前提
    assert operator.token == get_settings().local_secret()
    assert operator.id == local.id


def test_operator_token_resolves_to_that_person(db):
    """每人一个 token 的正路：解析出来的就是那个人。"""
    name = f"张三-{_suffix()}"
    row, token = operator_service.create(db, name)
    db.commit()

    operator = deps.current_operator(db, authorization=f"Bearer {token}")

    assert operator.id == row.id
    assert operator.name == name


def test_operator_display_name_cannot_be_used_as_bearer(db):
    """显示名是公开信息，绝不能当 Bearer 凭据绕过 token 直接登录。"""
    name = f"公开名字-{_suffix()}"
    operator_service.create(db, name)
    db.commit()

    with pytest.raises(errors.AppError) as info:
        deps.require_token(authorization=f"Bearer {name}", db=db)
    assert info.value.code == errors.TOKEN_INVALID
    assert info.value.status == 401

    with pytest.raises(errors.AppError) as info:
        deps.current_operator(db, authorization=f"Bearer {name}")
    assert info.value.code == errors.TOKEN_INVALID
    assert info.value.status == 401


def test_declared_operator_name_with_shared_secret(db):
    """`X-Operator` 只报名字时必须另有一个有效凭据，且名字可以百分号编码。

    HTTP 头只能放 ASCII，中文名只能编码后传（见 deps._declared_operator）；
    这里走的就是前端实际用的那条路。
    """
    name = f"李四-{_suffix()}"
    row, _token = operator_service.create(db, name)
    db.commit()

    operator = deps.current_operator(
        db,
        authorization=f"Bearer {get_settings().local_secret()}",
        x_operator=quote(name),
    )

    assert operator.id == row.id
    assert operator.name == name


def test_unknown_declared_operator_is_rejected(db):
    """声明了不存在的名字要明确 401，不能静默记成「本机」。

    静默降级会让审计出现「张三做的事记在本机名下」——比拒绝更糟。
    """
    with pytest.raises(errors.AppError) as info:
        deps.current_operator(
            db,
            authorization=f"Bearer {get_settings().local_secret()}",
            x_operator=f"查无此人-{_suffix()}",
        )

    assert info.value.code == errors.TOKEN_INVALID
    assert info.value.status == 401


def test_missing_credential_is_auth_required(db):
    """什么都不带 = 401 AUTH_REQUIRED，与 0.1 完全一致。"""
    with pytest.raises(errors.AppError) as info:
        deps.current_operator(db)

    assert info.value.code == errors.AUTH_REQUIRED
    assert info.value.status == 401


def test_optional_operator_returns_none_without_credential(db):
    """免鉴权的 GET /operators 靠这个 None 回落到「本机」，不能抛。"""
    assert deps.optional_operator(db) is None


def test_disabled_operator_is_rejected(db):
    """停用必须真的挡住写入路径，否则「停用」只是界面上的一个标记。"""
    name = f"停用的人-{_suffix()}"
    row, token = operator_service.create(db, name)
    db.commit()
    operator_service.disable(db, row.id, is_admin_operator=True)
    db.commit()

    with pytest.raises(errors.AppError) as info:
        deps.current_operator(db, authorization=f"Bearer {token}")

    assert info.value.code == errors.TOKEN_INVALID
    assert info.value.status == 403
    assert info.value.details.get("enabled") is False
    assert str(info.value.details.get("operatorId")) == str(row.id)


def test_last_global_manager_cannot_be_disabled(db):
    """最后一个启用中的老师/旧管理员不能停用：停了就没人能管理操作员。

    为了让「只剩下一个管理员」这个前提不受用例执行顺序影响，这里先把其它
    管理员临时降级，结束后在 finally 里恢复——被验的是 `disable` 的判定，
    不是库里的现状。
    """
    local = operator_service.ensure_default(db)
    db.commit()

    others = [(row.id, row.role, row.is_admin) for row in db.query(Operator).all()
              if row.id != local.id and operator_service.role_of(row) in {
                  operator_service.ROLE_ADMIN, operator_service.ROLE_TEACHER}]
    try:
        for ident, _role, _is_admin in others:
            row = db.query(Operator).filter(Operator.id == ident).one()
            row.role = operator_service.ROLE_MEMBER
            row.is_admin = False
        db.commit()

        managers = [row for row in db.query(Operator).filter(Operator.enabled.is_(True)).all()
                    if operator_service.role_of(row) in {
                        operator_service.ROLE_ADMIN, operator_service.ROLE_TEACHER}]
        assert len(managers) == 1

        with pytest.raises(errors.AppError) as info:
            operator_service.disable(db, local.id, is_admin_operator=True)

        assert info.value.code == errors.FORBIDDEN
        # 409 而不是 403：这是「当前状态不允许」，不是「你没权限」
        assert info.value.status == 409
        assert info.value.details.get("reason") == "last_admin"
        # 判定必须在写之前：抛异常之后这一行不能被改成停用
        assert db.query(Operator).filter(Operator.id == local.id).one().enabled is True

        with pytest.raises(errors.AppError) as downgrade_info:
            operator_service.set_role(
                db, local.id, operator_service.ROLE_MEMBER, is_admin_operator=True)
        assert downgrade_info.value.status == 409
        assert downgrade_info.value.details.get("reason") == "last_admin"
    finally:
        for ident, role, is_admin in others:
            row = db.query(Operator).filter(Operator.id == ident).one_or_none()
            if row is not None:
                row.role = role
                row.is_admin = is_admin
        db.commit()


def test_non_admin_cannot_disable(db):
    """非管理员停用别人 → 403 reason=not_admin。"""
    row, _token = operator_service.create(db, f"普通操作员-{_suffix()}")
    target, _t2 = operator_service.create(db, f"另一个-{_suffix()}")
    db.commit()

    with pytest.raises(errors.AppError) as info:
        operator_service.disable(db, target.id, is_admin_operator=bool(row.is_admin))

    assert info.value.status == 403
    assert info.value.details.get("reason") == "not_admin"


# --- 二、审计 -------------------------------------------------------------
def test_audit_success_writes_row_with_operator_snapshot(db):
    """成功路径：一条审计落库，operator_id 与 operator_name 都写。"""
    name = f"操作人-{_suffix()}"
    row, _token = operator_service.create(db, name)
    db.commit()
    action = f"test.ok.{_suffix()}"

    audit_service.record(db, operator=row, action=action,
                         target_kind="product", target_id="11:22",
                         detail={"fields": ["groupId"]})
    db.commit()

    items = audit_service.query(db, action=action)["items"]
    assert len(items) == 1
    assert items[0]["operatorName"] == name
    assert items[0]["operatorId"] == str(row.id)
    assert items[0]["detail"] == {"fields": ["groupId"]}


def test_audit_write_failure_does_not_reach_business_caller(db, monkeypatch):
    """`_write` 直接炸掉时，`record` 既不抛，调用方的业务写入也照常提交。"""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("模拟审计写入失败")

    monkeypatch.setattr(audit_service, "_write", _boom)
    group_name = f"审计容错-{_suffix()}"

    db.add(Group(name=group_name, color="slate", sort=0))
    audit_service.record(db, operator=None, action="test.boom",
                         target_kind="group", target_id="x")
    db.commit()  # 这一步若抛异常，说明审计把业务连累了

    assert db.query(Group).filter(Group.name == group_name).count() == 1


def test_audit_constraint_violation_is_isolated_by_savepoint(db):
    """真实 SQL 层失败也必须只回滚审计那一段（SAVEPOINT 的意义）。

    手法：给审计塞一个不存在的 operator_id，让 SQLite 的外键约束在 flush 时
    报 IntegrityError。这时候调用方会话里已经有一笔未提交的业务写入——如果
    审计写在一个没有 SAVEPOINT 的同一事务里，这条 IntegrityError 之后整个
    会话就废了，业务写入也跟着没。它必须只炸掉自己那一段。
    """
    group_name = f"外键容错-{_suffix()}"
    ghost = SimpleNamespace(id=999_999_999, name="幽灵操作员")
    action = f"test.fk.{_suffix()}"

    db.add(Group(name=group_name, color="slate", sort=0))
    audit_service.record(db, operator=ghost, action=action,
                         target_kind="operator", target_id="1")
    db.commit()

    assert db.query(Group).filter(Group.name == group_name).count() == 1
    # 那条审计本来就不该存在（外键挡住了），而不是「半条」
    assert audit_service.query(db, action=action)["total"] == 0


def test_record_failure_rolls_back_caller_and_swallows(db):
    """失败审计用独立会话：先回滚调用方（放掉写锁），自己的失败也不上抛。"""
    group_name = f"失败审计-{_suffix()}"
    action = f"test.failed.{_suffix()}"

    db.add(Group(name=group_name, color="slate", sort=0))
    db.flush()  # 调用方此刻握着一笔未提交的业务写入
    audit_service.record_failure(db=db, operator=None, action=action,
                                 target_kind="group", target_id="x",
                                 message="VALIDATION_ERROR: 模拟失败")

    # 业务写入被回滚掉了（走到这里说明这次请求已经注定失败）
    assert db.query(Group).filter(Group.name == group_name).count() == 0
    # 但失败审计本身用独立会话提交成功
    items = audit_service.query(db, action=action)["items"]
    assert len(items) == 1
    assert items[0]["result"] == "failed"


def test_audit_operator_name_survives_rename_and_deletion(db):
    """历史审计里的名字是快照：改名、删行之后都不许被改写。

    这是审计表的第一原则——历史不能被后来的改动污染，否则「当时是谁做的」
    这个问题永远答不准。
    """
    old_name = f"离职员工-{_suffix()}"
    new_name = f"改名之后-{_suffix()}"
    action = f"test.history.{_suffix()}"

    row, _token = operator_service.create(db, old_name)
    db.commit()
    audit_service.record(db, operator=row, action=action,
                         target_kind="product", target_id="33:44")
    db.commit()

    operator_service.rename(db, row.id, new_name)
    db.commit()
    assert audit_service.query(db, action=action)["items"][0]["operatorName"] == old_name

    # 硬删（operator_service 没有删除接口，只有停用；这里模拟「行被清掉了」）
    db.delete(db.query(Operator).filter(Operator.id == row.id).one())
    db.commit()

    items = audit_service.query(db, action=action)["items"]
    assert items[0]["operatorName"] == old_name
    # ON DELETE SET NULL：关联断掉，快照仍在
    assert items[0]["operatorId"] is None


# --- 三、乐观锁 -----------------------------------------------------------
def _make_product_with_group(db) -> tuple[str, int]:
    """造一件 revision=1 的商品 + 一个分组，返回 (warehouse_key, group_id)。"""
    suffix = _suffix()
    group = Group(name=f"并发分组-{suffix}", color="slate", sort=0)
    db.add(group)
    detail_id = _digits()
    shop_id = _digits()
    product = Product(warehouse_key=f"{detail_id}:{shop_id}", detail_id=detail_id,
                      shop_id=shop_id, title="并发测试商品", revision=1,
                      status="ready", payload={})
    db.add(product)
    db.commit()
    return product.warehouse_key, group.id


def _update_worker(key: str, group_id: int, expected: int | None,
                   barrier: threading.Barrier, out: list) -> None:
    """一个线程 = 一个人：自己的会话、自己的事务，同时点保存。"""
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)
        try:
            result = warehouse_service.update(
                session, key, set_group=True, group_id=str(group_id),
                expected_revision=expected)
            session.commit()
            out.append(("ok", result["product"]["revision"]))
        except errors.AppError as exc:
            session.rollback()
            out.append(("err", exc.code, exc.status, exc.details))
        except Exception as exc:  # noqa: BLE001 - 收集起来当断言失败的理由
            session.rollback()
            out.append(("crash", type(exc).__name__, str(exc)))
    finally:
        session.close()


def test_two_threads_with_same_revision_exactly_one_wins(db):
    """两人拿同一版版本号同时保存：恰好一个成功、一个 409。

    这条是用例集里唯一能证明「乐观锁真的生效」的东西。顺序调用两次证明不了：
    第二次会被快路径（expected != actual）拦掉，那是最容易也对的那条分支；
    真正的判定权在 `WHERE id=? AND revision=?` 这条条件 UPDATE 上，只有并发
    才能走到 rowcount=0。
    """
    key, group_id = _make_product_with_group(db)
    out: list = []
    barrier = threading.Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_update_worker, key, group_id, 1, barrier, out)
                   for _ in range(2)]
        for future in futures:
            future.result(timeout=60)

    assert len(out) == 2, out
    oks = [item for item in out if item[0] == "ok"]
    errs = [item for item in out if item[0] == "err"]
    assert not [item for item in out if item[0] == "crash"], out
    assert len(oks) == 1, out
    assert len(errs) == 1, out

    assert errs[0][1] == errors.REVISION_CONFLICT
    assert errs[0][2] == 409
    # details 固定带 expected / actual / hint：前端不必解析 message 就能提示
    assert errs[0][3]["expected"] == 1
    assert errs[0][3]["actual"] == 2
    assert errs[0][3]["hint"]

    # 只有赢家推了一版；输家不许留下任何写入
    fresh = SessionLocal()
    try:
        product = (fresh.query(Product)
                   .filter(Product.warehouse_key == key).one())
        assert product.revision == 2
        assert product.group_id == group_id
    finally:
        fresh.close()


def test_expected_revision_none_skips_the_check(db):
    """不传版本号 = 主动放弃这一层保护，但仍然推进版本号。

    必须推进：否则不传参数的写入方能把别人手上的版本号「冻结」住，
    下一次他带版本号来反而是他赢。
    """
    key, group_id = _make_product_with_group(db)

    result = warehouse_service.update(db, key, set_group=True,
                                      group_id=str(group_id),
                                      expected_revision=None)
    db.commit()

    assert result["product"]["revision"] == 2


# --- 四、认领互斥 ---------------------------------------------------------
class _CountingClient:
    """数上游调用次数的假客户端。

    `claim_to_shop` 里用两道 Event 卡住：第一道告诉主线程「我进来了」，
    第二道等主线程放行。这样另一个线程必然在「锁被持有」的窗口里撞锁，
    而不是碰巧等赢家跑完之后再顺序执行——后者会让这条用例永远绿。
    """

    def __init__(self, gate: threading.Event, started: threading.Event) -> None:
        self.calls: list[tuple[tuple[int, ...], tuple[int, ...]]] = []
        self._gate = gate
        self._started = started

    def claim_to_shop(self, detail_ids, shop_ids):  # noqa: ANN001 - 模仿上游签名
        self.calls.append((tuple(detail_ids), tuple(shop_ids)))
        self._started.set()
        self._gate.wait(timeout=30)
        return {"ok": True}

    def close(self) -> None:
        return None


def _claim_worker(account, detail_id: str, shop_id: str, target_shop_id: str,
                  barrier: threading.Barrier, out: list) -> None:
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)
        try:
            # mark_collected=False：这条用例只验互斥，不让两个会话去抢商品行的
            # 写锁（那会变成在测 SQLite，而不是在测这把互斥锁）。
            result = collect_box_service.claim(
                session, account, detail_id=detail_id, shop_id=shop_id,
                target_shop_id=target_shop_id, mark_collected=False)
            out.append(("ok", result))
        except errors.AppError as exc:
            out.append(("err", exc.code, exc.status, exc.details))
        except Exception as exc:  # noqa: BLE001 - 收集起来当断言失败的理由
            out.append(("crash", type(exc).__name__, str(exc)))
    finally:
        session.rollback()
        session.close()


def test_concurrent_claim_calls_upstream_exactly_once(db, monkeypatch):
    """两人同时认领同一件商品：上游 `claim_to_shop` 只被调用一次。

    认领是不可撤销的上游动作——多调一次，妙手采集箱里就多一条记录，本工具
    没有接口能把它退回来。所以这里断言的是「调用次数」，不是「返回好看」。
    """
    account = Account(name=f"认领账号-{_suffix()}", app_key="k",
                      app_secret_enc="s", site="MY")
    detail_id = _digits()
    shop_id = _digits()
    gate = threading.Event()
    started = threading.Event()
    client = _CountingClient(gate, started)

    monkeypatch.setattr(collect_box_service, "account_client", lambda _account: client)
    monkeypatch.setattr(
        collect_box_service, "require_shop",
        lambda _db, _account, wanted: {"shopId": str(wanted), "name": f"店铺{wanted}"})

    out: list = []
    barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_claim_worker, account, detail_id, shop_id, shop_id,
                              barrier, out) for _ in range(2)]
        assert started.wait(timeout=30), "上游 claim_to_shop 一次都没被调用"
        time.sleep(0.5)  # 让另一个线程在这个窗口里撞上锁
        gate.set()
        for future in futures:
            future.result(timeout=60)

    assert len(client.calls) == 1, client.calls
    assert client.calls[0] == ((int(detail_id),), (int(shop_id),))

    assert len(out) == 2, out
    assert not [item for item in out if item[0] == "crash"], out
    oks = [item for item in out if item[0] == "ok"]
    errs = [item for item in out if item[0] == "err"]
    assert len(oks) == 1, out
    assert len(errs) == 1, out
    # 抢不到立刻 409，不让人盯着转圈等一个几秒的上游调用
    assert errs[0][1] == errors.CONFLICT_BUSY
    assert errs[0][2] == 409
    assert oks[0][1]["claimed"] is True
    assert oks[0][1]["count"] == 1
    assert oks[0][1]["mode"] == "direct"
