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

## 已完成（v0.1.25 续，2026-11-18 下午）

- [x] **多店铺发布失败根因**：多店铺账号复制出的载体是 `editModel="site"`、
      `collectBoxDetailShopList=[]`，写入前没有「预发布店铺」，妙手报
      「店铺ID【xxx】未设置为预发布店铺」。单店铺账号之所以正常，是妙手复制时
      自动绑了唯一那个店铺。修法：写入前显式 `claim_to_shop([detailId], [shopId])`。
- [x] **载体读接口按 editModel 选**：`site` 维度必须用 `site_detail`，用
      `shop_detail` 读会报「未选择预发布店铺」（文案误导，实际是读错接口）。
- [x] **扇出条件** `checkedAccountIds.length > 1` → `> 0`：只勾 1 个账号时
      原来掉进单账号分支，用当前连接账号的 shopId 且不传 accountId。
- [x] **认领价保留拿货价**：仓库卡片显示 `purchasePriceMin/Max`（CNY 拿货价），
      但 payload 里 `skuMap.price` 是入仓时套模板算出的售价，认领直接沿用 →
      22CNY 商品进采集箱变 40+ MYR。新增 `_reset_claim_prices_to_purchase`，
      按目标站点汇率把拿货价换算成本币。
- [x] **五国运费表按站点分离**：原来只有一张 `DEFAULT_FREIGHT_TABLE`（写死 MYR），
      菲律宾/泰国/越南/新加坡全用 MYR 运价当本币算，报价整体失真。
- [x] **补两个丢失的费率字段**：`orderProcessCny`（订单处理服务费）、
      `enterpriseWithholdPercent`（企业代扣税）。原来不在 `DEFAULT_TEMPLATE` 里，
      `normalize_template` 静默丢弃，菲律宾少算 0.52 CNY、越南少算 1%。
- [x] **尾程服务税基数**：从「售价」改为「尾程运费」。马来 47.56 MYR 的展示价里
      该项应为 0.60，原按售价算是 2.69（差 4 倍多）。
- [x] **关税改价内税**：`税前价 × r/(1+r)`。泰国 411.27 THB 的展示价里关税 34.94，
      原按售价直乘得 41.13（高估 18%）。
- [x] **消费税改价内税**：新加坡 27.54×9/109=2.27，原算法得 2.48。
- [x] **提现费基数**：改为「扣完其他费用后的余额」。越南实测 1500.60 与该口径完全一致。
- [x] **按站点选模板**：前端定价模板改成一行五国复选框（马来/菲律宾/泰国/越南/新加坡），
      上方选了哪个国家的店铺，对应国家模板才可勾选；后端 `_template_name_for_site`
      再兜一道（老客户端 / 定时任务路径）。
- [x] **算价币种跟站点**：`display_price` 返回的 `currency` 不再写死 MYR。

## 已完成（v0.1.26，2026-11-19）

- [x] **P0 前端语言拦截是死逻辑**：`ff07096` 起后端 `check_language` 就不在调用链上，
      前端 `PublishDialog.tsx` 却按 `mismatch && !allowMismatch` 拦人，提示「会被后端拒绝
      422」是假话；`language` / `exchangeRate` 两个 state 也从没进提交体。
      已删三块 state（`language`、`exchangeRate`、`allowMismatch`）与开关、`mismatch`
      计算、提交体里的 `allowMismatch`。理由：一个发布任务可能同时发五个国家，
      单个 `language` 字段从结构上就不成立，站点差异由各店铺自己的定价模板承担。
      后端 `check_language` 与 `PublishRequest.allowMismatch` 保留，供将来真做站点强校验。
- [x] **P1 定时发布页 role 门控漂移**：`SchedulesPage.tsx:698` 从
      `identity?.role !== "member" && accounts.length > 1` 改成 `accounts.length > 1`，
      与发布弹窗同一判据；顺带删掉不再使用的 `useOperatorIdentity` 导入。
- [x] **P1 构建缓存被跟踪**：`git rm --cached web/tsconfig.tsbuildinfo`。
- [x] **P1 根目录 `main.js` 过期副本**：确认无引用（`app/package.json` 的 `main` 指向
      `dist/main.js`，源头是 `app/src/main.ts`）后 `git rm main.js`。
