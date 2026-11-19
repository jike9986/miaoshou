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

## 待完成

- [ ] **P0 前端语言拦截已成死逻辑**：`ff07096` 只注释了后端 `check_language`，
      前端 `PublishDialog.tsx` 仍按 `mismatch && !allowMismatch` 拦，提示「会被后端
      拒绝 422」已是假话。且 `language` / `exchangeRate` 两个 state 根本没进提交体。
- [ ] **P1 定时发布页未同步**：`SchedulesPage.tsx` 仍是 role 门控 + 每账号单选一个店铺，
      没有「多店铺 + 按站点选模板」能力。
- [ ] **P1 提交里混入构建噪音**：`web/tsconfig.tsbuildinfo` 自 `d4d1df8` 起被跟踪，
      虽在 `.gitignore` 但已跟踪不受影响，每次构建都产生 diff。需 `git rm --cached`。
- [ ] **P1 根目录 `main.js` 是过期副本**：与 `app/dist/main.js` 内容不同，真实入口是
      `app/src/main.ts`。确认无引用后删除。
- [ ] **P2 多账号共用同一个模板**：扇出时若某站点没勾模板会传 null，
      部分商品按原价上架，需要在结果区明确提示。

## 相关 Bug 记录

| ID | 现象 | 原因 | 修复版本 |
| --- | --- | --- | --- |
| BUG-P04 | 多店铺账号发布必失败「载体全部写入失败」 | 载体是 site 维度、无预发布店铺 | v0.1.25 |
| BUG-P05 | 只勾 1 个账号发到错误账号 | 扇出条件写成 `> 1` | v0.1.25 |
| BUG-P06 | 认领到采集箱价格莫名变高 | 沿用入仓时套模板算出的售价 | v0.1.25 |
| BUG-P07 | 菲律宾店铺价格只有正确值一半 | 用马来模板（MYR 费率）算 PHP 价 | v0.1.25 |
| BUG-P08 | 五国定价与妙手官方试算差 15%~36% | 运费表单一 + 缺字段 + 3 处公式基数错 | v0.1.25 |
