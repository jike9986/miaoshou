"""`listing_rows` 对 site 模式的回归测试。

## 这个 bug 的形状

妙手采集箱有两种 `editModel`：

  `shop` —— 已分配到店铺，行里有 `collectBoxDetailShopList`
  `site` —— 只采集到采集箱、还没分配店铺，没有 shop 列表

旧代码把 `site` 静默丢弃：

```python
if row.get("editModel") not in (None, "shop"):
    continue
```

而 `_scan_upstream` 的 `total` 用**上游原始行数**（不受这个过滤影响）。
于是账号 8 出现：**「采集箱 / 49」但列表空白**。

## 修复后的约定（这些测试钉住它）

1. `site` 模式的行**必须返回**，不能丢
2. `shopId` 用空字符串 `""`，显式表示「未分配店铺」
   —— 不能是 None，否则下游 `str(row["shopId"])` 会变成 "None"
3. `key` 不能与 shop 模式冲突：用 `{detailId}:site:{site}` 而非 `{detailId}:`
4. 未知的 `editModel` 仍然跳过（保守：不认识的结构不能塞给下游）
5. 同一 detailId 在同一 site 下只能出现一次（去重）

运行：
    cd miaoshou/server && python -m pytest tests/test_collect_box_site_mode.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from services.collect_box_service import listing_rows  # noqa: E402


def site_row(detail_id: int, *, site: str | None = None,
             platform: str = "tiktok", title: str = "商品标题") -> dict:
    """构造一条真实的 site 模式行（字段取自线上账号 8 的实际返回）。"""
    return {
        "editModel": "site",
        "collectBoxDetailId": detail_id,
        "commonCollectBoxDetailId": 4156684809,
        "collectBoxDetailShopList": [],
        "shopId": None,
        "site": site,
        "platform": platform,
        "title": title,
        "price": "22.00",
        "stock": 623641,
        "thumbnail": "https://cbu01.alicdn.com/img/ibank/x.jpg",
        "copyType": "msPlugin",
    }


def shop_row(detail_id: int, shop_id: int, *, title: str = "店铺商品") -> dict:
    return {
        "editModel": "shop",
        "collectBoxDetailId": detail_id,
        "collectBoxDetailShopList": [{"shopId": shop_id, "shopName": "测试店"}],
        "title": title,
        "thumbnail": "https://cbu01.alicdn.com/img/ibank/y.jpg",
    }


def wrap(rows: list[dict]) -> dict:
    return {"data": {"detailList": rows}}


# --- site 模式必须返回 -----------------------------------------------------

def test_site_mode_rows_are_returned() -> None:
    """核心回归：site 模式不能被丢弃。

    修复前这里返回 []，导致「采集箱 / 49」但列表空白。
    """
    rows = listing_rows(wrap([site_row(3549614232)]))
    assert len(rows) == 1
    assert rows[0]["editModel"] == "site"
    assert rows[0]["detailId"] == "3549614232"


def test_site_mode_49_rows_all_returned() -> None:
    """账号 8 的真实数量：49 条 site 模式全部要出来。"""
    payload = [site_row(3549614000 + index) for index in range(49)]
    rows = listing_rows(wrap(payload))
    assert len(rows) == 49


def test_site_mode_shop_id_is_empty_string_not_none() -> None:
    """`shopId` 必须是空字符串。

    下游 HTML 属性 / 日志里有 `str(row["shopId"])` 这种用法，
    如果是 None 会得到字符串 "None"，前端会显示奇怪的店名。
    """
    row = listing_rows(wrap([site_row(1)]))[0]
    assert row["shopId"] == ""
    assert row["shopId"] is not None


def test_site_mode_shop_name_marks_unassigned() -> None:
    """店名要明确写「未分配店铺」，让用户知道为什么发布不了。"""
    row = listing_rows(wrap([site_row(1, site="MY")]))[0]
    assert "未分配店铺" in row["shopName"]
    assert "MY" in row["shopName"]


def test_site_mode_derives_site_from_platform_when_absent() -> None:
    """行里 `site` 常常是 None，要退回 platform（线上实测 tiktok）。"""
    row = listing_rows(wrap([site_row(1, site=None, platform="tiktok")]))[0]
    assert row["site"] == "TIKTOK"


def test_site_mode_prefers_explicit_site_over_platform() -> None:
    row = listing_rows(wrap([site_row(1, site="MY", platform="tiktok")]))[0]
    assert row["site"] == "MY"


def test_site_mode_carries_title_and_image() -> None:
    """数据本身是完整的 —— 这正是「应该显示却显示不出来」的原因。"""
    row = listing_rows(wrap([site_row(1, title="镂空凉鞋")]))[0]
    assert row["title"] == "镂空凉鞋"
    assert "alicdn" in row["imageUrl"]


# --- key 唯一性 -------------------------------------------------------------

def test_site_key_does_not_collide_with_shop_key() -> None:
    """site 模式的 key 必须与 shop 模式区分开。

    同一 detailId 如果两边都用 `{detailId}:{shopId}`，
    site 模式会被去重掉（或与 shop 模式混淆）。
    """
    site_key = listing_rows(wrap([site_row(100)]))[0]["key"]
    shop_key = listing_rows(wrap([shop_row(100, 19405197)]))[0]["key"]
    assert site_key != shop_key
    assert ":site:" in site_key


def test_same_detail_same_site_deduped() -> None:
    payload = [site_row(200, site="MY"), site_row(200, site="MY")]
    assert len(listing_rows(wrap(payload))) == 1


def test_same_detail_different_site_kept() -> None:
    """同一件商品可能同时挂在多个站点下，属于不同条目。"""
    payload = [site_row(200, site="MY"), site_row(200, site="PH")]
    assert len(listing_rows(wrap(payload))) == 2


# --- shop 模式不受影响 ------------------------------------------------------

def test_shop_mode_still_works() -> None:
    row = listing_rows(wrap([shop_row(100, 19405197)]))[0]
    assert row["editModel"] == "shop"
    assert row["shopId"] == "19405197"
    assert row["shopName"] == "测试店"


def test_shop_mode_without_shop_list_uses_flat_shop_id() -> None:
    """老结构：没有 shopList，但有平铺的 shopId。"""
    payload = [{
        "editModel": "shop",
        "collectBoxDetailId": 300,
        "shopId": 19977183,
        "title": "平铺结构",
    }]
    row = listing_rows(wrap(payload))[0]
    assert row["shopId"] == "19977183"


def test_shop_mode_multiple_shops_expands() -> None:
    payload = [{
        "editModel": "shop",
        "collectBoxDetailId": 400,
        "collectBoxDetailShopList": [
            {"shopId": 1, "shopName": "A"},
            {"shopId": 2, "shopName": "B"},
        ],
    }]
    assert len(listing_rows(wrap(payload))) == 2


# --- 混合与边界 -------------------------------------------------------------

def test_mixed_modes_all_returned() -> None:
    """真实场景：同一账号里 shop 和 site 混合。"""
    payload = [
        site_row(1, site="MY"),
        shop_row(2, 19405197),
        site_row(3, site="PH"),
        shop_row(4, 19977183),
    ]
    rows = listing_rows(wrap(payload))
    assert len(rows) == 4
    models = sorted(item["editModel"] for item in rows)
    assert models == ["shop", "shop", "site", "site"]


def test_unknown_edit_model_is_skipped() -> None:
    """不认识的模式仍然跳过 —— 不能把未知结构塞给下游。"""
    payload = [{"editModel": "weird", "collectBoxDetailId": 1, "title": "x"}]
    assert listing_rows(wrap(payload)) == []


def test_missing_detail_id_is_skipped() -> None:
    payload = [{"editModel": "site", "title": "没有 ID"}]
    assert listing_rows(wrap(payload)) == []


def test_non_dict_rows_ignored() -> None:
    payload = [site_row(1), "not a dict", 123, None]
    assert len(listing_rows(wrap(payload))) == 1


def test_missing_detail_list_raises() -> None:
    from core import errors

    with pytest.raises(errors.AppError):
        listing_rows({"data": {}})