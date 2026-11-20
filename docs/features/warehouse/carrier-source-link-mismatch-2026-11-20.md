# 载体 1688 采购链接错配（2026-11-20）

> 负责人：「从成品仓库导入采集箱还是发布，不是先弄一个载体吗，然后再覆盖数据，
> 有个问题就是这个载体是有对应的 1688 采购的链接的，而我覆盖的产品是另一个
> 1688 链接，然后店铺出单采购是最开始载体的链接而不是成品仓库的 1688 链接，
> 导致我需要去重新复制链接重新弄。」

**诊断结论：不是偶发 bug，是设计缺陷 —— 采购链接从采集那一刻起就丢了，
后续任何环节都没有把它带下来。**

---

## 一、载体机制本来在做什么

`carrier_service.py:1-10` 的注释：

```
detailId 只在账号内唯一，所以跨店铺发布不能引用原商品。流程：
  1. 从链接池取一个 1688 链接，采集进公共采集箱（fetch_item）
  2. 认领进目标店铺的采集箱，一次要 N 份（claimed + serialNumber）
  3. 用成品 payload 覆盖每个载体（save_shop）
```

第 3 步覆盖 **标题 / 图片 / SKU / 类目 / 描述 / 价格** —— 唯独**没有覆盖
1688 货源链接**，因为它根本不在 `payload` 里。

载体只是一个「空壳」，它自己的 1688 链接来自
`take_link()`（`carrier_service.py:320`）：

```python
def take_link(db):
    """取最该用的那条：失败少的优先，其次最久没用的。"""
    usable.sort(key=lambda item: (int(item.get("failed") or 0),
                                  item.get("usedAt") or 0))
```

**它完全不知道这件商品原本对应哪个 1688 链接。** 池子里哪条最闲就取哪条。

所以必然错配：

```
成品 A（真实货源 = 1688/offer/111）
  ↓ 要发到店 2
take_link() → 池子里最闲的是 1688/offer/999
  ↓ 采集 999 → 生成载体
载体（货源 = 999）
  ↓ 用成品 A 覆盖 999
封面/标题/价格/SKU 全是 A，但 pull 链接还是 999   ← 错配点
  ↓ 出单
采购员照着 999 去采购 —— 买错货
```

---

## 二、根因：payload 里从来没有货源链接

实测生产库 **全库 532 件商品（含已归档/已发布），0 件**有任何货源字段：

```
商品总数: 532
有货源字段: 0
无货源字段: 532
出现的字段名: []
```

`payload` 的 36 个顶层字段里没有一个跟 1688 有关：

```
brandId  brandName  categoryName  cid  colorMap  colorPropName
deliveryOptionSetType  detailId  editModel  hasTranslate  imgUrls
isCodOpen  keyProductFeatures  mainImgPlatformVideoId  mainImgVideoUrl
manufacturerIds  notes  oriTitle  packageHeight  packageLength
packageWidth  productAttributes  productCertifications
responsiblePersonIds  shopId  site  sizeChart  sizeChartType
sizeMap  sizePropName  skuMap  skuPropertyList  title  warrantyPolicy
weight  weightBeforeCap
```

**代码自己的注释已经承认了这一点**（`warehouse_service.py:4156-4159`）：

> 「成品 payload 只留 oriTitle 之类的展示字段，**原始 1688 链接早就没了**；
> 重建载体时要拿 offer id 去 1688 重新抓货，否则就得整批重新采集一遍。」

而 `all_source_item_ids()` 兜底去抠的 `sourceItemId` / `oriItemId` / `sourceList`
三个字段 —— **实测全部为空**，所以这个兜底也是空的。

### 妙手侧的同样没有

直接调妙手 `get_shop_collect_item_info` 看真实返回：

```
商品 #390  顶层字段: ['claimToShopIds', 'editModel', 'isSupportMultiWarehouse',
                     'ossMd5', 'shopCollectItemInfo']
货源相关字段 15 个 —— 全部是 originPrice（采购价数字 11.5），没有链接
```

**只有价格，没有链接。** 妙手也没给你留货源链接。

---

## 三、为什么当初会这样设计

链路是这样丢的：

