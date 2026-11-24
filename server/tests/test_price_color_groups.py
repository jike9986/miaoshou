"""汇率按站点取 + 颜色数量上限 + 受限分组权限。

覆盖 2026-11-19 负责人提出的三个问题：
  1. 菲律宾价格偏低 —— 汇率永远用 MYR（1.6393）算比索，错 2.1 倍
  2. SKU 图数量实际是颜色数量，但一半商品没被截断
  3. 三个精品分组只给老师/管理员，学员不可见不可发不可排任务
"""
from __future__ import annotations

import json
import uuid

import pytest

from decimal import Decimal

from services import exchange_rate, price_template


@pytest.fixture(autouse=True)
def isolated_rate_cache():
    exchange_rate.reset_cache()
    yield
    exchange_rate.reset_cache()


# --- 1. 汇率按站点取 -------------------------------------------------------
PH_TEMPLATE = {
    "name": "菲律宾", "site": "PH", "profitType": "bySalePriceProfit",
    "profitPercent": 15, "paymentChargePercent": 2.24, "sfpChargePercent": 5.5,
    "withdrawChargePercent": 1, "lastMileFreightCny": 6.29,
    "orderProcessCny": 0.52, "vatChargePercent": 0,
    # 以下字段必须显式给 0：normalize_template 会把缺失项填成
    # DEFAULT_TEMPLATE 的默认值（bcp 3.24 / platformSupport 0.83），
    # 基准价就会漂移。真实菲律宾模板里这两项都是 0。
    "platformSupportCny": 0, "otherChargeCny": 0, "bcpChargePercent": 0,
    "platformCommissionPercent": 0, "affiliateChargePercent": 0,
    "freightTable": {"currency": "PHP", "firstWeightGrams": 10,
                     "firstWeightPrice": 5, "continuedUnitGrams": 10,
                     "continuedUnitPrice": 4.5},
}
MY_TEMPLATE = {
    "name": "马来", "site": "MY", "profitType": "bySalePriceProfit",
    "profitPercent": 15, "paymentChargePercent": 3.78, "vatChargePercent": 10,
    "withdrawChargePercent": 1, "bcpChargePercent": 3.24,
    "platformSupportCny": 0.81, "lastMileFreightCny": 16.17,
    "lastMileFreightServiceTaxPercent": 6,
    "freightTable": {"currency": "MYR", "firstWeightGrams": 30,
                     "firstWeightPrice": 0.06, "continuedUnitGrams": 10,
                     "continuedUnitPrice": 0.08},
}


def test_philippines_uses_php_rate_not_myr(monkeypatch):
    """菲律宾必须按 PHP 汇率算 —— 这是「价格偏低」的根因。

    实测基准（负责人提供的妙手官方试算）：35 CNY / 0.6kg = 796.54 PHP。
    旧代码拿 MYR 汇率算出 379.30 PHP，只有正确值的 47.6%，
    发出去就是亏本卖。
    """
    monkeypatch.setattr(
        exchange_rate, "live_rate",
        lambda currency="MYR", **_kw: (
            {"PHP": Decimal("0.1048"), "MYR": Decimal("1.63590083")}[currency],
            "test"))

    result = price_template.preview_price(35, 0.6, PH_TEMPLATE)

    assert result["currency"] == "PHP"
    # 与妙手官方口径 796.54 PHP 的差距必须在一元以内（纯四舍五入）
    assert abs(float(result["price"]) - 796.54) < 1.0, result["price"]
    assert "PHP" in result["rateSource"]


def test_myanmar_still_uses_myr(monkeypatch):
    """马来站不能因为这次修改而变样。"""
    monkeypatch.setattr(
        exchange_rate, "live_rate",
        lambda currency="MYR", **_kw: (
            {"PHP": Decimal("0.1048"), "MYR": Decimal("1.63590083")}[currency],
            "test"))

    result = price_template.preview_price(35, 0.6, MY_TEMPLATE)

    assert result["currency"] == "MYR"
    assert "MYR" in result["rateSource"]


@pytest.mark.parametrize("site,expected", [
    ("MY", "MYR"), ("PH", "PHP"), ("TH", "THB"), ("VN", "VND"), ("SG", "SGD"),
])
def test_each_site_resolves_its_own_currency(site, expected):
    """五个站点各自用自己的币种，不能都落到默认的 MYR。"""
    assert price_template._resolve_currency(site=site) == expected


def test_template_currency_field_wins_over_site():
    """模板里显式声明了 freightTable.currency 时以它为准。"""
    template = {"site": "MY", "freightTable": {"currency": "PHP"}}
    assert price_template._resolve_currency(template=template) == "PHP"


def test_rate_source_reports_currency_for_traceability(monkeypatch):
    """来源文案必须带上币种 —— 报价对不对要能追溯。"""
    monkeypatch.setattr(exchange_rate, "live_rate",
                        lambda currency="MYR", **_kw: (Decimal("0.1048"), "prov"))
    _rate, source = price_template.resolve_rate_with_source(
        None, template=PH_TEMPLATE)
    assert "PHP" in source


