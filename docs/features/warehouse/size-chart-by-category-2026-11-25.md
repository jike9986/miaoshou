# 尺码图按品类区分（2026-11-25）

> 负责人口径来源：
> 「像帽子，户外椅帽子什么的都是不需要套尺码图的啊」
> 「自动化处理是可以套模板的，都是入库是需要我手动处理选择入库，
>   入库的时候价格是没有套模板的价格」

## 一、两个阶段的边界（负责人确认）

| 阶段 | 谁触发 | 套定价模板 | 尺码图 |
|---|---|---|---|
| **自动化处理** | 桌面端跑批 | ✅ 可以套 | 按品类判 |
| **入库** | 负责人手动挑选 | ❌ 不套（拿货价口径） | 按品类判 |

`POST /api/warehouse/automation-results/import` 是「入库」入口，走
`save_from_collect_box`，模板只校验存在、不用于定价（见
`price-and-claim-2026-11-25.md`）。

自动化处理阶段 `settings.template` 生效，产出 `automation_result = true`
的记录供负责人挑。

## 二、问题

原来两处都是**无条件**写尺码图：

- `publish_service._fill_claim_size_chart`（认领到采集箱）
- `warehouse_service.save_from_collect_box`（入库）

结果帽子的详情页也挂一张鞋码表。

实测黄映明（账号 12）233 件未发布商品：

```
鞋          164 件   需要尺码图
帽           47 件   不需要   ← 都挂了鞋码表
椅/凳        10 件   不需要   ← 都挂了鞋码表
配件/其它      6 件   不需要
包            2 件   不需要
```

**65 件（28%）本不该有尺码图。**

## 三、判据踩了两次坑

### 第一版：判「有没有尺码维度」—— 错

实测帽子**也有**尺码维度：

| 品类 | 尺码值 |
|---|---|
| 帽 | `均码`、`可调节`、`55-60cm(可调节)`、`58`/`60` |
| 鞋 | `36`、`37`、`36-37`、`38-39【内长235mm】`、`24/25码` |

只看维度存在，47 顶帽子全被判成需要。

### 第二版：判「尺码值像不像鞋号」—— 27/28 对

加了鞋子号正则 + 排除「均码/可调节/头围」+ 排除容量单位（12L/20L）。
28 件抽查 27 件正确，漏一件：

```
「户外迷彩战术帽定制 圆边帽定制」尺码 = ['58', '60']
```

纯数字头围，和鞋号 58/60 **完全无法区分**。

### 最终版：类目名优先

这件帽子 `cid=603654`，本地类目表查到
`name_cn='运动与户外帽'`，一锤定音。

```
1. 类目名含 鞋/靴/服/裤/袜/泳/裙           → 需要尺码图
2. 类目名含 帽/包/袋/椅/凳/桌/帐/伞/床/
           套/垫/架/灯/杯/壶              → 不需要
3. 没有类目名 → 看尺码值像不像鞋号
4. 都没有 → 不需要（宁可少挂一张图，
              也不要给帽子挂鞋码表）
```

payload 只有 `cid` 时用 `product_service._category_name_from_db(cid)` 查表兜底，
这个映射本来就带进程内缓存，不增加请求。

## 四、实现

### warehouse_service

```python
def _is_shoe_or_clothes_size(values: list[str]) -> bool:
    """这组尺码值是不是鞋号/服装号（而不是帽子的均码/头围）。"""

def _payload_needs_size_chart(payload, category_name="") -> bool:
    """这件商品该不该套尺码对照图。"""
```

`save_from_collect_box`：

```python
if chart_url and _payload_needs_size_chart(payload, _cat_name):
    payload["sizeChart"] = chart_url
    payload["sizeChartType"] = "image"
elif not _payload_needs_size_chart(payload, _cat_name):
    payload["sizeChart"] = ""      # 历史带进来的也清掉
    payload["sizeChartType"] = ""
```

### publish_service

