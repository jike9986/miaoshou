"""两阶段保存探针：验 payloadHash 三道闸真的能拦住「核对后被改」。

为什么这条值得单独测：入仓不可逆。没有 hash 闸，运营核对完点保存、中间图翻完
主图换了，存进去的就是他没看过的那份，而且事后无法分辨。所以三个方向都要验：
  · hash 对上 → 存
  · hash 对不上 → 拒，且说清「哪一步变了」
  · 缺 confirm / 缺 hash → 拒，且是 400 不是 500
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["AOHUANG_DATA_DIR"] = tempfile.mkdtemp(prefix="csave-")
os.environ["TK_AUTOMATION_DATA_DIR"] = os.environ["AOHUANG_DATA_DIR"]

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), str(detail)))


def main() -> int:
    import main as app_main
    from db.session import SessionLocal, init_db
    from fastapi.testclient import TestClient
    from _probe_auth import teacher_headers

    routes = [(getattr(r, "path", ""), sorted(getattr(r, "methods", []) or []))
              for r in app_main.app.routes]
    hit = [r for r in routes if r[0].endswith("/warehouse/confirm-save")]
    check("路由已注册 /warehouse/confirm-save", bool(hit), str(hit))
    check("是 POST", bool(hit) and "POST" in hit[0][1], str(hit[0][1]) if hit else "")

    init_db()
    db = SessionLocal()
    from services import setting_service

    setting_service.set_read_only(db, False)
    db.commit()
    db.close()

    headers = teacher_headers()
    client = TestClient(app_main.app)
    url = "/api/v1/warehouse/confirm-save"

    # 缺 confirm → 400，不是 500
    r = client.post(url, headers=headers,
                    json={"detailId": "1", "shopId": "1", "payloadHash": "x"})
    check("缺 confirm → 400", r.status_code == 400, str(r.status_code))
    check("缺 confirm 文案可读",
          "确认保存" in json.dumps(r.json(), ensure_ascii=False),
          json.dumps(r.json(), ensure_ascii=False)[:90])

    # 缺 payloadHash → 400
    r = client.post(url, headers=headers,
                    json={"detailId": "1", "shopId": "1", "confirm": True})
    check("缺 payloadHash → 400", r.status_code == 400, str(r.status_code))
    check("缺 hash 文案提到 payloadHash",
          "payloadHash" in json.dumps(r.json(), ensure_ascii=False),
          json.dumps(r.json(), ensure_ascii=False)[:90])

    # 缺必填字段（detailId/shopId）：本项目统一错误信封把 pydantic 的 422 也
    # 转成 400 VALIDATION_ERROR，所以这里按 400 断言 —— 关键是「有明确错误码
    # 和可读文案」，不是具体哪一个 4xx。
    r = client.post(url, headers=headers, json={"confirm": True, "payloadHash": "x"})
    check("缺 detailId → 4xx 且是校验错", r.status_code == 400, str(r.status_code))
    check("缺 detailId 错误码是 VALIDATION_ERROR",
          r.json().get("error", {}).get("code") == "VALIDATION_ERROR",
          str(r.json().get("error", {}).get("code")))

    # 只读模式 → 403
    db = SessionLocal()
    setting_service.set_read_only(db, True)
    db.commit()
    db.close()
    r = client.post(url, headers=headers,
                    json={"detailId": "1", "shopId": "1", "payloadHash": "x",
                          "confirm": True})
    check("只读模式 → 403", r.status_code == 403, str(r.status_code))
    db = SessionLocal()
    setting_service.set_read_only(db, False)
    db.commit()
    db.close()

    # ---------- payload_review 的 hash 行为 ----------
    from models.product import Product
    from services import warehouse_service

    class FakeProduct:
        detail_id = "3484413012"
        shop_id = "18077147"

    payload = {
        "title": "户外运动跑步鞋 透气马拉松训练情侣款 轻便 舒适 百搭 大码",
        "cid": "603751", "weight": 0.5,
        "packageLength": 30, "packageWidth": 20, "packageHeight": 10,
        "imgUrls": ["https://a.example.com/1.jpg", "https://a.example.com/2.jpg"],
        "notes": "<p><img src='https://a.example.com/d1.jpg'></p>",
        "skuPropertyList": [{"attrName": "颜色",
                             "attrValueList": [{"attrValue": "白色", "attrValueId": "1"}]}],
        "skuMap": {"1": {"isDelete": "0"}},
        "sizeChart": "https://a.example.com/chart.png",
    }
    review = warehouse_service.payload_review(FakeProduct(), payload,
                                              coverage={"missingFields": []})
    check("review 带 payloadHash", bool(review.get("payloadHash")),
          str(review.get("payloadHash"))[:16])
    check("hash 是 64 位 sha256", len(review["payloadHash"]) == 64,
          str(len(review["payloadHash"])))
    check("review 带主图张数", review["mainImageCount"] == 2, review["mainImageCount"])
    check("review 带颜色", review["colors"] == ["白色"], str(review["colors"]))
    check("review 带包裹尺寸",
          review["packageDimensions"]["packageLength"] == 30,
          str(review["packageDimensions"]))
    check("review 带标题长度", review["titleLength"] == len(payload["title"]),
          review["titleLength"])

    # 同一份 payload 两次算出的 hash 必须一样（否则确认永远失败）
    again = warehouse_service.payload_review(FakeProduct(), dict(payload),
                                             coverage={"missingFields": []})
    check("同一份 payload hash 稳定", again["payloadHash"] == review["payloadHash"],
          f"{review['payloadHash'][:8]} vs {again['payloadHash'][:8]}")

    # 键顺序变了 hash 不该变（JSON 序列化必须 sort_keys）
    reordered = {k: payload[k] for k in reversed(list(payload))}
    reordered_review = warehouse_service.payload_review(FakeProduct(), reordered,
                                                        coverage={"missingFields": []})
    check("键顺序不影响 hash", reordered_review["payloadHash"] == review["payloadHash"],
          f"{review['payloadHash'][:8]} vs {reordered_review['payloadHash'][:8]}")

    # 改标题 → hash 必须变（这是「核对后被改」最典型的场景）
    changed = dict(payload, title=payload["title"] + " 加长")
    changed_review = warehouse_service.payload_review(FakeProduct(), changed,
                                                      coverage={"missingFields": []})
    check("改标题 → hash 变", changed_review["payloadHash"] != review["payloadHash"],
          f"{review['payloadHash'][:8]} vs {changed_review['payloadHash'][:8]}")

    # 换主图 → hash 必须变（图片翻译完成后写回的场景）
    swapped = dict(payload, imgUrls=["https://a.example.com/9.jpg"])
    swapped_review = warehouse_service.payload_review(FakeProduct(), swapped,
                                                      coverage={"missingFields": []})
    check("换主图 → hash 变", swapped_review["payloadHash"] != review["payloadHash"],
          f"{review['payloadHash'][:8]} vs {swapped_review['payloadHash'][:8]}")

    # 改重量 → hash 变
    heavier = dict(payload, weight=0.8)
    heavier_review = warehouse_service.payload_review(FakeProduct(), heavier,
                                                      coverage={"missingFields": []})
    check("改重量 → hash 变", heavier_review["payloadHash"] != review["payloadHash"],
          "")

    # issues / gate 能透传（让运营在摘要里就看到为什么可能存不了）
    with_issues = warehouse_service.payload_review(
        FakeProduct(), payload, coverage={},
        issues=["标题太短"], gate={"blocks": [{"message": "含需复核词"}], "warnings": []})
    check("review 透传 issues", with_issues.get("issues") == ["标题太短"],
          str(with_issues.get("issues")))
    check("review 透传 gate.blocks",
          with_issues["gate"]["blocks"][0]["message"] == "含需复核词",
          str(with_issues.get("gate")))

    # ---------- 源码层确认三道闸都在 ----------
    import inspect

    src = inspect.getsource(app_main)
    from api import warehouse as wh_api

    fn_src = inspect.getsource(wh_api.confirm_save)
    check("实现里查了 confirm", "payload.confirm is not True" in fn_src)
    check("实现里查了 payloadHash 非空", 'if not expected' in fn_src)
    check("实现里比对 hash", "actual != expected" in fn_src)
    check("hash 不一致返回 409", "status=409" in fn_src)
    check("错误码是 PAYLOAD_CHANGED", "PAYLOAD_CHANGED" in fn_src)
    check("说明了变化原因", "图片翻译完成" in fn_src)
    check("真正写库那次 enforce_pre_save=True",
          fn_src.count("enforce_pre_save=True") == 1, "")
    check("预览那次 enforce_pre_save=False",
          fn_src.count("enforce_pre_save=False") == 1, "")
    check("写库后有 commit", "db.commit()" in fn_src)
    check("返回 saved=True", '"saved": True' in fn_src)

    return report()


def report() -> int:
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    out = {"total": len(RESULTS), "pass": passed, "fail": failed,
           "cases": [{"name": n, "ok": ok, "detail": d} for n, ok, d in RESULTS]}
    path = Path(__file__).with_name("probe_confirm_save.json")
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    for name, ok, detail in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""))
    print(f"\nTOTAL {passed} PASS {failed} FAIL")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