# --- 2. 颜色数量上限 -------------------------------------------------------
def _payload(colors: int, sizes: int = 2) -> dict:
    """造一件 attrId 全为 None 的商品（生产库里一半是这种）。"""
    color_values = [
        {"attrValueId": f"c{i}", "attrValue": f"Color{i}",
         "imgUrl": f"http://x/{i}.jpg"}
        for i in range(colors)
    ]
    size_values = [
        {"attrValueId": f"s{i}", "attrValue": str(35 + i), "imgUrl": ""}
        for i in range(sizes)
    ]
    sku_map = {
        f";Color{c};{35 + s};": {"price": 10.0, "originPrice": 18}
        for c in range(colors) for s in range(sizes)
    }
    return {
        "skuPropertyList": [
            {"attrId": None, "attrNameCn": "颜色", "attrValueList": color_values},
            {"attrId": None, "attrNameCn": "尺码", "attrValueList": size_values},
        ],
        "skuMap": sku_map,
    }


def test_color_attribute_detected_without_attrid():
    """attrId=None 也要认出颜色维度。

    旧代码写死 attrId == "100000"，生产库里 29 / 4 / 97 / 110 / 112
    这些商品的颜色属性 attrId 全是 None，颜色上限对它们完全没作用。
    """
    from services.warehouse_service import _is_color_property

    assert _is_color_property({"attrId": None, "attrNameCn": "颜色"}) is True
    assert _is_color_property({"attrId": "", "attrNameCn": "Warna"}) is True
    assert _is_color_property({"attrId": "100000", "attrNameCn": "X"}) is True
    # 值上挂图 = 颜色维度（最可靠判据）
    assert _is_color_property({
        "attrId": None, "attrNameCn": "Something",
        "attrValueList": [{"imgUrl": "http://x/1.jpg"}]}) is True
    # 尺码：无名、无图、无 attrId
    assert _is_color_property({
        "attrId": None, "attrNameCn": "Whatever",
        "attrValueList": [{"imgUrl": ""}]}) is False


def test_color_trim_drops_sku_combinations():
    """裁掉多余颜色时，对应 SKU 组合必须一起删。

    skuMap 的 key 是 `;颜色值文本;尺码值文本;`（不是 attrValueId）。
    旧代码拿 attrValueId 去比对，一个都删不掉，留下孤儿 SKU。
    """
    from services.warehouse_service import apply_image_limits

    payload = _payload(colors=12, sizes=2)
    assert len(payload["skuMap"]) == 24

    report = apply_image_limits(payload, main_limit=0, detail_limit=0, sku_limit=9)

    assert report["colors"] == {"before": 12, "after": 9}
    assert str(report["sku"]["dropped"]) == "6"
    assert len(payload["skuMap"]) == 18
    kept_colors = [v["attrValue"] for v in payload["skuPropertyList"][0]["attrValueList"]]
    assert kept_colors == [f"Color{i}" for i in range(9)]
    # 尺码维度不许动
    assert len(payload["skuPropertyList"][1]["attrValueList"]) == 2


def test_color_trim_noop_when_within_limit():
    from services.warehouse_service import apply_image_limits

    payload = _payload(colors=5, sizes=2)
    report = apply_image_limits(payload, main_limit=0, detail_limit=0, sku_limit=9)
    assert "colors" not in report
    assert len(payload["skuMap"]) == 10


def test_color_trim_zero_means_unlimited():
    from services.warehouse_service import apply_image_limits

    payload = _payload(colors=20, sizes=1)
    report = apply_image_limits(payload, main_limit=0, detail_limit=0, sku_limit=0)
    assert "colors" not in report
    assert len(payload["skuMap"]) == 20


# --- 3. 受限分组 -----------------------------------------------------------
def test_restricted_groups_are_the_three_expected_names():
    from services.group_access import RESTRICTED_GROUP_NAMES

    assert set(RESTRICTED_GROUP_NAMES) == {"自营海外仓", "海外仓定制款", "精品"}


class _FakeOperator:
    def __init__(self, role="member", enabled=True):
        self.role = role
        self.enabled = enabled
        self.is_admin = (role == "admin")
        self.id = 1


@pytest.mark.parametrize("role,allowed", [
    ("teacher", True), ("admin", True), ("member", False),
])
def test_only_teacher_and_admin_reach_restricted_groups(role, allowed):
    from services.group_access import may_access_restricted

    assert may_access_restricted(_FakeOperator(role)) is allowed


def test_disabled_operator_cannot_reach_restricted_groups():
    from services.group_access import may_access_restricted

    assert may_access_restricted(_FakeOperator("teacher", enabled=False)) is False


def test_no_operator_cannot_reach_restricted_groups():
    from services.group_access import may_access_restricted

    assert may_access_restricted(None) is False


