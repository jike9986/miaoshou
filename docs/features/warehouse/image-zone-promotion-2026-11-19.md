# 入库搬运 SKU 图失效（2026-11-19）

> 现象：成品仓库里大量商品的 SKU 图 / 颜色图地址仍指向 `auto-temp`，
> 而那个目录**14 天自动清理**。到期商品图会全部变成挂图。
>
> 实测：180 件 `status=ready` 商品里 **95 件**中招，共 **2121 处**引用。

---

## 一、设计意图（本来该是对的）

图床分两个区（负责人 2026-11-09 定）：

| 目录 | 用途 | 保留 |
|---|---|---|
| `auto-temp/` | 自动化处理的临时图 | **14 天自动清理** |
| `products/` | 已入库成品仓商品图 | 长期保留 |

入库入口 `warehouse_service.py:2990` 会调
`image_upload.promote_payload_images(payload)` 把图从临时区搬到成品区。
注释写得明确：

> 「这里必须**真的搬文件**，只改数据库标记的话临时区整批清理时
> 会把成品商品的图一起删掉，商品全变挂图。」

代码确实在搬文件，日志也有大量 `入库搬运图片到成品区 moved=18 failed=0`。

**但只有所有字段都被覆盖，这套机制才成立。而它漏了三个地方。**

---

## 二、三个叠加 bug

### BUG-017 `item.get("imgUrl")` 循环体从未执行

`image_upload.py` 原来的写法：

```python
for item in warehouse_service.sku_image_sources(payload):
    for field in ("imgUrl", "imageUrl", "image"):
        if item.get(field):
            item[field] = _swap(str(item[field]))
```

`sku_image_sources()` 返回的是：

```python
{"path": ..., "kind": ..., "url": ..., "propertyIndex": ..., "valueIndex": ...}
```

**里面没有 `imgUrl` / `imageUrl` / `image` 这些 key。**
`item.get(field)` 恒为 `None`，`if` 永不成立 —— **循环体从来没跑过**。

也就是说：主图搬了、详情图搬了，**SKU 图一张都没搬过**。

### BUG-018 只认老字段名，新字段不认

`sku_image_sources()` 和 `rewrite_sku_image_urls()` 都只处理：

```
imgUrl                        ← 妙手老字段
supplementarySkuImageUrls     ← 补充图
```

妙手新数据用的是：

```
imageUrl      ← 同一个图，字段名换了
imgUrls[]     ← 列表形态，一色多图
```

这两个字段既不枚举也不替换。

实测分布：

| 字段 | auto-temp 引用数 |
|---|---|
| `imgUrl` | 367 |
| `imageUrl` | 339 |
| `imgUrls[]` | 339 |
| `colorMap` | 678 |

### BUG-019 `colorMap` 完全没被覆盖

`promote_payload_images` 处理 `imgUrls` / `notes` / SKU 图 / `sizeChart`，
**没有 `colorMap`**。

`colorMap` 的形态：

```json
{
  "红色": "https://…/auto-temp/x.jpg",
  "蓝色": {"imgUrl": "…", "imageUrl": "…", "imgUrls": ["…"]}
}
```

实测 #511 搬走 24 处之后**还剩 8 处全在 `colorMap` 里**。

### 附带：存量数据永远改不过来

`promote_to_product_zone()` 开头：

```python
source = _Path(local_auto) / name
if not source.is_file():
    return url          # ← 临时区没有就直接放弃
```

但入库搬运成功时会把临时区原件 `unlink()` 删掉。于是存量商品处于：

```
文件：在 products/ ✓
地址：还指着 auto-temp/ ✗
```

再跑搬运 → 临时区找不到文件 → 直接返回原地址 → **永远改不过来**。

---

## 三、修复

| 文件 | 改动 |
|---|---|
| `services/warehouse_service.py` | `sku_image_sources` 补 `imageUrl` + `imgUrls[]`；`rewrite_sku_image_urls` 同步支持四个字段 |
| `services/image_upload.py` | `promote_payload_images` 改用 `rewrite_sku_image_urls` 做映射替换（修 BUG-017）；补 `colorMap` 处理（BUG-019）；`promote_to_product_zone` 在源文件不在时检查成品区同名并直接改地址 |
| `scripts/promote_warehouse_images.py` | 新增，处理存量。默认 dry-run，`--apply` 写库，走 `revision_guard` + 同步 `draft_verified_revision` |
| `tests/test_promote_image_zone.py` | 新增 8 条测试 |

---

## 四、验证

**测试**（`tests/test_promote_image_zone.py`，8 条全通过）：

- 三个图片字段 + 补充图都要枚举到
- 替换要把四个字段全改掉
- `deepcopy` 不污染调用方快照
- **入库搬运必须真的动到 SKU 图**（钉住 BUG-017 那个空循环）
- 上游 1688 地址不能动
- `colorMap` 两种形态都要搬
- 源文件已删但成品区有同名 → 只改地址
- 幂等：跑两次结果一样

