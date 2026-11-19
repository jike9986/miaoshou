# 定价与仓库权限 · 2026-11-19 修复记录

> 负责人当日提出的三个问题一次修完，全部已上线验证。

## 1. 菲律宾价格偏低（P0，发出去就是亏本卖）

### 现象

负责人截图对比同一个模板（菲律宾，利润 15%）：

| | 我们系统 | 妙手官方 |
|---|---|---|
| 拿货价 35 CNY / 0.6kg | **48.93 CNY** | 92.65 CNY |
| 折算本币 | **466.89 PHP** | 883.98 PHP |

两对数字比值都是 9.54 —— 妙手用的真实 CNY→PHP 汇率，我们没用。

### 根因（两层）

**第一层：汇率币种写死 MYR**

`price_template.resolve_rate_with_source()` 调
`exchange_rate.cached_rate()` / `live_rate()` 时**不传 currency**，
而这两个函数签名是 `currency: str = DEFAULT_CURRENCY`，
`DEFAULT_CURRENCY = "MYR"`。

于是马来店恰好正确（MYR 就是默认值），菲律宾/泰国/越南/新加坡
**全部拿 MYR 汇率去算本币报价**。

实测（35 CNY / 0.6kg）：

| 站点 | 正确值 | 旧代码算出 | 倍数 |
|---|---|---|---|
| 菲律宾 | 796.54 PHP | 379.30 PHP | **0.476×** |

**第二层：兜底汇率也是写死的单一值**

`FALLBACK_EXCHANGE_RATE = Decimal("1.6393")` 是常量，
网络不通时所有站点都用它。

### 修法

`resolve_rate_with_source(product, override, *, allow_live, site, template)`：

- 新增 `site` / `template` 参数，内部 `_resolve_currency()` 定币种
  （优先级：`template.freightTable.currency` > 站点映射 > MYR）
- 调 `cached_rate(currency=...)` / `live_rate(currency=...)` 传币种
- 兜底改用 `exchange_rate.fallback_rate(currency)`（按币种查表）
- 来源文案带上币种：`实时汇率（open.er-api）· PHP`

`apply_template()` 和 `preview_price()` 两个内部调用点都传 `template=template`。

### 验证

生产环境实测（真实模板）：

```
MY:      40.33 MYR  rate=1.635901
PH:     796.47 PHP  rate=0.1048     <- 妙手官方 796.54，差 0.07（四舍五入）
TH:     349.81 THB  rate=0.197298
VN:  249159.97 VND  rate=0.000259
SG:      22.12 SGD  rate=5.182744
```

菲律宾的七项费用分解与妙手截图逐项对上：
采购价 333.97 / 妙手 334.02，税前 525.97 / 526.04，
利润 119.47 / 119.48。

### 注意：汇率方向

`exchange_rate` 返回值的含义是「**1 单位本币 = N CNY**」
（PHP = 0.1048），而 `display_price` 内部一律 `除以 rate` 做 CNY→本币换算，
两者方向**本来就是一致的**，不需要倒数转换。

马来站之所以长期看起来正常，是因为 1 CNY ≈ 1.64 MYR、
1 MYR ≈ 0.61 CNY，两个数字接近，掩盖了币种错误。

---

## 2. 「SKU 图数量」实际是颜色数量（两个 bug 让它形同虚设）

### 澄清

负责人问「每个 SKU 翻译几张图是什么意思，sku 哪有图片啊」。

实测确认：**只有颜色值会挂图，尺码值的 `imgUrl` 恒为空串。**

```python
# product 426 真实结构
Warna（颜色）2 个值  -> 每个都有 imgUrl
Saiz（尺码） 6 个值  -> imgUrl 全是 ''
sku_image_sources 总数 = 2
```

所以「SKU 图数量 = N」的真实效果就是「保留前 N 个颜色」。
`apply_image_limits()` 的文档本来就写着「SKU 图数量 = 颜色数」，
**功能早已实现，但两个 bug 让它对一半商品无效**。

### bug 1：attrId 写死 `"100000"`

```python
if str(prop.get("attrId") or "") != "100000":
    continue    # 直接跳过，颜色截断不执行
```

生产库实测各商品的 attrId：

| 商品 | 颜色属性 attrId | 是否被截断 |
|---|---|---|
| 426 | `100000` | ✅ |
| 29 | `None` | ❌ |
| 4 | `None` | ❌ |
| 97 | `None` | ❌ |
| 467 | `100000`（尺码是 `''`） | ✅ |

**一半商品的颜色属性 attrId 是 `None` 或空串。**

修法：三级判定 `_is_color_property()`，任一命中即认颜色维度：

1. `attrId == "100000"`
2. 属性名命中 `Warna / 颜色 / Color / Colour / màu / สี / cor …`
3. **该属性的值里带图**（只有颜色值挂图，最可靠）

### bug 2：skuMap 的 key 用错了标识

```python
# 旧代码：拿 attrValueId 去比对 skuMap 的 key
kept_ids = {str(v.get("attrValueId") or "") for v in kept}
```

但 skuMap 的真实格式是**属性值文本**：

```
attrValueId='2d64a58af5'  attrValue='350 Black and Gray'
skuMap key = ';350 Black and Gray;35;'
```

拿 `attrValueId` 永远匹配不上，于是**颜色值被删了、SKU 全留着** ——
留下的是没有属性定义的孤儿 SKU，发布时必然报错。

修法：同时用属性值文本和 attrValueId 比对，任一命中即删。

### 修完后的实际效果

