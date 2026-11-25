# 价格口径：仓库只记拿货价，发布才套模板

> 负责人 2026-11-25：
> 「成品仓库里面的价格是不能套模板的，就是拿货价，然后只有发布才套模板」
> 「认领到采集箱不能套模板，必须不能套，价格低就往上提一点」

## 一句话

成品仓库 = 拿货价；认领 = 拿货价换算 + 最多 15%；发布 = 才套定价模板。

## 问题现场

同一批认领（152 件）里，商品在妙手采集箱的「本地展示价」差别巨大：

| 商品 | 货源价格 | 本地展示价 | 倍数 |
|---|---|---|---|
| `1066528254277` | CNY 5.5 | MYR 5.37 | 1.0× |
| 另一件 | CNY 23 | MYR 38.41（CNY 62.29） | **2.7×** |

负责人一眼看出不对：「不是同一批吗，为什么后面的全部套了定价模板？」

## 两处都套了模板，是同一个根子

### ① 入仓时就套了

`warehouse_service.save_from_collect_box` 里：

```python
template = raw                       # 取操作员的定价模板
payload, _report = product_service.process_price(payload, template=template, ...)
```

于是 `skuMap.price` 存的是**模板售价**，不是拿货价。仓库卡片上显示的
`purchasePriceMin/Max` 又取自 `originPrice`（拿货价），两套数字并存。

### ② 认领时必须专门重置，改漏一件就露馅

为了把模板价改回去，认领路径里加了一段：

```python
_reset_claim_prices_to_purchase(...)   # price = originPrice / rate
```

它本身是对的，但只要有一件没走到这段（分支、异常、复用旧快照），
那件商品就带着模板价进采集箱 —— 表现为「同一批里有的正常有的像套了模板」。

### ③ 妙手成本校验把价格顶到全成本口径

`carrier_service._raise_price_to_reported_floor` 老实现：

```python
new_price = floor + Decimal("0.01")   # floor = 妙手报的「产品基础成本」
```

妙手那个基础成本是它自己的**全成本口径**（采购价 + 头程运费 + 平台费 +
各类附加），实测 25 CNY 拿货价的商品被顶到 38.41 MYR（≈62 CNY，2.7 倍）——
数字上完全像套了一套 15% 利润的模板。

## 改法

### 成品仓库不再定价

`save_from_collect_box`：只校验模板存在，不再用它定价。

```python
template = None
if price_template:
    raw = ...            # 查不到就报错，早发现比发布时才炸好
    # 只验证存在，不用它定价（负责人 2026-11-25）
```

新增 `warehouse_service._price_warehouse_at_purchase`：把 `price` /
`priceIncludeVat` 改写成 `originPrice / rate`，与认领时完全同口径。
拿货价缺失时原样保留，不去猜。

`product_service.purchase_range`：**不再回退到 `price`**。

`price` 是目标站点币种的售价，历史数据里可能是模板价也可能是汇率换算值，
两种都不是拿货价。原来拿它兜底，导致拿货价缺失的商品在卡片上显示一个
「看起来像拿货价」的售价 —— 负责人核对时发现「有些成品仓库的价格不对」，
却查不到数字从哪来。现在宁可留空。

### 发布才套模板

`publish_service._repricing`（第 1498 行附近）不动，仍是唯一的定价点。
`_repricing` 在 `template is None` 时直接返回原 payload。

### 认领只管换算 + 小幅提价

`_raise_price_to_reported_floor` 重写：

```python
CLAIM_PRICE_BUMP_RATIO = Decimal("1.15")   # 价格低就往上提一点
CLAIM_PRICE_MAX_BUMP = Decimal("1.6")      # 天花板，绝不采纳妙手全成本
```

规则：

1. 基准 = `originPrice / 站点汇率`，**不是当前价**；
2. 目标价 = 基准 × 1.15；妙手报的 `floor` 更高时一次提到 `floor + 0.01`；
3. 硬上限 = 基准 × 1.6，超了就压在 1.6；
4. 当前价已经 ≥ `floor` → 不动（这个报错不是「价格太低」引起的，
   抬价治不了它，只会把价格弄错）；
5. 提到上限仍被拒 → 抛错交给人，不无限加价。

基准钉在 payload 的 `_claimPriceBase` 上，顶到上限后打
`_claimPriceCapped` 跨轮标记 —— 否则每次重试都拿当前价当基准，
`15.38` 会滚成 `24.61 → 39.38 → 63.01 → 100.82`。

## 验证

`tools/source_match/verify_price_rules.py`（用真实汇率与生产数据）：

```
① 成品仓库入库
   汇率 1.62550086（open.er-api）
   SKU a: 拿货 25 CNY → price=15.38   （原来是模板价 46.0）
   SKU c: 拿货价缺失  → price 原样保留，不猜
   purchase_range → 25.0（不再回退到 46.0）

③ 认领提价 RATIO=1.15 MAX=1.6
   A. 拿货价 15.38，妙手要 38.40 → 24.61
      旧行为 38.41 ✗
   B. 价格 50.00 已高于成本 48.00 → 不改 ✓
   C. 非成本错误 → 不处理 ✓
   D. 连续 4 次被拒 → 24.61 / 24.61 / 24.61 / 24.61（封顶）✓
      旧行为 12 轮能滚到 82.3
```

后端回归：`test_claim_price_currency.py` + `test_claim_jobs.py` 共 15 项全绿。

## 遗留

- 已有商品的价格是旧口径存进去的，需要重跑一次入仓才会变成拿货价口径。
- `test_student_publish.py` / `test_parity.py` 的失败是改动前就存在的陈旧测试
  （`fill_carriers()` 多了 `category_trees` 参数等），与本改动无关。