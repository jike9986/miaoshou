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

实测生产库 **532 件商品，0 件**有任何货源字段：

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
| 需要人工逐个重新复制原始链接 | **高 —— 532 件，无法批量修** |
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

### 层 3：存量 532 件怎么补救

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