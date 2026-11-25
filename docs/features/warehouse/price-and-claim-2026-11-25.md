# 价格口径与认领修复（0.1.54）

> 负责人口径来源：
> 「成品仓库里面的价格是不能套模板的，就是拿货价，然后只有发布才套模板」
> 「认领到采集箱不是拿货价增一点点，比如 23 就 27、28 人民币」
> 「只需要微微上挑让他不报成本不够就行了」
> 「其他国家也是一样的」

## 一、价格口径（五处）

### 1. 成品仓库只记拿货价

`warehouse_service.save_from_collect_box`：模板只校验存在，不再用于定价。
新增 `_price_warehouse_at_purchase`：`price = originPrice / rate`，与认领同口径。

`product_service.purchase_range`：**不再回退到 `price`**。
`price` 是站点币种售价，历史数据里可能是模板价也可能是换算值，都不是拿货价。
原来拿它兜底，卡片上就显示出「看起来像拿货价」的错误数字。

### 2. 认领加价在人民币层面

```python
CLAIM_PRICE_BUMP = Decimal("1.18")   # 只有这一处定义
23 CNY × 1.18 = 27.14 CNY → 16.70 MYR
5.5 CNY × 1.18 = 6.49 CNY  → 3.99 MYR
```

加价永远在人民币层面做。先换算成外币再加百分比是错的 —— 那样加的是外币的
18%，折回人民币看就不是「微微上挑」。

### 3. 妙手嫌低才抬到成本线

`carrier_service._raise_price_to_reported_floor`：

1. 按上面公式算出目标价；
2. 若仍低于妙手报的成本线 → 抬到 `成本线 + 0.01`，**只多一分**；
3. 当前价已 ≥ 成本线 → 不动（这个报错另有原因，抬价治不了）。

妙手报的「产品基础成本」是它自己的全成本口径，不是拿货价，所以不能当常规定价；
但为了「不报错」抬到它的底线是唯一办法。真正的定价由负责人在妙手套模板完成。

### 4. 认领任务补上价格重算

认领有三条路径，只有 `publish(claimOnly)` 漏了：

| 路径 | 价格重算 |
|---|---|
| `claim_site_batch` | ✓ |
| `claim_carrier_batch` | ✓ |
| `publish(claimOnly)` ← 认领任务走这条 | 原来 ✗，已补 |

`claim_job_service` 调 `publish_service.publish`，进 claimOnly 分支后只跑了
`prepare_claim_payloads`（清洗/图片/重量，**不动价格**），于是库里存的旧售价
原样发给了目标店铺。

现场：李小芳往菲律宾店 `19990473` 认领 9 件，连败三批，妙手报
「本地展示价 18.71PHP，无法覆盖基础成本 225.50PHP」。
18.71 是马币时代的旧值被当成 PHP 提交。

### 5. 成本线正则不能写死币种 ★

`carrier_service` 里两个正则原本都以 `\s*MYR` 结尾：

```python
_COST_FLOOR_VALUE = r"基础成本.*?([0-9.]+)\s*MYR"    # 只认 MYR
```

于是：

| 站点 | 报错 | 是否识别 |
|---|---|---|
| MY | `…38.40MYR` | ✓ |
| PH | `…225.50PHP` | ✗ 一个字都匹配不上 |
| TH | `…310.00THB` | ✗ |
| VN | `…128000VND` | ✗ |
| SG | `…8.50SGD` | ✗ |

`_raise_price_to_reported_floor` 每次在第一行 `return None`，抬价逻辑等于不存在。
**只有马来店能用，其他国家全部失败** —— 这就是「其他国家也是一样的」。

改成 `[A-Z]{3}` 通配。`scripts/claim_fast.py` 有同一处隐患，一起修了。

## 二、妙手成本线是什么

实测妙手报的成本严格按重量线性：

| 重量 | 妙手报的成本 | 我们菲律宾模板运费表算出 |
|---|---|---|
| 0.5 kg | 225.50 PHP | 225.5 PHP |
| 0.9 kg | 405.50 PHP | 405.5 PHP |
| 1.1 kg | 495.50 PHP | 495.5 PHP |
| 1.2 kg | 540.50 PHP | 540.5 PHP |

**分毫不差** —— 就是跨境物流运费（450 PHP/kg），不是妙手乱算。
所以低单价商品容易被拦：拿货 8.49 CNY 的货，上挑后才 95.89 PHP，
远不够 225.50 的运费成本线，必须抬到 225.51 才能存进去。

## 三、尺码图

已确认**妙手会保留尺码图**。实测：建载体 → `fill_carriers` 写入 →
`shop_detail` 读回，`sizeChart` 与 `sizeChartType=image` 都在。

链路各环节都不丢字段：

```
库里 payload.sizeChart  →  prepare_claim_payloads（保留）
                        →  _reset_claim_prices_to_purchase（保留）
                        →  adapt_payload（保留）
                        →  save_shop（sizeChart 非空时不 pop）
                        →  妙手读回（保留）
```

## 四、验证

服务器实测（部署后）：

```
成本线识别：PH 225.50 ✓  MY 38.40 ✓  TH 310.00 ✓  VN 128000 ✓  SG 8.50 ✓

真实菲律宾数据（#415，拿货 8.49 CNY）：
  提交 95.89 PHP → 妙手报成本 225.50 → 抬到 225.51 → 写入成功
  读回 sizeChart = 'https://aohuang888.com/tk-product-automation/size-chart.png' ✓
```

后端测试 48 项全绿，其中新增防回归用例：

- `test_cost_floor_regex_matches_every_site_currency`（五币种全覆盖）
- `test_raise_uses_cost_floor_for_php_like_myr`（PHP 与 MY 同等生效）
- `test_claim_price_bump_is_applied_in_cny_not_local_currency`（23→27 口径）
- `test_raise_to_cost_floor_never_lowers_price`（只挑不压）

## 五、改动文件

```
server/services/carrier_service.py     CLAIM_PRICE_BUMP、币种通配、抬价口径
server/services/collect_box_service.py 认领时人民币层面加价
server/services/warehouse_service.py   入库只记拿货价
server/services/publish_service.py     claimOnly 分支补价格重算
server/services/product_service.py     purchase_range 只认 originPrice
server/scripts/claim_fast.py           CLI 脚本同口径
scripts/deploy_server.py               部署清单补三个服务文件
server/tests/test_claim_price_currency.py
server/tests/test_claim_spec_standard.py
```