@pytest.fixture
def restricted_db():
    from db.session import SessionLocal, init_db
    from models.group import Group

    init_db()
    db = SessionLocal()
    try:
        suffix = uuid.uuid4().hex[:8]
        for name in ("自营海外仓", "海外仓定制款", "精品", f"普通-{suffix}"):
            existing = db.query(Group).filter(Group.name == name).first()
            if existing is None:
                db.add(Group(name=name))
        db.commit()
        yield db
    finally:
        db.rollback()
        db.close()


def test_restricted_group_ids_include_named_groups(restricted_db):
    from services.group_access import restricted_group_ids

    ids = restricted_group_ids(restricted_db)
    assert ids, "受限分组 id 列表不应为空"


def test_member_guard_rejects_restricted_product(restricted_db):
    """学员碰受限分组的商品必须 403，且错误里说明是哪个分组。"""
    from core import errors
    from services.group_access import guard_products, restricted_group_ids

    restricted = restricted_group_ids(restricted_db)
    with pytest.raises(errors.AppError) as exc:
        guard_products(restricted_db, _FakeOperator("member"),
                       list(restricted), action="发布")
    assert exc.value.status == 403
    assert "受限" in str(exc.value)


def test_teacher_guard_passes_restricted_product(restricted_db):
    from services.group_access import guard_products, restricted_group_ids

    # 老师不该被拦：不抛异常即通过
    guard_products(restricted_db, _FakeOperator("teacher"),
                   list(restricted_group_ids(restricted_db)), action="发布")


def test_visible_group_ids_is_none_for_teacher(restricted_db):
    from services.group_access import visible_group_ids

    assert visible_group_ids(restricted_db, _FakeOperator("teacher")) is None
    assert visible_group_ids(restricted_db, _FakeOperator("admin")) is None


def test_visible_group_ids_returns_ids_for_member(restricted_db):
    from services.group_access import restricted_group_ids, visible_group_ids

    member_ids = visible_group_ids(restricted_db, _FakeOperator("member"))
    assert member_ids == restricted_group_ids(restricted_db)


def test_member_list_excludes_restricted_products(restricted_db):
    """列表查询要真的查不到受限分组的商品（分页总数也要跟着变）。"""
    from models.product import Product
    from services.warehouse_service import list_products

    db = restricted_db
    restricted = sorted(__import__("services.group_access",
                                   fromlist=["x"]).restricted_group_ids(db))
    gid = restricted[0]
    suffix = uuid.uuid4().hex[:8]
    db.add(Product(warehouse_key=f"restricted-{suffix}", detail_id=f"d-{suffix}",
                   title="受限组测试商品", status="ready", group_id=gid,
                   payload={"skuMap": {}}, revision=1, saved_at=0, updated_at=0))
    db.add(Product(warehouse_key=f"normal-{suffix}", detail_id=f"n-{suffix}",
                   title="普通组测试商品", status="ready", group_id=None,
                   payload={"skuMap": {}}, revision=1, saved_at=0, updated_at=0))
    db.commit()

    def titles(operator):
        data = list_products(db, page=1, page_size=1000, operator=operator)
        return {row["title"] for row in (data.get("products") or data.get("items") or [])}

    teacher_sees = titles(_FakeOperator("teacher"))
    member_sees = titles(_FakeOperator("member"))

    assert "受限组测试商品" in teacher_sees
    assert "受限组测试商品" not in member_sees
    assert "普通组测试商品" in member_sees

def test_member_group_list_hides_restricted_folders(restricted_db):
    """学员的分组列表里不该出现受限分组那三个文件夹。

    回归 BUG：`filter_group_options` 早就写好了，但从来没被调用过 ——
    `group_service.list_groups` 直接 `db.query(Group).all()` 全给。
    商品行是过滤掉了，可筛选栏按 Group 表渲染，学员照样看到
    「精品 0 / 自营海外仓 0」三个空壳文件夹，等于告诉他这三个组存在。
    """
    from services import group_service

    member_view = group_service.list_groups(
        restricted_db, operator_id=1, operator=_FakeOperator("member"))
    teacher_view = group_service.list_groups(
        restricted_db, operator_id=1, operator=_FakeOperator("teacher"))

    member_names = {row["name"] for row in member_view["groups"]}
    teacher_names = {row["name"] for row in teacher_view["groups"]}

    assert "精品" not in member_names
    assert "自营海外仓" not in member_names
    assert "海外仓定制款" not in member_names
    # 老师照样能看到
    assert {"精品", "自营海外仓", "海外仓定制款"} <= teacher_names
    # 非受限分组对所有身份都在
    assert any(name.startswith("普通-") for name in member_names)


def test_member_warehouse_facets_hide_restricted_group_chips(restricted_db):
    """成品仓库筛选面板的分组计数同样不能出现受限分组。"""
    from services import warehouse_service

    member_facets = warehouse_service.facets(restricted_db, operator_id=1)
    member_chips = {row["name"] for row in member_facets.get("groups") or []}
    assert "精品" not in member_chips
    assert "自营海外仓" not in member_chips
    assert "海外仓定制款" not in member_chips