- [x] **P1 `.claude/launch.json` 进仓库**：untrack 并写进 `.gitignore`。
- [x] **P1 发布明细看不到售价**：`publish_job_service.run` 从 `result["repriced"]` 取
      `priceAfter` 写进 `BatchItem.message`，明细显示「已提交 · 售价 47.56 MYR」。
      顺带修掉 `publish_service._repricing` 里写死 `currency: "MYR"` 的 bug ——
      发菲律宾店会显示成马币，用户按错的钱数做判断。

## 已完成（v0.1.27，2026-11-19）

- [x] **P2 单店重试**：`POST /publish-jobs/{id}/retry` + 明细页「重试失败店铺」按钮。
      `publish_job_service.retry()` 从 `BatchItem` 逐件真状态里挑 `failed`/`pending`，
      按店铺归集成 `settings.runPlan`，批次改回 pending 后投递 `run()`。
      已成功的一件都不碰（发布不可撤销，整批重发会多建商品）。
      `run()` 改成读 `_run_plan(settings)`：有 runPlan 只跑它，否则跑完整叉乘；
      老批次缺 `targets`/`warehouseIds` 时从 `detail_id` 的 `<shopId>:<warehouseId>` 还原。
- [x] **顺带修掉「按店铺粗判成败」**：原来 `ok_count > 0` 就把该店铺所有件记成成功，
      5 件里成功 4 件时第 5 件被误标「已提交」，用户点重试也补不回来。
      现在逐件读 `result["records"]["items"][].warehouseId/status`。
- [x] **顺带修掉 results 重复**：重试成功后同一件在 `results` 里会留两条
      （failed + ok），成功/失败计数翻倍。改成按 `detailId` 原地覆盖（`_merge_result`）。
- [x] **顺带修掉批次状态**：重试修好失败店之后批次要变 `done`，不能停在 failed。
- [x] **连点重试不投两次**：`hold_for_session("publish-job-retry:<account>:<batch>")`。
- [x] **前端进度口径分开**：发布任务按 `progress.attempted` 算进度（重试用子集），
      认领任务仍按组推进；发布任务的 `ok` 显示「已提交」而不是「已导入采集箱」。
- [x] 测试：`server/tests/test_publish_job_retry.py`（8 条全绿）。
- [x] 顺手修掉 `test_role_permissions.py` 里 3 条基线失败：`/publish-jobs` 三个路由
      与 `DELETE /claim-jobs/{id}` 加进 `DYNAMIC_WRITE_ROUTES`（这些路由挂的是
      `publish_claimed`，处理函数里按 account 归属 + 操作员隔离，和 `/claim-jobs` 同性质）。

## 待完成

- [ ] **P1 定时任务每账号只能一个店铺**：`ScheduleCreate` 只有一个 `shopId`，
      要支持「一个定时任务发多个店铺」得改数据模型（一条任务拆多条 schedule），
      属独立需求，本次未做。
      > 2026-11-19 更新：已通过 `schedules.targets` 列实现（迁移 `20261119_0001`），本条已完成。
- [ ] **P2 多账号共用同一个模板**：扇出时若某站点没勾模板会传 null，
      部分商品按原价上架，需要在结果区明确提示。
- [ ] **P2 发布任务实时推送**：现在靠批次页 3 秒轮询，量小时够用，不建议做。

## 相关 Bug 记录

| ID | 现象 | 原因 | 修复版本 |
| --- | --- | --- | --- |
| BUG-P04 | 多店铺账号发布必失败「载体全部写入失败」 | 载体是 site 维度、无预发布店铺 | v0.1.25 |
| BUG-P05 | 只勾 1 个账号发到错误账号 | 扇出条件写成 `> 1` | v0.1.25 |
| BUG-P06 | 认领到采集箱价格莫名变高 | 沿用入仓时套模板算出的售价 | v0.1.25 |
| BUG-P07 | 菲律宾店铺价格只有正确值一半 | 用马来模板（MYR 费率）算 PHP 价 | v0.1.25 |
| BUG-P08 | 五国定价与妙手官方试算差 15%~36% | 运费表单一 + 缺字段 + 3 处公式基数错 | v0.1.25 |
| BUG-P09 | 5 店发 10 件、1 店失败只能整批重发 | 缺单店重试接口，重发会把成功的店再发一遍 | v0.1.27 |
| BUG-P10 | 部分成功时未成功的那件被记成「已提交」 | `run()` 按 `ok_count > 0` 判整店成败 | v0.1.27 |
| BUG-P11 | 重试后进度条显示「已处理 10/3」、成败计数翻倍 | `results` 用 append 而非覆盖；分母用 `detailIds.length` | v0.1.27 |
