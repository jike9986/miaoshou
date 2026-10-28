"""类目链路 P0 验证：三档兜底 + 必填属性校验。

跑法（从 server 目录）：pwsh -Command "python _meas/probe_category.py"
数据目录指到临时目录，绝不碰真实 app.db。
结果写 _meas/probe_category.json，因为中文在 PowerShell 控制台会乱码。
"""
import json
import os
import sys
import tempfile
import traceback

sys.path.insert(0, os.getcwd())
os.environ["AOHUANG_DATA_DIR"] = tempfile.mkdtemp(prefix="probe_cat_")

from db.session import init_db, session_scope  # noqa: E402

init_db()

from services import category_service as cats  # noqa: E402
from services.pre_save_check import pre_save_issues  # noqa: E402

RESULTS = []
DETAIL = []


def record(name, ok, detail=""):
    RESULTS.append({"case": name, "pass": bool(ok), "detail": detail})
    DETAIL.append(f"{'PASS' if ok else 'FAIL'} {name}"
                  + (f" | {detail}" if detail else ""))


def check_eq(name, got, want):
    ok = got == want
    record(name, ok, "" if ok else f"got={got!r} want={want!r}")


def check_true(name, value, detail=""):
    record(name, bool(value), detail)


# --- 造类目树 ---------------------------------------------------------------
# 末级「运动拖鞋」100 / 末级「凉拖」101 / 非末级「鞋类」1 / disabled 末级「沙滩拖」102
TREE = {
    "cid": "1", "name": "Shoes", "nameChinese": "鞋类", "isLastLevel": "0",
    "children": {
        "a": {"cid": "100", "name": "Sport Slipper", "nameChinese": "运动拖鞋",
              "isLastLevel": "1"},
        "b": {"cid": "101", "name": "Sandal", "nameChinese": "凉拖",
              "isLastLevel": "1"},
        "c": {"cid": "102", "name": "Beach Slipper", "nameChinese": "沙滩拖",
              "isLastLevel": "1", "disabled": "1"},
    },
}


def seed(db):
    resp = {"data": {"cateTree": {"root": TREE}}}
    return cats.sync_from_response(db, resp, site="MY", account_id=1)


# ===========================================================================
# 1) name_index
# ===========================================================================
def case_name_index(db):
    index = cats.name_index(db, 1)
    cids = {cid for values in index.values() for cid in values}
    check_eq("name_index 只收 100/101（102 disabled、1 非末级被跳过）",
             sorted(cids), ["100", "101"])
    # 名字同时收 name 与 name_cn，且 casefold
    check_true("name_index 收中文名「运动拖鞋」", "运动拖鞋" in index, str(sorted(index)))
    check_true("name_index 收英文名（casefold）", "sport slipper" in index,
               str(sorted(index)))
    check_true("name_index 不含 disabled 类目名「沙滩拖」", "沙滩拖" not in index,
               str(sorted(index)))
    check_true("name_index 不含非末级类目名「鞋类」", "鞋类" not in index,
               str(sorted(index)))
    return index


# ===========================================================================
# 2) fallback_by_keyword
# ===========================================================================
def case_fallback(db):
    got = cats.fallback_by_keyword(db, "运动拖鞋 男款", account_id=1)
    check_eq("fallback_by_keyword('运动拖鞋 男款') 唯一命中", got, "100")

    # 命中 2 个（100 与 101 都叫「拖鞋」的语义）→ 必须抛错说明原因
    # 构造方式：临时把 101 的中文名也改成「运动拖鞋」，模拟重名
    from models.category import Category
    row = db.query(Category).filter(Category.cid == "101",
                                    Category.account_id == 1).one()
    row.name_cn = "运动拖鞋"
    db.flush()
    err = ""
    try:
        cats.fallback_by_keyword(db, "运动拖鞋", account_id=1)
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
    check_true("fallback_by_keyword 命中 2 个不唯一时抛错", bool(err), err)
    check_true("不唯一报错里带命中数", "命中 2 个" in err, err)
    row.name_cn = "凉拖"
    db.flush()

    # 关键词表不命中 → 拒绝（保险丝）
    err2 = ""
    try:
        cats.fallback_by_keyword(db, "蓝牙耳机", account_id=1)
    except Exception as exc:  # noqa: BLE001
        err2 = str(exc)
    check_true("fallback_by_keyword 非拖鞋标题直接拒绝", "未命中已确认的本地类目规则" in err2,
               err2)


