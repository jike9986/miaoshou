# 采集箱「有 49 个但列表空白」—— site 模式被丢弃

> 2026-11-21 负责人反馈：某个账号采集箱显示「采集箱 / 49」，
> 但列表是空的，提示「当前筛选没有结果」。

---

## 一、根因

妙手采集箱的商品有**两种存在形态**，由 `editModel` 区分：

| editModel | 含义 | 有店铺？ |
|---|---|---|
| `shop` | 已分配到某个店铺的草稿箱 | 有 `collectBoxDetailShopList` |
| `site` | 只采集到采集箱、**还没分配店铺** | 无，`shopId` 是 `None` |

`listing_rows()`（`server/services/collect_box_service.py`）只保留了 `shop`：

```python
if row.get("editModel") not in (None, "shop"):
    continue          # ← site 模式整行丢弃
```

而 `total` 用的是**上游原始行数**（`raw_count`，不受这个过滤影响）：

```python
total = raw_count if count_all else matched
```

于是产生错位：**`total=49`（原始行数）但 `items=0`（全被过滤）**。

### 线上实测确认

账号 8 的采集箱：

```
上游返回 49 行
editModel 分布: {'site': 49}
有店铺绑定: 0   无店铺绑定: 49
```

**49 条全是 site 模式，一条都显示不出来。**

---

## 二、site 模式的数据其实是完整的

这是关键判断依据 —— 不是「数据缺失」，只是「缺店铺归属」：

```
collectBoxDetailId         = 3549614232      ✅
commonCollectBoxDetailId   = 4156684809      ✅
title                      = 单鞋女2025秋季...
thumbnail                  = https://cbu01.alicdn.com/img/ibank/...
price                      = 22.00           ✅
stock                      = 623641          ✅
platform                   = tiktok
collectBoxDetailShopList   = []              ← 唯一缺的
```

所以这些商品**应该显示出来**，用户至少能看、能选、能删。

### 还实测了它能不能走后续流程

`site_detail(detailId, site)` 是 site 模式的详情接口（对应 `shop_detail`）：

```
site=MY    OK   返回 5 字段（含 siteCollectItemInfo：标题/图片/SKU 全有）
site=PH    OK
site=SG/TH/ID/VN   FAIL  「当前站点没有绑定的店」
site=TIKTOK        FAIL  「站点【TIKTOK】不支持」
site=(空)          FAIL  「site 必填」
```

`claimToShopIds` 返回空 —— 说明这个账号没有可认领的店铺，
**要发布必须先到妙手界面把商品分配到店铺**。

---

## 三、修复

`listing_rows()` 现在返回 site 模式的行：

```python
if model == "site":
    site = first_text(row.get("site"), row.get("siteCode"),
                      row.get("platform")).upper()
    key = f"{detail_id}:site:{site}"
    products.append({
        "key": key,
        "detailId": str(detail_id),
        "shopId": "",                                 # 空串 = 未分配
        "shopName": f"未分配店铺（{site}）",
        "site": site,
        "title": _row_title(row),
        "imageUrl": _row_image(row),
        "editModel": "site",
        ...
    })
```

### 三个刻意的设计选择

**① `shopId` 用空字符串 `""`，不是 `None`**

下游有 `str(row["shopId"])` 这类用法，`None` 会变成字符串 `"None"`。
空串语义明确，且 `if shop_id:` 判空天然成立。

**② `key` 用 `{detailId}:site:{site}`，不用 `{detailId}:{shopId}`**

shop 模式的 key 是 `product_key(detail_id, shop_id)`。
如果 site 模式也走它，`shopId=""` 会让 key 变成 `123:` ——
既容易和真实店铺冲突，也看不出这是 site 模式。

**③ 未知 `editModel` 仍然跳过**

只放行明确认识的 `shop` 和 `site`。不认识的模式不能塞给下游，
那会把「列表空白」变成「下游崩」。

### 顺便修好的 `site` 字段

行里的 `site` 常常是 `None`（线上 49 条全是 `None`），
用 `siteCode` → `platform` 依次兜底，所以最终显示为 `TIKTOK`。

---

## 四、验证

### 修复前后对比（线上账号）

| 账号 | 上游行数 | 修复前 listing_rows | 修复后 |
|---|---|---|---|
| 8 | 49 | **0** | **49** |
| 2 | 6 | **0** | **6** |
| 7 | 23 | 23（全 shop） | 23 |
| 12 | 74 | 74（全 shop） | 74 |

### 端到端（走 `list_items`，即 GET /collect-box 的服务层）

```
账号 8: total=49  items=20（第一页）  hasMore=True
   key=3549614232:site:TIKTOK
     shopId=''  shopName='未分配店铺（TIKTOK）'
     title='单鞋女2025秋季新款镂空透气...'
     image='https://cbu01.alicdn.com/img/ibank/...'
```

### 回归测试

`server/tests/test_collect_box_site_mode.py` **18 条**，钉住：

- site 模式必须返回（含 49 条的真实数量）
- `shopId` 必须是空串而非 `None`
- `shopName` 必须写明「未分配店铺」
- `site` 从 `platform` 兜底、显式 `site` 优先
- key 不与 shop 模式冲突
- 同 detailId 同 site 去重、不同 site 保留
- shop 模式完全不受影响（含老结构 `shopId` 平铺、多店铺展开）
- 未知 `editModel` 跳过、缺 `detailId` 跳过、非 dict 忽略
- `detailList` 缺失仍抛 upstream 错误

---

## 五、用户接下来要做什么

列表能看到了，但 **site 模式商品不能直接发布** —— 没有店铺归属。

要发布必须先到**妙手界面**把商品分配到店铺：

```
妙手 → 采集箱 → 勾选商品 → 「认领到店铺」/「分配到店铺」
分配后 editModel 变成 shop，我们这边就能正常走发布流程
```

**我们这边不做自动分配**：`claimToShopIds` 对账号 8 返回空，
说明没有可认领的店铺，自动认领会失败。这需要用户在妙手侧决定
「这批商品要发到哪个店」，不是软件能猜的。

---

## 六、代码规范（已写入 rules.md）

`listing_rows()` 里任何新的过滤都要先回答一个问题：

> **`total` 用原始行数还是过滤后行数？**

两者不一致，就会出现「页面上有数字、列表却空白」这种极难自查的 bug。
本次就是 `total=raw_count` 配 `listing_rows` 三重过滤（editModel /
shopList / int 转换）导致的。