**生产迁移**：

```
待处理：95 件，2121 处 auto-temp 引用
执行后：ready 商品 180 件 → 0 件含 auto-temp、0 处引用、0 处失效文件
```

---

## 五、为什么这个 bug 藏了这么久

1. **前 14 天看不出来**。临时区的图还在，页面显示一切正常。
2. **日志是绿的**。`moved=18 failed=0` 看起来完全成功，只是那个数字不包含 SKU 图。
3. **只有部分字段中招**。主图和详情图搬对了，所以抽查几张图不会发现异常。
4. **发现时已经错过现场**。等 14 天清理跑过，商品图才突然全挂，那时再查已经看不到原文件。
5. **没有测试覆盖**。「入库会把图搬到成品区」这个约定从来没被断言过。

---

## 六、遗留观察点

`auto-temp` 里现在还有 6059 个文件 / 2.9 GB，绝大部分是历史中间产物。
迁移后成品仓已不引用它们，**可以安全按原设计 14 天清理**。

如果想让清理更激进（省磁盘），先把保留期从 14 天改短，但**改之前必须确认
`promote_warehouse_images` 的残留计数为 0**，否则会误删还在被引用的图。
---

## 七、后续修正：多余颜色的图不该搬进成品区（2026-11-19 补）

负责人指出：「有些成品是处理过最多保留九种颜色，多余的颜色是清理的，
**这些图片不能同步传到成品区里面**。」

上面第三到五节的修复解决了「图停在临时区」，但**没解决「多余的图被搬走」**。

### BUG-020 裁色不裁 colorMap

`apply_image_limits` 裁 `attrValueList` + `skuMap`，**没裁 `colorMap` / `sizeMap`**。

这两张表是详情页的展示快照，本地字段，不发给妙手 —— 但**每个条目挂着图片地址**。
不裁它们，被删颜色的条目还留在 payload，入库搬运时一起搬进成品区。

实测：92 件商品共 **362 条**多余条目、**340 张图**被误搬进 `products/`。

**修复**：`apply_image_limits` 在裁 SKU 的同一个循环里同步裁这两张表。

### BUG-021 尺码被写进 colorMap

`product_standard.py` 判断尺码用 `propName in ("尺码", "尺寸")`。
实际数据里：

```
propName='颜色'  attrName='Warna'  → 颜色，正确
propName='尺码'  attrName='尺码'   → 尺码，正确（但有个变体）
propName=''      attrName='Saiz'   → 尺码，漏判 → 写进 colorMap
```

实测 product 472：`colorMap` 20 条里 **11 条是尺码**（EUR33/EUR34/32码）。
product 392：`colorMap` 5 条里 3 条是尺码（`40` / `45` / `Saiz`）。

**修复**：三个字段一起看（`propName` / `attrName` / `attrNameCn`），
名字认不出来时用「值里是否带图」兜底判断。

### BUG-022 saiz 属性被 `_is_color_property` 判成颜色

`_is_color_property` 有三级判定，第三级是「值上挂了图就是颜色」。
但历史数据里 `product_standard` **给尺码值也补过图**，于是 `saiz` 属性
满足了这条兜底 → 被判成颜色 → **颜色上限拿尺码值去截断**。

实测 5 件：472(尺码) / 381(saiz) / 395(saiz) / 248(saiz) / 392(saiz)。

**修复**：新增 `_SIZE_ATTR_NAMES` 清单，名字命中就先直接否决，
不管值上有没有图。覆盖 尺码/尺寸/size/saiz/ukuran/ขนาด/kích thước/サイズ。

### 存量清理

新增 `scripts/trim_color_map.py`（默认 dry-run）：

- 按 `skuPropertyList` 实际保留的颜色裁掉 `colorMap` 多余条目
- 用正则识别混进来的尺码 key（`40` / `EUR33` / `L(37-39)` / `Saiz`）
- **只删 key，不删文件**，并把变成孤儿引用的图列出来供人工决定

生产执行结果：

```
第一批（BUG-020）：清理 92 件，删 362 条，340 张图变孤儿
第二批（BUG-022）：清理 4 件，删 21 条，21 张图变孤儿
最终：颜色超限 0 / colorMap 超限 0 / 多属性误判 0 / auto-temp 引用 0
```

### 新增测试

`tests/test_color_trim_zone_sync.py` 6 条：

- 裁到 9 色时 `colorMap` 也要只剩 9 条
- 裁色时 `skuMap` 孤儿 SKU 一起删（原有行为不能被破坏）
- 尺码值不能进 `colorMap`
- 尺码属性即使值上有图也不算颜色
- 限色时截断的是颜色维度不是尺码维度
- 端到端：裁色后再搬运，被删颜色的图不该出现在成品区地址里