# ===========================================================================
# 3) resolve_category 三档
# ===========================================================================
class FakeClient:
    """按需返回：唯一 cid / 两个 cid / 直接报错。"""

    def __init__(self, response=None, raises=None):
        self.response = response
        self.raises = raises
        self.calls = []

    def match_category(self, detail_id, shop_ids):
        self.calls.append((detail_id, shop_ids))
        if self.raises is not None:
            raise self.raises
        return self.response


def case_resolve(db):
    # 3.1 info 已有 cid
    out = cats.resolve_category(db, client=None, detail_id=1, shop_id=9,
                                info={"cid": "101", "title": "x"},
                                account_id=1, site="MY")
    check_eq("resolve: info 已有 cid → existing", out["source"], "existing")
    check_eq("resolve: existing 保留原 cid", out["cid"], "101")

    # 3.2 mock AI 返回唯一 cid
    client = FakeClient({"data": {"cid": 100}})
    out = cats.resolve_category(db, client=client, detail_id=2, shop_id=9,
                                info={"cid": "", "title": "随便什么"},
                                account_id=1, site="MY")
    check_eq("resolve: AI 唯一 → source=ai", out["source"], "ai")
    check_eq("resolve: AI 唯一 → cid=100", out["cid"], "100")
    check_eq("resolve: 调了 match_category(detailId, [shopId])",
             client.calls, [(2, [9])])

    # 3.3 mock AI 返回 2 个 cid → 不唯一，落本地库
    client = FakeClient({"data": {"cid": 100, "targetCid": 101}})
    out = cats.resolve_category(db, client=client, detail_id=3, shop_id=9,
                                info={"cid": "", "title": "运动拖鞋 男款 防滑"},
                                account_id=1, site="MY")
    check_eq("resolve: AI 不唯一 → 落本地库 source=library", out["source"], "library")
    check_eq("resolve: 本地库命中「运动拖鞋」→ cid=100", out["cid"], "100")

    # 3.4 mock AI 失败 + 本地库无匹配 → 关键词命中
    client = FakeClient(raises=RuntimeError("上游炸了"))
    # 标题里没有完整末级类目名，只有关键词「防滑拖鞋」→ 本地库名字反查必空
    out = cats.resolve_category(db, client=client, detail_id=4, shop_id=9,
                                info={"cid": "", "title": "防滑拖鞋 家用"},
                                account_id=1, site="MY")
    check_eq("resolve: AI 失败 + 本地库无 → 关键词 source=keyword", out["source"], "keyword")
    check_eq("resolve: 关键词兜底 → cid=100", out["cid"], "100")

    # 3.5 全失败
    client = FakeClient(raises=RuntimeError("上游炸了"))
    out = cats.resolve_category(db, client=client, detail_id=5, shop_id=9,
                                info={"cid": "", "title": "蓝牙耳机"},
                                account_id=1, site="MY")
    check_eq("resolve: 全失败 → cid 为空", out["cid"], "")
    check_eq("resolve: 全失败 → source 为空", out["source"], "")
    check_true("resolve: 全失败 message 含老版提示语",
               "给这件商品手动选好类目后再处理" in (out.get("message") or "")
               and "没选类目时颜色、尺码、重量都不会正常显示" in (out.get("message") or ""),
               out.get("message") or "")


