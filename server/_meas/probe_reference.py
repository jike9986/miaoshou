"""只读参考路由的本地验证（TestClient + monkeypatch，不联网、不部署）。

覆盖任务书要求的 6 条：

  1. 5 个新路由都注册成功（打印 app.routes 里的真实路径）
  2. 路由顺序：静态路径在动态路径之前（/shops/warehouses 不被 /shops/{x} 吃，
     /categories/{cid}/metadata 与 /{cid}/path 并列不互相遮挡）
  3. 传了 shopId / shopIds / cid / site 时正确透传给客户端方法
  4. 上游抛 MiaoshouError → 返回错误信封（502），不是 500
  5. refresh=true 跳过缓存；连续两次不带 refresh 只调一次上游
  6. 缺参数 / 参数非法 → 400

另外补两条任务书没写但必须验的：

  * 只读路由**不受只读模式影响**（不套 require_writable）
  * 上游「不支持」→ 200 + supported:false + 空列表（不是 500）

数据目录用临时目录（AOHUANG_DATA_DIR），绝不碰真实 app.db。
结果写 JSON 文件再读——中文在 Windows 控制台会被吃成乱码。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP_DIR = Path(tempfile.mkdtemp(prefix="refprobe-"))
os.environ["AOHUANG_DATA_DIR"] = str(_TMP_DIR)
os.environ["TK_AUTOMATION_DATA_DIR"] = str(_TMP_DIR)
OUT = ROOT / "_meas" / "probe_reference.json"

from fastapi.testclient import TestClient  # noqa: E402

from api import API_PREFIX, reference  # noqa: E402
from core import errors  # noqa: E402
from core.config import get_settings  # noqa: E402
from db.session import Base, SessionLocal, engine  # noqa: E402
from main import app  # noqa: E402
from models.account import Account  # noqa: E402
from services.miaoshou import MiaoshouError  # noqa: E402

results: list[dict] = []


def check(label: str, ok: bool, detail: object = "") -> None:
    results.append({"label": label, "ok": bool(ok), "detail": detail})


# --- 假客户端 ---------------------------------------------------------------
# 记录每次调用，用来验证「参数透传」与「缓存命中时不回源」。
WAREHOUSE_PAYLOAD = {
    "result": "success", "code": "success", "message": "",
    "data": {"shopWarehouseList": [{
        "shopId": 18077147, "shopName": "C lm shop", "site": "MY",
        "warehouseList": [{
            "shopId": "18077147", "warehouseId": "7676031430461228818",
            "warehouseName": "奥皇云仓", "warehouseEffectStatus": "1",
            "warehouseSubType": "3", "isDefault": "1",
            "inventoryRule": {"allocationMode": "MANUAL", "associatedWarehouses": []},
        }],
    }]},
}
BRAND_PAYLOAD = {
    "result": "success", "code": "success", "message": "",
    "data": {"brandList": [
        {"authorizedStatus": "AUTHORIZED", "brandId": "0", "brandName": "无品牌"},
        {"authorizedStatus": "UNAUTHORIEZD", "brandId": "7543492705696057105",
         "brandName": "#aZTK"},
    ], "nextPageToken": "cGFnZV9udW1iZXI9Mg==", "totalCount": 10000},
}
MANUFACTURER_PAYLOAD = {"result": "success", "code": "success", "message": "",
                        "data": {"manufacturerList": []}}
RESPONSIBLE_PAYLOAD = {"result": "success", "code": "success", "message": "",
                       "data": {"responsiblePersonList": []}}
METADATA_PAYLOAD = {
    "result": "success", "code": "success", "message": "",
    "data": {"categoryMetadata": {
        "categoryConfig": {"packageDimensionIsRequired": "1",
                           "manufacturerIsRequired": "0",
                           "responsiblePersonIsRequired": "0",
                           "sizeChartIsRequired": "0"},
        "categorySaleAttrList": [
            {"attrId": "100000", "name": "Warna", "attributeType": 2,
             "isMandatory": False, "values": []},
        ],
        "categoryProductAttrList": [
            {"attrId": "200001", "name": "Brand", "attributeType": 1,
             "isMandatory": True, "values": [{"vid": "1", "name": "无品牌"}]},
        ],
    }},
}
PRICE_PAYLOAD = {
    "result": "success", "code": "success", "message": "",
    "data": {
        "siteAndBuyerLogisticDefaultChargeMap": {"MY": 10, "TH": 79},
        "siteAndBuyerLogisticDefaultChargeCNYMap": {"MY": 16.36, "TH": 15.61},
        "siteAndDefaultPlatformSupportChargeMap": {"MY": 0.5},
        "siteAndDefaultPlatformSupportChargeCNYMap": {"MY": 0.82},
        "tiktokCbSiteAndDefaultChargePercentMap": {
            "MY": {"platformChargePercent": 14.58, "vatChargePercent": 10},
        },
        "tiktokLocalDefaultChargePercentMap": {"withdrawChargePercent": 1},
    },
}


class FakeClient:
    """按需抛错 / 回固定载荷，并记录调用参数。"""

    def __init__(self, *, raise_error: str = "", unsupported: bool = False) -> None:
        self.calls: list[tuple] = []
        self.closed = 0
        self.raise_error = raise_error
        self.unsupported = unsupported

    # --- 内部 ---------------------------------------------------------------
    def _maybe_fail(self, name: str) -> None:
        if self.unsupported:
            raise MiaoshouError(f"妙手接口失败 [fail]：该站点不支持{name}查询",
                                code="fail", kind="error")
        if self.raise_error:
            raise MiaoshouError(self.raise_error, code="500", kind="error")

    # --- 上游方法 -----------------------------------------------------------
    def warehouse_list(self, shop_ids):
        self.calls.append(("warehouse_list", list(shop_ids)))
        self._maybe_fail("仓库")
        return WAREHOUSE_PAYLOAD

    def brand_list(self, shop_id):
        self.calls.append(("brand_list", shop_id))
        self._maybe_fail("品牌")
        return BRAND_PAYLOAD

    def brand_list_page(self, shop_id, *, page_token="", page_size=100, name=""):
        # 路由层已改为走 brand_list_page（游标分页）。它的返回是**已拆解**的
        # 分页结构（items / nextPageToken / totalCount），不是上游原始信封 ——
        # 这里必须按真实现的形状给，否则测的是假实现自己的结构。
        self.calls.append(("brand_list_page", shop_id, page_token, name))
        self._maybe_fail("品牌")
        data = BRAND_PAYLOAD["data"]
        return {"items": data["brandList"], "nextPageToken": data["nextPageToken"],
                "totalCount": data["totalCount"], "raw": BRAND_PAYLOAD}

    def brand_list_all(self, shop_id, *, max_pages=20, name=""):
        self.calls.append(("brand_list_all", shop_id, max_pages, name))
        self._maybe_fail("品牌")
        return {"items": BRAND_PAYLOAD["data"]["brandList"], "pages": 1,
                "truncated": False,
                "totalCount": BRAND_PAYLOAD["data"]["totalCount"],
                "nextPageToken": "", "error": ""}

    def manufacturer_list(self, shop_id, refresh=0):
        self.calls.append(("manufacturer_list", shop_id, refresh))
        self._maybe_fail("制造商")
        return MANUFACTURER_PAYLOAD

    def responsible_person_list(self, shop_id, refresh=0):
        self.calls.append(("responsible_person_list", shop_id, refresh))
        self._maybe_fail("负责人")
        return RESPONSIBLE_PAYLOAD

    def category_metadata(self, site, cid, shop_ids):
        self.calls.append(("category_metadata", site, cid, list(shop_ids)))
        self._maybe_fail("类目元数据")
        return METADATA_PAYLOAD

    def price_site_defaults(self):
        self.calls.append(("price_site_defaults",))
        self._maybe_fail("默认定价")
        return PRICE_PAYLOAD

    def close(self):
        self.closed += 1


_CURRENT = {"client": FakeClient()}


def _install(fake: FakeClient) -> None:
    """三个 api 模块各自 from services.account_service import account_client，
    所以要在**它们各自的命名空间**里替换，改 services 里的原件不生效。"""
    from api import categories, shops, system

    _CURRENT["client"] = fake
    for module in (shops, categories, system):
        module.account_client = lambda _account, _fake=fake: _fake


# --- 1. 路由注册与顺序 ------------------------------------------------------
def test_routes() -> None:
    paths = [getattr(route, "path", "") for route in app.routes]
    wanted = [
        f"{API_PREFIX}/shops/warehouses",
        f"{API_PREFIX}/shops/brands",
        f"{API_PREFIX}/shops/manufacturers",
        f"{API_PREFIX}/shops/responsible",
        f"{API_PREFIX}/categories/{{cid}}/metadata",
        f"{API_PREFIX}/system/price-defaults",
    ]
    for path in wanted:
        check(f"路由已注册 {path}", path in paths,
              [p for p in paths if "shops" in p or "categor" in p][:12])

    # 静态 /shops/warehouses 必须排在所有 /shops/{...} 之前（当前没有动态路由，
    # 这条断言是给将来加 /shops/{shopId} 的人留的护栏）
    static_index = paths.index(f"{API_PREFIX}/shops/warehouses")
    dynamic = [i for i, p in enumerate(paths)
               if p.startswith(f"{API_PREFIX}/shops/{{")]
    check("静态 /shops/warehouses 在动态 /shops/{..} 之前",
          all(static_index < i for i in dynamic),
          {"staticIndex": static_index, "dynamic": dynamic})

    # /categories/{cid}/metadata 与 /{cid}/path 并列，两条都在
    meta_index = paths.index(f"{API_PREFIX}/categories/{{cid}}/metadata")
    path_index = paths.index(f"{API_PREFIX}/categories/{{cid}}/path")
    check("metadata 与 path 两条都在", meta_index >= 0 and path_index >= 0,
          {"meta": meta_index, "path": path_index})

    # 顺序表本身也打出来，方便人工核对
    check("路由顺序快照", True,
          [p for p in paths if p.startswith(f"{API_PREFIX}/shops")
           or p.startswith(f"{API_PREFIX}/categories")
           or p.startswith(f"{API_PREFIX}/system")])


# --- 2. 参数透传 ------------------------------------------------------------
def test_passthrough(client: TestClient, headers: dict) -> None:
    fake = FakeClient()
    _install(fake)
    reference._cache().clear()

    resp = client.get(f"{API_PREFIX}/shops/warehouses?shopId=18077147",
                      headers=headers)
    body = resp.json()
    check("warehouses 200", resp.status_code == 200, resp.status_code)
    check("warehouses 透传 shopIds=[18077147]",
          fake.calls and fake.calls[0] == ("warehouse_list", [18077147]), fake.calls)
    rows = body.get("warehouses") or []
    check("warehouses 拍平成一行", len(rows) == 1, rows)
    check("warehouses isDefault 归一成布尔",
          rows and rows[0]["isDefault"] is True, rows[:1])
    check("warehouses 保留 upstream 原值",
          rows and rows[0]["isDefaultRaw"] == "1", rows[:1])
    check("warehouses 带 supported/cached 头部",
          body.get("supported") is True and body.get("cached") is False, body)

    fake = FakeClient()
    _install(fake)
    reference._cache().clear()
    resp = client.get(f"{API_PREFIX}/shops/brands?shopId=18077147", headers=headers)
    body = resp.json()
    check("brands 200", resp.status_code == 200, resp.status_code)
    check("brands 走分页方法并透传 shopId=18077147",
          fake.calls == [("brand_list_page", 18077147, "", "")], fake.calls)
    check("brands 两条", len(body.get("brands") or []) == 2, body.get("brands"))
    check("brands authorized 归一",
          [b["authorized"] for b in body["brands"]] == [True, False],
          body.get("brands"))
    paging = body.get("paging") or {}
    check("brands 带分页信息（上游一页只回 100 条）",
          paging.get("totalCount") == 10000 and paging.get("returned") == 2
          and paging.get("truncated") is True, paging)

    fake = FakeClient()
    _install(fake)
    reference._cache().clear()
    resp = client.get(f"{API_PREFIX}/shops/manufacturers?shopId=18077147",
                      headers=headers)
    body = resp.json()
    check("manufacturers 200", resp.status_code == 200, resp.status_code)
    check("manufacturers 默认 refresh=0 透传",
          fake.calls == [("manufacturer_list", 18077147, 0)], fake.calls)
    check("manufacturers 空列表 + empty=true",
          body.get("manufacturers") == [] and body.get("empty") is True, body)

    fake = FakeClient()
    _install(fake)
    reference._cache().clear()
    resp = client.get(f"{API_PREFIX}/shops/responsible?shopId=18077147"
                      "&refresh=true", headers=headers)
    body = resp.json()
    check("responsible 200", resp.status_code == 200, resp.status_code)
    check("responsible refresh=true 透传成上游 refresh=1",
          fake.calls == [("responsible_person_list", 18077147, 1)], fake.calls)
    check("responsible 空列表 + empty=true",
          body.get("responsible") == [] and body.get("empty") is True, body)

    fake = FakeClient()
    _install(fake)
    reference._cache().clear()
    resp = client.get(f"{API_PREFIX}/categories/600415/metadata"
                      "?site=my&shopIds=18077147", headers=headers)
    body = resp.json()
    check("metadata 200", resp.status_code == 200, resp.status_code)
    check("metadata 透传 site 大写 + cid 整数 + shopIds",
          fake.calls == [("category_metadata", "MY", 600415, [18077147])], fake.calls)
    check("metadata 带 requirements",
          (body.get("requirements") or {}).get("packageDimensionRequired") == "1",
          body.get("requirements"))
    check("metadata 必填属性只挑 isMandatory",
          [a["id"] for a in (body.get("mandatoryAttributes") or [])] == ["200001"],
          body.get("mandatoryAttributes"))
    check("metadata 属性计数",
          body.get("saleAttributeCount") == 1 and body.get("productAttributeCount") == 1,
          body)

    fake = FakeClient()
    _install(fake)
    reference._cache().clear()
    resp = client.get(f"{API_PREFIX}/system/price-defaults?site=MY", headers=headers)
    body = resp.json()
    check("price-defaults 200", resp.status_code == 200, resp.status_code)
    check("price-defaults 调 price_site_defaults（不带 site）",
          fake.calls == [("price_site_defaults",)], fake.calls)
    defaults = body.get("defaults") or {}
    check("price-defaults 切出 MY 物流费",
          defaults.get("logisticCharge") == 10, defaults.get("logisticCharge"))
    check("price-defaults 切出 MY 扣点",
          (defaults.get("chargePercent") or {}).get("platformChargePercent") == 14.58,
          defaults.get("chargePercent"))
    check("price-defaults 带整表",
          "siteAndBuyerLogisticDefaultChargeMap" in (body.get("allSites") or {}),
          list(body.get("allSites") or {})[:6])


# --- 3. 错误处理 ------------------------------------------------------------
def test_errors(client: TestClient, headers: dict) -> None:
    fake = FakeClient(raise_error="妙手接口失败 [500]：内部错误")
    _install(fake)
    reference._cache().clear()
    resp = client.get(f"{API_PREFIX}/shops/warehouses?shopId=18077147",
                      headers=headers)
    body = resp.json()
    check("上游报错 -> 502（不是 500）", resp.status_code == 502, resp.status_code)
    check("上游报错 -> 错误信封有 code",
          (body.get("error") or {}).get("code") == errors.UPSTREAM_ERROR,
          body)
    check("上游报错 -> 信封有 requestId",
          bool((body.get("error") or {}).get("requestId")), body)

    fake = FakeClient(raise_error="妙手接口失败 [500]：内部错误")
    _install(fake)
    reference._cache().clear()
    resp = client.get(f"{API_PREFIX}/categories/600415/metadata?shopIds=18077147",
                      headers=headers)
    check("metadata 上游报错 -> 502", resp.status_code == 502, resp.status_code)
    check("metadata 上游报错 -> 信封",
          (resp.json().get("error") or {}).get("code") == errors.UPSTREAM_ERROR,
          resp.json())

    # 不支持 -> 200 + supported:false + 空列表
    fake = FakeClient(unsupported=True)
    _install(fake)
    reference._cache().clear()
    resp = client.get(f"{API_PREFIX}/shops/manufacturers?shopId=18077147",
                      headers=headers)
    body = resp.json()
    check("不支持 -> 200（不是 500）", resp.status_code == 200, resp.status_code)
    check("不支持 -> supported=false", body.get("supported") is False, body)
    check("不支持 -> 空列表", body.get("manufacturers") == [], body)
    check("不支持 -> 有 reason", bool(body.get("reason")), body)

    fake = FakeClient(unsupported=True)
    _install(fake)
    reference._cache().clear()
    resp = client.get(f"{API_PREFIX}/shops/warehouses?shopId=18077147",
                      headers=headers)
    check("warehouses 不支持 -> 200 + 空列表",
          resp.status_code == 200 and resp.json().get("warehouses") == [],
          resp.json())


# --- 4. 缓存 ----------------------------------------------------------------
def test_cache(client: TestClient, headers: dict) -> None:
    fake = FakeClient()
    _install(fake)
    reference._cache().clear()

    first = client.get(f"{API_PREFIX}/shops/manufacturers?shopId=18077147",
                       headers=headers).json()
    second = client.get(f"{API_PREFIX}/shops/manufacturers?shopId=18077147",
                        headers=headers).json()
    check("连续两次只调一次上游", len(fake.calls) == 1, fake.calls)
    check("第一次 cached=false", first.get("cached") is False, first.get("cached"))
    check("第二次 cached=true", second.get("cached") is True, second.get("cached"))

    refreshed = client.get(f"{API_PREFIX}/shops/manufacturers"
                           "?shopId=18077147&refresh=true", headers=headers).json()
    check("refresh=true 跳过缓存（第二次回源）", len(fake.calls) == 2, fake.calls)
    check("refresh=true 时 cached=false", refreshed.get("cached") is False,
          refreshed.get("cached"))
    check("refresh=true 回写缓存（再取一次不回源）",
          client.get(f"{API_PREFIX}/shops/manufacturers?shopId=18077147",
                     headers=headers).json().get("cached") is True
          and len(fake.calls) == 2, fake.calls)

    # 不同 shopId 不能串
    client.get(f"{API_PREFIX}/shops/manufacturers?shopId=99999999", headers=headers)
    check("不同 shopId 各自回源", len(fake.calls) == 3, fake.calls)

    # metadata 也吃 refresh=true（类目发布要求会随平台规则变，得能强制刷）
    fake = FakeClient()
    _install(fake)
    reference._cache().clear()
    url = f"{API_PREFIX}/categories/600415/metadata?shopIds=18077147"
    client.get(url, headers=headers)
    client.get(url, headers=headers)
    check("metadata 连续两次只调一次上游", len(fake.calls) == 1, fake.calls)
    client.get(url + "&refresh=true", headers=headers)
    check("metadata refresh=true 回源", len(fake.calls) == 2, fake.calls)

    # 上游失败时用旧值兜底并如实报 degraded / staleSeconds
    fake2 = FakeClient(raise_error="妙手接口失败 [500]：内部错误")
    _install(fake2)
    store = reference._cache()
    # 缓存 key 里的账号前缀必须与路由算出来的完全一致，否则这条断言是假通过
    db = SessionLocal()
    try:
        account = db.query(Account).order_by(Account.id.asc()).first()
        prefix = reference.account_prefix(account)
    finally:
        db.close()
    store.set(f"{prefix}manufacturers|shop=18077147", MANUFACTURER_PAYLOAD, ttl=0.001)
    import time
    # Windows 上 time.monotonic 的分辨率约 15.6ms，sleep(0.01) 可能测出 0 秒流逝，
    # 那样缓存条目还会被判为「新鲜」，这条断言就成了假通过/假失败。
    # 所以等到 store.has() 真的变 False 再继续。
    deadline = time.monotonic() + 2.0
    while store.has(f"{prefix}manufacturers|shop=18077147"):
        if time.monotonic() > deadline:
            break
        time.sleep(0.02)
    resp = client.get(f"{API_PREFIX}/shops/manufacturers?shopId=18077147",
                      headers=headers)
    body = resp.json()
    check("上游失败但有旧值 -> 200 + degraded", resp.status_code == 200
          and body.get("degraded") is True, body)
    check("degraded 时 cached=true", body.get("cached") is True, body.get("cached"))
    store.clear()

    # 上游失败且无旧值 -> 502
    resp = client.get(f"{API_PREFIX}/shops/manufacturers?shopId=18077147",
                      headers=headers)
    check("上游失败且无旧值 -> 502", resp.status_code == 502, resp.status_code)


# --- 5. 参数校验 ------------------------------------------------------------
def test_validation(client: TestClient, headers: dict) -> None:
    _install(FakeClient())
    reference._cache().clear()
    cases = [
        ("warehouses 缺 shopId", f"{API_PREFIX}/shops/warehouses"),
        ("brands 缺 shopId", f"{API_PREFIX}/shops/brands"),
        ("manufacturers 缺 shopId", f"{API_PREFIX}/shops/manufacturers"),
        ("responsible 缺 shopId", f"{API_PREFIX}/shops/responsible"),
        ("warehouses shopId 非整数", f"{API_PREFIX}/shops/warehouses?shopId=abc"),
        ("warehouses shopId 为 0", f"{API_PREFIX}/shops/warehouses?shopId=0"),
        ("metadata 缺 shopIds", f"{API_PREFIX}/categories/600415/metadata"),
        ("metadata shopIds 非整数",
         f"{API_PREFIX}/categories/600415/metadata?shopIds=abc"),
        ("metadata cid 非整数",
         f"{API_PREFIX}/categories/abc/metadata?shopIds=18077147"),
    ]
    for label, url in cases:
        resp = client.get(url, headers=headers)
        body = resp.json()
        check(f"{label} -> 400", resp.status_code == 400,
              {"status": resp.status_code, "body": body})
        check(f"{label} -> VALIDATION_ERROR",
              (body.get("error") or {}).get("code") == errors.VALIDATION_ERROR,
              body)


# --- 6. 只读语义 ------------------------------------------------------------
def test_readonly(client: TestClient, headers: dict) -> None:
    """只读模式开着时这 5 条路由必须照样能用（它们不写库）。"""
    _install(FakeClient())
    reference._cache().clear()
    turned = client.post(f"{API_PREFIX}/system/settings",
                         json={"securityReadOnly": True}, headers=headers)
    check("进入只读模式", turned.status_code == 200, turned.status_code)
    urls = [
        f"{API_PREFIX}/shops/warehouses?shopId=18077147",
        f"{API_PREFIX}/shops/brands?shopId=18077147",
        f"{API_PREFIX}/shops/manufacturers?shopId=18077147",
        f"{API_PREFIX}/shops/responsible?shopId=18077147",
        f"{API_PREFIX}/categories/600415/metadata?shopIds=18077147",
        f"{API_PREFIX}/system/price-defaults?site=MY",
    ]
    for url in urls:
        resp = client.get(url, headers=headers)
        check(f"只读模式下仍可用 {url.split('?')[0].replace(API_PREFIX, '')}",
              resp.status_code == 200,
              {"status": resp.status_code, "body": str(resp.json())[:160]})
    client.post(f"{API_PREFIX}/system/settings",
                json={"securityReadOnly": False}, headers=headers)


# --- 7. 未连接 --------------------------------------------------------------
def test_not_connected(client: TestClient, headers: dict) -> None:
    """没账号时返回 401 AUTH_REQUIRED（要求 require_account）。"""
    db = SessionLocal()
    try:
        for row in db.query(Account).all():
            db.delete(row)
        db.commit()
    finally:
        db.close()
    reference._cache().clear()
    resp = client.get(f"{API_PREFIX}/shops/warehouses?shopId=18077147",
                      headers=headers)
    body = resp.json()
    check("未连接 -> 401", resp.status_code == 401, resp.status_code)
    check("未连接 -> AUTH_REQUIRED",
          (body.get("error") or {}).get("code") == errors.AUTH_REQUIRED, body)


def seed_account() -> None:
    db = SessionLocal()
    try:
        db.add(Account(name="probe-acc", app_key="probe-key",
                       app_secret_enc="probe-secret", site="MY",
                       is_default=True, status="active"))
        db.commit()
    finally:
        db.close()


def main() -> int:
    Base.metadata.create_all(bind=engine)
    test_routes()

    token = get_settings().local_secret()
    headers = {"Authorization": f"Bearer {token}"}

    with TestClient(app) as client:
        seed_account()
        test_passthrough(client, headers)
        test_errors(client, headers)
        test_cache(client, headers)
        test_validation(client, headers)
        test_readonly(client, headers)
        test_not_connected(client, headers)

    failed = [item for item in results if not item["ok"]]
    payload = {"total": len(results), "passed": len(results) - len(failed),
               "failed": len(failed), "results": results}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print("TOTAL %d PASS %d FAIL %d" % (len(results), len(results) - len(failed),
                                        len(failed)))
    for item in failed:
        print("FAIL", item["label"])
    print("OUT " + str(OUT))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
