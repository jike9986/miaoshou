# 项目规范

> 本文是强制约束，不可因"方便"而绕过。每次修改代码前先读相关章节。

---

## 0. 文档及时更新（最高优先级）

**用户说的所有有用信息，当次对话里就要写进文档。不只是"规范"——任何业务背景、偏好、限制、临时决定、发现的问题都算。对话结束前检查一遍有没有漏。**

| 用户说的内容 | 更新目标文档 |
|---|---|
| 新 bug / 已修复问题 | `docs/features/{功能}/tasks.md` |
| 新业务规则/约束/偏好 | `docs/project/rules.md` + `docs/features/{功能}/spec.md` |
| 需求变更 / 新验收标准 | `docs/features/{功能}/requirements.md` |
| 部署/环境信息 | `CLAUDE.md` 和/或 `DEPLOY.md` |
| 技术方案变更 | `docs/features/{功能}/spec.md` |
| 临时决定/背景说明/特殊情况 | `docs/features/{功能}/spec.md` 或 `docs/project/rules.md` |

不更新 = 下次对话丢失 = 等于白说。

---

## 1. 版本管理

### 版本号格式
`0.MINOR.PATCH` — 修 bug 加 PATCH，新功能加 MINOR，不用 MAJOR。

### 四处必须同步
```
package.json                     "version": "0.1.xx"
web/package.json                 "version": "0.1.xx"
app/package.json                 "version": "0.1.xx"
server/core/constants.py         return "0.1.xx"
```

验证命令（输出应全部一致）：
```bash
grep '"version"' package.json web/package.json app/package.json
grep 'return "0\.' server/core/constants.py
```

---

## 2. 代码规范

### Python（server/）
- 所有时间戳用 `now_ts()`（Unix 秒，`REAL`），不用 `datetime`
- 并发写商品必须调 `revision_guard(db, product, client_revision)`
- 编辑商品任意字段后，若 `product.automation_result` 存在且 `draft_verified_revision is not None`，立即同步：
  ```python
  product.draft_verified_revision = int(product.revision or 0)
  ```
- 错误用 `raise errors.AppError("CODE", "中文说明", status=4xx)`，不要裸 `raise HTTPException`
- 新增 API 路由必须在 `docs/project/api-changelog.md` 记录

### TypeScript（web/）
- 服务端状态全用 TanStack Query，不用 `useState` 存服务器数据
- 修改后调 `queryClient.invalidateQueries({ queryKey: queryKeys.xxx.all })`，不要手动刷页面
- 组件只用 Tailwind 类名，不写 inline style
- 新查询键在 `src/api/queryKeys.ts` 统一注册

### 禁止事项
- 不往代码里硬编码密钥、密码、API key
- 不直接 `git reset --hard` 或 `rm -rf`（除非用户明确要求）
- 不在 Electron 渲染进程里直连数据库
- 不绕过 `revision_guard` 修改 `product.revision`

---

## 3. 分支与提交

- 主分支：`main`
- 修 bug：`fix/简短描述`
- 新功能：`feat/简短描述`
- 提交消息末尾加：`Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

---

## 4. 构建与部署

### 前端构建
```bash
# 必须在 bash（不是 PowerShell）
cd /d/缓存文件夹/codex工作台/p006/miaoshou
pnpm build
```

产物在 `release/win-unpacked/`。

### 桌面端部署（本地）
```bash
cmd /c scripts\\do_deploy.bat
```

脚本会：停止旧进程 → 覆盖 exe + app.asar + resources/web → 完成。用户手动重启应用。

### 后端部署（服务器）
```bash
python scripts/deploy_server.py
```

脚本会：SSH 上传 4 个 Python 文件 → `systemctl restart aohuang-listing-v2` → 验证 `active`。

SSH 凭据：`C:\Users\Administrator\Desktop\雨云.txt`（最后一行是密码）。

### 完整发版顺序
1. 四处同步版本号
2. `pnpm build`（构建前端+Electron）
3. `python scripts/deploy_server.py`（部署后端）
4. `cmd /c scripts\\do_deploy.bat`（部署桌面端）

---

## 5. 数据库

- 生产：PostgreSQL 14，`127.0.0.1:5432/miaoshou_listing_v2`（服务器本地）
- 开发：SQLite WAL，`%LOCALAPPDATA%\aohuang-desktop\app.db`
- 迁移用 Alembic，见 `docs/migrations.md`
- 不要在迁移脚本里写业务逻辑

---

## 6. 外部依赖

| 服务 | 用途 | 限制 |
|---|---|---|
| 妙手开放平台 | 商品读写、发布 | 账号级 1.5s 节流，HMAC-SHA256 签名 |
| 图片翻译 API | 图片文字翻译 | 费用上限 10 元/批次，异步任务 |
| DeepSeek API | 颜色规范化翻译 | key 在服务器环境变量 |
| aohuang888.com | 更新包分发 | nginx 反代，Let's Encrypt 证书 |