# ===========================================================================
# 4) extract_requirements / validate_requirements
# ===========================================================================
def case_requirements():
    meta = {
        "packageDimensionIsRequired": True,
        "categoryProductAttrList": [
            {"name": "材质", "isMandatory": True},
            {"name": "产地", "isMandatory": False},
        ],
    }
    req = cats.extract_requirements(meta)
    check_eq("extract_requirements: packageDimensionRequired", req["packageDimensionRequired"], True)
    check_eq("extract_requirements: requiredAttributes 只收必填", req["requiredAttributes"], ["材质"])

    # 上游真实形状（外面还包一层 data.categoryMetadata）
    wrapped = {"data": {"categoryMetadata": meta}}
    req2 = cats.extract_requirements(wrapped)
    check_eq("extract_requirements: 剥 data.categoryMetadata 一层", req2, req)

    # 三个必填标记别名都认
    alias = {"categoryMetadata": {
        "categoryConfig": {"packageDimensionIsRequired": "1"},
        "categorySaleAttrList": [{"attrName": "尺码", "required": "1"},
                                 {"attrName": "颜色", "isRequired": True}],
    }}
    req3 = cats.extract_requirements(alias)
    check_eq("extract_requirements: 认 required/isRequired 别名",
             req3["requiredAttributes"], ["尺码", "颜色"])
    check_eq("extract_requirements: 认字符串 \"1\" 的布尔", req3["packageDimensionRequired"], True)

    # validate：缺包裹尺寸
    info_missing_pkg = {"cid": "100", "productAttributes": [{"name": "材质"}]}
    issues = cats.validate_requirements(info_missing_pkg, req)
    check_true("validate_requirements: 缺包裹尺寸 → 有 issue",
               any("类目要求包裹长宽高" in i for i in issues), str(issues))
    check_true("validate_requirements: 填了「材质」→ 不报必填属性",
               not any("必填属性" in i for i in issues), str(issues))

    # validate：包裹尺寸齐 + 缺「材质」
    info_ok = {"cid": "100", "packageLength": 30, "packageWidth": 20,
               "packageHeight": 10, "productAttributes": []}
    issues2 = cats.validate_requirements(info_ok, req)
    check_eq("validate_requirements: 尺寸齐 → 只剩必填属性一条", len(issues2), 1)
    check_true("validate_requirements: 缺「材质」→ issue 含「材质」",
               bool(issues2) and "材质" in issues2[0], str(issues2))

    # 通过路径
    info_pass = {"cid": "100", "packageLength": 30, "packageWidth": 20,
                 "packageHeight": 10,
                 "productAttributes": [{"name": "材质"}]}
    check_eq("validate_requirements: 全部满足 → 空列表",
             cats.validate_requirements(info_pass, req), [])

    # 填在 SKU 属性里也算填了（老版 :169-171）
    info_sku = {"cid": "100", "packageLength": 30, "packageWidth": 20,
                "packageHeight": 10,
                "skuPropertyList": [{"attrName": "材质"}]}
    check_eq("validate_requirements: 属性填在 skuPropertyList 也算",
             cats.validate_requirements(info_sku, req), [])

    # 直接传原始上游响应也认
    check_eq("validate_requirements: 可直接吃原始 categoryMetadata",
             cats.validate_requirements(info_pass, {"data": {"categoryMetadata": meta}}), [])
    return req


# ===========================================================================
# 5) pre_save_issues 规则 11/12
# ===========================================================================
def baseline():
    original = {
        "cid": "100", "imgUrls": ["https://a/b.jpg"],
        "notes": '<img src="https://a/d1.jpg">',
        "packageLength": 30, "packageWidth": 20, "packageHeight": 10,
        "skuMap": {"s1": {"originPrice": "17.00", "price": "36.63"}},
        "skuPropertyList": [],
    }
    payload = dict(original, title="Sport Slipper")
    coverage = {"complete": True, "missingFields": []}
    return original, payload, coverage


def case_pre_save(req):
    o, p, c = baseline()
    # 向后兼容：不传 requirements 时，缺必填属性也不报
    issues = pre_save_issues(o, p, c)
    check_eq("pre_save_issues(requirements=None) → 不报类目要求（向后兼容）", issues, [])

    # 传了 requirements，但商品已满足 → 仍为空
    p_ok = dict(p, productAttributes=[{"name": "材质"}])
    check_eq("pre_save_issues(requirements=req) 且满足 → 空",
             pre_save_issues(o, p_ok, c, requirements=req), [])

    # 缺必填属性 → 规则 12 命中
    issues2 = pre_save_issues(o, p, c, requirements=req)
    check_true("规则 12：缺必填属性被拦",
               any("类目必填属性未填" in i and "材质" in i for i in issues2), str(issues2))

    # 缺包裹尺寸 → 规则 11 命中（把尺寸从 payload 抹掉）
    p_nopkg = dict(p_ok, packageLength=None)
    issues3 = pre_save_issues(o, p_nopkg, c, requirements=req)
    check_true("规则 11：类目要求包裹尺寸但没填全被拦",
               any("类目要求包裹长宽高" in i for i in issues3), str(issues3))

    # 原有 10 条规则没被动：cid 缺失仍被拦
    p_nocid = dict(p_ok, cid="")
    check_true("原有规则 2 未受影响（cid 缺失仍被拦）",
               any("category cid is missing" in i
                   for i in pre_save_issues(o, p_nocid, c, requirements=req)))


# ===========================================================================
def main():
    out = {"cases": RESULTS}
    try:
        with session_scope() as db:
            seeded = seed(db)
            db.flush()
            record("类目树入库成功", seeded["total"] == 4, str(seeded))
            case_name_index(db)
            case_fallback(db)
            case_resolve(db)
            req = case_requirements()
            case_pre_save(req)
    except Exception:
        record("probe 未捕获异常", False, traceback.format_exc())

    ok = sum(1 for item in RESULTS if item["pass"])
    fail = len(RESULTS) - ok
    out = {"pass": ok, "fail": fail, "cases": RESULTS}
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "probe_category.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(out, handle, ensure_ascii=False, indent=1)
    print(f"pass={ok} fail={fail} -> {path}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
