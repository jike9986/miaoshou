# 仓库 → 采集箱：妙手认领标准（唯一执行口径）

> 定稿 2026-11-05。这份文件是「仓库商品怎么才算成功采集进采购箱」的唯一标准。
> 任何脚本、界面按钮、批量任务都必须按本文执行；改动前先看第五节「不能违反的约束」。
>
> 配套代码：
> - `server/services/carrier_service.py` —— 规格归一化 / 类目下沉 / payload 适配
> - `server/scripts/claim_fast.py` —— 批量认领主流程
> - `server/tests/test_claim_spec_standard.py` —— 钉死本文规则的回归测试
>
> 关联文档：`docs/PRODUCT_STANDARD.md`（入库标准）

---

## 一、一句话结论

**认领 = 复制一个载体到目标店铺采集箱，再用仓库数据把载体覆盖一遍。**
复制解决「那里得有个东西」，覆盖解决「那个东西得是对的」。两步都成功才算认领成功。

---

## 二、标准流程（七步，顺序不能换）

| # | 步骤 | 动作 | 失败怎么办 |
|---|---|---|---|
| 1 | 找公共箱 | `common_box_list(page=1,size=50)`，取第一个 `status ∈ {success, skip}` 的 `commonCollectBoxDetailId` | 公共箱空 → 采集一次链接池货源，等就绪再取 |
| 2 | 预取仓库 | `target_warehouse_id(client, shop_id)` | 店铺没仓库 → 报错终止整批 |
| 3 | 预取类目树 | `client.category_tree(site)`，整批共用 | 取不到就保留原 cid，让上游报错，不猜 |
| 4 | 复制载体 | `claim_common_to_platform([(common_id, "tiktok", 1)])` | 每次只要 1 份；返回里没有 detailId 就停 |
| 5 | 读载体 | `shop_detail(detail_id, shop_id)` → 拿 `ossMd5` + `shopCollectItemInfo` | 缺 `ossMd5` 直接跳过这件 |
| 6 | 覆盖载体 | 归一化规格 → 适配 payload → `save_shop(...)` | 见第四节「三类可自动重试的错误」 |
| 7 | 落库 | 把新 detailId 写进 `products.collected`，置 `claimed=True` | 不落库就不算完成 |

**第 1 步的 2..7 是循环体**：复制一份 → 立刻覆盖 → 立刻落库，不要攒着。

---

## 三、覆盖时到底要提交什么（这是最关键的一节）

### 3.1 skuMap 的 key 格式

```
;属性值ID1;属性值ID2;
```

- 首尾都要有分号
- 各段**按 ID 字符串升序**排列（不是按维度顺序！）
- 每个维度恰好出现一次

### 3.2 维度和 key 的关系（曾经踩过的坑）

key 段的位置**不代表维度**。正确做法是建立 `id → 维度` 的全局映射，
再去校验「每条 key 里每个维度各出现一次」。

反例（2026-11-05 线上 17 件失败就是这个）：

```
颜色 White = 25a81701fb
尺码 35    = 1c383cd30b      # 1c38… < 25a8…，所以尺码段排在前面
key = ";1c383cd30b;25a81701fb;"   # 合法
```

如果按「第 0 段必须是颜色」去校验，就会把 `1c383cd30b`（尺码）当颜色，
最终妙手报 `SKU【White-35】不存在`。

### 3.3 attrId 必须用类目元数据的真实值

从 `client.category_metadata(site, cid, [shop_id])` 的 `categorySaleAttrList` 取：

| 维度 | attrId | attrName（MY 站） |
|---|---|---|
| 颜色 | `100000` | `Warna` |
| 尺码 | `100007` | `Saiz` |

**不能用 `null`，也不能自造**（如 `1000000001` 会失败）。

### 3.4 规格值：直接用商品自己的

妙手接受任意非空 `attrValueId` 作为「新规格值」。所以：

- 不需要把商品颜色映射到载体已有颜色（那样反而更容易失败）
- 只需要：`attrId` 对 + `attrValueId` 非空 + `skuMap` 升序
- 同一维度内 `attrValue` 去重（大小写、空格归一后比较），重名会报「选项名称重复」

### 3.5 必须剔除的字段

| 字段 | 原因 |
|---|---|
| `brandName` / `categoryName` | 是我们自己的中文标注，妙手报「不能包含中文」 |
| `skuPropertyList[].propName`、`propId` | 同上，维度定位用 `attrName` |
| `colorPropName` / `sizePropName` | 同上 |
| `colorMap` / `sizeMap` | 文档未列，上游对多余字段敏感 |
| `imgUrls` / `imageUrl` / `supplementarySkuImageUrls`（规格值内） | 文档只认 `imgUrl` |

### 3.6 其他必填

| 字段 | 值 |
|---|---|
| `deliveryOptionSetType` | `"default"` |
| `isCodOpen` | 字符串 `"0"` / `"1"`（布尔会被拒） |
| `brandId` | `0`（无品牌） |
| `sizeChart` + `sizeChartType="image"` | 缺了会报「请上传主图」 |
| `cid` | 必须是叶子类目；父级自动下沉到第一个可用叶子 |