```
1688 商品页（链接在这里）
  ↓ 采集（fetch_item）
公共采集箱（sourceList 在这一层，之后就不带了）
  ↓ 认领到店铺（claim_to_shop）
店铺采集箱（只剩标题/图/价/SKU）
  ↓ 入成品仓库（save_from_collect_box）
Product.payload（36 个字段，**没有一个存链接**）  ← 在这里彻底丢掉
```

采集箱那层其实有 `sourceList`，`sync_service.harvest_from_common_box()`
也确实会去回收它 —— 但那是**回收进链接池供载体轮换**，不是「存到这件商品上」。

两件事被混在一起了：

| 需求 | 现状 |
|---|---|
| 载体生成需要一条能采集的 1688 链接 | 链接池轮换 ✅ 满足 |
| 这件商品出单后该采购哪个链接 | **没有记录** ❌ |

---

## 四、影响面

| 影响 | 严重度 |
|---|---|
| 店铺出单后采购员照着错误链接买货 | **高 —— 直接买错货** |
| 需要人工逐个重新复制原始链接 | **高 —— 待上架 178 件，无法批量修** |
| 入库越久越难找回（采集箱已删） | 高 |
| 多店铺分发时错配概率随件数上升 | 高 |

实测：`shop_detail` 对多件商品直接报
**「该产品在采集箱中已被删除」** —— 连载体快照都读不到，追查无路。

---

## 五、修复方向（三个层次，按必要性排序）

### 层 1：入仓时必须存下货源链接（治本）

`save_from_collect_box` 那一步，把采集箱行里的 `sourceList` 里的
`sourceItemId` + `sourceUrl` 写进 `payload`：

```python
payload["sourceItemId"] = "..."          # 1688 offer id
payload["sourceUrl"] = "https://detail.1688.com/offer/....html"
payload["sourceList"] = [...]            # 保留原始结构
```

代价：payload 多 2~3 个字段，无上游请求。
效果：**以后所有新入仓的商品都能追溯到正确货源。**

### 层 2：载体生成时按商品的货源链接取，而不是池子轮换

`collect_carriers` 加参数：

```python
def collect_carriers(db, client, *, count, preferred_url=None, ...):
    link = preferred_url or take_link(db)
```

`collect_and_fill_carriers` / `fill` 流程把每件商品自己的
`payload["sourceUrl"]` 传进去 —— 载体天生就带对链接，覆盖之后依然一致。

代价：同一条链接连续用多了可能被上游限流（需保留失败回退到池子）。
效果：**覆盖之后载体链接 = 商品真实货源，出单采购不会错。**

### 层 3：存量 178 件（ready 且未删除）怎么补救

老实说：**大部分已经救不回来了**，因为：

- `payload` 没存（已确认 0/532）
- 妙手接口不返回链接（已确认只有 originPrice）
- 采集箱原件大部分已删（实测报「已被删除」）

唯一可试的路子：

1. 从 `products.detail_id` 反查妙手**店铺商品**（不是采集箱），
   店铺商品页有时会带 `sourceUrl` —— 需实测验证
2. 按 `oriTitle` + 主图哈希去 1688 反向搜索匹配 —— 不可靠，误配风险高于收益
3. **接受现状，靠人工补**：界面加一个「货源链接」输入框，允许手工填/改，
   填了以后就存进 `payload.sourceUrl`，载体生成优先用它

> 建议做 3 —— 至少让负责人能一次性把在用的商品链接补对，
> 而不是每次发布都要重新复制粘贴。

---

## 六、当前状态

**还没改代码。** 这是设计缺陷，涉及入仓、载体生成、界面三处，
需要先确认方案再动手：

1. 层 1（入仓存链接）—— 无风险，建议立刻做
2. 层 2（载体优先用商品自己的链接）—— 需要处理上游限流回退
3. 层 3（存量补录）—— 需确认走「手工填」还是先试「店铺商品反查」

等你确认从哪一层开始。
---

# 第二部分：修复方案（已实施，2026-11-20）

负责人补充了两个决定性约束，推翻了「重新采集」这条路：

1. **载体是「生成一个然后一直复制」**，不是每次重新采集；
2. **一个妙手账号每天只能采集 10 个产品** —— 一条 50 件的批次重新采集就直接废了。

