# 成品仓库 · 技术方案

## 关键文件

| 文件 | 职责 |
|---|---|
| `web/src/features/warehouse/WarehousePage.tsx` | 仓库列表页 |
| `web/src/features/warehouse/ProductDetailPage.tsx` | 商品详情抽屉（含 SKU 表格） |
| `web/src/features/warehouse/AutomationResultsPage.tsx` | 自动化结果标签页 |
| `server/services/warehouse_service.py` | 核心业务：增删改查、质检、发布 |
| `server/api/warehouse.py` | HTTP 路由（含图片 patch） |
| `server/models/product.py` | ORM 模型 |

---

## SKU 维度映射（重要）

妙手返回的 `skuMap` key 格式是将每个 SKU 的所有维度 `attrValueId` 按字符串升序排列后拼接，**不是**按维度定义顺序。

错误写法（按位置查找）：
```typescript
// 危险：当 skuMap key 顺序与 props 顺序不同时会查错
const label = props[i].attrValueList?.[partIndex]?.attrValue;
```

正确写法（全局 id→label Map）：
```typescript
const flatIdToLabel = new Map<string, string>();
for (const record of props) {
  for (const item of record.attrValueList ?? []) {
    flatIdToLabel.set(String(item.attrValueId), String(item.attrValue ?? ""));
  }
}
// 查找时：
const dims = keyParts.map(part => flatIdToLabel.get(part) ?? part);
```

---

## draft_verified_revision 生命周期

```
自动化处理完成
  → finish_process() 写 automation_result
  → 设 draft_verified_revision = revision
  → 商品出现在自动化结果页

用户编辑商品（图片/重量/尺寸/分类/标题）
  → revision_guard() 递增 revision
  → 若未同步：draft_verified_revision < revision → 商品从结果页消失！
  → 修复：每个写接口在 revision_guard 后补：
    if product.automation_result and product.draft_verified_revision is not None:
        product.draft_verified_revision = int(product.revision or 0)
```

需要同步的接口（已修复 v0.1.24）：
- `warehouse_service.review_product_quality()` — 编辑重量/尺寸
- `warehouse_service.update()` — 编辑分类/分组/标题
- `warehouse.patch_product_images()` — 编辑图片

---

## API 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/warehouse/products` | 商品列表（分页+筛选） |
| GET | `/api/v1/warehouse/products/{id}` | 商品详情 |
| PUT | `/api/v1/warehouse/products/{id}` | 更新基本信息 |
| POST | `/api/v1/warehouse/products/{id}/review` | 质检（重量/尺寸） |
| PATCH | `/api/v1/warehouse/products/{id}/images` | 更新图片 |
| DELETE | `/api/v1/warehouse/products/{id}` | 软删除 |
| GET | `/api/v1/warehouse/groups` | 分组列表 |
| POST | `/api/v1/warehouse/groups` | 创建分组 |
