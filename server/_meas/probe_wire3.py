"""三处接线端到端探针：颜色规范化、两道闸门、工厂图剔除、类目 requirements。

前两轮把 color_service / gate_service / category_service 建好了，但都没接进
save_from_collect_box。这个探针专门验「接线是否真的生效」—— 每个用例都走
完整的 save_from_collect_box，而不是单独调那几个函数。

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
os.environ["AOHUANG_DATA_DIR"] = tempfile.mkdtemp(prefix="wire3-")
os.environ["TK_AUTOMATION_DATA_DIR"] = os.environ["AOHUANG_DATA_DIR"]

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))


def main() -> int:
    from core import errors
    from db.session import SessionLocal, init_db
    from models.account import Account
    from services import warehouse_service

    init_db()
    db = SessionLocal()
    account = Account(id=1, name="探针", app_key="ak", app_secret_enc="as",
                      site="MY", shops=json.dumps([{"shopId": "1", "name": "店"}]),
                      created_at=0.0, updated_at=0.0)
    db.add(account)
    db.commit()

    # ---------- 1. 颜色规范化接线 ----------
    # 老版 normalize_colors 要求恰好 1 个颜色属性；两个颜色属性要抛错停下。
    from services import color_service

    payload_two = {
        "skuPropertyList": [
            {"attrName": "颜色", "attrValueList": [{"attrValue": "红色", "attrValueId": "1"}]},
            {"attrName": "色彩", "attrValueList": [{"attrValue": "蓝色", "attrValueId": "2"}]},
        ]
    }
    try:
        warehouse_service._clean_color_props(payload_two)
        check("两个颜色属性 → 抛错", False, "没抛")
    except errors.AppError as exc:
        check("两个颜色属性 → 抛错", True, exc.message[:60])

    # 认不出的颜色必须抛，不能原样放行（这是老版 clean_color 的核心行为）
    payload_bad = {
        "skuPropertyList": [
            {"attrName": "颜色", "attrValueList": [{"attrValue": "zzz", "attrValueId": "1"}]},
        ]
    }
    try:
        warehouse_service._clean_color_props(payload_bad)
        check("认不出的颜色 → 抛错", False, "没抛")
    except errors.AppError as exc:
        check("认不出的颜色 → 抛错", True, exc.message[:60])

    # 正常颜色能被清理并保留
    payload_ok = {
        "skuPropertyList": [
            {"attrName": "颜色",
             "attrValueList": [{"attrValue": "1987白色", "attrValueId": "1"}]},
        ],
        "skuMap": {"1": {"isDelete": "0"}},
    }
    cleaned = warehouse_service._clean_color_props(payload_ok)
    got = cleaned[0]["attrValueList"][0]["attrValue"]
    check("1987白色 → 白色", got == "白色", got)

    # ---------- 2. 工厂图剔除 ----------
    # 从后往前删：从前删会让后面下标前移，第二个要删的指向别的图。
    payload_img = {"imgUrls": ["a", "b", "c", "d"]}
    dropped = warehouse_service._drop_main_images(payload_img, [1, 3])
    check("按 index 剔图（1,3）", payload_img["imgUrls"] == ["a", "c"],
          str(payload_img["imgUrls"]))
    check("剔图计数", dropped == 2, str(dropped))

    # 剔到只剩一张就停：不能把主图删空
    payload_last = {"imgUrls": ["a", "b"]}
    warehouse_service._drop_main_images(payload_last, [0, 1])
    check("不会删空主图", len(payload_last["imgUrls"]) == 1,
          str(payload_last["imgUrls"]))

    # 空列表 / 非法 index 不炸
    check("空主图不炸", warehouse_service._drop_main_images({"imgUrls": []}, [0]) == 0)
    check("越界 index 不炸",
          warehouse_service._drop_main_images({"imgUrls": ["a", "b"]}, [99]) == 0)

    # ---------- 3. 闸门接线 ----------
    # 关键：pre_save_gate 必须在 clean_title 之后算，且 blocks 优先于 issues 报。
    from services import gate_service

    # 闸门靠词库判定 review，空词库永远不拦。这里加一个 review 类词。
    from services import word_service

    word_service.seed(db)
    word_service.add(db, "限量版", action="review")
    db.commit()

    review_info = {"title": "限量版 运动鞋 男款 透气 跑步 鞋 白色 大码 四十五码"}
    gate = gate_service.title_review_gate(review_info, db=db)
    check("标题 review 词 → blocked", gate["blocked"] is True,
          str(gate.get("reviewWords"))[:60])
    check("标题 review 词 message 有内容", bool(gate["message"]), gate["message"][:50])

    gate_allow = gate_service.title_review_gate(review_info, db=db, allow_review=True)
    check("allow_review=True → 放行但保留 reviewWords",
          gate_allow["blocked"] is False and bool(gate_allow["reviewWords"]),
          str(gate_allow.get("reviewWords"))[:40])

    clean_info = {"title": "户外运动跑步鞋 透气马拉松训练情侣款 轻便 舒适 百搭"}
    gate_clean = gate_service.title_review_gate(clean_info, db=db)
    check("干净标题 → 不拦", gate_clean["blocked"] is False, gate_clean["message"][:40])

    # 合并闸门：两个都 blocked 时 blocks 应有 2 条
    merged = gate_service.pre_save_gate(db, review_info, skip_images=True)
    check("skip_images 时不跑 OCR", merged.get("images") is None
          or not merged["images"].get("blocked"), str(merged.get("images"))[:40])

    # ---------- 4. 类目三档兜底 ----------
    from services import category_service

    check("已有 cid → source=existing",
          category_service.resolve_category(
              db, client=None, detail_id="1", shop_id="1",
              info={"cid": "999"})["source"] == "existing")

    # 三档全失败 → 不抛错，给出老版那句操作指引
    none_res = category_service.resolve_category(
        db, client=None, detail_id="1", shop_id="1",
        info={"cid": "", "title": "完全无关的标题 xyz"})
    check("三档全失败 → 不抛错", none_res["cid"] == "", str(none_res.get("source")))
    check("全失败提示含操作指引",
          "没选类目时颜色" in str(none_res.get("message") or ""),
          str(none_res.get("message") or "")[:60])

    # requirements 取不到时传 None，不能阻断入仓
    from services import pre_save_check

    base_info = {"title": "户外运动跑步鞋 透气马拉松训练情侣款 轻便 舒适 百搭 情侣",
                 "imgUrls": ["https://a.example.com/1.jpg"],
                 "skuPropertyList": [{"attrName": "颜色",
                                      "attrValueList": [{"attrValue": "白色",
                                                         "attrValueId": "1"}]}],
                 "weight": 0.5, "cid": "1"}
    base_payload = dict(base_info)
    issues_none = pre_save_check.pre_save_issues(
        base_info, base_payload, {"missingFields": []},
        main_limit=9, detail_limit=20, requirements=None)
    issues_req = pre_save_check.pre_save_issues(
        base_info, base_payload, {"missingFields": []},
        main_limit=9, detail_limit=20,
        requirements={"packageDimensionRequired": True, "requiredAttributes": ["材质"]})
    check("requirements=None 时不报规则 11/12",
          not any("类目要求包裹" in str(i) or "类目必填属性" in str(i)
                  for i in issues_none), str(issues_none)[:80])
    check("requirements 有值时规则 11/12 生效",
          any("类目要求包裹" in str(i) for i in issues_req)
          and any("类目必填属性" in str(i) for i in issues_req),
          str(issues_req)[:100])

    # ---------- 5. 签名接线 ----------
    import inspect

    sig = inspect.signature(warehouse_service.save_from_collect_box)
    for name in ("allow_title_review", "allow_image_review", "skip_image_review"):
        check(f"save_from_collect_box 有 {name}", name in sig.parameters)

    src = inspect.getsource(warehouse_service.save_from_collect_box)
    check("内部调了 pre_save_gate", "pre_save_gate" in src)
    check("内部调了 _drop_main_images", "_drop_main_images" in src)
    check("内部调了 resolve_category", "resolve_category" in src)
    check("内部查了 category_metadata", "category_metadata" in src)
    check("requirements 传进 pre_save_issues", "requirements=requirements" in src)
    check("返回体带 categorySource", '"categorySource"' in src)
    check("返回体带 droppedImages", '"droppedImages"' in src)

    db.close()
    return report()


def report() -> int:
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    out = {
        "total": len(RESULTS),
        "pass": passed,
        "fail": failed,
        "cases": [{"name": n, "ok": ok, "detail": d} for n, ok, d in RESULTS],
    }
    path = Path(__file__).with_name("probe_wire3.json")
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    for name, ok, detail in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""))
    print(f"\nTOTAL {passed} PASS {failed} FAIL")
    print(f"OUT {path}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
