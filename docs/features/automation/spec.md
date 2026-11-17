# 自动化处理 · 技术方案

## 关键文件

| 文件 | 职责 |
|---|---|
| `web/src/features/automation/AutomationPage.tsx` | 主页面：运行列表、创建运行、进度轮询 |
| `web/src/features/automation/ProgressPanel.tsx` | 进度面板：批次进度条、单商品状态列表 |
| `web/src/features/automation/RunHistoryPanel.tsx` | 右侧历史面板：运行列表 |
| `server/services/automation_run_service.py` | 核心业务：创建/推进/完成运行 |
| `server/api/automation.py` | HTTP 路由 |
| `server/models/automation_run.py` | ORM 模型 |

---

## 数据模型

### AutomationRun
```
id, account_id, operator_id
status: pending|running|completed|failed
item_count, done_count, fail_count
image_planned, image_done, image_failed
created_at, updated_at, finished_at
```

### AutomationRunItem
```
id, run_id, key (商品唯一标识)
process_status: queued|running|succeeded|needs_review|failed
process_reason: 失败原因
publish_status: queued|running|succeeded|needs_review|failed
publish_reason
created_at, updated_at
```

### Product（成品仓库）
```
...
automation_result: JSON  ← 妙手草稿写回内容
draft_verified_revision: Int  ← 与 revision 相等时才在自动化结果页显示
revision: Int  ← 每次写操作自动递增（revision_guard）
```

---

## 核心算法

### 进度计算（ProgressPanel.tsx）
```typescript
// effectivelyDone 在 textPercent/imagePercent/batchPercent 之前计算
const effectivelyDone =
  run.phase === "done" ||
  (run.phase === "stopped" && run.total > 0 && run.done >= run.total);

const textPercent = settings.textEnabled && run.total > 0
  ? ((run.done) / run.total) * 100
  : 0;

// 已完成态锁定 100%，防止状态重置后显示 0% 或 50%
const imagePercent = effectivelyDone && settings.imagesEnabled
  ? 100
  : run.imagePlanned > 0
    ? (run.imageDone / run.imagePlanned) * 100
    : !running && run.imagePlanned === 0 ? 100 : 0;

const batchPercent = effectivelyDone
  ? 100
  : bothEnabled
    ? (textPercent + imagePercent) / 2
    : settings.imagesEnabled ? imagePercent
    : run.total > 0 ? (run.done / run.total) * 100 : 0;
```

**进度条动画**：setInterval 1%/50ms 匀速逼近（不再用 rAF 一步跳），CSS transition 150ms（原 700ms）。

### start_item()（服务器）
- 接受 `queued | needs_review | failed` 三种状态（失败可重试）
- 切换为 `running`，同时把 `publish_status` 从 `failed/needs_review` 重置为 `queued`

### finish_process()（服务器）
- 核验 `product.draft_verified_revision == product.revision`
- 若 `automation_images` 记录存在（图片处理中），跳过该核验（图片线程负责最终核验）

### 编辑后 draft_verified_revision 同步
所有修改商品的接口（warehouse_service.review_product_quality, update, warehouse.patch_product_images）在写完后：
```python
if product.automation_result and product.draft_verified_revision is not None:
    product.draft_verified_revision = int(product.revision or 0)
```

### SKU 维度映射
妙手 skuMap 的 key 格式：各维度 attrValueId 按 ID 字符串升序排列（不是按维度定义顺序）。
解法：用全局 `flatIdToLabel: Map<string, string>` 把所有 attrValueId → 中文标签，然后按 key 拆分部分逐一查找。

---

## 缓存失效时机

运行结束时（AutomationPage.tsx）必须同时刷新：
```typescript
void queryClient.invalidateQueries({ queryKey: queryKeys.automationRuns.all });
void queryClient.invalidateQueries({ queryKey: queryKeys.warehouse.all });
```

缺少 `warehouse.all` 会导致成品仓库看不到新完成的商品。

---

## API 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/automation/runs` | 创建运行 |
| GET | `/api/v1/automation/runs` | 运行列表 |
| GET | `/api/v1/automation/runs/{id}` | 运行详情（含 items） |
| POST | `/api/v1/automation/runs/{id}/start` | 推进运行 |
| POST | `/api/v1/automation/runs/{id}/items/{key}/start` | 开始单个商品 |
| POST | `/api/v1/automation/runs/{id}/items/{key}/finish` | 完成单个商品 |
| POST | `/api/v1/automation/runs/{id}/items/{key}/fail` | 标记单个商品失败 |
