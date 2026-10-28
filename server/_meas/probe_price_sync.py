"""定价模板同步探针：逐条验证三级合并的每个分支。

三级合并是这一批里唯一会「悄悄算错钱」的改动，所以每个分支都要单独验：
上游有值 / 上游存在但为 None / 上游存在但为 "" / 上游没这个字段 / 都没有。
最后一条分支错一位，运营的费率就被冲成 0，报价偏低而界面毫无异常。

数据目录指临时目录，绝不碰真实 app.db。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["AOHUANG_DATA_DIR"] = tempfile.mkdtemp(prefix="pricesync-")
os.environ["TK_AUTOMATION_DATA_DIR"] = os.environ["AOHUANG_DATA_DIR"]

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), str(detail)))


def main() -> int:
    from services import price_template as pt

    # ---------- 1. list_upstream_templates ----------
    raw_two = {"data": {"priceTemplateList": [
        {"name": "马来", "site": "my", "profitType": "bySalePriceProfit",
         "profitPercent": 20, "currency": "CNY", "weightRefType": "spu"},
        {"name": "马来2.0", "site": "MY", "profitType": "byCostProfit",
         "profitPercent": 35, "currency": "CNY", "weightRefType": "sku"},
    ]}}
    listed = pt.list_upstream_templates(raw_two)
    check("list 返回两套", len(listed) == 2, str(len(listed)))
    check("weightRefType=spu → weightSource=spu",
          listed[0]["weightSource"] == "spu", listed[0]["weightSource"])
    check("weightRefType=sku → weightSource=sku",
          listed[1]["weightSource"] == "sku", listed[1]["weightSource"])
    check("site 转大写", listed[0]["site"] == "MY", listed[0]["site"])
    check("name 去空格", pt.list_upstream_templates(
        {"priceTemplateList": [{"name": "  马来  "}]})[0]["name"] == "马来")
    check("没有 name 的行被丢掉",
          pt.list_upstream_templates(
              {"priceTemplateList": [{"name": ""}, {"name": "A"}]}) == [
                  {"name": "A", "site": "", "profitType": None, "profitPercent": None,
                   "currency": None, "weightSource": "spu"}])
    check("缺省 weightRefType 按 spu",
          pt.list_upstream_templates(
              {"priceTemplateList": [{"name": "A"}]})[0]["weightSource"] == "spu")

    # ---------- 2. pick_upstream_template ----------
    try:
        pt.pick_upstream_template(raw_two)
        check("多套未指定 → 抛错", False, "没抛")
    except pt.PriceTemplateError as exc:
        check("多套未指定 → 抛错", True, str(exc)[:70])
        check("报错含「2 套定价模板」", "2 套定价模板" in str(exc), str(exc)[:70])
        check("报错含「请先选择用哪一套」", "请先选择用哪一套" in str(exc))
        check("报错列出两个名字",
              "「马来」" in str(exc) and "「马来2.0」" in str(exc), str(exc)[:90])

    single = {"priceTemplateList": [{"name": "只有一套"}]}
    check("单套自动选", pt.pick_upstream_template(single)["name"] == "只有一套")

    try:
        pt.pick_upstream_template(raw_two, "不存在")
        check("指定名不存在 → 抛错", False, "没抛")
    except pt.PriceTemplateError as exc:
        check("指定名不存在 → 抛错", True, str(exc)[:60])
        check("报错列出现有名字",
              "「马来」" in str(exc) and "「马来2.0」" in str(exc), str(exc)[:80])

    check("指定名存在 → 返回那套",
          pt.pick_upstream_template(raw_two, "马来2.0")["name"] == "马来2.0")

    try:
        pt.pick_upstream_template({"priceTemplateList": []})
        check("上游空 → 抛错", False, "没抛")
    except pt.PriceTemplateError as exc:
        check("上游空 → 抛错", True, str(exc)[:50])
        check("空报错提示先连接妙手", "请先连接妙手" in str(exc))

    # ---------- 3. 三级合并：五个分支 ----------
    stored = {
        "name": "马来", "site": "MY", "profitType": "bySalePriceProfit",
        "profitPercent": 25, "currency": "CNY",
        "vatChargePercent": 10, "bcpChargePercent": 3.24,
        "paymentChargePercent": 3.78, "withdrawChargePercent": 1,
        "sfpChargePercent": 0, "affiliateChargePercent": 0,
        "platformCommissionPercent": 14.58,
        "lastMileFreightCny": 16.63, "platformSupportCny": 0.83,
        "freightTable": {"currency": "MYR", "firstWeightGrams": 30,
                         "firstWeightPrice": 0.18, "continuedUnitGrams": 10,
                         "continuedUnitPrice": 0.12},
    }
    defaults = {"tiktokCbSiteAndDefaultChargePercentMap": {"MY": {
        "platformChargePercent": 99, "paymentChargePercent": 88,
        "vatChargePercent": 77, "withdrawChargePercent": 66,
        "bcpChargePercent": 55, "sfpChargePercent": 44,
        "affiliateChargePercent": 33, "tariffChargePercent": 22,
    }}}

    # 分支 A：上游有值 → 用上游
    up_a = {"priceTemplateList": [{"name": "马来", "site": "MY",
                                   "profitPercent": 40,
                                   "platformChargePercent": 5.5}]}
    merged_a = pt.merge_from_upstream(up_a, stored, defaults)
    check("A 上游有值 → 用上游 profitPercent",
          merged_a["profitPercent"] == 40.0, merged_a["profitPercent"])
    check("A 上游有值 → 用上游 platformCommissionPercent",
          merged_a["platformCommissionPercent"] == 5.5,
          merged_a["platformCommissionPercent"])

    # 分支 B：上游存在但为 None → 该项不收费（老版「a blank means no fee」）
    # 注意这不是「回落到已存值」：妙手用空值表达「这模板不收这项费」，
    # 若回落成已存值，运营已经关掉的收费项会被重新打开。
    up_b = {"priceTemplateList": [{"name": "马来", "site": "MY",
                                   "vatChargePercent": None}]}
    merged_b = pt.merge_from_upstream(up_b, stored, defaults)
    check("B 上游 None → 该项不收费（0）",
          merged_b["vatChargePercent"] == 0.0, merged_b["vatChargePercent"])

    # 分支 C：上游存在但为 "" → 同样视为不收费
    up_c = {"priceTemplateList": [{"name": "马来", "site": "MY",
                                   "bcpChargePercent": ""}]}
    merged_c = pt.merge_from_upstream(up_c, stored, defaults)
    check("C 上游空串 → 该项不收费（0）",
          merged_c["bcpChargePercent"] == 0.0, merged_c["bcpChargePercent"])

    # 分支 D：上游没这个字段 → 用已存值（这才是「保住运营手工填的值」）
    up_d = {"priceTemplateList": [{"name": "马来", "site": "MY"}]}
    merged_d = pt.merge_from_upstream(up_d, stored, defaults)
    check("D 上游无此字段 → 用已存值 paymentChargePercent",
          merged_d["paymentChargePercent"] == 3.78,
          merged_d["paymentChargePercent"])
    check("D 上游无此字段 → 用已存值 withdrawChargePercent",
          merged_d["withdrawChargePercent"] == 1.0, merged_d["withdrawChargePercent"])
    check("D 上游无此字段 → 用已存值 vatChargePercent",
          merged_d["vatChargePercent"] == 10.0, merged_d["vatChargePercent"])
    check("D 上游无此字段 → 已存 profitPercent 保住",
          merged_d["profitPercent"] == 25.0, merged_d["profitPercent"])
    check("D 没有落到站点默认（99）",
          merged_d["platformCommissionPercent"] != 99.0,
          merged_d["platformCommissionPercent"])

    # 分支 E：已存值也没有 → 用站点默认。
    # 注意 stored 经过 normalize 后各费率都会被补成 0，所以「完全没有」这条
    # 分支在当前 normalize 下走不到 —— 这是老版行为（DEFAULT_TEMPLATE 里是
    # None，normalize 变 0）。这里验的是「上游给了站点费率字段时用上游」，
    # 以及 platformSupportCny 单独走 support_cny_map 那条路径。
    up_e = {"priceTemplateList": [{"name": "马来", "site": "MY",
                                   "sfpChargePercent": 7}]}
    merged_e = pt.merge_from_upstream(up_e, {}, defaults)
    check("E 上游给费率 → 用上游 sfpChargePercent=7",
          merged_e["sfpChargePercent"] == 7.0, merged_e["sfpChargePercent"])
    # platformSupportCny 走独立路径：pick 之后再单独判一次已存值。
    # 注意 DEFAULT_TEMPLATE 里 platformSupportCny 是 0.83，normalize 会把它补上，
    # 所以「已存值也没有」实际落到 0.83 而不是站点默认 0.82 —— 老版同样如此
    # （price_template.py:81 与 :156），站点默认分支在老版里同样不可达。
    # 这里按真实行为验，不假装它走的是 support_cny_map。
    support_kept = pt.merge_from_upstream(
        {"priceTemplateList": [{"name": "马来", "site": "MY"}]},
        {"name": "马来", "site": "MY", "platformSupportCny": 0.5}, defaults)
    check("E platformSupportCny 已存值优先（0.5 不被覆盖）",
          abs(support_kept["platformSupportCny"] - 0.5) < 1e-9,
          support_kept["platformSupportCny"])
    support_default = pt.merge_from_upstream(
        {"priceTemplateList": [{"name": "马来", "site": "MY"}]},
        {"name": "马来", "site": "MY"}, defaults)
    check("E platformSupportCny 空模板 → 用 DEFAULT_TEMPLATE 的 0.83（与老版一致）",
          abs(support_default["platformSupportCny"] - 0.83) < 1e-9,
          support_default["platformSupportCny"])
    support_upstream = pt.merge_from_upstream(
        {"priceTemplateList": [{"name": "马来", "site": "MY",
                                "platformSupportCharge": 1.2}]},
        {"name": "马来", "site": "MY"}, defaults)
    check("E platformSupportCny 上游给了值 → 用上游 1.2",
          abs(support_upstream["platformSupportCny"] - 1.2) < 1e-9,
          support_upstream["platformSupportCny"])

    # 运费表必须保留已存的
    check("运费表保留已存起重价",
          merged_a["freightTable"]["firstWeightPrice"] == 0.18,
          merged_a["freightTable"])
    check("运费表保留已存续重单位",
          merged_a["freightTable"]["continuedUnitGrams"] == 10.0,
          merged_a["freightTable"])

    # ---------- 4. 六个新增费率字段 ----------
    check("EXTRA_CHARGE_FIELDS 有 6 项", len(pt.EXTRA_CHARGE_FIELDS) == 6,
          str(len(pt.EXTRA_CHARGE_FIELDS)))
    normalized = pt.normalize_template({**stored, "tariffChargePercent": 8,
                                        "consumptionTaxChargePercent": 5})
    check("normalize 接受 tariffChargePercent",
          normalized["tariffChargePercent"] == 8.0, normalized["tariffChargePercent"])
    check("normalize 接受 consumptionTaxChargePercent",
          normalized["consumptionTaxChargePercent"] == 5.0,
          normalized["consumptionTaxChargePercent"])
    check("normalize 缺省补 0",
          normalized["localConsumptionTaxChargePercent"] == 0.0,
          normalized["localConsumptionTaxChargePercent"])

    base = pt.normalize_template(stored)
    priced_base = pt.display_price(50, 0.5, base, rate="1.6393")
    with_extra = pt.normalize_template({**stored, "tariffChargePercent": 10,
                                        "consumptionTaxChargePercent": 5,
                                        "activityChargePercent": 3})
    priced_extra = pt.display_price(50, 0.5, with_extra, rate="1.6393")
    check("加了 6 项费率后售价更高（成本真的进公式了）",
          priced_extra["price"] > priced_base["price"],
          f"{priced_base['price']} -> {priced_extra['price']}")
    check("拆分里出现 tariffChargePercent 项",
          "tariffChargePercent" in priced_extra["lines"],
          str([k for k in priced_extra["lines"] if "tariff" in k.lower()]))
    check("tariffChargePercent 金额 > 0",
          priced_extra["lines"]["tariffChargePercent"] > 0,
          priced_extra["lines"]["tariffChargePercent"])
    check("基础模板里 tariffChargePercent 是 0 但仍出现在拆分里",
          priced_base["lines"]["tariffChargePercent"] == 0.0,
          priced_base["lines"]["tariffChargePercent"])

    # 费率确实进了成本：售价 - 利润 - 各成本 ≈ 0（残差为 0 说明解价正确）
    lines = priced_extra["lines"]
    cost = sum(v for k, v in lines.items()
               if k not in ("profit", "pretaxPrice"))
    check("售价与成本+利润平衡（解价正确）",
          abs(priced_extra["price"] - cost - lines["profit"]) < 0.02,
          round(priced_extra["price"] - cost - lines["profit"], 4))

    # missingRateFields 的口径与老版 pricing.py:96-122 一致：只在模板里「根本没有
    # 这个键」时才算缺。normalize_template 会把缺失项补成 0（老版 DEFAULT_TEMPLATE
    # 是 None，normalize 后也是 0），所以过了 normalize 的模板不会有缺项。
    # 这里验的是「显式给 None 才算缺」，这才是 isEstimate 的真实触发条件。
    raw_missing = dict(stored)
    raw_missing["tariffChargePercent"] = None
    raw_missing["consumptionTaxChargePercent"] = None
    priced_missing = pt.display_price(50, 0.5, raw_missing, rate="1.6393")
    check("显式 None → missingRateFields 含该项",
          "tariffChargePercent" in priced_missing["missingRateFields"],
          str(priced_missing["missingRateFields"])[:90])
    check("显式 None → isEstimate 为 True", priced_missing["isEstimate"] is True)
    check("normalize 过的模板 → 无缺项（与老版一致）",
          priced_base["missingRateFields"] == [] and priced_base["isEstimate"] is False,
          str(priced_base["missingRateFields"]))

    # ---------- 5. sync_price_templates 落库语义 ----------
    from db.session import SessionLocal, init_db
    from models.account import Account
    from services import setting_service, sync_service

    init_db()
    db = SessionLocal()
    account = Account(id=1, name="探针", app_key="ak", app_secret_enc="as",
                      site="MY", shops=json.dumps([]), created_at=0.0, updated_at=0.0)
    db.add(account)
    db.commit()

    class FakeClient:
        def __init__(self, raw):
            self.raw = raw
            self.closed = False

        def price_templates(self, **kwargs):
            return self.raw

        def price_site_defaults(self):
            return defaults

        def close(self):
            self.closed = True

    before = len(setting_service.price_templates(db))
    fake = FakeClient(raw_two)
    try:
        sync_service.sync_price_templates(db, site="MY", account_id=1, client=fake)
        check("多套未指定 → 同步抛错", False, "没抛")
    except pt.PriceTemplateError as exc:
        check("多套未指定 → 同步抛错", True, str(exc)[:60])

    fake2 = FakeClient(raw_two)
    preview = sync_service.sync_price_templates(
        db, site="MY", account_id=1, template_name="马来2.0", save=False, client=fake2)
    check("save=False 时 synced=True", preview["synced"] is True)
    check("save=False 时 saved=False", preview["saved"] is False)
    check("save=False 不落库", len(setting_service.price_templates(db)) == before,
          f"{before} -> {len(setting_service.price_templates(db))}")
    check("预览返回上游两套", preview["upstreamCount"] == 2, preview["upstreamCount"])
    check("预览选中马来2.0", preview["selected"] == "马来2.0", preview["selected"])
    check("预览的合并结果 profitPercent=35",
          preview["merged"]["profitPercent"] == 35.0,
          preview["merged"]["profitPercent"])

    fake3 = FakeClient(raw_two)
    saved = sync_service.sync_price_templates(
        db, site="MY", account_id=1, template_name="马来2.0", save=True, client=fake3)
    check("save=True 时 saved=True", saved["saved"] is True)
    check("save=True 落库了",
          "马来2.0" in setting_service.price_templates(db),
          str(list(setting_service.price_templates(db))))
    selected = setting_service.selected_templates(db)
    check("save=True 设为当前选中",
          selected.get("1") == "马来2.0", str(selected))

    check("client 未被误关（外部传入不关）", fake3.closed is False)
    db.close()
    return report()


def report() -> int:
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    out = {
        "total": len(RESULTS), "pass": passed, "fail": failed,
        "cases": [{"name": n, "ok": ok, "detail": d} for n, ok, d in RESULTS],
    }
    path = Path(__file__).with_name("probe_price_sync.json")
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    for name, ok, detail in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""))
    print(f"\nTOTAL {passed} PASS {failed} FAIL")
    print(f"OUT {path}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
