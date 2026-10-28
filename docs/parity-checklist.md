# 功能对齐清单（新桌面端 vs 0.1.4 老版）

审查用。左列是老版真实能力，右列是新实现状态，实施完成后逐项打勾。

## A. 连接与账号

| # | 老版能力 | 老版接口 | 新版状态 |
|---|---|---|---|
| A1 | 输入 AppKey/AppSecret 连接妙手 | `/api/connect` | `POST /auth/connect` |
| A2 | 连接后拉店铺列表 | `/api/shops` | `GET /shops` |
| A3 | 保存多个连接（凭据库） | `/api/save-connection` | `POST /auth/connect` + `GET /auth/accounts` |
| A4 | 切换已保存连接 | `/api/connect-saved` | `GET /auth/accounts` + connect |
| A5 | 重命名连接 | ✅ `PATCH /auth/accounts/{id}` |
| A6 | 顶部显示当前账号 + 店铺 | bootstrap `connected` | `GET /auth/status` |
| A7 | 凭据加密存储 | Windows Credential Manager | Fernet（本机 secret 派生） |
| A8 | 多人各自 key（共享服务器） | ✅ X-Miaoshou-AppKey/Secret 请求头 |

## B. 采集箱

| # | 老版能力 | 新版状态 |
|---|---|---|
| B1 | 列表分页（pageSize ≤20） | `GET /collect-box` |
| B2 | 关键词搜索 | 参数 `keyword` |
| B3 | 状态筛选 | 参数 `status` |
| B4 | 商品详情（主图/详情图/SKU） | `GET /collect-box/{id}` |
| B5 | 单条认领到店铺 | `POST /collect-box/{id}/claim` |
| B6 | 批量认领 | `POST /collect-box/claim` |
| B7 | 诊断（标题/类目/颜色问题） | `GET /collect-box/{id}?mode=diagnose` |
| B8 | 处理入仓 | `POST /collect-box/{id}/process` |
| B9 | 删除采集箱商品 | ✅ `POST /collect-box/delete` |

## C. 成品仓库（核心）

| # | 老版能力 | 新版状态 |
|---|---|---|
| C1 | 列表分页 | `GET /warehouse` |
| C2 | 类目筛选（chips） | facets.categories |
| C3 | 自定义分组筛选 | facets.groups |
| C4 | 关键词搜索 | 参数 `q` |
| C5 | 只显示未采集 | 参数 `claimed` |
| C6 | 商品卡片（图/标题/价格） | 前端待实现 |
| C7 | 多选 + 全选/取消全选 | 前端待实现 |
| C8 | 批量改分类 | `POST /warehouse/batch-category` |
| C9 | 批量删除 | `POST /warehouse/batch-delete` |
| C10 | 新增自定义分类 | `POST /groups` |
| C11 | 改单个商品分类 | `PATCH /warehouse/{id}` |
| C12 | 认领到采集箱 | `POST /publish` 或 claim |
| C13 | 主图上限 9 张 | 入库截断 ✅ |
| C14 | 价格区间显示 | priceMin/priceMax ✅ |
| C15 | 已采集标记 | `collected` 字段 ✅ |
| C16 | 统计（总数/已认领/已发布） | stats ✅ |

## D. 商品处理

| # | 老版能力 | 新版状态 |
|---|---|---|
| D1 | 标题翻译（中→马来语） | ✅ 已接入 process（失败降级保留原文） |
| D2 | 违规词检测 | ✅ remove 类自动删、review 类记录到 payload.violations |
| D3 | 类目 AI 匹配 | `POST /categories/sync` + `GET /categories` |
| D4 | 颜色规范化 | ✅ clean_color 已接入（Y18[beige] → Beige） |
| D5 | 重量/包装计算 | process 内置 |
| D6 | 定价模板套用 | `GET/POST /system/templates` |
| D7 | 保存草稿到妙手 | process |

## E. 图片翻译

| # | 老版能力 | 新版状态 |
|---|---|---|
| E1 | 单图翻译 | `POST /images/translate` |
| E2 | 批量翻译 | 同上（batch） |
| E3 | 任务查询 | `GET /images/jobs/{id}` |
| E4 | 同图去重（不重复扣费） | `reused` ✅ |
| E5 | 预算上限（10 元） | `IMAGE_BUDGET_EXCEEDED` ✅ |
| E6 | 限流 | `GET /images/limits` ✅ |
| E7 | 结果文件下载 | `GET /images/jobs/{id}/file` |
| E8 | 中文残留 OCR 校验 | ⏸ P2（需要 OCR 引擎，可延后） |

## F. 发布

| # | 老版能力 | 新版状态 |
|---|---|---|
| F1 | 单个发布 | `POST /publish` |
| F2 | 批量发布（≤200） | 同上 |
| F3 | 必须 confirm | `CONFIRM_REQUIRED` ✅ |
| F4 | 同店铺直发 | `mode=direct` ✅ |
| F5 | 跨店铺载体重建 | `mode=carrier` ✅ |
| F6 | 定价模板重算 | template ✅ |
| F7 | 发布记录 | `GET /publish/logs` |
| F8 | 回执刷新 | `POST /publish/logs/refresh` |
| F9 | 站点语言校验 | `SITE_LANGUAGE_MISMATCH` ✅ |

## G. 定时发布

| # | 老版能力 | 新版状态 |
|---|---|---|
| G1 | 任务 CRUD | `/schedules` ✅ |
| G2 | 每天/每周/每月/日期区间 | frequency ✅ |
| G3 | 每次数量 | perRun ✅ |
| G4 | 启用/停用 | enabled ✅ |
| G5 | 立即执行 | `POST /schedules/{id}/run` |
| G6 | 后台定时扫描 | ✅ 60s 循环 + run_due() |

## H. 违规词库

| # | 老版能力 | 新版状态 |
|---|---|---|
| H1 | 列表/搜索 | `GET /violation-words` |
| H2 | 添加（单条/批量） | `POST /violation-words` |
| H3 | 删除 | `DELETE` |
| H4 | 启用开关 | enable/disable |
| H5 | 导入种子词 | `POST /violation-words/seed` |

## I. 系统

| # | 老版能力 | 新版状态 |
|---|---|---|
| I1 | 检查更新 | `GET /system/version` + `POST /system/update-check` |
| I2 | 软件内下载安装 | `POST /system/update` |
| I3 | 版本号展示 | `system/version` ✅ |
| I4 | 健康检查 | `/api/system/health` ✅ |

## 优先级定义
- **P0**：核心流程阻断，必须处理
- **P1**：影响多人协作或自动化，交付前处理
- **P2**：增强项，可延后
