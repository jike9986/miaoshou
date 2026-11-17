# 采集箱 · 技术方案

## 关键文件

| 文件 | 职责 |
|---|---|
| `web/src/features/collect-box/CollectBoxPage.tsx` | 采集箱列表页 |
| `server/services/collect_box_service.py` | 从妙手拉取、同步商品 |
| `server/api/collect_box.py` | HTTP 路由 |
| `server/workers/image_worker.py` | 后台图片任务轮询（20s 间隔） |

---

## 图片处理后台线程

`collect_box.py` 中有后台线程处理图片任务：
- 轮询 `automation_images` 表，检查图片翻译任务状态
- 任务完成后写回 `product.automation_result`，同步 `draft_verified_revision`
- 间隔 20 秒，异常有堆栈日志，不静默吞掉

---

## API 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/collect-box` | 采集箱列表（分页） |
| POST | `/api/v1/collect-box/sync` | 从妙手同步最新商品 |
| POST | `/api/v1/collect-box/items/{id}/process` | 单条入库 |
| POST | `/api/v1/collect-box/batch-process` | 批量入库（进入自动化流水线） |
