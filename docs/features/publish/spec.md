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

---

## 五国定价（2026-11-18）

### 官方运费表（按站点分离，币种各异）

| 站点 | 币种 | 起重 | 起重价 | 续重单位 | 续重价 |
| --- | --- | --- | --- | --- | --- |
| MY | MYR | 30g | 0.06 | 10g | 0.08 |
| PH | PHP | 10g | 5.00 | 10g | 4.50 |
| TH | THB | 10g | 0.40 | 10g | 0.40 |
| VN | VND | 10g | 500 | 10g | 500 |
| SG | SGD | 40g | 0.98 | 10g | 0.15 |

代码：`price_template.OFFICIAL_FREIGHT_TABLES`，`official_freight_table(site)` 取表。
模板里显式给了 `freightTable` 时以模板为准，否则按站点取官方表。

### 五套模板参数（利润统一 15%）

| 模板 | 站点 | 尾程运费CNY | 主要费率 |
| --- | --- | --- | --- |
| 马来 | MY | 16.17 | 交易3.78% / VAT10% / BCP3.24% / 平台支持0.81 / 尾程税6% |
| 菲律宾 | PH | 6.29 | 交易2.24% / 包邮5.5% / 订单处理0.52 |
| 泰国 | TH | 15.55 | 交易3.21% / VAT7% / 关税10% / 电商增长8.03% / 平台支持0.21 |
| 越南 | VN | 7.79 | 交易6% / VAT10% / 订单处理0.78 / 代扣税1% |
| 新加坡 | SG | 0 | 交易3.27% / 消费税9% |

### 三处容易算错的基数

1. **尾程服务税**：基数是尾程运费，不是售价
   `service_tax = last_mile_local × rate`
   （马来 47.56 MYR 展示价里该项 0.60；按售价算会得 2.69，差 4 倍）
2. **关税**：价内税，`关税 = 税前价 × r / (100 + r)`
   （泰国 411.27 里关税 34.94；按售价直乘得 41.13，高估 18%）
3. **提现费基数**：扣完其他所有费用后的余额
   `base = 税前价 - 交易 - BCP - 平台支持 - 佣金 - SFP - 达人 - 关税 - 代扣税 - 订单处理`
   （越南 220312.85 里提现 1500.60，与该口径完全一致）

### 按站点选模板

前端：`PublishDialog` 一行五国复选框。`selectedShopSites` 算上方选中店铺的站点集合，
只有该集合里的国家才可勾选；勾了才套模板，没勾传 `null` 不套模板。

后端：`publish_service._template_name_for_site(db, name, site)` ——
传进来的模板站点与目标店铺站点不一致时，换同站点模板（找不到就保持原样）。
兜住老客户端与定时任务这类没有前端参与的路径。

### 载体绑定预发布店铺

多店铺账号从公共箱复制出的载体是 `editModel="site"`、`collectBoxDetailShopList=[]`，
写入前必须 `claim_to_shop([detailId], [shopId])` 绑到目标店铺，否则报
「店铺ID【xxx】未设置为预发布店铺」。单店铺账号妙手会自动绑，所以看不到这个问题。

读载体也要按维度：`shop` 用 `get_shop_collect_item_info`，
`site` 用 `get_site_collect_item_info`（`carrier_service._carrier_snapshot` 自动选）。

### 认领价格

认领到采集箱时 `skuMap.price` 重置为「拿货价 ÷ 目标站点汇率」，不套定价模板。
真正的定价发生在妙手那边套模板（负责人 2026-11-18 明确）。
函数：`collect_box_service._reset_claim_prices_to_purchase`。
