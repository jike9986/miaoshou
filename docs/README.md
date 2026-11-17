# 奥皇上品 · 文档索引

> **新对话必读**：先看根目录 [CLAUDE.md](../CLAUDE.md)，里面有强制规则和部署流程。

---

## 项目规划（必须遵守）

| 文档 | 内容 |
|---|---|
| [../CLAUDE.md](../CLAUDE.md) | AI 助手必读：规则速查、版本号要求、部署命令、常见 bug 诊断 |
| [project/rules.md](project/rules.md) | 完整约束：版本管理、代码规范、禁止事项、发版顺序 |
| [../DEPLOY.md](../DEPLOY.md) | 运维手册：服务器配置、nginx、环境变量、TLS 证书 |
| [architecture.md](architecture.md) | 系统架构：进程图、分层职责、运行时事实 |

---

## 功能开发资料

每个功能目录包含三个文件：
- `requirements.md` — 需求说明（用户视角、验收标准）
- `spec.md` — 技术方案（关键文件、数据模型、算法）
- `tasks.md` — 任务清单（已完成 / 待完成 / Bug 记录）

| 功能 | 目录 | 当前版本 |
|---|---|---|
| 自动化处理 | [features/automation/](features/automation/) | v0.1.24（7 个 bug 已修） |
| 成品仓库 | [features/warehouse/](features/warehouse/) | v0.1.24 |
| 采集箱 | [features/collect-box/](features/collect-box/) | v0.1.24 |
| 发布记录 | [features/publish/](features/publish/) | — |

---

## 其他参考文档

| 文档 | 内容 |
|---|---|
| [api.md](api.md) | 全部 HTTP 接口契约 |
| [migrations.md](migrations.md) | Alembic 迁移用法 |
| [design-system.md](design-system.md) | 设计令牌与组件规范 |
| [HANDOFF.md](HANDOFF.md) | 历史交接备忘（踩过的坑） |
| [BUSINESS_RULES.md](BUSINESS_RULES.md) | 业务规则（定价、发布、违规词） |
| [PRODUCT_STANDARD.md](PRODUCT_STANDARD.md) | 商品数据标准 |
| [package-notes.md](package-notes.md) | 打包相关备忘 |
| [env-issues.md](env-issues.md) | 环境问题记录 |