### 3.7 体重 / 尺寸

覆盖时直接写仓库 payload 里的值，不做二次估算：

- `weight` 缺失时从 SKU weight 取，仍无则保留载体原值
- `packageLength/Width/Height` 三边都 > 0

---

## 四、三类可自动重试的错误（人工不用管）

| 上游报错关键字 | 原因 | 处理 |
|---|---|---|
| `基础成本 ... MYR` | 售价低于成本线 | 按报错里的准确成本线上调（+3%，至少 +0.15），重试；连锁上调最多 3 次 |
| `编辑过程中产品数据发生变动` / `重新打开` | 新建载体的 `ossMd5` 还在变 | 重读 `shop_detail` 拿新 md5，最多重试 5 轮（每轮间隔 1.2s） |
| `下游服务响应超时` | 妙手网关偶发 | 读操作重试 3 次；超过就跳过并记录，不要判成数据问题 |

**成本线上调后必须把新价格写回仓库**（`payload.skuMap[*].priceIncludeVat` 和
`price_min/price_max`），否则下次核对会认为「仓库和采集箱不一致」。

---

## 五、不能违反的约束

1. **不许并发打妙手。** 单线程最优（~5s/件）；并发反而更慢且更容易被限频。
2. **不许用 `serialNumber` 要 N 份。** 实测无效，要 N 份就调 N 次，每次 1。
3. **采集就绪只看列表接口的 `detailList[].status`**，详情接口没有稳定的 status 字段。
4. **认领阶段不做读回核验。** 每件多花 6s，且对新建载体必然报不一致。
5. **不落库不算成功。** `claim_fast.py` 历史上只打印 `id=x -> detailId` 不写库，
   导致页面上看不出认领过、也无法核对。所有认领路径必须写 `products.collected`。
6. **不许带坏数据打上游。** 认领前先用 `PRODUCT_STANDARD` 的校验器过一遍，
   不通过就跳过并报告，别指望上游给出可读的错误。
7. **覆盖失败的临时载体要删掉**，不要在采集箱里留半成品。

---

## 六、怎么验证一次认领是对的

写库之后，逐件调 `shop_detail(detail_id, shop_id)`，比对的是
**`shopCollectItemInfo`**（覆盖真正写入的那一层），不是采集箱列表行。

> 采集箱列表里的 `title` 是采集箱原始标题（1688 中文），不会被覆盖改写。
> 拿它来核对会得出「全都不一致」的错误结论 —— 2026-11-05 踩过。

| 比对项 | 通过条件 |
|---|---|
| 标题 | 与仓库 `products.title` 前 50 字一致，且无中文 |
| 重量 | 与 `payload.weight` 差值 ≤ 0.06kg |
| 三边 | 无 0，与仓库差值 ≤ 0.6cm |
| SKU 条数 | 与 `payload.skuMap` 条数相等 |
| SKU 价格集合 | 排序后完全相等 |
| 主图 | 张数非 0 且与 `products.images` 相等 |
| cid | 与 `payload.cid` 一致 |

验证脚本：`_verify_deep.py`（服务器上执行为 `_run_remote.py _verify_deep.py`）。

---

## 七、性能基准（实测，MY 站单线程）

| 场景 | 耗时 |
|---|---|
| 整批预取（公共箱 + 仓库 + 类目树） | ~3.7s（一次性） |
| 单件复制 | 2.7~3.2s |
| 单件覆盖 | 3.0~3.2s |
| **单件合计** | **~5s** |
| 70 件整批 | ~385s |
| 老实现（每件重复查询） | ~20s/件 |

---

## 八、常见报错对照表

| 报错 | 根因 | 对应章节 |
|---|---|---|
| `SKU【颜色-尺码】不存在` | key 段被按位置错判成维度 / attrId 不对 | 3.2、3.3 |
| `SKU 规格[Warna] 选项名称重复` | 同维度 `attrValue` 有重名 | 3.4 |
| `不能包含中文` | `brandName`/`categoryName`/`propName` 没剔 | 3.5 |
| `请上传主图` | 缺 `sizeChart` | 3.6 |
| `本地展示价 X MYR 无法覆盖基础成本 Y MYR` | 售价低于成本线 | 4 |
| `编辑过程中产品数据发生变动` | `ossMd5` 过期 | 4 |
| `站点【MY】类目【xxx】不存在` | 传了父级类目 | 3.6 |
| `下游服务响应超时` | 网关偶发 | 4 |

---

## 九、改动这份标准的流程

1. 先在 `test_claim_spec_standard.py` 里加一条能复现问题的用例
2. 改代码直到用例通过
3. 用真实 70 件（或抽样 10 件）跑一遍认领 + `_verify_deep.py` 核对
4. 更新本文对应章节，写清「原来错在哪、现在怎么判」

只改代码不改文档、或只改文档不补测试，都算没做完。