所以方向确定为 **「覆盖链接」而不是「重新采集」**。

---

## 一、实测三条路径的结论

我把妙手所有相关接口都试了一遍：

| 接口 | 能否改货源链接 | 实测证据 |
|---|---|---|
| **公共采集箱** `edit_common_collect_box_detail` | ✅ **能** | 写入 `826406158136` 后回读一致 |
| 店铺采集箱 `save_shop_collect_item_info` | ❌ 不能 | 返回 `success` 但回读是 `null`（静默忽略） |
| `get_shop_collect_item_info` | — | 返回里根本没有 `sourceList` 字段 |

公共箱详情返回的结构里，`sourceList` 就藏在 **`editCommonCollectBoxDetail`**（编辑用结构体）里：

```json
"editCommonCollectBoxDetail": {
  "commonCollectBoxDetailId": 4148378072,
  "sourceList": [{
    "remark": null, "source": "1688", "sourceSite": "",
    "sourceItemId": "717267894593",
    "sourceItemUrl": "http://detail.1688.com/offer/717267894593.html"
  }],
  "sourceMataInfo": {"source": "1688", "oriItemNum": "717267894593", "price": "22.0"},
  ...
}
```

**关键点：这条路径不消耗每天 10 个采集配额** —— 它走的是 edit，不是 `fetch_item`。

### 踩过的两个坑

**坑 1：`commonCollectBoxDetailId` 必须放顶层**

```
放进 editCommonCollectBoxDetail 里 → 「commonCollectBoxDetailId 不能为空」
放顶层                             → 通过
```

**坑 2：`stock` 必须夹到 99999 以内**

```
「editCommonCollectBoxDetail.stock 不能大于 99999」
```

而株洲鞋类的原始库存动辄 **623927** —— 直接整份回写必被拒。所以 `override_common_box_source()`
里先夹住 stock 再写。

---

## 二、实现的代码

### 2.1 `services/source_link.py`（新增）

货源链接的全部逻辑集中在这里，单一出处：

| 函数 | 作用 |
|---|---|
| `extract_offer_id()` | 从裸 id / 桌面链接 / 移动端链接 / 分享链接里抠 offer id |
| `normalize_source_url()` | 统一归一到 `https://detail.1688.com/offer/<id>.html` |
| `capture_from_box_row()` | 从采集箱行里抽货源（`source` 大小写不统一也认） |
| `attach_source()` | 写进 payload，**只补缺不覆盖**（用户手改的更可信） |
| `override_common_box_source()` | **覆盖公共箱的 sourceList**（夹 stock + 回读校验） |
| `check_payload_source()` | 有效性检查（本地格式 / 妙手反查两级） |

### 2.2 载体重建改造（`carrier_service.py`）

`collect_carriers()` 新增 `preferred_urls` 参数：

```python
# 先消耗商品自带的货源，用完了才轮到池子轮换
if common_id is None and preferred_index < len(preferred):
    url = preferred[preferred_index]
    wanted_source = url          # 这条载体该对齐的货源
```

在**复制之前**插入覆盖步骤：

```python
if wanted_source and common_id:
    override = source_link.override_common_box_source(client, common_id, wanted_source)
    # 改不上不阻断复制：至少标题/图/价/SKU 是对的，
    # 链接留给界面提示人工确认
```

顺序很重要：**先在公共箱改对，再复制到店铺** —— 反过来无效（店铺接口改不了）。

`collect_and_fill_carriers()` 从 payload 里取货源传下去：

```python
_preferred = source_link.source_urls_of(payload_list)
```

### 2.3 入仓时保存货源（`warehouse_service.py`）

在 `save_from_collect_box` 的 `shop_detail` 之后抓一次（那时还可能抓到），
写进 `product.payload`。原来只把链接收进**池子**，没存到**商品身上** —— 这就是错配的根源。

### 2.4 API（`api/warehouse.py`）

| 端点 | 作用 |
|---|---|
| `GET /warehouse/source-links` | 批量扫状态（`live=1` 逐件反查，默认只做本地检查） |
| `POST /warehouse/{ident}/source-link` | 手工设置/修改链接（老师与管理员） |

学员 403 —— 货源链接是采购侧信息，与受限分组同口径。