用真实商品数据验证（本地跑 `apply_image_limits(sku_limit=9)`）：

| 商品 | 颜色 | SKU |
|---|---|---|
| 59 | 29 → 9 | 232 → 72 |
| 67 | 24 → 9 | 72 → 27 |
| 54 | 12 → 9 | 72 → 54 |
| 48 | 10 → 9 | 120 → 108 |

### UI 改名

`ProcessSettings.tsx`：标签「SKU 图数量」→「**颜色数量**」，后缀「张」→「个」。
字段名保留 `skuImageCount` 以兼容已存设置和旧客户端。

---

## 3. 三个受限分组（只给老师和管理员）

### 负责人口径（2026-11-19）

> 「成品仓库的这三个分组里面的鞋不能随便给用户上都是精品，其他的可以批量上，
> 学员账号都看不到这三个分组也上不了这三个分组的鞋子，
> 新建定时任务里面也不能选择这些鞋」

受限组：**自营海外仓(id=1) / 海外仓定制款(id=2) / 精品(id=3)**
，共 18 件成品。

### 为什么这三组要关起来

这三组走**合作厂家**，不走采购，所以拿货价是**标高的**（不是真实成本）。
售价按正常利润算下来反而低于标高价。

生产库实测：**这三组 18 件 100% 倒挂**（售价 < 拿货价）。
这不是 bug，是正常业务结构 —— 但让学员按这个价发出去，
看起来就是卖一件亏一件，且合作厂家的货不能走普通学员渠道。

### 实现

新增 `services/group_access.py`，判定集中一处：

| 函数 | 用途 |
|---|---|
| `restricted_group_ids(db)` | 受限组 id 集合（按名字匹配 + id 兜底 1/2/3） |
| `may_access_restricted(operator)` | 老师/管理员 true，学员与未登录 false |
| `visible_group_ids(db, operator)` | 老师返回 None（不限）；学员返回受限 id 集合 |
| `guard_group` / `guard_products` | 写入/发布前闸门，403 + 说明是哪个组 |

三个拦截点：

1. **列表查询**（`warehouse_service.list_products`）
   SQL 层 `Product.group_id.notin_(受限ids)` 过滤 ——
   不只是筛结果，分页总数和分组计数（facets）都跟着变，
   否则会出现「显示 20 件、总数 38 件」对不上。

2. **发布任务**（`publish_job_service.create`）
   学员发布含受限组商品 → 403，点名是哪几个组。

3. **认领入库**（`claim_job_service._validate_member_items`）
   学员导入采集箱 → 403。

4. **定时任务**（`schedule_service.create` / `update`）
   `_guard_restricted_groups()`，创建和 PATCH 两条路径都拦
   （否则「先建空任务、再 PATCH 塞受限商品」就绕过去了）。

### 验证

生产环境实测：

| 角色 | may_access | 可见商品数 |
|---|---|---|
| 学员 lee | False | **175 件** |
| 老师 teacher | True | **194 件** |

差 19 件 —— 三个受限组的 18 件 + 1 件测试数据，
`products` 列表与 `total` 数值一致。

---

## 4. 颜色超限商品清理（一次性）

### 清理前

全库 `status=ready` 共 238 件，排除受限组后按「颜色数 > 9」扫描：

| 商品 | 分组 | 颜色 | SKU |
|---|---|---|---|
| 59 | 运动球鞋 | 29 | 232 |
| 67 | 运动球鞋 | 24 | 72 |
| 54 | 运动鞋 | 12 | 72 |
| 48 | 运动鞋 | 10 | 120 |

受限组 18 件全部在 9 色以内，无需处理。

### 清理后

| 商品 | 颜色 | SKU |
|---|---|---|
| 48 | 10 → 9 | 120 → 108 |
| 54 | 12 → 9 | 72 → 54 |
| 59 | 29 → 9 | 232 → 72 |
| 67 | 24 → 9 | 72 → 27 |

全库复扫：**非受限组颜色 > 9 的商品 = 无**。孤儿 SKU = 0。

### 脚本

`server/scripts/trim_extra_colors.py`，默认 dry-run，`--apply` 才写库。

**踩过的坑**：第一版直接 `payload = product.payload` 后原地修改。
`apply_image_limits` 是原地改入参的，SQLAlchemy 看到的是同一个对象引用，
认为「这个字段没被重新赋值」，commit 时不写库 ——
**脚本报告 4/4 成功，但库里数据一点没变**。
必须 `deepcopy` 后重新赋值。

---

## 相关文件

| 文件 | 改动 |
|---|---|
| `server/services/price_template.py` | 汇率按币种取（`_resolve_currency` + `site`/`template` 参数） |
| `server/services/warehouse_service.py` | `_is_color_property` 三级判定、skuMap key 文本比对、列表受限组过滤 |
| `server/services/group_access.py` | 新增，受限分组判定与闸门 |
| `server/services/publish_job_service.py` | 发布前受限组闸门 |
| `server/services/claim_job_service.py` | 认领前受限组闸门 |
| `server/services/schedule_service.py` | `_guard_restricted_groups`，create + update 双路径 |
| `server/api/warehouse.py` | 列表传 `operator` |
| `server/schemas/requests.py` | `skuImageCount` 注释纠正为颜色数量 |
| `web/src/features/automation/ProcessSettings.tsx` | 标签改「颜色数量」 |
| `server/scripts/trim_extra_colors.py` | 新增，一次性清理脚本 |
| `server/tests/test_price_color_groups.py` | 新增，25 条 |