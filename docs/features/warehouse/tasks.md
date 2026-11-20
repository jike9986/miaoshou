# 成品仓库 · 任务清单

## 已完成

- [x] SKU 颜色/尺码显示乱码（v0.1.24）：改用全局 `flatIdToLabel` Map
- [x] 编辑后商品从自动化结果消失（v0.1.24）：三个写接口补同步 `draft_verified_revision`
- [x] 仓库缓存不刷新（v0.1.24）：运行结束时 `invalidateQueries({ queryKey: queryKeys.warehouse.all })`
- [x] **列表页加载 654 KB 原图**（v0.1.26）：卡片只显示 200×200，一页 20 件 ≈ 13 MB。
      入库时生成 `products/thumbs/<stem>.jpg`（200px，8~12 KB），`warehouse_service.serialize` 加
      `imageThumb`，前端 `ProductCard` 用 `product.imageThumb || product.image`（取不到回退原图）。
- [x] **PNG 当照片存**（v0.1.26）：`promote_to_product_zone` 入库时统一转 JPEG q92，
      带 alpha 的先合白底再转（直接 `convert("RGB")` 会变黑底）。详见
      [image-pipeline/spec.md](../image-pipeline/spec.md)。
- [x] **翻译阶段流量翻 4 倍**（v0.1.26）：`image_service.SHRINK_LIMIT` 2 MB → 500 KB、
      `SHRINK_MAX_SIDE` 1600 → 1200。单张翻译往返从 ≈15 MB 降到 ≈2 MB。
- [x] **存量图没变小**（v0.1.26）：新增 `scripts/optimize_product_images.py`（plan/thumbs/convert
      三模式）。实测 4,277 张 → 缩略图 44 MB；1,072 张 PNG/WebP 转 JPEG，
      `products/` 从 2,733 MB 降到 959 MB（省 1,774 MB，约 65%）。
- [x] **nginx 缓存头**：`location ^~ /tk-product-automation/` 早已发
      `Cache-Control: public, max-age=31536000, immutable`，2026-11-19 用 `curl -I` 复核确认。
- [x] **BUG：转换后 `automation_images` 列漏改**（v0.1.26，我自己引入的）：
      `optimize_product_images.py` 的 convert 只重写了 6 列（cover / images /
      detail_images / sku_properties / payload / collected），漏掉
      `products.automation_images`，导致 31 个在用商品在自动化结果页
      229 处图片地址指向已删除的 .png（挂图）。
      修法：列清单改成 `information_schema` 动态发现；另写
      `scripts/fix_converted_image_refs.py` 扫全库补漏（按「旧文件不存在 且
      新 .jpg 存在」才改，带 --dry-run），已跑完，229 处清零。
- [x] **审计口径修正**：判断「孤儿图片」不能只扫 `products` 表 ——
      `image_jobs`（翻译任务）也存图片地址，900 条引用会被误判成孤儿。
      全库扫（161 个文本/JSON 列）后真实孤儿 **38 个文件 / 7.0 MB**，
      不是最初报告的 848 个。

## 已完成（v0.1.28，2026-11-19）

- [x] **定价汇率按站点取（P0）**：`resolve_rate_with_source` 原来调
      `exchange_rate.cached_rate()` / `live_rate()` 时不传币种，两个函数默认
      `DEFAULT_CURRENCY = "MYR"`，于是菲律宾/泰国/越南/新加坡**全部用马来汇率
      算本币报价**。菲律宾 35CNY/0.6kg 算出 379.30 PHP，妙手官方是 796.54 PHP
      —— 只有 47.6%，发出去就是亏本卖。修法：按 `template.site` /
      `freightTable.currency` 定币种，兜底也用 `fallback_rate(currency)`。
      修复后菲律宾 796.47 PHP（差 0.07，纯四舍五入），五站点全部正确。
      详见 `docs/features/warehouse/price-and-groups-2026-11-19.md`
- [x] **颜色上限对一半商品失效（BUG-015）**：写死 `attrId == "100000"` 认颜色
      维度，生产库一半商品该字段是 `None`/空串。改为三级判定
      （attrId / 属性名 / 值上挂图）。
- [x] **颜色删了但 SKU 留着（BUG-016）**：用 `attrValueId` 比对 `skuMap` 的 key，
      真实 key 是 `;颜色值文本;尺码值文本;`，永远匹配不上，留下孤儿 SKU。
      改为同时用属性值文本和 attrValueId 比对。
- [x] **「SKU 图数量」改名「颜色数量」**：实测确认只有颜色值挂图、尺码值
      `imgUrl` 恒空，所以「保留前 N 张 SKU 图」等价于「保留前 N 个颜色」。
      功能本来就有，只是上面两个 bug 让它形同虚设。UI 标签改为「颜色数量」。