### 2.5 前端（`SourceLinkCard.tsx`）

成品详情页最下面的 ⑧ 卡片：

- 显示链接 / offer id / 货源标题
- **老师与管理员可改**（学员只读）
- 「检查有效性」按钮
- 状态徽章：已记录 / 已确认存在 / 未记录货源 / 链接格式不对 / 无法确认

---

## 三、有效性检查：为什么不能用 HTTP 判死活

负责人要求的「读取这些链接是否还有效」，我实测踩了个坑：

**从服务器侧请求 1688 商品页，存在与不存在返回的是完全相同的字节。**

```
offer 717267894593（真实存在）: HTTP 200, len 2739
offer 999999999999（乱造）    : HTTP 200, len 2739
```

两者都是 `_____tmd_____` 反爬跳转壳页（阿里的滑块验证）。
移动端 `m.1688.com` 同理（2039 字节壳页）。
**所以 HTTP 完全无法区分死活 —— 拿它当判据会全部误判成「有效」。**

而且妙手开放平台自己就写着警告（负责人截图里那条）：

> **高危：不存在的链接也会 success 并采到无关的真实商品。采集前务必自行校验链接有效性。**

也就是说 `fetch_item` 也不能当校验器 —— 它是坏尺子。

### 现在用的方案：妙手公共箱反查

`common_box_list(source_item_keyword=<offerId>)` 能按货源 id 搜索，不消耗采集配额：

| offer id | 搜索结果 |
|---|---|
| `717267894593`（真实存在） | 搜到 1 行，标题正确 → `verified` |
| `999999999999`（乱造） | 0 行 → `unknown` |

**为什么不是 `dead`**：搜不到**不等于**失效，可能只是这个账号没采过它。
判成 dead 会让老师误删好链接，所以一律报 `unknown`（界面显示「无法确认」）。

---

## 四、生产实测

```
公共箱商品 cid=4148378072，当前货源 717267894593

=== 同一链接再写一次 ===
  changed=False          ← 不浪费上游配额

=== 换成 826406158136 ===
  changed=True，after=http://detail.1688.com/offer/826406158136.html   ← 回读确认

=== 还原 ===
  changed=True，after=http://detail.1688.com/offer/717267894593.html   ← 完整可逆

结论：覆盖生效且可还原
```

---

## 五、测试

`tests/test_source_link.py` **36 条全通过**：

| 覆盖点 | 条数 |
|---|---|
| offer id 抽取（11 种写法） | 11 |
| 归一化 | 2 |
| 采集箱行抓取（含大小写变体、非 1688、缺失） | 4 |
| `attach_source` 不覆盖手改值 | 2 |
| 公共箱 sourceList 构造 | 2 |
| **覆盖：改成功 / 夹 stock / 跳过同值 / 识别静默忽略 / 拒绝坏参数** | 5 |
| 有效性检查（缺失/格式/本地/反查/查不到判 unknown/上游报错） | 10 |

其中最关键的一条：

```python
def test_override_detects_silently_ignored_write():
    """店铺接口会静默忽略 sourceList —— 所以必须回读校验。"""
    client = FakeBoxClient(ignore_edit=True)
    result = override_common_box_source(client, 4148378072, VALID)
    assert result["ok"] is False
    assert "回读" in result["error"]
```

---

## 六、还剩什么

- ✅ 新入仓商品会存下货源链接
- ✅ 载体生成用商品自己的链接（公共箱覆盖，不耗采集配额）
- ✅ 界面可见可改可校验
- ⬜ **存量 178 件**（ready 且未删除）：payload 里 0 条货源链接

  > 2026-11-20 修正：先前文档写「532 件」是错的。532 是**含已归档**的总数
  > （archived 285 + ready 180 + published 67）。真正待上架的是 **178 件**
  > （ready 且未删除）。负责人质疑「不是才一百多个成品吗」是对的。

  这 178 件 payload 里没有、妙手不返回、采集箱多数已删 → 只能靠界面手工补录
  （老师可改）。补一件生效一件。

  实测抽样 8 件，本地检查全部返回 `missing`（未记录货源）——
  **有效性检查对存量商品没有可检的东西，必须先补录**。
