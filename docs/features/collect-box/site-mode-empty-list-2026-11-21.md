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

---

## 七、后续：自动化处理自动认领到默认店铺（0.1.32）

上一节说「要发布必须先到妙手界面手动分配店铺」。负责人随即要求：

> 自动化处理不能默认给他选择一个店铺吗，加到文字处理里面

### 实测确认可行

`claim_to_shop([detailId], [shopId])` 对 site 商品有效：

```
认领前: editModel=site, shopId=''         key=3549599651:site:TIKTOK
claim_to_shop([3549599651], [19974446])   → {'result': 'success', 'code': 'success'}
认领后: editModel=shop, shopId=19974446   key=3549599651:19974446
```

账号 8 绑定的店铺（`shop_list` 实测）：

```
site=(全部): 2 个
  shopId=19974446  site=MY
  shopId=20075695  site=PH
```

### 实现

**服务端** `collect_box_service.claim_site_item(db, account, detail_id, target_shop_id)`

专门处理「把没有店铺的商品第一次分配出去」：

- 不做源店铺校验（它本来就没有店铺）
- 不写 `claimed_at` 账本（那是防「同一件被两人抢到同一店」；
  而这里认领后 `editModel` 变 shop，上游天然幂等）
- 仍然校验目标店铺属于该账号（否则上游报错很难懂）
- 上游说「已在采集箱/已存在」时当幂等成功，不冒泡成失败

**路由** `POST /collect-box/{detailId}/process`

```python
resolved_shop_id = str(payload.shopId or "").strip()
if not resolved_shop_id:
    target_shop = str(payload.targetShopId or "").strip()
    if not target_shop:
        raise errors.validation(
            "这件商品还没分配到店铺。请在处理设置里选择「默认店铺」，"
            "或先去妙手采集箱把它分配到店铺。", field="shopId")
    resolved_shop_id = collect_box_service.claim_site_item(
        db, account, detail_id=detail_id, target_shop_id=target_shop)
```

**Schema** `ProcessRequest`

```python
shopId: str = ""          # 改可空：site 商品传空
targetShopId: str | None = None   # 兜底店铺
```

`shopId` 从 `str` 改成 `str = ""` 是必须的 —— 否则 site 商品连请求都发不出来。

**前端** `AutomationPage` 的 process 调用

```typescript
shopId: target.shopId,
targetShopId: target.shopId ? null : currentShopId || null,
```

`currentShopId` 是顶部店铺选择器的值 —— 用户已经选过「这次操作针对哪个店」，
直接复用它做兜底，不再新增一个设置项。

**前端** `ProductPicker` 商品卡片

site 商品显示「未分配店铺 · TIKTOK」小字，`title` 提示
「处理时会自动认领到顶部选中的店铺」。不提示的话用户会以为已经分好了。

### 验证

```
当前 site 模式商品: 47 条（从 49 减到 47，因为我做了两次真实认领测试）
ProcessRequest(shopId="", targetShopId="19974446")
  → shopId=''  targetShopId='19974446'  OK

ProcessRequest(shopId="19974446")   # 旧客户端
  → shopId='19974446'  targetShopId=None  OK（向后兼容）
```

端到端认领实测：

```
claim_site_item(detail_id=3549614232, target=19974446) → '19974446'
复查: editModel=shop  shopId='19974446'   ✅
```

### 测试

`server/tests/test_claim_site_item.py` **10 条**：

- 正常认领返回 shopId、调用参数正确（`[detailId], [shopId]`）
- 校验目标店铺属于账号、client 用完关闭
- 空/空白 targetShopId 拒绝、非数字 detailId 拒绝
- **参数不合法时不打上游**（认领是不可撤销动作，不能先斩后奏）
- 上游说「已在采集箱」当幂等成功
- 其他上游错误包装成可读的 AppError

### 顺手修掉一个真实 bug

`collect_box_service.py:702` 用了 `_source_link.source_urls_of(...)`，
但文件里只 `import` 了 `carrier_service, product_service, warehouse_service` ——
**`source_link` 从没导入过**，跑到这一行会 `NameError`。

ruff 的 F821 抓到了。已改为 `source_link.source_urls_of(...)` 并补上导入。

这个 bug 是我上一轮（per-item source link）引入的，当时测试没覆盖到
`claim_carrier_batch` 这条路径。
