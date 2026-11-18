# 发布到店铺 · 技术方案

## 关键文件

| 文件 | 职责 |
|---|---|
| `web/src/features/publish/PublishDialog.tsx` | 发布弹窗：账号选择、店铺选择、模板、提交 |
| `web/src/features/publish/PublishPage.tsx` | 发布记录页 |
| `web/src/features/warehouse/WarehousePage.tsx` | 入口页，向弹窗传 `accounts` / `warehouseIds` |
| `web/src/lib/queries.ts` | `usePublish` / `useShops` / `useTemplates` |
| `server/api/publish.py` | `POST /api/v1/publish`，强制 `confirm=true` |
| `server/services/publish_service.py` | 发布主逻辑、语言校验、汇率、模板重算 |

---

## 两种模式

### 单账号模式（`accounts.length <= 1`）

- 「目标店铺」是一个 `Select`，值存 `shopId`（`PublishDialog.tsx:512-522`）
- 店铺数据来自 `useShops(auth?.accountId)`
- 提交走 `publish.mutate`（第 275-285 行），**不带 `accountId`**，后端用当前连接账号
- 打开弹窗时默认店铺 = `defaultShopId`，若店铺列表里没有则取 `shops[0]`（第 160-173 行）

### 多账号模式（`accounts.length > 1`）

- 每个账号一张卡片，卡片内是该账号的店铺复选框（店铺数 > 1 才出复选框；= 1 显示 `→ 店名`；= 0 显示红字）
- 账号勾选存 `checkedAccountIds`，每账号选中的店铺存 `selectedShopIdsByAccount[accountId] = string[]`
- 店铺列表按账号懒加载：`fetchShopsForAccount`（第 109-118 行）用 `GET /shops?accountId=`，一次性缓存到 `shopsByAccount`
- `shopsByAccount[accId]` 三态：`undefined` 未触发 / `null` 加载中 / `Shop[]` 已加载
- 打开弹窗默认全选所有账号（第 154-158 行）
- 提交时逐账号 × 逐店铺串行 fan-out（第 223-267 行）：

```ts
if (isTeacher && checkedAccountIds.length > 1) {
  for (const accId of checkedAccountIds) {
    const shopsToPublish = selectedShopIdsByAccount[accId] ?? [shopsByAccount[accId]?.[0]?.shopId];
    for (const publishShopId of shopsToPublish) {
      await publish.mutateAsync({ items, shopId: publishShopId, accountId: accId, ... });
    }
  }
}
```

⚠️ 未勾选店铺时回落到该账号第一个店铺（第 233 行）。
⚠️ 扇出条件写的是 `> 1`，只勾 1 个账号会掉进单账号分支（见 tasks.md P0）。

---

## 角色门控的历史

| 版本 | 条件 | 后果 |
|---|---|---|
| `< 8cbe89b` | `identity?.role !== "member" && accounts.length > 1` | `member` 即使绑定了多个账号也看不到多选 UI，只能单选店铺 |
| `>= 8cbe89b` | `accounts.length > 1` | 所有多账号用户都能用；后端 `require_account` 仍按 `has_account_access` 校验成员可见性 |

后端不会因为前端放开就放权：`member` 只能操作 `has_account_access` 为真的账号（`account_service.py:54-76`），teacher/admin 全局放行。

---

## 提交体字段（`PublishRequest`）

| 字段 | 弹窗是否传 | 说明 |
|---|---|---|
| `items` | ✅ | `toPublishItems([{warehouseId}])` |
| `shopId` | ✅ | 目标店铺 |
| `accountId` | 仅多账号扇出 | 单账号不传，后端用当前连接账号 |
| `template` | ✅ | 定价模板名，空串 → `null` |
| `allowMismatch` | ✅ | 前端仍拦语言，见 tasks.md P0 |
| `confirmUnverifiedSpecifications` | ✅ 恒 `true` | 未核验规格不再拦截 |
| `claimOnly` | ✅ | 「只认领进采集箱」开关 |
| `confirm` | ✅ 字面量 `true` | 后端 `CONFIRM_REQUIRED` 三重保险 |
| `language` | ❌ | state 有，提交体没传 |
| `exchangeRate` | ❌ | state 有，提交体没传 |

---

## 幂等与去重

- `usePublish` 每次 mutate 生成新的 `idempotencyKey("pub")`（`queries.ts:110-116`），扇出 N 个店铺 = N 个独立 key
- 后端按「商品 × 目标店铺」的 `publish_keys` 去重（`publish_service.py` 内注释），所以并发发到不同店铺不会互相挡
- 连点两次「发布」会产生全新 key，**重复上架不会被拦** —— 需批次级去重（见 tasks.md 待完成）
