# 发布到店铺 · 任务清单

## 已完成（v0.1.25，2026-11-18）

- [x] 多店铺勾选放开角色限制：`isTeacher` 从 `identity?.role !== "member" && accounts.length > 1` 改为 `accounts.length > 1`（`8cbe89b`）。所有多账号用户都能看到账号复选 + 每账号独立店铺多选，不再只给老师/管理员。
- [x] 店铺复选框状态更新改函数式：`prev` 从闭包外层取值改为在 `setSelectedShopIdsByAccount(s => …)` 内部取，避免连点多个店铺时被旧快照覆盖（`8cbe89b`）。
- [x] 清理 `PublishDialog.tsx` 中已无用的 `useOperatorIdentity` 导入。
- [x] 移除 `sourceShopIds` 从 props 接口（实现仍在 `WarehousePage` 内部用于跨店载体判断，`3ed4c66`）。
- [x] **扇出条件修正（2026-11-18 补）**：`checkedAccountIds.length > 1` → `> 0`。原来只勾 1 个账号会掉进单账号分支，用当前连接账号的 `shopId` 且不传 `accountId`，结果「勾了账号 B，实际发到账号 A」。这直接导致多店铺勾选形同虚设。
- [x] 按钮文案改为按实际提交条数统计：`向 N 个店铺发布 M 件`（不再按账号数说「向 10 个账号发布」，避免严重低估工作量）。
- [x] 扇出失败提示带上「账号名 / 店铺名：原因」，最多列 3 条 + 「等 N 条」（原来只透出 `failMsgs[0]`）。
- [x] 未选店铺时的扇出失败也算作失败项（原来 `if (!publishShopId) continue` 静默跳过，用户以为发了其实没发）。
- [x] 多账号区提示文案改为「每个账号独立发布；多店铺账号可勾选多个店铺一起发」。

## 待完成
- [ ] ~~**P0 只勾 1 个账号时发到错误账号**~~（已于 2026-11-18 修复，见上方「扇出条件修正」）
- [ ] **P0 前端语言拦截已成死逻辑**：`ff07096` 只注释了后端 `check_language`（`publish_service.py:1108-1112`），前端 `PublishDialog.tsx:217` 仍按 `mismatch && !allowMismatch` 拦截，第 589 行还提示「会被后端拒绝 422」。且 `language` / `exchangeRate` 两个 state 根本没进提交体。要么补传参并放开拦截，要么整块删掉。
- [ ] **P1 定时发布页未同步**：`SchedulesPage.tsx:698` 仍是 `identity?.role !== "member" && accounts.length > 1`；且每账号只能单选 1 个店铺（第 1009 行 `Select`），没有多店铺能力。建议抽 `AccountShopPicker` 组件两页共用。
- [ ] **P1 提交里混入构建噪音**：`8cbe89b` 带进了 `.claude/launch.json` 与 `web/tsconfig.tsbuildinfo`。后者虽在 `.gitignore:69` 但自 `d4d1df8` 起就被跟踪，gitignore 对已跟踪文件无效。需 `git rm --cached`。
- [ ] **P1 根目录 `main.js` 是过期副本**：21803 B，与 `app/dist/main.js`（21595 B）内容不同，真实入口是 `app/src/main.ts`。确认无引用后删除。
- [ ] **P2 多账号共用同一个模板**：扇出时 `template: template || null` 是全局单值，各账号无法分别指定定价模板。
- [ ] **P2 扇出失败只显示第一条**：`toast.error` 的 description 只透出 `failMsgs[0]`，其余失败原因丢失，应在结果区列全量「账号 × 店铺 → 原因」。
- [ ] **P2 `e.target.checked` 读取时机**：虽是 React 18（不池化事件），仍建议在 `onChange` 开头先落 `const next = e.target.checked` 再用。

## 相关 Bug 记录

| ID | 现象 | 原因 | 修复版本 |
|---|---|---|---|
| BUG-P01 | 学员账号看不到多店铺勾选 | `isTeacher` 用 `role !== "member"` 门控，`member` 被挡 | v0.1.25（`8cbe89b`） |
| BUG-P02 | 连点多个店铺只有一个生效 | `prev` 读自闭包外层快照，连续 `setState` 互相覆盖 | v0.1.25（`8cbe89b`） |
| BUG-P03 | 多店铺勾选用了错误的 state setter | `bb57e8e` 前用了 `setSelectedShopByAccount` | v0.1.25（`bb57e8e`） |