`_payload_has_size_axis` 直接复用 `warehouse_service._payload_needs_size_chart`，
避免两处判据漂移。

`_fill_claim_size_chart` 对没有尺码轴的商品：

```python
if payload.get("sizeChart"):
    payload["sizeChart"] = ""
    payload["sizeChartType"] = ""
    adjustments.append({..., "source": "cleared_no_size_axis"})
```

**为什么要清**：仓库里 225 件的 payload 是旧逻辑无条件写进去的，
帽子上那张鞋码表就是这么来的，不清会一直带进妙手。

### pre_save_check 配套修改

第 4 条原来要求 `payload.sizeChart == 本次选中的图`。
清空帽子尺码图后，每顶帽子都会被判
`selected size chart is missing` 直接拦死。

改成只对需要尺码图的商品校验：

```python
if size_chart_url:
    from services import warehouse_service   # 函数内导入，避免循环引用
    if warehouse_service._payload_needs_size_chart(payload):
        if _ci(payload, "sizeChart") != size_chart_url:
            issues.append("selected size chart is missing")
```

> 为什么函数内导入：本模块定位是纯函数（被入仓/批量预览/只读检查三处调用），
> 模块级导入 `warehouse_service` 会形成 services 内部循环引用。

## 五、验证

### 服务器实测（真实 229 件，抽 28 件）

```
帽   不需要  类目=运动与户外帽       尺码=['均码']
帽   不需要  类目=运动与户外帽       尺码=['可调节(57-60cm）']
帽   不需要  类目=运动与户外帽       尺码=['58', '60']          ← 原来误判的那件
椅   不需要  类目=露营家具          尺码=[]
鞋   需要   类目=溯溪鞋            尺码=['36-37', '38-39', '40-41']
鞋   需要   类目=运动拖鞋          尺码=['36', '37', '38', ...]
其它  不需要  类目=滤水、净水和储水设备  尺码=['12L', '20L']

统计：帽 7/7 不需要 · 椅 7/7 不需要 · 鞋 7/7 需要 · 其它 5/5 不需要
判错数：0
```

### 单元测试

`server/tests/test_size_chart_axis.py` 共 21 项：

- 不需要：帽子均码/可调节/头围cm、椅子只有颜色、水桶容量升/毫升
- 需要：鞋单码/区间/带内长/童鞋斜杠码/全码、服装字母码
- 类目优先：同是 58/60，类目是帽 → 不需要，类目是鞋 → 需要
- 生产真实数据：帽子/鞋/椅子各一件原文
- 认领时清空：帽子 payload 里的历史尺码图会被清掉，鞋的保留

## 六、顺带修的：缺拿货价时成本线兜底

`carrier_service._raise_price_to_reported_floor` 原来在 SKU 没有
`originPrice` 时直接 `continue`，于是妙手明确报了成本线也抬不了价，
**这件商品永远认领不了**。

改成：

```python
if purchase_d is not None:
    # 微微上挑路径（拿货价 × 1.18 ÷ 汇率）
elif floor is not None:
    # 没有拿货价：用成本线兜底，不做任何比例上挑
    target = current      # 下面统一抬到 floor + 0.01
else:
    continue              # 既没拿货价也没成本线，不猜
```

不拿当前价乘任何系数，所以不会滚价。实测 4 轮稳定在 15.01。

## 七、改动文件

```
server/services/warehouse_service.py   _is_shoe_or_clothes_size / _payload_needs_size_chart
                                        save_from_collect_box 按品类判
server/services/publish_service.py       _payload_has_size_axis 复用同一判据
                                        _fill_claim_size_chart 清空无尺码轴商品的图
server/services/pre_save_check.py      第 4 条只对有尺码的商品校验
server/services/carrier_service.py     缺拿货价时成本线兜底
server/tests/test_size_chart_axis.py   新增 21 项
server/tests/test_carrier_copy.py      更新尺码图用例为鞋类场景
```