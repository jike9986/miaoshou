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

## 待完成

- [ ] 软删除恢复：被删除的商品可从"回收站"恢复（目前删了就没了）
- [ ] 批量发布：自动化结果页支持一次选多个商品发布（当前只能逐个）
- [ ] 图片本地上传：用户可以在仓库直接上传本地图片替换（当前只能输入 URL）
- [ ] 自动化结果导出：导出为 Excel 便于离线核对
