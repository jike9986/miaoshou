"""保存前校验的自测：每条规则都要能被触发。

运行：python _meas/test_pre_save.py（从 server 目录）
"""
import sys

sys.path.insert(0, ".")

from services.pre_save_check import (  # noqa: E402
    detail_images_only,
    pre_save_issues,
)

OK = 0
FAIL = 0


def check(name: str, issues: list[str], want_hit: bool, keyword: str = "") -> None:
    global OK, FAIL
    hit = any(keyword in issue for issue in issues) if keyword else bool(issues)
    if hit == want_hit:
        OK += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}: issues={issues} 期望命中={want_hit} 关键词={keyword!r}")


# 一份「完全合格」的基线
def baseline():
    original = {
        "cid": "603751",
        "imgUrls": ["https://a/b.jpg"],
        "notes": '<img src="https://a/d1.jpg">',
        "packageLength": 30, "packageWidth": 20, "packageHeight": 10,
        "sizeChart": "https://a/chart.png",
        "skuMap": {"s1": {"originPrice": "17.00", "price": "36.63"}},
        "skuPropertyList": [],
    }
    payload = {
        "cid": "603751",
        "title": "Baju Sukan",
        "imgUrls": ["https://a/b.jpg"],
        "notes": '<img src="https://a/d1.jpg">',
        "packageLength": 30, "packageWidth": 20, "packageHeight": 10,
        "sizeChart": "https://a/chart.png",
        "skuMap": {"s1": {"originPrice": "17.00", "price": "36.63"}},
        "skuPropertyList": [],
    }
    coverage = {"complete": True, "missingFields": []}
    return original, payload, coverage


def run() -> None:
    print("基线：应无问题")
    o, p, c = baseline()
    check("基线通过", pre_save_issues(
        o, p, c, main_limit=9, detail_limit=30,
        package_dimensions={"packageLength": 30, "packageWidth": 20, "packageHeight": 10},
        size_chart_url="https://a/chart.png"), want_hit=False)

    print("规则 1：译文不完整")
    o, p, c = baseline()
    check("漏翻字段被拦", pre_save_issues(
        o, p, {"complete": False, "missingFields": ["title"]}), True, "translation coverage")

    print("规则 2：cid 缺失")
    o, p, c = baseline()
    p2 = dict(p, cid="")
    check("cid 为空被拦", pre_save_issues(o, p2, c), True, "category cid")

    print("规则 3：包裹尺寸不符")
    o, p, c = baseline()
    check("尺寸不符被拦", pre_save_issues(
        o, p, c, package_dimensions={"packageLength": 40, "packageWidth": 20,
                                     "packageHeight": 10}), True, "package dimensions")

    print("规则 4：尺码图缺失")
    o, p, c = baseline()
    p2 = dict(p, sizeChart="")
    check("尺码图缺失被拦", pre_save_issues(
        o, p2, c, size_chart_url="https://a/chart.png"), True, "size chart")

    print("规则 5：主图为空")
    o, p, c = baseline()
    p2 = dict(p, imgUrls=[])
    check("主图为空被拦", pre_save_issues(o, p2, c), True, "main image")

    print("规则 6：详情图超平台硬上限 30")
    o, p, c = baseline()
    many = "".join(f'<img src="https://a/{i}.jpg">' for i in range(31))
    p2 = dict(p, notes=many)
    check("详情图 31 张被拦", pre_save_issues(o, p2, c), True, "platform limit")

    print("规则 7：详情含文字")
    o, p, c = baseline()
    p2 = dict(p, notes='<p>降價促銷</p><img src="https://a/d1.jpg">')
    check("详情含文字被拦", pre_save_issues(o, p2, c), True, "non-image layout")

    print("规则 8：关掉图片处理却改了主图")
    o, p, c = baseline()
    p2 = dict(p, imgUrls=["https://a/other.jpg"])
    check("关掉图片处理改主图被拦", pre_save_issues(
        o, p2, c, process_images_enabled=False), True, "image fields changed")

    print("规则 9：没开定价却改了价格")
    o, p, c = baseline()
    p2 = dict(p, skuMap={"s1": {"originPrice": "17.00", "price": "99.99"}})
    check("偷改价格被拦", pre_save_issues(o, p2, c, pricing_enabled=False),
          True, "price fields changed")

    print("规则 10：开了定价但动了 originPrice")
    o, p, c = baseline()
    p2 = dict(p, skuMap={"s1": {"originPrice": "1.00", "price": "36.63"}})
    check("改 originPrice 被拦", pre_save_issues(o, p2, c, pricing_enabled=True),
          True, "originPrice")

    print("反例：开了定价只改 price 应放行")
    o, p, c = baseline()
    p2 = dict(p, skuMap={"s1": {"originPrice": "17.00", "price": "41.25"}})
    check("只改 price 放行", pre_save_issues(o, p2, c, pricing_enabled=True),
          want_hit=False)

    print("反例：详情图正好 30 张应放行")
    o, p, c = baseline()
    exactly = "".join(f'<img src="https://a/{i}.jpg">' for i in range(30))
    p2 = dict(p, notes=exactly)
    check("详情图 30 张放行", pre_save_issues(o, p2, c, detail_limit=30), want_hit=False)

    print("反例：detail_images_only 生成的 HTML 应通过布局检查")
    o, p, c = baseline()
    urls = ["https://a/d1.jpg", "https://a/d2.jpg"]
    p2 = dict(p, notes=detail_images_only(urls))
    check("纯图片 HTML 放行", pre_save_issues(o, p2, c), want_hit=False)

    print(f"\n结果：{OK} 通过，{FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    run()
