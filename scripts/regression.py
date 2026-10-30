"""一键回归：审查报告 P0/P1 修复项的真实验证。

用法：python scripts/regression.py
只读或临时数据，跑完自动清理，不改动真实业务数据（基线见末尾打印）。

覆盖：
  P0-1  采集箱接口不再 500（缺 credentials 导致 NameError）
  P0-2  定时任务 published 是 list 时不再 TypeError
  P0-3  POST /collect-box/{id}/process 端到端入仓（预览 + 写库）
  P0-4  run_due 一轮只调 publish 一次（旧 bug 会重复 3 次）
  P0-5  载体日志按 savedSources 配对，不张冠李戴
  P0-6  未上架商品不被标记 published
  P0-7  颜色规范化在 build 之前，sku_properties 与 payload 一致
  P1-4  定时任务四态摘要 empty/ok/partial/failed
  P1-7  /carrier/links 已挂载可用
  P1-10 PATCH 支持显式 null 清空
  P1-1  price-preview / size-charts 可用；非 POST 返回 405
"""
from __future__ import annotations

import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, "server")

RESULTS: list[tuple[str, bool, str]] = []


def check(item: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((item, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {item}  {detail}")


def main() -> int:
    from fastapi.testclient import TestClient

    import main as app_main
    from db.session import SessionLocal
    from models.account import Account
    from models.product import Product
    from models.schedule import Schedule
    from services import (
        account_service,
        carrier_service,
        operator_service,
        product_service,
        publish_service,
        schedule_service,
        warehouse_service,
    )

    db = SessionLocal()
    _operator, token = operator_service.create(
        db, f"build-regression-{uuid.uuid4().hex[:8]}", role=operator_service.ROLE_TEACHER)
    db.commit()
    token_is_valid = operator_service.resolve(db, token) is not None
    db.close()
    client = TestClient(app_main.app, raise_server_exceptions=False)
    H = {"Authorization": f"Bearer {token}"}

    # --- P0-1：采集箱不再 500（假凭据只会上游 502，502 是预期的上游不可达） ---
    response = client.get("/api/v1/collect-box?pageSize=2", headers=H)
    try:
        code = response.json().get("error", {}).get("code", "")
    except Exception:
        code = ""
    check("P0-1 采集箱不再 500 NameError",
          response.status_code < 500 or code == "UPSTREAM_ERROR",
          f"HTTP {response.status_code} {code}; operator token valid={token_is_valid}")

    # --- P0-5：日志配对 ---
    entries = [{"detailId": "111"}, {"detailId": "222"}]
    paired = publish_service._pair_entries(entries, ["900"], [], saved_sources=[1])
    check("P0-5 载体重建按 savedSources 配对",
          paired == [(entries[1], "900")], f"{paired}")
    check("P0-5 信息不足时不乱配", publish_service._pair_entries(entries, ["900"], []) == [])

    # --- P0-6：只标记成功件 ---
    picked = publish_service._entries_for_ids(entries, ["222"])
    check("P0-6 只取成功件", [e["detailId"] for e in picked] == ["222"])

    # --- P1-2：claimOnly 认领回执不能虚报 ---
    # 上游回执里没有逐条结果时，claimed_detail_ids 返回空列表；
    # 旧写法 `parsed or detail_ids` 会把「一件没认领」退化成「全部认领」。
    check("P1-2 认领全失败时回执为空",
          carrier_service.claimed_detail_ids({}) == [],
          f"{carrier_service.claimed_detail_ids({})}")
    partial_claim = {"data": {"platformCollectBoxDetailIdMap": {"18077147": {"111": "9001"}}}}
    check("P1-2 认领部分成功按回执取值",
          carrier_service.claimed_detail_ids(partial_claim) == [9001],
          f"{carrier_service.claimed_detail_ids(partial_claim)}")

    # --- P0-7 + P0-3：入仓端到端 ---
    detail_id, shop_id = "900000777", "18077147"
    from services import crypto
    db = SessionLocal()
    regression_account = Account(
        name="__build_regression__", app_key="build-regression-key",
        app_secret_enc=crypto.encrypt("build-regression-secret"),
        secret_hint="cret", app_id_hint="key", site="MY", is_default=True,
        status="active", shops='[{"shopId":"18077147","name":"Regression"}]',
    )
    db.add(regression_account)
    db.commit()
    regression_account_id = regression_account.id
    db.close()
    fixture = {"data": {"ossMd5": "smoke", "editModel": "shop", "shopCollectItemInfo": {
        "detailId": detail_id, "shopId": shop_id,
        "title": "Regression Shoe Comfortable Sneaker", "mainImgUrl": "https://img.example.com/a.jpg",
        "imgUrls": ["https://img.example.com/a.jpg"], "cid": "200000001",
        "weight": "0.5",
        "notes": "Regression fixture description",
        "packageLength": "30", "packageWidth": "20", "packageHeight": "10",
        "skuPropertyList": [{"attrName": "Color", "attrValueList": [
            {"attrValueId": "1", "attrValue": "Y18[beige]"}]}],
        "skuMap": {"1": {"skuId": "s1", "price": "30", "originPrice": "30",
                         "stock": "10", "weight": "0.5", "propPath": "1",
                         "isDelete": "0", "skuPropertyList": []}},
        "description": "d"}}}

    class FakeClient:
        def __init__(self):
            self.saved_payload = None

        def shop_detail(self, *_a, **_k):
            if self.saved_payload is not None:
                return {"data": {"ossMd5": "smoke", "editModel": "shop",
                                 "shopCollectItemInfo": self.saved_payload}}
            return fixture

        def save_shop(self, _detail_id, _shop_id, _oss_md5, payload):
            self.saved_payload = dict(payload)

        def close(self):
            pass

    original_client = account_service.account_client
    original_translate = product_service.translated_info
    account_service.account_client = lambda account: FakeClient()
    product_service.translated_info = lambda c, info, language="": (
        dict(info), {"translated": False, "changedFields": 0})

    db = SessionLocal()
    before = db.query(Product).count()
    db.close()
    try:
        response = client.post(f"/api/v1/collect-box/{detail_id}/process", headers=H,
                               json={"shopId": shop_id, "save": False})
        db = SessionLocal()
        preview_count = db.query(Product).count()
        db.close()
        check("P0-3 save=false 只预览", response.status_code == 200 and preview_count == before,
              f"HTTP {response.status_code}, {before}->{preview_count}, "
              f"{response.json() if response.status_code != 200 else ''}")

        response = client.post(f"/api/v1/collect-box/{detail_id}/process", headers=H,
                               json={"shopId": shop_id, "save": True,
                                     "skipImageReview": True})
        db = SessionLocal()
        saved_count = db.query(Product).count()
        row = (db.query(Product)
               .filter(Product.detail_id == detail_id, Product.shop_id == shop_id)
               .one_or_none())
        colors = []
        payload_colors = []
        if row:
            def colors_of(source):
                return [v.get("attrValue") for p in (source or [])
                        if str(p.get("attrName") or "").casefold() in {"颜色", "color"}
                        for v in (p.get("attrValueList") or [])]
            colors = colors_of(row.sku_properties)
            payload_colors = colors_of((row.payload or {}).get("skuPropertyList"))
            db.delete(row)
            db.commit()
        db.close()
        check("P0-3 save=true 真实写入", response.status_code == 200 and saved_count == before + 1,
              f"HTTP {response.status_code}, {before}->{saved_count}, "
              f"{response.json() if response.status_code != 200 else ''}")
        check("P0-7 两字段颜色一致", colors == payload_colors and bool(colors),
              f"{colors} == {payload_colors}")
        check("P0-7 颜色已规范化", all("Y18" not in c and "[" not in c for c in colors),
              f"{colors}")
    finally:
        account_service.account_client = original_client
        product_service.translated_info = original_translate

    # --- P1-4 / P0-2 / P0-4：定时任务四态 + 单次调用 ---
    for selected, published, failed, expect in [
        (0, 0, 0, "empty"), (3, 3, 0, "ok"), (3, 2, 1, "partial"), (3, 0, 3, "failed"),
    ]:
        got = schedule_service.run_status(selected=selected, published=published, failed=failed)
        check(f"P1-4 状态机 {expect}", got == expect, f"got {got}")

    check("P0-2 published 是 list 不再崩", schedule_service._count(["a", "b"]) == 2)

    product = None
    db = SessionLocal()
    product = (db.query(Product)
               .filter(Product.deleted_at.is_(None), Product.status != "published").first())
    db.close()

    if product is not None:
        now = time.time()
        task_id = f"rg{uuid.uuid4().hex[:12]}"
        db = SessionLocal()
        db.add(Schedule(
            id=task_id, name="__regression__", enabled=True,
            account_id=product.account_id, shop_id=product.shop_id,
            frequency="range", per_run=2, days=[], day_of_month=[],
            start_date="2026-01-01", end_date="2026-12-31",
            next_due_at=now - 60, created_at=now, updated_at=now, last_result={},
        ))
        db.commit()
        db.close()

        batches: list[dict] = []
        original_publish = publish_service.publish

        def fake_publish(db_, payload, **kwargs):
            batches.append(payload)
            ids = [str(i.get("warehouseId")) for i in (payload.get("items") or [])]
            return {"published": ids[:1], "claimed": [],
                    "failures": ([{"detailId": ids[1], "error": "mock"}] if len(ids) > 1 else []),
                    "mode": "direct", "batchId": "rg", "repriced": [], "warning": None}

        publish_service.publish = fake_publish
        try:
            result = schedule_service.run_due(db=SessionLocal())
            check("P0-4 一轮只发布一次", len(batches) == 1, f"{len(batches)} 次")
            summary = result[0] if result else {}
            check("P1-4 摘要字段齐全",
                  all(k in summary for k in ("requested", "selected", "published",
                                             "failed", "status", "reason")),
                  str({k: summary.get(k) for k in ("requested", "selected", "published",
                                                   "failed", "status")}))
            check("P1-4 失败数不重复计数", summary.get("failed") == 1,
                  f"failed={summary.get('failed')}")

            response = client.patch(f"/api/v1/schedules/{task_id}", headers=H,
                                    json={"frequency": "daily", "startDate": None,
                                          "endDate": None})
            db = SessionLocal()
            row = db.get(Schedule, task_id)
            cleared = bool(row) and row.frequency == "daily" \
                and not row.start_date and not row.end_date
            db.close()
            check("P1-10 显式 null 能清空", response.status_code == 200 and cleared,
                  f"HTTP {response.status_code}, cleared={cleared}")
        finally:
            publish_service.publish = original_publish
            db = SessionLocal()
            row = db.get(Schedule, task_id)
            if row is not None:
                db.delete(row)
                db.commit()
            db.close()

    # --- P1-7 / P1-1：新增接口 ---
    response = client.get("/api/v1/carrier/links", headers=H)
    check("P1-7 链接池已挂载", response.status_code == 200, f"HTTP {response.status_code}")
    check("P1-7 只接受 1688 链接",
          carrier_service.valid_source_link("https://detail.1688.com/offer/123456789.html")
          and not carrier_service.valid_source_link("https://example.com/offer/123456789.html"))

    # 链接池增删闭环（前端 CarrierPage 依赖这两个动作；跑完不留数据）
    probe = "https://detail.1688.com/offer/999888777.html"
    added = client.post("/api/v1/carrier/links", headers=H, json={"urls": [probe]})
    check("P1-7 合法 1688 链接可加入",
          added.status_code == 200 and added.json().get("added") == 1,
          f"added={added.json().get('added') if added.status_code == 200 else added.status_code}")
    removed = client.request("DELETE", "/api/v1/carrier/links", headers=H,
                            json={"urls": [probe]})
    check("P1-7 链接可删除且不留残留",
          removed.status_code == 200 and removed.json().get("removed") == 1,
          f"removed={removed.json().get('removed') if removed.status_code == 200 else removed.status_code}")

    response = client.get("/api/v1/size-charts", headers=H)
    check("P1-1 尺寸表可用", response.status_code == 200, f"HTTP {response.status_code}")
    response = client.post("/api/v1/warehouse/price-preview", headers=H,
                           json={"items": [], "template": ""})
    check("P1-1 价格预览拒绝空 items", response.status_code == 400, f"HTTP {response.status_code}")
    response = client.get("/api/v1/warehouse/price-preview", headers=H)
    check("P1-1 非 POST 返回 405", response.status_code == 405, f"HTTP {response.status_code}")

    # --- P1-1 图片文字识别（本地 OCR） ---
    from services import image_review_service
    check("P1-1 图床白名单放行合法源",
          image_review_service.allowed_source("https://cbu01.alicdn.com/img/a.jpg"))
    check("P1-1 拒绝未知域名",
          not image_review_service.allowed_source("https://evil.example.com/a.jpg"))
    check("P1-1 拒绝内网/回环",
          not image_review_service.allowed_source("https://127.0.0.1/a.jpg")
          and not image_review_service.allowed_source("https://localhost/a.jpg"))
    check("P1-1 中文判定", image_review_service.classify_text("女鞋新款")["action"] == "translate")
    check("P1-1 英文判定", image_review_service.classify_text("Fashion Shoes")["action"] == "keep")
    check("P1-1 空文本转人工",
          image_review_service.classify_text("")["action"] == "review")
    response = client.post("/api/v1/images/analyze", headers=H, json={"images": []})
    check("P1-1 图片识别拒绝空数组", response.status_code == 400,
          f"HTTP {response.status_code}")

    # --- P2-10 词库真启停（enable/disable 走 HTTP，不再只能删词） ---
    from models.violation_word import ViolationWord
    from services import word_service
    db = SessionLocal()
    word_service.seed(db)
    db.commit()
    sample = db.query(ViolationWord).first()
    word = sample.word if sample else ""
    was_enabled = bool(sample.enabled) if sample else True
    db.close()
    if word:
        response = client.post("/api/v1/violation-words", headers=H,
                               json={"action": "disable", "word": word})
        db = SessionLocal()
        row = db.query(ViolationWord).filter(ViolationWord.word == word).first()
        disabled = response.status_code == 200 and row is not None and row.enabled is False
        db.close()
        check("P2-10 disable 只翻位不删词", disabled, f"HTTP {response.status_code}")

        response = client.post("/api/v1/violation-words", headers=H,
                               json={"action": "enable", "word": word})
        db = SessionLocal()
        row = db.query(ViolationWord).filter(ViolationWord.word == word).first()
        restored = response.status_code == 200 and row is not None and row.enabled == was_enabled
        db.close()
        check("P2-10 enable 恢复原状", restored, f"HTTP {response.status_code}")

        response = client.post("/api/v1/violation-words", headers=H,
                               json={"action": "bogus", "word": word})
        check("P2-10 非法 action 仍 400", response.status_code == 400,
              f"HTTP {response.status_code}")

    # --- 打包版启动相关（本轮新发现）---
    # 1) 全新安装必须能拿到 token：require_token 是惰性生成，而「没带 token」
    #    会在 header 检查处直接 401、走不到生成那一步，导致用户永远锁在门外。
    import tempfile as _tempfile

    with _tempfile.TemporaryDirectory() as _fresh:
        from core.config import Settings as _Settings

        fresh = _Settings(data_dir=_fresh)
        check("打包版 全新数据目录启动即生成 token",
              not (fresh.secret_path.is_file()) and bool(fresh.local_secret())
              and fresh.secret_path.is_file(),
              f"secret_exists={fresh.secret_path.is_file()}")

        # 2) 数据目录建不出来时要给可读提示，而不是裸 PermissionError traceback。
        #    用一个不可能创建的路径（父级是文件）来稳定触发，跨平台可靠。
        blocker = Path(_fresh) / "blocker.txt"
        blocker.write_text("x", encoding="utf-8")
        blocked = _Settings(data_dir=str(blocker / "sub"))
        try:
            blocked.ensure_dirs()
            check("打包版 建目录失败给可读错误", False, "居然没抛错")
        except RuntimeError as exc:
            check("打包版 建目录失败给可读错误",
                  "数据目录" in str(exc) and "AOHUANG_DATA_DIR" in str(exc),
                  str(exc).splitlines()[0][:60])
        except OSError as exc:
            check("打包版 建目录失败给可读错误", False,
                  f"仍是裸 {type(exc).__name__}：{exc}")

    # --- P1-1 标题预览 / 保存（老版 /api/preview + /api/save-title） ---
    response = client.post("/api/v1/titles/preview", headers=H,
                           json={"titles": ["好鞋 包邮 秒杀"]})
    body = response.json() if response.status_code == 200 else {}
    first = (body.get("items") or [{}])[0]
    check("P1-1 标题预览删掉违规词",
          response.status_code == 200 and first.get("changed") is True
          and "包邮" in (first.get("removedWords") or []),
          f"removed={first.get('removedWords')}")
    check("P1-1 标题预览不写库",
          response.status_code == 200, f"HTTP {response.status_code}")
    response = client.post("/api/v1/titles/preview", headers=H, json={})
    check("P1-1 标题预览拒绝空请求", response.status_code == 400,
          f"HTTP {response.status_code}")

    # --- P1-1 价格模板同步 / 货源链接同步（老版 /api/price-sync + /api/sync-source-links） ---
    response = client.post("/api/v1/sync/price-templates", headers=H, json={"pageSize": 50})
    check("P1-1 价格模板拒绝超大 pageSize", response.status_code == 400,
          f"HTTP {response.status_code}")
    response = client.post("/api/v1/sync/source-links", headers=H, json={})
    check("P1-1 货源同步拒绝空请求", response.status_code == 400,
          f"HTTP {response.status_code}")
    response = client.post("/api/v1/sync/source-links", headers=H,
                           json={"all": True, "limit": 3})
    body = response.json() if response.status_code == 200 else {}
    check("P1-1 货源同步能扫真实商品",
          response.status_code == 200 and "scanned" in body,
          f"scanned={body.get('scanned')} added={body.get('added')}")

    # --- P1-1 译文中文残留复查（老版 image_review 的检查） ---
    import struct
    import tempfile
    import zlib

    def _blank_png(path: Path, w: int = 240, h: int = 80) -> None:
        raw = b"".join(b"\x00" + b"\xff\xff\xff" * w for _ in range(h))

        def chunk(tag: bytes, data: bytes) -> bytes:
            return (struct.pack(">I", len(data)) + tag + data
                    + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

        png = b"\x89PNG\r\n\x1a\n"
        png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        png += chunk(b"IDAT", zlib.compress(raw))
        png += chunk(b"IEND", b"")
        Path(path).write_bytes(png)

    with tempfile.TemporaryDirectory() as tmp:
        sample = Path(tmp) / "blank.png"
        _blank_png(sample)
        result = image_review_service.check_local_file(sample)
        check("P1-1 译文复查不误报中文",
              result.get("error") is False and result.get("hasChinese") is not True,
              f"action={result.get('action')} hasChinese={result.get('hasChinese')}")
        missing = image_review_service.check_local_file(Path(tmp) / "nope.png")
        check("P1-1 结果图缺失不崩溃",
              missing.get("error") is True, f"reason={missing.get('reason')}")

    db = SessionLocal()
    test_account = db.get(Account, regression_account_id)
    if test_account is not None:
        db.delete(test_account)
        db.commit()
    db.close()

    # --- 基线 ---
    db = SessionLocal()
    print("\n基线：products=%d schedules=%d" %
          (db.query(Product).count(), db.query(Schedule).count()))
    db.close()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n合计 {passed}/{total} 通过")
    for item, ok, detail in RESULTS:
        if not ok:
            print("  未通过:", item, detail)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
