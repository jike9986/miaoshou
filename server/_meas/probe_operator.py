"""操作员身份 + 审计留痕的本地验证（0.2）。

验证目标（对应任务书六条）：
  1. 身份解析：只有 secret.key → 「本机」而**不是** 401（向后兼容命门）；
     某人 token → 那个人；乱码 token → 401 TOKEN_INVALID；什么都不带 → 401
     AUTH_REQUIRED；停用的操作员 → 403 且文案含「已被停用」；最后一个管理员
     不能被停用。
  2. 审计写入：record() 落一行且字段齐全；db.commit() 抛异常时**不往上抛**；
     操作员被删之后历史 operator_name 仍在；按 operatorId/action/targetKind
     筛选；before 游标分页不漏不重。
  3. 接线：confirm-save 成功 → action="save" 且 result="success"；
     hash 不匹配 → result="failed" 且 message 含 PAYLOAD_CHANGED；
     POST /operators 返回的 token 立刻可用；GET /operators 的 JSON 里
     **不含任何 token 明文**。
  4. 显示名回填：publish 记录有 operatorName；batch 有 operatorName；
     warehouse 列表有 updatedBy。
  5. 向后兼容：老库（有 products 表但没有 operator_id 列）跑完
     `_add_missing_columns()` 之后 `GET /warehouse` 照常 200、updatedBy 是
     空串、不报 no such column；`import main` 路由数 ≥ 126。

数据目录一律指向临时目录（AOHUANG_DATA_DIR），**绝不碰真实 app.db**。
控制台输出只用 ASCII 前缀（Windows 终端会把中文吃成乱码），
完整结果落到 _meas/_out/probe_operator.json。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 必须在 import 应用代码之前设好：core.config 用 @lru_cache 缓存数据目录
_TMP_DIR = Path(tempfile.mkdtemp(prefix="operator-probe-"))
os.environ["AOHUANG_DATA_DIR"] = str(_TMP_DIR)
os.environ["TK_AUTOMATION_DATA_DIR"] = str(_TMP_DIR)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from api import API_PREFIX  # noqa: E402
from core import errors  # noqa: E402
from db.session import Base, SessionLocal, engine, init_db  # noqa: E402
from main import app  # noqa: E402
from models.account import Account  # noqa: E402
from models.audit_log import AuditLog  # noqa: E402
from models.batch import Batch  # noqa: E402
from models.operator import Operator  # noqa: E402
from models.product import Product  # noqa: E402
from models.publish_log import PublishLog  # noqa: E402
from services import (  # noqa: E402
    audit_service,
    batch_service,
    crypto,
    operator_service,
    publish_service,
    warehouse_service,
)

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append((label, bool(ok), "" if ok else detail))


# --- 假上游 ---------------------------------------------------------------
class FakeClient:
    """够用的假妙手客户端：详情 / 类目元数据 / 关连接。"""

    def __init__(self, detail_id: str = "7001") -> None:
        self.detail_id = detail_id
        self.closed = False

    def shop_detail(self, detail_id, shop_id):  # noqa: ANN001
        return detail_response(str(detail_id or self.detail_id))

    def category_metadata(self, site, cid, shop_ids):  # noqa: ANN001
        return {}

    def close(self) -> None:
        self.closed = True


def detail_info(detail_id: str, title: str = "probe english title here") -> dict:
    """一份能过 pre_save 全套规则的详情。

    三个坑都是踩出来的，改这里之前先看注释：
      · `ossMd5` 在外层 `data` 上而不是 `shopCollectItemInfo` 里，
        缺了 product_service 直接 422「商品详情缺少 ossMd5」；
      · 每个 SKU 必须有 `weight`，否则报「无法识别包裹重量」；
      · skuMap 的键必须是 `颜色id;尺码id`，颜色 id 必须能在
        `skuPropertyList` 的 attrValueId 里找到唯一一个，否则 color_service
        抛「SKU 键与颜色取值的对应关系不明确」。
    另外 title 用 ASCII 英文，避开门槛里的「需复核词」。
    """
    return {
        "title": title,
        "imgUrls": [f"https://cdn.example.com/{detail_id}-1.jpg"],
        "notes": f"<img src='https://cdn.example.com/{detail_id}-d.jpg'>",
        "weight": 0.75,
        "packageLength": 30, "packageWidth": 20, "packageHeight": 10,
        "cid": "1001",
        "site": "MY",
        "description": "probe description",
        "skuMap": {"1;9": {
            "skuId": "s1", "isDelete": "0", "weight": 0.75,
            "price": 10, "originPrice": 8, "priceIncludeVat": 10.6, "stock": 99,
        }},
        "skuPropertyList": [{
            "attrName": "Color",
            "attrValueList": [{"attrValue": "White", "attrValueId": "1"}],
        }],
    }


def detail_response(detail_id: str, title: str = "probe english title here") -> dict:
    """外层信封：ossMd5 / editModel 在这一层（见 detail_info 的说明）。"""
    return {"data": {
        "ossMd5": "a" * 32,
        "editModel": "shop",
        "shopCollectItemInfo": dict(detail_info(detail_id, title),
                                    detailId=detail_id),
    }}


# --- 1. 身份解析 ----------------------------------------------------------
def test_identity() -> None:
    from core.config import get_settings

    secret = get_settings().local_secret()
    db = SessionLocal()
    try:
        # 建一个「张三」用来测「某人 token → 那个人」
        zhang, zhang_token = operator_service.create(db, "张三")
        db.commit()

        # (a) 只有 secret.key → 本机，不是 401（向后兼容命门）
        me = operator_service.resolve(db, secret)
        if me is None:
            operator_service.ensure_default(db)
            db.commit()
            me = operator_service.resolve(db, secret)
        check("identity: secret.key resolves to builtin operator",
              me is not None and me.name == operator_service.DEFAULT_OPERATOR_NAME,
              f"got {getattr(me, 'name', None)!r}")
        check("identity: builtin token equals secret.key",
              me is not None and me.token == secret, "token mismatch")
        check("identity: builtin is admin (needed to manage others)",
              me is not None and bool(me.is_admin), "not admin")

        # (b) 某人 token → 那个人
        who = operator_service.resolve(db, zhang_token)
        check("identity: operator token resolves to that person",
              who is not None and who.id == zhang.id,
              f"got {getattr(who, 'name', None)!r}")

        # (c) 名字不是登录凭据；只有 X-Operator 声明才允许按名字解析
        by_name = operator_service.resolve(db, "张三")
        check("identity: operator name cannot be used as bearer credential",
              by_name is None, "display name unexpectedly resolved as token")
        declared = operator_service.resolve_declared(db, "张三")
        check("identity: X-Operator name resolves to declared person",
              declared is not None and declared.id == zhang.id, "name lookup failed")

        # (d) 乱码 → None（由调用方拒）
        check("identity: garbage resolves to None",
              operator_service.resolve(db, "not-a-real-token-xyz") is None, "")

        # (e) 停用的操作员 → 403 且文案含「已被停用」
        operator_service.disable(db, zhang.id, is_admin_operator=True)
        db.commit()
        try:
            operator_service.require_enabled(
                operator_service.resolve(db, zhang_token))
        except errors.AppError as exc:
            check("identity: disabled operator -> 403 with reason",
                  exc.status == 403 and "已被停用" in exc.message,
                  f"status={exc.status} msg={exc.message[:60]}")
        else:
            check("identity: disabled operator -> 403 with reason", False,
                  "no exception raised")

        # (f) 普通操作员不能停用别人
        li, _li_token = operator_service.create(db, "李四")
        db.commit()
        try:
            operator_service.disable(db, zhang.id, is_admin_operator=False)
        except errors.AppError as exc:
            check("identity: non-admin cannot disable others",
                  exc.status == 403, f"status={exc.status}")
        else:
            check("identity: non-admin cannot disable others", False,
                  "no exception raised")

        # (g) 不能停用最后一个启用中的管理员
        admins = (db.query(Operator)
                  .filter(Operator.is_admin.is_(True), Operator.enabled.is_(True))
                  .all())
        check("identity: exactly one enabled admin before test",
              len(admins) == 1, f"count={len(admins)}")
        try:
            operator_service.disable(db, admins[0].id, is_admin_operator=True)
        except errors.AppError as exc:
            check("identity: cannot disable last enabled admin",
                  exc.status == 409 and "最后一个" in exc.message,
                  f"status={exc.status} msg={exc.message[:60]}")
        else:
            check("identity: cannot disable last enabled admin", False,
                  "no exception raised")

        # (h) 改名不回填历史（把李四改回可用状态再改个名）
        operator_service.enable(db, li.id, is_admin_operator=True)
        db.commit()
        renamed = operator_service.rename(db, li.id, "李四四")
        db.commit()
        check("identity: rename returns new name",
              renamed["name"] == "李四四", str(renamed))
        check("identity: rename keeps same id",
              str(renamed["id"]) == str(li.id), str(renamed))

        # (i) serialize 不含 token
        body = operator_service.serialize(me)
        check("identity: serialize has no token field",
              "token" not in body, str(sorted(body)))
        check("identity: serialize exposes admin + lastActiveAt",
              "admin" in body and "lastActiveAt" in body, str(sorted(body)))
    finally:
        db.close()


# --- 2. 审计写入 ----------------------------------------------------------
def test_audit() -> None:
    db = SessionLocal()
    try:
        me = operator_service.ensure_default(db)
        db.commit()

        before = db.query(AuditLog).count()
        audit_service.record(
            db, operator=me, action="probe.full", target_kind="product",
            target_id="1111:9500", account_id=None, shop_id="9500",
            result="success", message="", detail={"k": "v"})
        db.commit()
        after = db.query(AuditLog).count()
        check("audit: record writes exactly one row", after == before + 1,
              f"{before} -> {after}")

        row = (db.query(AuditLog).order_by(AuditLog.id.desc()).first())
        check("audit: row keeps operator_id + name snapshot",
              row.operator_id == me.id and row.operator_name == me.name,
              f"id={row.operator_id} name={row.operator_name!r}")
        check("audit: row keeps action/target/shop",
              row.action == "probe.full" and row.target_kind == "product"
              and row.target_id == "1111:9500" and row.shop_id == "9500",
              f"{row.action}/{row.target_kind}/{row.target_id}/{row.shop_id}")
        check("audit: row keeps detail JSON",
              isinstance(row.detail, dict) and row.detail.get("k") == "v",
              str(row.detail))
        check("audit: row keeps created_at",
              isinstance(row.created_at, float) and row.created_at > 0,
              str(row.created_at))

        # (b) commit 抛异常时不往上抛：把 commit 换成炸弹，record 内部
        # 走 SAVEPOINT + try/except，异常必须被吞掉。
        class BoomSession:
            """只包 commit：模拟「审计落盘时磁盘满」。"""

            def __init__(self, real) -> None:  # noqa: ANN001
                self._real = real

            def begin_nested(self):  # noqa: ANN201
                raise RuntimeError("simulated disk full")

            def __getattr__(self, name):  # noqa: ANN001
                return getattr(self._real, name)

        try:
            audit_service.record(BoomSession(db), operator=me,
                                 action="probe.boom", target_kind="product",
                                 target_id="boom")
        except Exception as exc:  # noqa: BLE001
            check("audit: write failure is NOT propagated", False,
                  f"{type(exc).__name__}: {exc}")
        else:
            check("audit: write failure is NOT propagated", True, "")
        # 会话仍然可用（这是 SAVEPOINT 的另一个作用）
        usable = True
        try:
            db.query(AuditLog).count()
        except Exception:  # noqa: BLE001
            usable = False
        check("audit: session still usable after failed write", usable, "")

        # (c) 操作员被删之后历史 operator_name 仍在
        temp, _token = operator_service.create(db, "临时工")
        db.commit()
        temp_id = temp.id
        audit_service.record(db, operator=temp, action="probe.deleted",
                             target_kind="product", target_id="d1")
        db.commit()
        db.delete(temp)
        db.commit()
        kept = (db.query(AuditLog).filter(AuditLog.action == "probe.deleted")
                .order_by(AuditLog.id.desc()).first())
        check("audit: history survives operator deletion (name kept)",
              kept is not None and kept.operator_name == "临时工",
              f"name={getattr(kept, 'operator_name', None)!r}")
        check("audit: history operator_id becomes NULL after delete",
              kept is not None and kept.operator_id is None,
              f"id={getattr(kept, 'operator_id', None)}")

        # (d) 筛选
        by_op = audit_service.query(db, operator_id=me.id, limit=200)
        check("audit: filter by operatorId",
              all(item["operatorId"] == str(me.id) for item in by_op["items"])
              and by_op["items"],
              f"n={len(by_op['items'])}")
        by_action = audit_service.query(db, action="probe.full", limit=200)
        check("audit: filter by action",
              [item["action"] for item in by_action["items"]] == ["probe.full"],
              str([item["action"] for item in by_action["items"]][:3]))
        by_kind = audit_service.query(db, target_kind="product", limit=200)
        check("audit: filter by targetKind",
              all(item["targetKind"] == "product" for item in by_kind["items"])
              and by_kind["items"], f"n={len(by_kind['items'])}")
        check("audit: query returns total",
              isinstance(by_action.get("total"), int)
              and by_action["total"] >= 1, str(by_action.get("total")))
        check("audit: serialize exposes targetLabel + details",
              "targetLabel" in by_action["items"][0]
              and "details" in by_action["items"][0],
              str(sorted(by_action["items"][0])))

        # (e) before 游标分页不漏不重
        for index in range(6):
            audit_service.record(db, operator=me, action="probe.page",
                                 target_kind="product", target_id=f"p{index}")
        db.commit()
        page1 = audit_service.query(db, action="probe.page", limit=2)
        page2 = audit_service.query(db, action="probe.page", limit=2,
                                    before=page1["nextBefore"])
        page3 = audit_service.query(db, action="probe.page", limit=2,
                                    before=page2["nextBefore"])
        seen = ([item["id"] for item in page1["items"]]
                + [item["id"] for item in page2["items"]]
                + [item["id"] for item in page3["items"]])
        check("audit: cursor pagination covers all rows", len(seen) == 6,
              f"got {len(seen)}: {seen}")
        check("audit: cursor pagination has no duplicates",
              len(set(seen)) == len(seen), str(seen))
        check("audit: last page nextBefore is None",
              page3["nextBefore"] is None, str(page3["nextBefore"]))
    finally:
        db.close()


# --- 3. 接线：confirm-save ------------------------------------------------
def test_wiring_confirm_save() -> None:
    from core.config import get_settings

    secret = get_settings().local_secret()
    headers = {"Authorization": f"Bearer {secret}"}
    db = SessionLocal()
    try:
        account = Account(name="probe", app_key="probe-key",
                          app_secret_enc=crypto.encrypt("probe-secret"),
                          is_default=True, site="MY", status="active")
        db.add(account)
        db.commit()
        account_id = account.id
    finally:
        db.close()

    from services import account_service

    original = account_service.account_client
    account_service.account_client = lambda account: FakeClient("7001")
    try:
        with TestClient(app) as client:
            # 先预览拿 hash。skipImageReview / allowTitleReview / allowImageReview
            # 三个开关都打开：探针要验的是审计接线，不是闸门（闸门有自己的探针）。
            preview = client.post(
                f"{API_PREFIX}/collect-box/7001/process",
                json={"shopId": "9500", "accountId": str(account_id),
                      "save": False, "skipImageReview": True,
                      "allowTitleReview": True, "allowImageReview": True},
                headers=headers)

            check("wiring: preview (save=false) returns 200",
                  preview.status_code == 200, str(preview.status_code))
            body = preview.json()
            payload_hash = str(
                ((body.get("product") or {}).get("payloadReview") or {})
                .get("payloadHash") or "")
            check("wiring: preview yields payloadHash", bool(payload_hash),
                  str(body)[:200])

            # hash 对不上 → 409 + 审计记 failed + message 含 PAYLOAD_CHANGED
            mismatch = client.post(
                f"{API_PREFIX}/warehouse/confirm-save",
                json={"detailId": "7001", "shopId": "9500",
                      "accountId": str(account_id),
                      "payloadHash": "deadbeef" * 8, "confirm": True,
                      "skipImageReview": True, "allowTitleReview": True,
                      "allowImageReview": True},
                headers=headers)
            check("wiring: hash mismatch -> 409",
                  mismatch.status_code == 409, str(mismatch.status_code))
            check("wiring: hash mismatch code is PAYLOAD_CHANGED",
                  mismatch.json().get("error", {}).get("code") == "PAYLOAD_CHANGED",
                  str(mismatch.json())[:200])

            db2 = SessionLocal()
            try:
                failed = (db2.query(AuditLog)
                          .filter(AuditLog.action == "warehouse.import",
                                  AuditLog.target_id == "7001:9500")
                          .order_by(AuditLog.id.desc()).first())
                check("wiring: failed save is recorded with result=failed",
                      failed is not None and failed.result == "failed",
                      f"result={getattr(failed, 'result', None)}")
                check("wiring: failed save message carries PAYLOAD_CHANGED",
                      failed is not None and "PAYLOAD_CHANGED" in (failed.message or ""),
                      f"message={getattr(failed, 'message', '')[:80]}")
            finally:
                db2.close()

            # hash 对上 → 真正保存，审计记 success
            ok = client.post(
                f"{API_PREFIX}/warehouse/confirm-save",
                json={"detailId": "7001", "shopId": "9500",
                      "accountId": str(account_id),
                      "payloadHash": payload_hash, "confirm": True,
                      "skipImageReview": True, "allowTitleReview": True,
                      "allowImageReview": True},
                headers=headers)
            check("wiring: confirm-save with right hash -> 200",
                  ok.status_code == 200, str(ok.status_code) + str(ok.json())[:300])
            if ok.status_code == 200:
                product = ok.json().get("product") or {}
                check("wiring: saved product reports updatedBy",
                      "updatedBy" in product, str(sorted(product))[:200])
                check("wiring: updatedBy is the builtin operator name",
                      product.get("updatedBy") == operator_service.DEFAULT_OPERATOR_NAME,
                      repr(product.get("updatedBy")))

            db3 = SessionLocal()
            try:
                saved = (db3.query(AuditLog)
                         .filter(AuditLog.action == "warehouse.import",
                                 AuditLog.target_id == "7001:9500",
                                 AuditLog.result == "success")
                         .order_by(AuditLog.id.desc()).first())
                check("wiring: successful save is recorded with result=success",
                      saved is not None, "no row")
                check("wiring: successful save carries payloadHash prefix",
                      saved is not None
                      and (saved.detail or {}).get("payloadHash") == payload_hash[:16],
                      str(getattr(saved, "detail", None)))
                prod = (db3.query(Product)
                        .filter(Product.warehouse_key == "7001:9500").one_or_none())
                check("wiring: product row carries operator_id",
                      prod is not None and prod.operator_id is not None,
                      f"operator_id={getattr(prod, 'operator_id', None)}")
            finally:
                db3.close()

            # warehouse 列表带 updatedBy
            listing = client.get(f"{API_PREFIX}/warehouse", headers=headers)
            check("wiring: GET /warehouse 200", listing.status_code == 200,
                  str(listing.status_code))
            items = (listing.json().get("items") or [])
            check("wiring: warehouse item exposes updatedBy",
                  bool(items) and "updatedBy" in items[0],
                  str(sorted(items[0]))[:200] if items else "no items")
    finally:
        account_service.account_client = original


# --- 4. 操作员接口 + token 不外泄 ----------------------------------------
def test_operator_http() -> None:
    from core.config import get_settings

    secret = get_settings().local_secret()
    headers = {"Authorization": f"Bearer {secret}"}

    with TestClient(app) as client:
        created = client.post(f"{API_PREFIX}/operators",
                              json={"name": "王五"}, headers=headers)
        check("http: POST /operators -> 201", created.status_code == 201,
              str(created.status_code) + str(created.json())[:200])
        payload = created.json()
        token = str(payload.get("token") or "")
        check("http: POST /operators returns token once", bool(token),
              str(payload)[:200])
        check("http: created operator has camelCase fields",
              "isAdmin" in (payload.get("operator") or {})
              and "lastSeenAt" in (payload.get("operator") or {}),
              str(sorted(payload.get("operator") or {})))

        # 新 token 立刻可用（写操作 + 身份解析）
        me = client.get(f"{API_PREFIX}/operators",
                        headers={"X-Operator": token})
        check("http: new token works immediately",
              me.status_code == 200
              and (me.json().get("current") or {}).get("name") == "王五",
              str(me.status_code) + str(me.json())[:200])

        # GET /operators 的 JSON 里不能出现任何 token 明文
        listing = client.get(f"{API_PREFIX}/operators", headers=headers)
        raw = listing.text
        check("http: GET /operators 200", listing.status_code == 200,
              str(listing.status_code))
        check("http: GET /operators never leaks the new token",
              token not in raw, "token found in response body")
        check("http: GET /operators never leaks secret.key",
              secret not in raw, "secret.key found in response body")
        check("http: GET /operators exposes operators + total + current",
              all(key in listing.json()
                  for key in ("operators", "total", "current")),
              str(sorted(listing.json())))

        # 不带任何凭据也能读名单（current 回落到本机），不是 401
        anon = client.get(f"{API_PREFIX}/operators")
        check("http: GET /operators without credentials -> 200 (builtin)",
              anon.status_code == 200
              and (anon.json().get("current") or {}).get("name")
              == operator_service.DEFAULT_OPERATOR_NAME,
              str(anon.status_code) + str(anon.json())[:200])

        # 乱码 token 打 GET /operators → 401（前端靠这个判「这串无效」）
        bad = client.get(f"{API_PREFIX}/operators",
                         headers={"X-Operator": "totally-bogus"})
        check("http: unknown X-Operator -> 401", bad.status_code == 401,
              str(bad.status_code))
        check("http: unknown X-Operator code is TOKEN_INVALID",
              bad.json().get("error", {}).get("code") == errors.TOKEN_INVALID,
              str(bad.json())[:200])

        # 只声明名字、不带凭据 → 401 AUTH_REQUIRED
        # 名字用 ASCII 的「wangwu」：HTTP 头必须是 ASCII（httpx 直接拒绝中文头值），
        # 前端发的本来就是 token 而不是名字，这里只验「声明了身份就必须带凭据」。
        name_only = client.get(f"{API_PREFIX}/operators",
                               headers={"X-Operator": "wangwu"})
        check("http: declared name without credential -> 401",
              name_only.status_code == 401, str(name_only.status_code))

        # 审计接口
        logs = client.get(f"{API_PREFIX}/audit-logs?limit=5", headers=headers)
        check("http: GET /audit-logs 200", logs.status_code == 200,
              str(logs.status_code))
        body = logs.json()
        check("http: audit response has items/total/nextBefore",
              all(key in body for key in ("items", "total", "nextBefore")),
              str(sorted(body)))
        check("http: audit items are camelCase",
              not body["items"] or "operatorName" in body["items"][0],
              str(sorted(body["items"][0]))[:200] if body["items"] else "empty")
        check("http: operator.create is audited",
              any(item.get("action") == "operator.create"
                  for item in body["items"]),
              str([item.get("action") for item in body["items"]]))

        # rotate：旧 token 立刻失效
        who = (payload.get("operator") or {})
        rotated = client.post(f"{API_PREFIX}/operators/{who.get('id')}/rotate",
                              headers=headers)
        check("http: POST /operators/{id}/rotate -> 200",
              rotated.status_code == 200, str(rotated.status_code))
        new_token = str(rotated.json().get("token") or "")
        check("http: rotate returns a different token",
              new_token and new_token != token, "token unchanged")
        stale = client.get(f"{API_PREFIX}/operators",
                           headers={"X-Operator": token})
        check("http: old token rejected after rotate",
              stale.status_code == 401, str(stale.status_code))
        fresh = client.get(f"{API_PREFIX}/operators",
                           headers={"X-Operator": new_token})
        check("http: new token accepted after rotate",
              fresh.status_code == 200, str(fresh.status_code))

        # 不带 token 打写接口仍然 401（0.1 的行为一字未改）
        anon_write = client.post(f"{API_PREFIX}/operators", json={"name": "x"})
        check("http: POST /operators without token -> 401",
              anon_write.status_code == 401, str(anon_write.status_code))
        anon_warehouse = client.get(f"{API_PREFIX}/warehouse")
        check("http: GET /warehouse without token -> 401",
              anon_warehouse.status_code == 401, str(anon_warehouse.status_code))


# --- 5. 显示名回填 --------------------------------------------------------
def test_display_names() -> None:
    db = SessionLocal()
    try:
        me = operator_service.ensure_default(db)
        db.commit()

        # publish 记录：operatorName
        account = db.query(Account).first()
        if account is None:
            account = Account(name="probe2", app_key="k2",
                              app_secret_enc=crypto.encrypt("s2"),
                              is_default=False, site="MY", status="active")
            db.add(account)
            db.commit()
        log = publish_service._upsert_log(
            db, account=account, product=None, detail_id="9001", shop_id="9500",
            shop_name="shop", title="t", mode="direct", batch_id="pub_x",
            operator_id=me.id)
        db.commit()
        body = publish_service.serialize_log(
            log, operator_name=warehouse_service.operator_names(db).get(
                log.operator_id or 0, ""))
        check("display: publish log has operatorName",
              body.get("operatorName") == me.name,
              repr(body.get("operatorName")))
        check("display: publish log row carries operator_id",
              log.operator_id == me.id, str(log.operator_id))

        # 老记录（operator_id 为空）operatorName 是空串而不是 None
        legacy = publish_service._upsert_log(
            db, account=account, product=None, detail_id="9002", shop_id="9500",
            shop_name="shop", title="t", mode="direct", batch_id="pub_y")
        db.commit()
        legacy_body = publish_service.serialize_log(legacy)
        check("display: legacy publish log operatorName is empty string",
              legacy_body.get("operatorName") == "",
              repr(legacy_body.get("operatorName")))

        # 批次：operatorName
        batch = batch_service.create(db, account_id=account.id, shop_id="9600",
                                     detail_ids=["1", "2"], operator_id=me.id)
        db.commit()
        batch_body = batch_service.serialize(
            batch, operator_name=batch_service.operator_names(db).get(
                batch.operator_id or 0, ""))
        check("display: batch has operatorName",
              batch_body.get("operatorName") == me.name,
              repr(batch_body.get("operatorName")))
        check("display: batch row carries operator_id",
              batch.operator_id == me.id, str(batch.operator_id))

        # 仓库：updatedBy
        product = db.query(Product).filter(Product.warehouse_key == "7001:9500").one_or_none()
        if product is not None:
            body = warehouse_service.serialize(
                product, operator_name=warehouse_service.operator_names(db).get(
                    product.operator_id or 0, ""))
            check("display: warehouse serialize has updatedBy",
                  body.get("updatedBy") == me.name,
                  repr(body.get("updatedBy")))
            # 老数据（operator_id 为 NULL）→ 空串
            product.operator_id = None
            db.flush()
            legacy_body = warehouse_service.serialize(
                product, operator_name=warehouse_service.operator_names(db).get(
                    product.operator_id or 0, ""))
            check("display: warehouse updatedBy empty for legacy rows",
                  legacy_body.get("updatedBy") == "",
                  repr(legacy_body.get("updatedBy")))
            db.rollback()
        else:
            check("display: warehouse serialize has updatedBy", False,
                  "no product row from confirm-save step")
            check("display: warehouse updatedBy empty for legacy rows", False,
                  "no product row from confirm-save step")
    finally:
        db.close()


# --- 5b. 其余写操作的接线 ------------------------------------------------
def test_wiring_other_routes() -> None:
    """剩下的写路由逐条打一遍，确认审计里有对应的 action。

    为什么要真打 HTTP 而不是只 grep 源码：`audit_service.record(...)` 写了但
    位置放错（比如放在 `raise` 之后、或 `db.commit()` 之后另开事务）在源码里
    看不出来，只有打一次才知道那行到底落没落库。
    """
    from core.config import get_settings

    secret = get_settings().local_secret()
    headers = {"Authorization": f"Bearer {secret}"}

    def actions_for(action: str, limit: int = 200) -> list[dict]:
        db = SessionLocal()
        try:
            return audit_service.query(db, action=action, limit=limit)["items"]
        finally:
            db.close()

    with TestClient(app) as client:
        # ---- 违规词库：加词 → words.create ----
        # WordRequest 的 add 语法是 `word`（单数）+ `category`，不是 words 数组；
        # words 数组是 import / remove-many 用的。第一版写错，服务层回「词不能为空」。
        added = client.post(f"{API_PREFIX}/words",
                            json={"action": "add", "word": "probe词"},
                            headers=headers)
        check("wiring: POST /words (add) -> 200", added.status_code == 200,
              str(added.status_code) + str(added.json())[:200])
        rows = actions_for("words.create")
        check("wiring: words add is audited as words.create",
              any(row["targetId"] == "add" for row in rows),
              str([row["action"] for row in rows][:5]))
        check("wiring: words audit carries the word itself",
              any("probe词" in (row["details"] or {}).get("sample", [])
                  for row in rows),
              str([(row["details"] or {}).get("sample") for row in rows][:3]))

        # ---- 自定义分类：新建 → warehouse.batch_category ----
        cat = client.post(f"{API_PREFIX}/warehouse/custom-categories",
                          json={"name": "探针分类"}, headers=headers)
        check("wiring: POST custom-categories -> 201", cat.status_code == 201,
              str(cat.status_code) + str(cat.json())[:200])
        rows = actions_for("warehouse.batch_category")
        check("wiring: custom category create is audited",
              any((row["details"] or {}).get("verb") == "create" for row in rows),
              str([row["details"] for row in rows][:3]))

        # ---- 定时任务：建 → 改 → 删 ----
        #
        # 两处前置条件都是踩出来的：
        #   · 任务必须绑一套**已存在**的定价模板（_verify_template 查白名单），
        #     所以先用 /system/templates 存一套「马来」，再拿它的名字建任务；
        #   · 店铺必须属于该账号（require_shop 会回源一次拿店铺列表——探针
        #     不联网，所以把 shops 缓存直接写进 Account.shops）。
        account_id = None
        db = SessionLocal()
        try:
            from models.account import Account

            account = db.query(Account).first()
            if account is not None:
                account_id = account.id
                account.shops = json.dumps([{"shopId": "9500", "name": "探针店"}],
                                           ensure_ascii=False)
                db.commit()
        finally:
            db.close()

        saved_template = client.post(f"{API_PREFIX}/system/templates",
                                     json={"template": {"name": "探针模板"}},
                                     headers=headers)
        check("wiring: POST /system/templates -> 200",
              saved_template.status_code == 200,
              str(saved_template.status_code) + str(saved_template.json())[:200])
        rows = actions_for("template.save")
        check("wiring: template save is audited",
              any(row["targetId"] == "探针模板" for row in rows),
              str([row["targetId"] for row in rows][:5]))

        if account_id is not None:
            made = client.post(
                f"{API_PREFIX}/schedules",
                json={"name": "探针任务", "accountId": str(account_id),
                      "shopId": "9500", "template": "探针模板",
                      "frequency": "daily", "time": "09:30", "perRun": 3},
                headers=headers)
            check("wiring: POST /schedules -> 201", made.status_code == 201,
                  str(made.status_code) + str(made.json())[:300])
            # 服务层返回的是 {"task": {...}}，id 在里层（第一版读错层级）
            task_id = str(((made.json() or {}).get("task") or {}).get("id") or "")
            check("wiring: created schedule has an id", bool(task_id), task_id)
            rows = actions_for("schedule.create")
            check("wiring: schedule create is audited",
                  any(row["targetId"] == task_id for row in rows),
                  str([row["targetId"] for row in rows][:5]))
            if task_id:
                patched = client.patch(f"{API_PREFIX}/schedules/{task_id}",
                                       json={"perRun": 5}, headers=headers)
                check("wiring: PATCH /schedules/{id} -> 200",
                      patched.status_code == 200, str(patched.status_code))
                rows = actions_for("schedule.patch")
                check("wiring: schedule patch is audited",
                      any(row["targetId"] == task_id for row in rows), "")
                removed = client.delete(f"{API_PREFIX}/schedules/{task_id}",
                                        headers=headers)
                check("wiring: DELETE /schedules/{id} -> 200",
                      removed.status_code == 200, str(removed.status_code))
                rows = actions_for("schedule.delete")
                check("wiring: schedule delete is audited",
                      any(row["targetId"] == task_id for row in rows), "")
                check("wiring: schedule delete keeps the name it deleted",
                      any((row["details"] or {}).get("name") == "探针任务"
                          for row in rows),
                      str([(row["details"] or {}).get("name") for row in rows][:3]))
        else:
            for label in ("wiring: POST /schedules -> 201",
                          "wiring: schedule create is audited",
                          "wiring: schedule patch is audited",
                          "wiring: schedule delete is audited"):
                check(label, False, "no account to attach the schedule to")

        # ---- 分组 / 货源池 / 尺寸表 / 图片额度 / 类目同步 ----
        # 这一批都是「共享配置」，出问题时影响的是所有人，所以逐个打一遍。
        made_group = client.post(f"{API_PREFIX}/groups",
                                 json={"name": "探针分组"}, headers=headers)
        check("wiring: POST /groups -> 201", made_group.status_code == 201,
              str(made_group.status_code) + str(made_group.json())[:200])
        rows = actions_for("warehouse.batch_group")
        check("wiring: group create is audited",
              any((row["details"] or {}).get("verb") == "create" for row in rows),
              str([row["details"] for row in rows][:3]))

        links = client.post(f"{API_PREFIX}/carrier/links",
                            json={"urls": ["https://detail.1688.com/offer/123.html"]},
                            headers=headers)
        check("wiring: POST /carrier/links -> 200", links.status_code == 200,
              str(links.status_code) + str(links.json())[:200])
        rows = actions_for("system.update")
        check("wiring: carrier add is audited",
              any((row["details"] or {}).get("verb") == "carrier.add" for row in rows),
              str([(row["details"] or {}).get("verb") for row in rows][:5]))

        chart = client.post(f"{API_PREFIX}/size-charts",
                            json={"name": "探针尺码表",
                                  "url": "https://img.example.com/size.png",
                                  "site": "MY"}, headers=headers)
        check("wiring: POST /size-charts -> 201", chart.status_code == 201,
              str(chart.status_code) + str(chart.json())[:200])
        rows = actions_for("system.update")
        check("wiring: size chart create is audited",
              any((row["details"] or {}).get("verb") == "size_chart.create"
                  for row in rows),
              str([(row["details"] or {}).get("verb") for row in rows][:5]))

        # 键名是 minute / hour / day / limitMilliyuan（IMAGE_LIMIT_BOUNDS），
        # 不是 perDay —— 写错键会得到「没有可更新的额度」，那条 400 不会留痕。
        limits = client.post(f"{API_PREFIX}/images/limits",
                             json={"day": 120}, headers=headers)
        check("wiring: POST /images/limits -> 200", limits.status_code == 200,
              str(limits.status_code) + str(limits.json())[:200])
        rows = actions_for("settings.change")
        check("wiring: image limits change is audited",
              any(row["targetId"] == "imageLimits" for row in rows),
              str([row["targetId"] for row in rows][:5]))

        # ---- 采集箱删除：一件一条 ----
        removed = client.post(f"{API_PREFIX}/collect-box/delete",
                              json={"detailIds": ["7001"]}, headers=headers)
        # 不联网，上游会失败；但失败也必须留痕（result=failed）
        rows = actions_for("warehouse.delete")
        check("wiring: collect-box delete is audited (success or failed)",
              bool(rows) and any(row["targetId"] == "7001" for row in rows),
              f"http={removed.status_code} rows={len(rows)}")

        # ---- 只读模式：审计本身不留痕（它不写业务数据）----
        db = SessionLocal()
        try:
            from services import setting_service

            setting_service.set_read_only(db, True)
            db.commit()
        finally:
            db.close()
        blocked = client.post(f"{API_PREFIX}/words",
                              json={"action": "add", "word": "x"},
                              headers=headers)
        check("wiring: read-only mode still rejects word edits (403)",
              blocked.status_code == 403, str(blocked.status_code))
        db = SessionLocal()
        try:
            from services import setting_service

            setting_service.set_read_only(db, False)
            db.commit()
        finally:
            db.close()


# --- 6. 向后兼容：老库补列 ------------------------------------------------
def test_legacy_db_upgrade() -> None:
    """模拟「现有用户的库」：products 表存在、但没有 operator_id 列。

    这不是假设：真实机器上 app.db 里有 355 件商品，升级时双击 exe 就跑了
    `init_db()` → `create_all`（不改表）→ `_add_missing_columns`（ADD COLUMN）。
    这里用同一套函数在临时库上重放一遍，确认补列之后一切照旧。
    """
    legacy_dir = Path(tempfile.mkdtemp(prefix="operator-legacy-"))
    legacy_db = legacy_dir / "app.db"
    os.environ["AOHUANG_DATA_DIR"] = str(legacy_dir)
    os.environ["TK_AUTOMATION_DATA_DIR"] = str(legacy_dir)

    # 换数据目录要清掉 lru_cache，否则拿到的还是刚才那个 settings
    from core.config import get_settings

    get_settings.cache_clear()

    # 用独立的引擎建一个「老结构」库：products 表故意不带 operator_id
    from sqlalchemy import create_engine

    legacy_engine = create_engine(f"sqlite:///{legacy_db}", future=True)
    with legacy_engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE products ("
            " id INTEGER PRIMARY KEY,"
            " warehouse_key VARCHAR(128),"
            " detail_id VARCHAR(64),"
            " shop_id VARCHAR(32),"
            " account_id INTEGER,"
            " title VARCHAR(512),"
            " status VARCHAR(16),"
            " saved_at FLOAT,"
            " updated_at FLOAT)"))
        conn.execute(text(
            "INSERT INTO products (id, warehouse_key, detail_id, shop_id, title,"
            " status, saved_at, updated_at) VALUES"
            " (1, '1111:9500', '1111', '9500', 'legacy title', 'ready', 1.0, 1.0)"))
        # 真实机器上是 355 件。只插一行验不出「补列之后整表还能不能翻页」：
        # ALTER TABLE ADD COLUMN 在 SQLite 上只改表头、不重写行，真正的风险是
        # 补列之后 ORM 的 SELECT 里多了这一列、而某些行是 NULL 触发类型错误。
        # 355 行刚好能跨过默认分页（20/页）与一次大页（100/页）。
        #
        # `char(58)` 而不是 `':'`：text() 会把 `':9500'` 里的 `:9500` 当成
        # 绑定参数（前面是引号，符合它的正则），于是报「缺少参数 9500」。
        conn.execute(text(
            "INSERT INTO products (id, warehouse_key, detail_id, shop_id, title,"
            " status, saved_at, updated_at) "
            "SELECT 1000 + value, '1' || value || char(58) || '9500',"
            " '1' || value, '9500', 'legacy ' || value, 'ready', 1.0, 1.0 "
            "FROM (WITH RECURSIVE seq(value) AS ("
            "  SELECT 1 UNION ALL SELECT value + 1 FROM seq WHERE value < 355)"
            " SELECT value FROM seq)"))
    legacy_engine.dispose()

    # 现在让应用代码在这个目录上跑 init_db：create_all 建新表（operators /
    # audit_logs），_add_missing_columns 给老 products 补 operator_id。
    import db.session as session_module

    original_engine = session_module.engine
    original_session = session_module.SessionLocal
    try:
        session_module.engine = create_engine(f"sqlite:///{legacy_db}", future=True)
        # 同样的 PRAGMA（外键/WAL），否则 operators 的外键约束行为不一致
        from sqlalchemy import event

        @event.listens_for(session_module.engine, "connect")
        def _pragmas(dbapi_connection, _record) -> None:  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        from sqlalchemy.orm import sessionmaker

        session_module.SessionLocal = sessionmaker(
            bind=session_module.engine, autoflush=False,
            expire_on_commit=False, future=True)
        session_module.init_db()

        with session_module.engine.begin() as conn:
            cols = {row[1] for row in
                    conn.execute(text("PRAGMA table_info(products)")).fetchall()}
        check("legacy: operator_id column added to existing products table",
              "operator_id" in cols, str(sorted(cols)))
        check("legacy: existing 355-product style row still readable",
              "warehouse_key" in cols and "title" in cols, str(sorted(cols)))

        # 老行读出来 updatedBy 是空串（operator_id 为 NULL），且不报 no such column
        legacy_session = session_module.SessionLocal()
        try:
            row = legacy_session.query(Product).first()
            check("legacy: old product row loads without error", row is not None,
                  "no row")
            body = warehouse_service.serialize(row) if row is not None else {}
            check("legacy: updatedBy is empty string for old rows",
                  body.get("updatedBy") == "", repr(body.get("updatedBy")))
            check("legacy: operator_id is NULL for old rows",
                  row is not None and row.operator_id is None,
                  str(getattr(row, "operator_id", None)))
        finally:
            legacy_session.close()

        # operators / audit_logs 两张新表被 create_all 建出来了
        with session_module.engine.begin() as conn:
            tables = {row[0] for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()}
        check("legacy: operators table created", "operators" in tables,
              str(sorted(tables))[:200])
        check("legacy: audit_logs table created", "audit_logs" in tables,
              str(sorted(tables))[:200])

        # 老库上 GET /warehouse 必须照常 200
        with TestClient(app) as client:
            from core.config import get_settings as _gs

            headers = {"Authorization": f"Bearer {_gs().local_secret()}"}
            resp = client.get(f"{API_PREFIX}/warehouse", headers=headers)
            check("legacy: GET /warehouse still 200 after column add",
                  resp.status_code == 200,
                  str(resp.status_code) + str(resp.json())[:200])
            items = resp.json().get("items") or []
            check("legacy: warehouse list has updatedBy on old rows",
                  bool(items) and items[0].get("updatedBy") == "",
                  str(items[0].get("updatedBy")) if items else "no items")
            # 355 件全都读得出来：补列之后不能出现「第 N 行开始就 500」
            total = int(resp.json().get("total") or 0)
            check("legacy: all 356 legacy rows are counted",
                  total == 356, f"total={total}")
            # 一次拉满（pageSize 上限 100）翻 4 页，每页都要 200
            seen = 0
            ok_all = True
            for page in range(1, 5):
                page_resp = client.get(
                    f"{API_PREFIX}/warehouse?page={page}&pageSize=100",
                    headers=headers)
                if page_resp.status_code != 200:
                    ok_all = False
                    break
                seen += len(page_resp.json().get("items") or [])
            check("legacy: 356 rows paginate without error",
                  ok_all and seen == 356, f"seen={seen} ok={ok_all}")
    finally:
        session_module.engine = original_engine
        session_module.SessionLocal = original_session
        get_settings.cache_clear()
        os.environ["AOHUANG_DATA_DIR"] = str(_TMP_DIR)
        os.environ["TK_AUTOMATION_DATA_DIR"] = str(_TMP_DIR)


# --- 7. 路由规模 ----------------------------------------------------------
def test_route_count() -> None:
    count = len(app.routes)
    check("routes: app has >= 126 routes", count >= 126, f"count={count}")


def main() -> int:
    init_db()
    Base.metadata.create_all(bind=engine)

    test_identity()
    test_audit()
    test_wiring_confirm_save()
    test_operator_http()
    test_display_names()
    test_wiring_other_routes()
    test_route_count()
    # 老库那条放最后：它会切数据目录，跑完再切回来
    test_legacy_db_upgrade()

    failed = [row for row in results if not row[1]]
    for label, ok, detail in results:
        print(("PASS " if ok else "FAIL ") + label
              + (("  [" + detail + "]") if detail and not ok else ""))
    print("TOTAL %d  PASS %d  FAIL %d"
          % (len(results), len(results) - len(failed), len(failed)))

    out_dir = ROOT / "_meas" / "_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "probe_operator.json").write_text(json.dumps(
        {"total": len(results), "failed": len(failed),
         "rows": [{"label": label, "ok": ok, "detail": detail}
                  for label, ok, detail in results]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