- [x] **三个受限分组权限**：自营海外仓 / 海外仓定制款 / 精品（共 18 件）
      只给老师和管理员。学员在列表看不到、接口层 403、定时任务选不到。
      新增 `services/group_access.py`。生产验证：学员可见 175 件、老师 194 件。
- [x] **一次性清理颜色超限的商品**：48 / 54 / 59 / 67 四件（颜色 10/12/29/24）
      裁到 9 个颜色，SKU 同步减少。全库复扫无超限、无孤儿 SKU。
- [x] **入库搬运 SKU 图完全失效，商品图停在临时区会 14 天后消失（BUG-017/018/019）**：
      成品仓 180 件商品里 **95 件**的图片 URL 指向 `auto-temp`（14 天自动清理），
      共 2121 处引用。三个叠加 bug：
      - BUG-017 `promote_payload_images` 遍历 `sku_image_sources()` 写的是
        `item.get("imgUrl")`，但那函数返回 `{path, kind, url, ...}` 没有这个 key，
        **循环体从未执行**，SKU 图一张没搬过。
      - BUG-018 `sku_image_sources` / `rewrite_sku_image_urls` 只认 `imgUrl`
        和 `supplementarySkuImageUrls`。妙手新数据用 `imageUrl` + `imgUrls[]`，
        这两个字段既不枚举也不替换。
      - BUG-019 `promote_payload_images` 完全没覆盖 `colorMap`（颜色图）。
      另外 `promote_to_product_zone` 遇到「临时区源文件已删、成品区有同名」时
      直接返回原地址，导致存量数据永远改不过来。
      修复后新增 `scripts/promote_warehouse_images.py` 处理存量：
      **生产已迁移 2121 处，180 件商品全部归零，0 处失效引用**。
      详见 [image-zone-promotion-2026-11-19.md](image-zone-promotion-2026-11-19.md)。
- [x] **多余颜色的图被同步搬进成品区（BUG-020/021）**：负责人 2026-11-19 指出
      「有些成品处理过只要 9 种颜色，多余的颜色是清理的，这些图片不能同步传到
      成品区」。
      - BUG-020 `apply_image_limits` 裁 `attrValueList` + `skuMap`，**不裁
        `colorMap`/`sizeMap`**。这两张表每条挂着图片地址 → 被删颜色的图留在
        payload → 入库搬运时一起搬进成品区。实测 92 件商品共 362 条多余条目、
        340 张图被误搬。
      - BUG-021 `product_standard` 判断尺码只认 `propName in ("尺码","尺寸")`，
        实际数据 propName 可能为空或只有 `attrName`（Saiz），于是尺码值
        （EUR33/32码/L(37-39)）被写进 `colorMap`。实测 product 472 的
        colorMap 20 条里 11 条是尺码。
      - 附带：`_is_color_property` 的「值里带图」兜底被历史数据骗到 ——
        早期给尺码值补过图，导致 saiz 属性被判成颜色，颜色上限拿尺码去截断。
        实测 5 件中招（472/381/395/248/392）。新增 `_SIZE_ATTR_NAMES`
        优先否决。
      修复后新增 `scripts/trim_color_map.py` 处理存量：
      **清理 96 件、383 条多余条目，361 张图不再被引用**。
      最终状态：180 件商品 / 颜色超限 0 / colorMap 超限 0 / 多属性误判 0。
- [x] **受限分组不受颜色数量上限约束（负责人 2026-11-19）**：
      「三个分组的那里面的颜色图片不受影响，超过九种都没关系」。
      自营海外仓 / 海外仓定制款 / 精品 是精品线，颜色多少由厂家定，
      裁掉等于改商品本身（SKU 少一批、颜色少几个，发出去就是另一件货）。
      - `apply_image_limits` 新增 `group_id` 参数，受限分组直接把
        `sku_limit` 置 0 并在报告里标 `skipped`。
      - 主图 / 详情图上限**仍然生效** —— 那两个是展示取舍，颜色是商品本体。
      - 新增 `group_access.is_restricted_group_id()`：不查库的快速判定，
        给手上没有 Session 的地方用（图片截断那一步只有 payload）。
      - `trim_extra_colors.py` / `trim_color_map.py` 都传入 `group_id`，
        脚本不会按「上限 9」去裁受限分组；只清混进来的尺码条目。
      生产实测：受限组 #10（group=3）设上限 3 → 颜色仍是 7 个（豁免生效）；
      普通组 #359（group=4）设上限 3 → 5 个正常裁到 3 个。
      新增 `tests/test_restricted_group_color_exempt.py` 10 条测试。

## 待完成

- [ ] 软删除恢复：被删除的商品可从"回收站"恢复（目前删了就没了）
- [ ] 批量发布：自动化结果页支持一次选多个商品发布（当前只能逐个）
- [ ] 图片本地上传：用户可以在仓库直接上传本地图片替换（当前只能输入 URL）
- [ ] 自动化结果导出：导出为 Excel 便于离线核对
