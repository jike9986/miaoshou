"""自定义分类标签 + 货源 offer id 汇总的本地验证（不联网、不部署）。

覆盖：
  1. custom_categories / add / remove / rename 的语义（幂等、截断、上限、空名）
  2. facets().categories 能看见自定义标签，且同名只出一条
  3. all_source_item_ids 的抠取规则（sourceList / sourceItemId / oriItemId、
     isdigit 过滤、去重保序、accountId 过滤）
  4. 路由层的 201 / query 删除 / 动态路由不吃静态路径

数据目录用临时目录（AOHUANG_DATA_DIR + TK_AUTOMATION_DATA_DIR），绝不碰真实
app.db。结果写 JSON 文件再读——中文在 Windows 控制台会被吃成乱码。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP_DIR = Path(tempfile.mkdtemp(prefix="customcat-probe-"))
os.environ["AOHUANG_DATA_DIR"] = str(_TMP_DIR)
os.environ["TK_AUTOMATION_DATA_DIR"] = str(_TMP_DIR)
OUT = ROOT / "_meas" / "custom_cat.json"

from fastapi.testclient import TestClient  # noqa: E402

from api import API_PREFIX  # noqa: E402
from core import errors  # noqa: E402
from db.session import Base, SessionLocal, engine  # noqa: E402
from main import app  # noqa: E402
from models.account import Account  # noqa: E402
from models.product import Product  # noqa: E402
from services import warehouse_service as svc  # noqa: E402

results: list[dict] = []


def check(label: str, ok: bool, detail: object = "") -> None:
    results.append({"label": label, "ok": bool(ok), "detail": detail})


# --- 自定义分类标签（服务层）-----------------------------------------------
def test_custom_categories() -> None:
    db = SessionLocal()
    try:
        check("初始为空", svc.custom_categories(db) == [],
              svc.custom_categories(db))

        # 加 3 个
        svc.add_custom_category(db, "精品")
        svc.add_custom_category(db, "待上架")
        svc.add_custom_category(db, "夏季")
        db.commit()
        got = svc.custom_categories(db)
        check("加 3 个 -> 3 条", len(got) == 3, got)
        check("每条形状 name/cid/count",
              all(item["cid"] == "" and item["count"] == 0 and item["name"]
                  for item in got), got)
        check("顺序按加入先后",
              [item["name"] for item in got] == ["精品", "待上架", "夏季"],
              [item["name"] for item in got])

        # 幂等：重复加同名
        svc.add_custom_category(db, "精品")
        db.commit()
        check("重复加同名仍是 3 条", len(svc.custom_categories(db)) == 3,
              svc.custom_categories(db))

        # 空名 -> validation
        try:
            svc.add_custom_category(db, "   ")
        except errors.AppError as exc:
            check("空名报错", exc.message == "分类名不能为空", exc.message)
            check("空名是 VALIDATION_ERROR", exc.code == errors.VALIDATION_ERROR, exc.code)
            check("空名带 field=name", exc.details.get("field") == "name", exc.details)
        else:
            check("空名报错", False, "没有抛异常")

        # 超 30 字截断
        long_name = "长" * 40
        svc.add_custom_category(db, long_name)
        db.commit()
        names = [item["name"] for item in svc.custom_categories(db)]
        check("超 30 字截断到 30", ("长" * 30) in names and long_name not in names,
              [len(n) for n in names])
        check("截断后共 4 条", len(names) == 4, names)

        # 改名
        svc.rename_custom_category(db, "夏季", "初秋")
        db.commit()
        names = [item["name"] for item in svc.custom_categories(db)]
        check("改名生效", "初秋" in names and "夏季" not in names, names)
        try:
            svc.rename_custom_category(db, "不存在", "随便")
        except errors.AppError as exc:
            check("改名不存在 -> 404", exc.status == 404, exc.message)
        else:
            check("改名不存在 -> 404", False, "没有抛异常")

        # 删除：删一个剩 3 条；删不存在不报错
        svc.remove_custom_category(db, "初秋")
        db.commit()
        check("删一个 -> 3 条", len(svc.custom_categories(db)) == 3,
              svc.custom_categories(db))
        try:
            svc.remove_custom_category(db, "根本没有这个")
            db.commit()
            check("删不存在不报错", True, "")
        except errors.AppError as exc:
            check("删不存在不报错", False, exc.message)
        check("删不存在后仍是 3 条", len(svc.custom_categories(db)) == 3,
              svc.custom_categories(db))

        # 读坏了的设置当空（不是数组 / 混了非字符串）
        svc.setting_service.put(db, "warehouse.customCategories", {"oops": 1})
        db.commit()
        check("读坏(非数组) -> []", svc.custom_categories(db) == [],
              svc.custom_categories(db))
        svc.setting_service.put(db, "warehouse.customCategories",
                                ["好", 123, None, "  ", "好"])
        db.commit()
        check("脏数组被清洗", [i["name"] for i in svc.custom_categories(db)] == ["好"],
              svc.custom_categories(db))

        # 上限 200
        svc.setting_service.put(db, "warehouse.customCategories",
                                [f"标签{i}" for i in range(200)])
        db.commit()
        try:
            svc.add_custom_category(db, "第201个")
        except errors.AppError as exc:
            check("超 200 报错", exc.message == "自定义分类最多 200 个", exc.message)
        else:
            check("超 200 报错", False, "没有抛异常")
        check("超 200 后仍是 200 条", len(svc.custom_categories(db)) == 200,
              len(svc.custom_categories(db)))

        # 还原成一组干净数据给 facets 用
        svc.setting_service.put(db, "warehouse.customCategories", ["精品", "待上架"])
        db.commit()
    finally:
        db.close()


# --- facets 合并 -----------------------------------------------------------
def test_facets_merge() -> None:
    db = SessionLocal()
    try:
        account = Account(name="probe-acc", site="MY")
        db.add(account)
        db.flush()
        # 两件商品挂在自定义标签「精品」上，一件走平台类目 cid=1001
        for index, (cid, name) in enumerate([("", "精品"), ("", "精品"),
                                             ("1001", "女鞋")]):
            db.add(Product(
                warehouse_key=f"probe-{index}:s1",
                account_id=account.id, detail_id=f"probe-{index}", shop_id="s1",
                title=f"probe {index}", cid=cid, category_name=name,
                payload={}, images=[], detail_images=[], sku_properties=[],
                collected={}, saved_at=1000.0 + index, status="ready",
            ))
        db.flush()

        facets = svc.facets(db)
        cats = facets["categories"]
        by_name = {item["name"]: item for item in cats}
        check("facets 含自定义标签", "精品" in by_name, [c["name"] for c in cats])
        check("自定义标签 count=2",
              by_name.get("精品", {}).get("count") == 2, by_name.get("精品"))
        check("自定义标签 cid 为空",
              by_name.get("精品", {}).get("cid") == "", by_name.get("精品"))
        check("平台类目仍在", by_name.get("女鞋", {}).get("count") == 1,
              by_name.get("女鞋"))
        check("自定义标签无重复行",
              len([c for c in cats if c["name"] == "精品"]) == 1,
              [c["name"] for c in cats])
        # 一个商品都没挂的标签也要出现在面板上
        check("空标签也出现", "待上架" in by_name, [c["name"] for c in cats])
        check("空标签 count=0", by_name.get("待上架", {}).get("count") == 0,
              by_name.get("待上架"))

        # 同名撞车：平台类目名也叫「精品」时只出一条，count 相加
        db.add(Product(
            warehouse_key="probe-dup:s1", account_id=account.id,
            detail_id="probe-dup", shop_id="s1", title="dup", cid="2002",
            category_name="精品", payload={}, images=[], detail_images=[],
            sku_properties=[], collected={}, saved_at=2000.0, status="ready",
        ))
        db.flush()
        cats2 = svc.facets(db)["categories"]
        same = [c for c in cats2 if c["name"] == "精品"]
        check("同名合并成一条", len(same) == 1, same)
        check("同名 count 相加", same and same[0]["count"] == 3, same)
        # 这一条商品的 categoryName 也叫「精品」，按「一个商品只算一个标签」
        # 的既有规则计入标签桶；筛 categoryId 走的仍是库里的真 cid，没丢能力。
        check("同名时计入标签桶(cid 为空)", same and same[0]["cid"] == "", same)

        # 两个真类目同中文名（不同 cid）不能合并：合并了就按 cid 筛不准
        db.add(Product(
            warehouse_key="probe-cid3:s1", account_id=account.id,
            detail_id="probe-cid3", shop_id="s1", title="cid3", cid="3003",
            category_name="女鞋", payload={}, images=[], detail_images=[],
            sku_properties=[], collected={}, saved_at=3000.0, status="ready",
        ))
        db.flush()
        cats3 = svc.facets(db)["categories"]
        shoes = [c for c in cats3 if c["name"] == "女鞋"]
        check("两个真类目同名各自保留", len(shoes) == 2,
              [(c["name"], c["cid"], c["count"]) for c in shoes])
        db.rollback()
    finally:
        db.close()


# --- 货源 offer id ---------------------------------------------------------
def test_source_item_ids() -> None:
    db = SessionLocal()
    try:
        account = Account(name="probe-ids", site="MY")
        other = Account(name="probe-other", site="MY")
        db.add(account)
        db.add(other)
        db.flush()

        def add(key: str, payload: dict, acc: Account) -> None:
            db.add(Product(
                warehouse_key=key, account_id=acc.id,
                detail_id=key, shop_id="s1", title=key, cid="", category_name="",
                payload=payload, images=[], detail_images=[], sku_properties=[],
                collected={}, saved_at=1000.0, status="ready",
            ))

        # 顺序：sourceList / sourceItemId / oriItemId / 非数字 / 另一账号
        add("ids-1", {"sourceList": [{"sourceItemId": "123"}]}, account)
        add("ids-2", {"sourceItemId": "456"}, account)
        add("ids-3", {"oriItemId": "789"}, account)
        add("ids-4", {"sourceItemId": "abc"}, account)
        add("ids-5", {"sourceItemId": "123"}, account)          # 重复应去重
        add("ids-6", {"sourceItemId": "999"}, other)            # 别的账号
        db.flush()

        ids = svc.all_source_item_ids(db, account_id=account.id)
        check("抠出 3 个 id 去重保序", ids == ["123", "456", "789"], ids)
        check("非数字被过滤", "abc" not in ids, ids)
        ids_all = svc.all_source_item_ids(db)
        check("不传 account 看全部", "999" in ids_all, ids_all)
        ids_str = svc.all_source_item_ids(db, account_id=str(account.id))
        check("accountId 传字符串也生效", ids_str == ["123", "456", "789"], ids_str)
        check("不存在的账号 -> 空", svc.all_source_item_ids(db, account_id="nope") == [],
              svc.all_source_item_ids(db, account_id="nope"))
        db.rollback()
    finally:
        db.close()


# --- 路由层 ---------------------------------------------------------------
def test_http() -> None:
    from core.config import get_settings

    token = get_settings().local_secret()
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        base = f"{API_PREFIX}/warehouse"

        # 清空重来：先删之前服务层写进去的两个
        for name in ("精品", "待上架"):
            client.request("DELETE", base + "/custom-categories",
                           json={"name": name}, headers=headers)
        listing = client.get(base + "/custom-categories", headers=headers)
        check("GET 自定义分类 200", listing.status_code == 200, listing.status_code)
        check("GET 结构 {categories: [...]}",
              isinstance(listing.json().get("categories"), list),
              str(listing.json())[:200])
        check("GET 初始为空", listing.json().get("categories") == [],
              str(listing.json())[:200])

        # POST 201
        created = client.post(base + "/custom-categories", json={"name": "爆款"},
                              headers=headers)
        check("POST 201", created.status_code == 201, created.status_code)
        check("POST 返回 categories",
              [c["name"] for c in created.json().get("categories", [])] == ["爆款"],
              str(created.json())[:200])

        # 重复 POST 仍 201、不重复
        again = client.post(base + "/custom-categories", json={"name": "爆款"},
                            headers=headers)
        check("重复 POST 不重复",
              [c["name"] for c in again.json().get("categories", [])] == ["爆款"],
              str(again.json())[:200])

        # 空名 -> 400 分类名不能为空
        empty = client.post(base + "/custom-categories", json={"name": "  "},
                            headers=headers)
        check("空名 400", empty.status_code == 400, empty.status_code)
        check("空名 message",
              empty.json().get("error", {}).get("message") == "分类名不能为空",
              str(empty.json())[:200])

        # 静态路径没被 /{ident} 吃掉：详情路由对 custom-categories 应返回 404 商品不存在
        detail = client.get(base + "/custom-categories-detail-not-exist", headers=headers)
        check("动态路由仍能命中原样路径", detail.status_code == 404, detail.status_code)

        # DELETE 用 body
        client.post(base + "/custom-categories", json={"name": "临时"},
                    headers=headers)
        deleted = client.request("DELETE", base + "/custom-categories",
                                 json={"name": "临时"}, headers=headers)
        check("DELETE body 200", deleted.status_code == 200, deleted.status_code)
        check("DELETE body 生效",
              "临时" not in [c["name"] for c in deleted.json().get("categories", [])],
              str(deleted.json())[:200])

        # DELETE 用 query
        client.post(base + "/custom-categories", json={"name": "临时2"},
                    headers=headers)
        deleted2 = client.request(
            "DELETE", base + "/custom-categories?name=%E4%B8%B4%E6%97%B62",
            headers=headers)
        check("DELETE query 200", deleted2.status_code == 200, deleted2.status_code)
        check("DELETE query 生效",
              "临时2" not in [c["name"] for c in deleted2.json().get("categories", [])],
              str(deleted2.json())[:200])

        # 删不存在：200 幂等
        missing = client.request("DELETE", base + "/custom-categories",
                                 json={"name": "不存在"}, headers=headers)
        check("DELETE 不存在仍 200", missing.status_code == 200, missing.status_code)

        # PATCH 改名
        renamed = client.patch(base + "/custom-categories",
                               json={"name": "超级爆款", "oldName": "爆款"},
                               headers=headers)
        check("PATCH 改名 200", renamed.status_code == 200, renamed.status_code)
        names = [c["name"] for c in renamed.json().get("categories", [])]
        check("PATCH 改名生效", names == ["超级爆款"], names)

        # source-item-ids
        ids_resp = client.get(base + "/source-item-ids", headers=headers)
        check("GET source-item-ids 200", ids_resp.status_code == 200,
              ids_resp.status_code)
        body = ids_resp.json()
        check("source-item-ids 结构",
              isinstance(body.get("ids"), list) and isinstance(body.get("total"), int),
              str(body)[:200])
        check("source-item-ids total == len(ids)",
              body.get("total") == len(body.get("ids") or []), str(body)[:200])
        scoped = client.get(base + "/source-item-ids?accountId=123", headers=headers)
        check("source-item-ids 支持 accountId", scoped.status_code == 200,
              scoped.status_code)

        # 只读模式：写接口被拒（POST /system/settings 是切换开关的那条）
        ro = client.post(f"{API_PREFIX}/system/settings",
                         json={"securityReadOnly": True}, headers=headers)
        check("进入只读模式", ro.status_code == 200, ro.status_code)
        blocked = client.post(base + "/custom-categories", json={"name": "只读测试"},
                              headers=headers)
        check("只读时 POST 被拒", blocked.status_code == 403, blocked.status_code)
        blocked_del = client.request("DELETE", base + "/custom-categories",
                                     json={"name": "超级爆款"}, headers=headers)
        check("只读时 DELETE 被拒", blocked_del.status_code == 403,
              blocked_del.status_code)
        client.post(f"{API_PREFIX}/system/settings",
                    json={"securityReadOnly": False}, headers=headers)


def main() -> int:
    Base.metadata.create_all(bind=engine)
    test_custom_categories()
    test_facets_merge()
    test_source_item_ids()
    test_http()
    failed = [item for item in results if not item["ok"]]
    payload = {"total": len(results), "passed": len(results) - len(failed),
               "failed": len(failed), "results": results}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print("TOTAL %d PASS %d FAIL %d" % (len(results), len(results) - len(failed),
                                        len(failed)))
    print("OUT " + str(OUT))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
