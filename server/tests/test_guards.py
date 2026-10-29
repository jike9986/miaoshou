"""数据保护闸：三处「写了却没接线」的保护，接上后必须一直被测试盯着。

这个文件的存在理由是三次真实事故，它们有同一个形状：**代码写了、注释齐全、
就是没被调用**。这类缺陷最难发现——代码在、测试在、review 也过，因为看代码
的人会以为别处调了它。所以每条断言都直接指向「这个保护到底生不生效」。

1. `warehouse_service.trim_overflow` —— 仓库上限保护从未接线。接线时发现它
   做的是**软删除最旧的商品**，挂到入仓路径上会让用户第 2001 件入仓时静默
   丢掉最旧的一件。所以这里断言的是「默认拒绝执行」，而不是「能删」。
2. `category_service.validate_rows` —— 类目树体检器从未接线。断言重点不是
   「能查出问题」，而是「查出问题时不破坏已有数据」（体检必须在 delete 之前）。
3. `config.max_body_bytes` —— 40 MB 全局上限从未被读取。断言它真的返回 413，
   而不是只有配置项好看。
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from core import errors
from core.config import get_settings
from core.constants import NEAR_LIMIT_RATIO, WAREHOUSE_MAX_ITEMS
from db.session import SessionLocal, init_db
from services import category_service, operator_service, warehouse_service


# --- fixture --------------------------------------------------------------
# 不用其它测试模块里的 db fixture：fixture 只在定义它的模块里可见，
# 跨模块 import 测试文件会把两套 schema 建表逻辑耦合在一起。
@pytest.fixture(scope="module")
def _schema() -> None:
    """建表 + 补列 + 种子设置。幂等，与其它测试模块重复调用无副作用。"""
    init_db()


@pytest.fixture()
def db(_schema):
    """每条用例一个会话，结束无条件回滚（脏数据不留给下一条）。"""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture()
def client(_schema):
    """真实 TestClient：请求体上限是在 ASGI 中间件里判的，绕不过去。"""
    from main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def auth_headers(_schema):
    db = SessionLocal()
    try:
        _operator, token = operator_service.create(
            db, f"guard-test-{uuid.uuid4().hex[:8]}",
            role=operator_service.ROLE_TEACHER)
        db.commit()
        return {"Authorization": f"Bearer {token}"}
    finally:
        db.close()


# --- 1. 仓库上限：提示而不是静默删除 --------------------------------------
def test_trim_overflow_refuses_without_explicit_confirm(db):
    """默认必须拒绝：它做的是软删除，误调用等于删用户数据。

    这条断言的意义不在「函数能抛错」，而在**锁死设计决定**：
    将来有人图省事把它挂进入仓路径，只要没传 confirm=True 就立刻炸，
    而不是安静地删掉 2000 件之外的商品。
    """
    with pytest.raises(errors.AppError) as excinfo:
        warehouse_service.trim_overflow(db)
    assert excinfo.value.code == errors.VALIDATION_ERROR
    assert "confirm" in str(excinfo.value.details.get("field", ""))
    # 提示语必须说清后果，否则读日志的人以为只是个参数忘了传
    assert "归档" in excinfo.value.message or "软删除" in excinfo.value.message


def test_trim_overflow_does_nothing_when_under_limit(db):
    """没超上限时传了 confirm 也是空操作——不能因为「确认过了」就删。"""
    assert warehouse_service.trim_overflow(db, confirm=True) == 0


def test_stats_reports_capacity_so_ui_can_warn(db):
    """上限的落地方式是「让人知道」：stats 必须给出上限与剩余量。"""
    stats = warehouse_service.stats(db)
    assert stats["maxProducts"] == WAREHOUSE_MAX_ITEMS
    assert stats["remaining"] == WAREHOUSE_MAX_ITEMS - stats["totalProducts"]
    assert isinstance(stats["nearLimit"], bool)
    # 空库当然没到提示线
    assert stats["nearLimit"] is False


def test_near_limit_ratio_is_a_warning_line_not_a_hard_stop():
    """比例必须是「提前提示」而不是「到线就停」：留出缓冲给用户安排清理。

    0.8 × 2000 = 1600，离 2000 还有 400 件。若这个比例被改成 1.0，
    提示就变成「满了才提示」，用户没有反应时间。
    """
    assert 0 < NEAR_LIMIT_RATIO < 1
    assert WAREHOUSE_MAX_ITEMS * NEAR_LIMIT_RATIO < WAREHOUSE_MAX_ITEMS


def test_over_limit_is_zero_when_under(db):
    assert warehouse_service.over_limit(db) == 0


# --- 2. 类目体检：查出问题，但别毁掉已有数据 ------------------------------
def _tree(*nodes):
    return {"data": {"cateTree": list(nodes)}}


def _node(cid, parent=None, *, leaf=True, children=None):
    return {"cid": cid, "parentCid": parent, "name": f"类目{cid}",
            "isLeaf": leaf, "children": children or []}


def test_valid_tree_passes_and_reports_ok(db):
    result = category_service.sync_from_response(
        db, _tree(_node("1", leaf=False, children=[_node("2", "1")])), site="MY")
    assert result["validate"]["ok"] is True
    assert result["total"] == 2
    assert result["leaf"] == 1


def test_duplicate_cid_aborts_the_whole_sync(db):
    """重复 cid 会被唯一索引吞掉后一条，叶子凭空消失——必须在入库前拦。"""
    with pytest.raises(errors.AppError) as excinfo:
        category_service.sync_from_response(
            db, _tree(_node("9"), _node("9")), site="MY")
    assert excinfo.value.code == errors.UPSTREAM_ERROR
    assert excinfo.value.details.get("duplicateCidTotal") == 1
    assert excinfo.value.details.get("reason") == "cateTree_invalid"


def test_bad_tree_does_not_touch_existing_rows(db):
    """这是本文件最重要的一条：体检在 delete 之前。

    如果体检放在「先删后插」之后，一次坏树就会把整站类目清空——用户界面上
    类目树突然空了，而原因是一次上游抖动。断言「坏树被拒后旧数据还在」，
    等于锁死「先验后删」这个顺序。
    """
    category_service.sync_from_response(db, _tree(_node("1")), site="MY")
    db.commit()
    before = db.query(category_service.Category).count()
    assert before == 1

    with pytest.raises(errors.AppError):
        category_service.sync_from_response(db, _tree(_node("9"), _node("9")), site="MY")

    assert db.query(category_service.Category).count() == before


def test_missing_parent_is_caught_by_the_validator_itself(db):
    """父节点不存在会让面包屑断裂，该类目挂不到树里。

    注意这条**不能**通过 `sync_from_response` 触发：它的输入是嵌套树，
    `flatten_tree` 递归时 parent_cid 永远指向真实存在的上一层，树结构里
    根本表达不出「父节点缺失」。所以直接验体检器本身——它同时服务于
    「从妙手拉的树」和「用户导入的扁平表」两条路，后者才可能出现孤儿行。
    断言直接调函数而不是构造一次假同步，是为了让测试和真实用途对齐。
    """
    rows = [
        {"cid": "1", "parent_cid": None, "is_leaf": False},
        {"cid": "9", "parent_cid": "404", "is_leaf": True},
    ]
    report = category_service.validate_rows(rows, root_cid="1")
    assert report["parentErrorTotal"] == 1
    assert report["ok"] is False
    assert "404" in report["parentErrors"][0]


def test_multiple_roots_are_reported_but_not_blocked(db):
    """多根只记进报告，不拦同步。

    硬拦会让用户彻底同步不了类目（上游偶尔返回多棵树），那比「树有点歪」
    严重得多。这个取舍写进断言，避免以后有人「顺手改成也拦」。
    """
    result = category_service.sync_from_response(
        db, _tree(_node("1"), _node("2")), site="MY")
    assert result["total"] == 2
    assert "validate" in result


# --- 3. 全局请求体上限 ----------------------------------------------------
def test_oversized_body_is_rejected_with_413(client, auth_headers):
    """声明超限的请求连读都不该读——直接 413。

    没有这道闸，一个手滑的脚本或共享部署里一台机器发的畸形请求就能把后端
    内存吃光，界面表现为「整个软件卡死」，日志里只有一条 MemoryError。
    """
    limit = get_settings().max_body_bytes
    assert limit > 0
    response = client.post(
        "/api/v1/warehouse",
        headers={**auth_headers, "Content-Length": str(limit + 1),
                 "Content-Type": "application/json"},
        content=b"{}")
    assert response.status_code == 413
    body = response.json()["error"]
    assert body["code"] == errors.VALIDATION_ERROR
    assert "过大" in body["message"]
    assert body["details"]["limitBytes"] == limit


def test_normal_sized_request_still_works(client, auth_headers):
    """上限不能误伤正常请求：一个普通的统计请求必须照旧 200。"""
    response = client.get("/api/v1/warehouse/stats", headers=auth_headers)
    assert response.status_code == 200
