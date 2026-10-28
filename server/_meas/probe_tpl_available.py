"""定价模板合并视图 + 四级兜底 + 未入仓预览 探针。

三条能力都是「少一级就悄悄算错钱」的东西，所以每条分支单独验：
  · selected_template_name 四级兜底：少一级 = 用户每次都要重选模板，选漏按原价上架；
  · template_available 合并视图：同名合并时上游覆盖本地 = 运营改过的利润率被冲掉；
  · preview_price：未入仓商品没有 skuMap 也算得出价。

数据目录指临时目录，绝不碰真实 app.db。结果写 JSON 再读（控制台中文会乱码）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["AOHUANG_DATA_DIR"] = tempfile.mkdtemp(prefix="tplavail-")
os.environ["TK_AUTOMATION_DATA_DIR"] = os.environ["AOHUANG_DATA_DIR"]

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    RESULTS.append((name, bool(ok), str(detail)))


def reset(db) -> None:
    """每个用例前把模板库与选中记录清空，用例之间不互相污染。"""
    from models.setting import PRICING_SELECTED_KEY, PRICING_TEMPLATES_KEY

    from services import setting_service

    setting_service.put(db, PRICING_TEMPLATES_KEY, {})
    setting_service.put(db, PRICING_SELECTED_KEY, {"accounts": {}})
    setting_service.put(db, setting_service.MIAOSHOU_SELECTED_KEY, None)
    db.commit()


def main() -> int:
    from db.session import init_db, session_scope

    init_db()

    from core import errors
    from services import price_template as pt
    from services import setting_service

    with session_scope() as db:
        # ---------- 1. selected_template_name 四级兜底 ----------
        reset(db)
        setting_service.save_template(db, pt.normalize_template({"name": "马来"}))
        setting_service.save_template(db, pt.normalize_template({"name": "马来2.0"}))
        setting_service.save_template(db, pt.normalize_template({"name": "泰国"}))
        db.commit()

        # 1.1 账号显式选中
        setting_service.select_template(db, "马来", account="罗蔓")
        setting_service.select_template(db, "马来2.0", account="")
        setting_service.put(db, setting_service.MIAOSHOU_SELECTED_KEY, "泰国")
        db.commit()
        check("1.1 账号显式选中优先", setting_service.selected_template_name(db, "罗蔓") == "马来",
              setting_service.selected_template_name(db, "罗蔓"))
        check("1.1 有账号记录时全局不抢",
              setting_service.selected_template_name(db, "罗蔓") != "马来2.0")

        # 1.2 该账号没选过 → 落全局
        check("1.2 无账号记录落全局",
              setting_service.selected_template_name(db, "蒋新宇") == "马来2.0",
              setting_service.selected_template_name(db, "蒋新宇"))
        check("1.2 不传账号也用全局",
              setting_service.selected_template_name(db) == "马来2.0",
              setting_service.selected_template_name(db))

        # 1.3 全局也没有 → 落妙手侧记录
        reset(db)
        setting_service.save_template(db, pt.normalize_template({"name": "马来"}))
        setting_service.save_template(db, pt.normalize_template({"name": "马来2.0"}))
        setting_service.put(db, setting_service.MIAOSHOU_SELECTED_KEY, "马来2.0")
        db.commit()
        check("1.3 只有妙手侧记录时用它",
              setting_service.selected_template_name(db, "罗蔓") == "马来2.0",
              setting_service.selected_template_name(db, "罗蔓"))
        check("1.3 妙手侧 dict 形态也认",
              setting_service._miaoshou_selected(db) == "马来2.0")
        setting_service.put(db, setting_service.MIAOSHOU_SELECTED_KEY,
                            {"accounts": {"罗蔓": "马来", "": "马来2.0"}})
        db.commit()
        check("1.3 妙手侧按账号取值",
              setting_service.selected_template_name(db, "罗蔓") == "马来",
              setting_service.selected_template_name(db, "罗蔓"))
        check("1.3 妙手侧账号缺省用空键",
              setting_service.selected_template_name(db, "蒋新宇") == "马来2.0",
              setting_service.selected_template_name(db, "蒋新宇"))

        # 1.4 都没有但模板库只有 1 套 → 自动用那一套
        reset(db)
        setting_service.save_template(db, pt.normalize_template({"name": "唯一模板"}))
        db.commit()
        check("1.4 库只有 1 套自动用它",
              setting_service.selected_template_name(db) == "唯一模板",
              setting_service.selected_template_name(db))
        check("1.4 库只有 1 套 + 带账号也自动用它",
              setting_service.selected_template_name(db, "罗蔓") == "唯一模板",
              setting_service.selected_template_name(db, "罗蔓"))

        # 1.5 什么都没有 → ""
        reset(db)
        db.commit()
        check("1.5 空库无记录返回空串",
              setting_service.selected_template_name(db) == "",
              repr(setting_service.selected_template_name(db)))

        # 1.6 选中指向已删除模板 → 跳过它继续往下找（不留死指针）
        reset(db)
        setting_service.save_template(db, pt.normalize_template({"name": "马来"}))
        setting_service.select_template(db, "已删除的模板", account="罗蔓")
        db.commit()
        check("1.6 指向已删模板的账号选择被跳过",
              setting_service.selected_template_name(db, "罗蔓") == "马来",
              setting_service.selected_template_name(db, "罗蔓"))

        # 1.7 set_miaoshou_selected 写进去的能被读出来（第三级的写入方）
        reset(db)
        setting_service.save_template(db, pt.normalize_template({"name": "马来"}))
        setting_service.save_template(db, pt.normalize_template({"name": "马来2.0"}))
        setting_service.set_miaoshou_selected(db, "马来2.0", "罗蔓")
        db.commit()
        check("1.7 写入的妙手侧记录能读回",
              setting_service.selected_template_name(db, "罗蔓") == "马来2.0",
              setting_service.selected_template_name(db, "罗蔓"))
        check("1.7 别的账号读不到这条",
              setting_service.selected_template_name(db, "蒋新宇") == "",
              "per-account 记录不跨账号，库有 2 套时无兜底 → 空串")
        setting_service.set_miaoshou_selected(db, "马来", "")
        db.commit()
        check("1.7 空账号键当全局妙手侧记录",
              setting_service._miaoshou_selected(db, "无名账号") == "马来",
              setting_service._miaoshou_selected(db, "无名账号"))

        # ---------- 2. template_available 合并视图 ----------
        # 2.1 只有本地
        reset(db)
        setting_service.save_template(db, pt.normalize_template(
            {"name": "马来", "profitPercent": 25.0}))
        db.commit()
        rows = setting_service.template_available(db)
        check("2.1 只有本地 → 1 行", len(rows) == 1, len(rows))
        check("2.1 只有本地 → source=local",
              all(row["source"] == "local" for row in rows),
              [row["source"] for row in rows])

        # 2.2 只有上游
        reset(db)
        db.commit()
        upstream_only = [
            {"name": "马来", "site": "MY", "profitType": "bySalePriceProfit",
             "profitPercent": 20.0, "currency": "CNY", "weightSource": "spu"},
            {"name": "马来2.0", "site": "MY", "profitType": "byCostProfit",
             "profitPercent": 35.0, "currency": "CNY", "weightSource": "sku"},
        ]
        rows = setting_service.template_available(db, upstream_only)
        check("2.2 只有上游 → 2 行", len(rows) == 2, len(rows))
        check("2.2 只有上游 → source=upstream",
              all(row["source"] == "upstream" for row in rows),
              [row["source"] for row in rows])
        check("2.2 上游字段原样带出",
              rows[1]["profitPercent"] == 35.0 and rows[1]["weightSource"] == "sku",
              rows[1])

        # 2.3 同名合并 + 本地优先
        reset(db)
        setting_service.save_template(db, pt.normalize_template(
            {"name": "马来", "site": "MY", "profitType": "bySalePriceProfit",
             "profitPercent": 25.0, "currency": "CNY"}))
        db.commit()
        merged = setting_service.template_available(db, [
            {"name": "马来", "site": "MY", "profitType": "byCostProfit",
             "profitPercent": 40.0, "currency": "MYR", "weightSource": "sku"},
            {"name": "马来2.0", "site": "MY", "profitType": "byCostProfit",
             "profitPercent": 35.0, "currency": "CNY", "weightSource": "sku"},
        ])
        same = [row for row in merged if row["name"] == "马来"]
        check("2.3 同名只留一行", len(same) == 1, len(same))
        check("2.3 同名 source=both", same and same[0]["source"] == "both",
              same[0]["source"] if same else "缺行")
        check("2.3 本地 profitPercent 没被上游覆盖",
              same and same[0]["profitPercent"] == 25.0,
              same[0]["profitPercent"] if same else "缺行")
        check("2.3 本地 profitType 没被上游覆盖",
              same and same[0]["profitType"] == "bySalePriceProfit",
              same[0]["profitType"] if same else "缺行")
        check("2.3 本地 currency 没被上游覆盖",
              same and same[0]["currency"] == "CNY",
              same[0]["currency"] if same else "缺行")
        check("2.3 上游独有那套仍在", any(row["name"] == "马来2.0" for row in merged),
              [row["name"] for row in merged])
        check("2.3 总行数 2（1 合并 + 1 上游）", len(merged) == 2, len(merged))

        # 2.4 selected 只有真正选中的那套是 True
        reset(db)
        setting_service.save_template(db, pt.normalize_template({"name": "马来"}))
        setting_service.save_template(db, pt.normalize_template({"name": "马来2.0"}))
        setting_service.select_template(db, "马来2.0", account="罗蔓")
        db.commit()
        flagged = setting_service.template_available(db, upstream_only, account_id="罗蔓")
        trues = [row["name"] for row in flagged if row["selected"]]
        check("2.4 只有选中那套 selected=True", trues == ["马来2.0"], trues)
        check("2.4 其余行 selected=False",
              sum(1 for row in flagged if not row["selected"]) == len(flagged) - 1,
              len(flagged))
        # 换账号看：该账号没选过 → 落全局（此处全局为空）→ 落妙手侧（空）→ 库有 2 套 → 无
        other = setting_service.template_available(db, upstream_only, account_id="蒋新宇")
        check("2.4 别的账号不继承他人选择",
              not any(row["selected"] for row in other),
              [row["name"] for row in other if row["selected"]])

        # 2.5 上游空数组 → 不炸，返回本地
        reset(db)
        setting_service.save_template(db, pt.normalize_template({"name": "马来"}))
        db.commit()
        empty_rows = setting_service.template_available(db, [])
        check("2.5 上游空数组不炸且返回本地",
              len(empty_rows) == 1 and empty_rows[0]["source"] == "local",
              [(row["name"], row["source"]) for row in empty_rows])

        # 2.6 上游 None → 不炸
        none_rows = setting_service.template_available(db, None)
        check("2.6 上游 None 不炸",
              len(none_rows) == 1 and none_rows[0]["source"] == "local",
              [(row["name"], row["source"]) for row in none_rows])

        # 2.7 上游是脏数据（非 list / 缺 name 的行）也不能炸
        dirty_rows = setting_service.template_available(
            db, ["字符串", {"site": "MY"}, {"name": "  "}, {"name": "有效"}])
        check("2.7 上游脏数据只留有效行",
              [row["name"] for row in dirty_rows] == ["马来", "有效"],
              [row["name"] for row in dirty_rows])

        # ---------- 3. preview_price ----------
        template = pt.normalize_template({"name": "马来", "profitPercent": 20.0})

        # 3.1 正常算出价
        priced = pt.preview_price("35.5", "0.35", template, rate="1.6393")
        check("3.1 price > 0", priced["price"] > 0, priced["price"])
        check("3.1 返回 currency", priced["currency"] == "MYR", priced["currency"])
        check("3.1 返回 lines / linesCny",
              bool(priced["lines"]) and bool(priced["linesCny"]),
              (len(priced["lines"]), len(priced["linesCny"])))
        check("3.1 带 templateName", priced.get("templateName") == "马来",
              priced.get("templateName"))

        # 3.2 给 rate → 用它
        check("3.2 给 rate 就用给的", priced["rate"] == 1.6393, priced["rate"])
        check("3.2 rateSource 标明手工填写",
              "手动" in priced["rateSource"], priced["rateSource"])

        # 3.3 不给 rate → 走兜底汇率（禁掉网络，确保是内置兜底那一条）
        from services import exchange_rate

        saved_live, saved_cached = exchange_rate.live_rate, exchange_rate.cached_rate
        exchange_rate.live_rate = lambda *a, **k: (None, "")
        exchange_rate.cached_rate = lambda: (None, "")
        try:
            fallback = pt.preview_price("35.5", "0.35", template)
            check("3.3 不给 rate 用兜底汇率",
                  fallback["rate"] == float(pt.FALLBACK_EXCHANGE_RATE),
                  fallback["rate"])
            check("3.3 rateSource 标明是兜底",
                  "内置汇率" in fallback["rateSource"], fallback["rateSource"])
            check("3.3 兜底也算出价", fallback["price"] > 0, fallback["price"])
        finally:
            exchange_rate.live_rate = saved_live
            exchange_rate.cached_rate = saved_cached

        # 3.4 采购价 0 / 负数 → 抛错
        for bad in (0, -1, "-0.01"):
            try:
                pt.preview_price(bad, "0.35", template, rate="1.6393")
                check(f"3.4 采购价 {bad} 抛错", False, "没抛")
            except pt.PriceTemplateError as exc:
                check(f"3.4 采购价 {bad} 抛错", "采购价" in str(exc), str(exc))

        # 3.5 重量 0 / 负数 → 抛错
        for bad in (0, -1, "-0.2"):
            try:
                pt.preview_price("35.5", bad, template, rate="1.6393")
                check(f"3.5 重量 {bad} 抛错", False, "没抛")
            except pt.PriceTemplateError as exc:
                check(f"3.5 重量 {bad} 抛错", "重量" in str(exc), str(exc))

        # 3.6 返回里带 missingRateFields / isEstimate
        # 注意：估算标记必须拿**上游原始模板**（费率是 None）才有意义。
        # 库里存的都是归一化结果，None 已经被折成 0，「没填」不可分辨。
        check("3.6 带 missingRateFields",
              isinstance(priced.get("missingRateFields"), list),
              priced.get("missingRateFields"))
        check("3.6 带 isEstimate",
              isinstance(priced.get("isEstimate"), bool), priced.get("isEstimate"))
        lean_raw = {"name": "精简", "platformCommissionPercent": None,
                    "sfpChargePercent": None, "affiliateChargePercent": None}
        lean_priced = pt.preview_price("35.5", "0.35", lean_raw, rate="1.6393")
        check("3.6 缺费率时 isEstimate=True", lean_priced["isEstimate"] is True,
              lean_priced["missingRateFields"])
        check("3.6 缺的字段被列出来",
              "platformCommissionPercent" in lean_priced["missingRateFields"],
              lean_priced["missingRateFields"])
        # 内置默认模板里 lastMileFreightServiceTaxPercent 本来就是 None，
        # 所以「没填」的清单里必然含它，一共四项。
        check("3.6 缺的字段一共四项",
              set(lean_priced["missingRateFields"]) == {
                  "platformCommissionPercent", "sfpChargePercent",
                  "affiliateChargePercent", "lastMileFreightServiceTaxPercent"},
              lean_priced["missingRateFields"])
        check("3.6 齐备时 isEstimate=False", priced["isEstimate"] is False,
              priced["missingRateFields"])
        check("3.6 归一化后的模板不误标估算",
              pt.preview_price("35.5", "0.35", lean_priced and
                               pt.normalize_template(lean_raw),
                               rate="1.6393")["isEstimate"] is False)

        # 3.7 不依赖 skuMap：传的模板/入参里根本没有商品
        check("3.7 无需 skuMap 也能算", "skuMap" not in priced, "ok")

        # ---------- 4. 路由 ----------
        import main

        paths = {}
        for route in main.app.routes:
            path = getattr(route, "path", "")
            for method in (getattr(route, "methods", None) or ()):
                paths.setdefault(path, set()).add(method)
        check("4.1 GET /system/templates/available 已注册",
              "GET" in paths.get("/api/v1/system/templates/available", set()),
              sorted(paths.get("/api/v1/system/templates/available", set())))
        check("4.2 POST /system/price-preview 已注册",
              "POST" in paths.get("/api/v1/system/price-preview", set()),
              sorted(paths.get("/api/v1/system/price-preview", set())))
        check("4.3 GET /system/templates 仍在",
              "GET" in paths.get("/api/v1/system/templates", set()),
              sorted(paths.get("/api/v1/system/templates", set())))

        # ---------- 5. 向后兼容 + 路由行为 ----------
        from api import system as system_api

        reset(db)
        setting_service.save_template(db, pt.normalize_template(
            {"name": "马来", "profitPercent": 25.0}))
        setting_service.select_template(db, "马来", account="")
        db.commit()

        legacy = system_api.templates(db)
        check("5.1 templates 字段还在", isinstance(legacy.get("templates"), list),
              type(legacy.get("templates")).__name__)
        check("5.1 total 字段还在", legacy.get("total") == 1, legacy.get("total"))
        check("5.1 selected 字段还在", isinstance(legacy.get("selected"), dict),
              legacy.get("selected"))
        check("5.2 新增 available", isinstance(legacy.get("available"), list),
              len(legacy.get("available") or []))
        check("5.2 新增 selectedName", legacy.get("selectedName") == "马来",
              legacy.get("selectedName"))
        check("5.2 无上游时 available 全 local",
              all(row["source"] == "local" for row in legacy.get("available") or []),
              [row["source"] for row in legacy.get("available") or []])

        # 5.2b bootstrap 里的 templates 段也带 selectedName（首屏直接用）
        boot = system_api._bootstrap_templates(db)
        check("5.2b bootstrap 带 selectedName", boot.get("selectedName") == "马来",
              boot.get("selectedName"))
        check("5.2b bootstrap 原有字段不动",
              isinstance(boot.get("items"), list) and boot.get("total") == 1
              and isinstance(boot.get("selected"), dict),
              sorted(boot))

        # 5.3 没连账号 → 上游失败不报错，返回本地 + upstreamError
        envelope = system_api.templates_available(db, site="MY", accountId=None,
                                                  refresh=None, credentials=None)
        check("5.3 上游失败仍返回本地", len(envelope["available"]) == 1,
              len(envelope["available"]))
        check("5.3 带 upstreamError 字段", bool(envelope.get("upstreamError")),
              str(envelope.get("upstreamError"))[:40])
        check("5.3 selectedName 照给", envelope.get("selectedName") == "马来",
              envelope.get("selectedName"))
        check("5.3 site 回显大写", envelope.get("site") == "MY", envelope.get("site"))

        # 5.4 POST /system/price-preview：不传 templateName → 四级兜底
        preview = system_api.price_preview(
            __import__("schemas.requests", fromlist=["PricePreviewRequest"])
            .PricePreviewRequest(purchaseCny="35.5", weightKg="0.35", rate="1.6393"),
            db)
        check("5.4 不传模板名走兜底", preview["templateName"] == "马来",
              preview["templateName"])
        for key in ("price", "currency", "lines", "linesCny", "rate", "rateSource",
                    "templateName", "missingRateFields", "isEstimate"):
            check(f"5.4 返回带 {key}", key in preview, sorted(preview))

        # 5.5 参数错 → 400 中文文案
        from schemas.requests import PricePreviewRequest

        for label, kwargs in (
            ("采购价缺失", {"purchaseCny": None, "weightKg": "0.35"}),
            ("采购价为 0", {"purchaseCny": 0, "weightKg": "0.35"}),
            ("采购价为负", {"purchaseCny": -5, "weightKg": "0.35"}),
            ("重量缺失", {"purchaseCny": "35.5", "weightKg": None}),
            ("重量为 0", {"purchaseCny": "35.5", "weightKg": 0}),
            ("重量为负", {"purchaseCny": "35.5", "weightKg": -1}),
            ("采购价非数字", {"purchaseCny": "abc", "weightKg": "0.35"}),
            ("汇率为 0", {"purchaseCny": "35.5", "weightKg": "0.35", "rate": 0}),
        ):
            try:
                system_api.price_preview(PricePreviewRequest(**kwargs), db)
                check(f"5.5 {label} → 400", False, "没抛")
            except errors.AppError as exc:
                check(f"5.5 {label} → 400", exc.status == 400,
                      f"{exc.status} {exc.message}")

        # 5.6 模板不存在 → 404
        try:
            system_api.price_preview(
                PricePreviewRequest(purchaseCny="35.5", weightKg="0.35",
                                    templateName="不存在的模板"), db)
            check("5.6 模板不存在 → 404", False, "没抛")
        except errors.AppError as exc:
            check("5.6 模板不存在 → 404", exc.status == 404, f"{exc.status} {exc.message}")

        # 5.7 库里空且没选中 → 用内置默认模板照样算得出价（不 500）
        reset(db)
        db.commit()
        bare = system_api.price_preview(
            PricePreviewRequest(purchaseCny="35.5", weightKg="0.35", rate="1.6393"), db)
        check("5.7 空库用内置默认模板", bare["price"] > 0, bare["price"])

        # ---------- 6. upstream_template_rows：上游那一半 ----------
        from models.account import Account
        from services import account_service, crypto, sync_service
        from services.miaoshou import MiaoshouError

        reset(db)
        account = Account(name="probe", app_key="probe-key",
                          app_secret_enc=crypto.encrypt("probe-secret"),
                          shops="[]", created_at=0, is_default=True)
        db.add(account)
        db.commit()
        account_id = account.id

        class FakeClient:
            """假上游：按 need 决定返回什么，记录被调用次数。"""

            def __init__(self, need):
                self.need = need
                self.calls = 0
                self.closed = False

            def price_templates(self, **_kwargs):
                self.calls += 1
                if isinstance(self.need, Exception):
                    raise self.need
                return self.need

            def close(self):
                self.closed = True

        saved_client = account_service.account_client
        upstream_raw = {"data": {"priceTemplateList": [
            {"name": "马来", "site": "my", "profitType": "bySalePriceProfit",
             "profitPercent": 20, "currency": "CNY", "weightRefType": "spu"},
            {"name": "马来2.0", "site": "MY", "profitType": "byCostProfit",
             "profitPercent": 35, "currency": "CNY", "weightRefType": "sku"},
        ]}}
        try:
            fake = FakeClient(upstream_raw)
            account_service.account_client = lambda _a: fake
            sync_service._TEMPLATE_CACHE = None
            rows, err = sync_service.upstream_template_rows(
                db, site="MY", account_id=account_id, refresh=True)
            check("6.1 上游正常 → 两行", len(rows) == 2, len(rows))
            check("6.1 上游正常 → 无错误", err == "", err)
            check("6.1 归一化字段齐", rows[1]["profitPercent"] == 35
                  and rows[1]["weightSource"] == "sku", rows[1])
            check("6.1 客户端被关掉", fake.closed is True)

            # 6.2 缓存命中：同一 key 第二次不再回源
            fake2 = FakeClient(upstream_raw)
            account_service.account_client = lambda _a: fake2
            cached_rows, cached_err = sync_service.upstream_template_rows(
                db, site="MY", account_id=account_id, refresh=False)
            check("6.2 缓存命中不回源", fake2.calls == 0, fake2.calls)
            check("6.2 缓存命中无错误", cached_err == "", cached_err)
            check("6.2 缓存内容一致", len(cached_rows) == 2, len(cached_rows))

            # 6.3 refresh=true 绕过缓存
            fake3 = FakeClient(upstream_raw)
            account_service.account_client = lambda _a: fake3
            sync_service.upstream_template_rows(db, site="MY", account_id=account_id,
                                                refresh=True)
            check("6.3 refresh=true 回源", fake3.calls == 1, fake3.calls)

            # 6.4 上游抛错 → 用旧缓存兜底，error 里说清楚
            fake4 = FakeClient(MiaoshouError("上游炸了"))
            account_service.account_client = lambda _a: fake4
            stale_rows, stale_err = sync_service.upstream_template_rows(
                db, site="MY", account_id=account_id, refresh=True)
            check("6.4 上游失败用旧值兜底", len(stale_rows) == 2, len(stale_rows))
            check("6.4 失败有 error 文案", bool(stale_err), stale_err[:40])

            # 6.5 没有旧缓存 + 上游失败 → 空列表 + error，不抛
            sync_service._TEMPLATE_CACHE = None
            fake5 = FakeClient(MiaoshouError("又炸了"))
            account_service.account_client = lambda _a: fake5
            empty_up, empty_err = sync_service.upstream_template_rows(
                db, site="TH", account_id=account_id, refresh=True)
            check("6.5 无缓存时上游失败返回空列表", empty_up == [], empty_up)
            check("6.5 无缓存时上游失败不抛且带 error", bool(empty_err), empty_err[:40])

            # 6.6 没连账号 → 空列表 + error，不抛 401
            no_acct, no_acct_err = sync_service.upstream_template_rows(
                db, site="MY", account_id="999999", refresh=True)
            check("6.6 账号不存在不抛", no_acct == [], no_acct)
            check("6.6 账号不存在带 error", bool(no_acct_err), no_acct_err[:40])
        finally:
            account_service.account_client = saved_client
            sync_service._TEMPLATE_CACHE = None

    return report()


def report() -> int:
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    out = {
        "total": len(RESULTS), "pass": passed, "fail": failed,
        "cases": [{"name": n, "ok": ok, "detail": d} for n, ok, d in RESULTS],
    }
    path = Path(__file__).with_name("probe_tpl_available.json")
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    for name, ok, detail in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""))
    print(f"\nTOTAL {passed} PASS {failed} FAIL")
    print(f"OUT {path}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
