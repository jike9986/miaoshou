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

---

## 发布任务化（publish_job_service，2026-11-18）

### 为什么改

负责人反馈：「这五个店铺同时发布产品，会显示正在提交然后没反应了」。

根因不是卡住，是**前端先超时**：后端一个店铺要在妙手重建载体（实测单店 30~60 秒），
5 个店铺串行就是几分钟，而 `usePublish` 的 `timeoutMs` 只有 180 秒。前端断了，
后台还在跑，用户看到按钮一直停在「正在提交…」。

### 任务模型

```
Batch（settings.kind = "publish_job"）
  settings.targets      = [{shopId, template}, ...]     每个店铺一个
  settings.warehouseIds = ["123", "456", ...]            要发的商品
  settings.request      = {language, exchangeRate, confirmUnverifiedSpecifications, ...}
  settings.progress     = {stage, shopIndex, shopTotal, succeeded, failed}
  BatchItem.detail_id   = "<shopId>:<warehouseId>"
```

**`detail_id` 为什么带店铺**：`BatchItem` 上有 `(batch_id, detail_id)` 唯一约束，
而同一件商品要发到多个店铺。只用 warehouseId 的话，第二条就被数据库拒了。

### 创建与执行

```
POST /publish-jobs  → publish_job_service.create()  只落库，几十毫秒返回
                    ↓ background.add_task(run)
                    run() 在后台线程按店铺逐个 publish
                    ↓ 每个店铺完成就写一次 batch.settings.progress
批次页 3 秒轮询 GET /publish-jobs 显示进度
```

**单店失败不拖垮整批**：5 个店里 1 个失败，剩下 4 个照常发。比整批中断有用得多。

**并发保护**：同一账号同一操作员不允许两个在途发布任务（`hold_for_session` 非阻塞锁）。
载体池是共享资源，两个任务同时抢会互相拖慢且难定位。

### 删除接口按 kind 分发

`DELETE /claim-jobs/{batch_id}` 一个入口，`claim_job_service.delete()` 按
`settings.kind` 分发：`warehouse_claim` 走认领台账，`publish_job` 走发布任务台账。
在途任务（pending/running）不让删。

---

## 语言校验已下线（2026-11-18）

**现在的行为**：发布不做语言校验。

**为什么**：
1. 后端 `publish_service.check_language` 自 `ff07096` 起就不在调用链上，函数留着但没人调；
2. 一个发布任务可能同时发 MY/PH/TH/VN/SG 五个站点，语言各不相同，单个 `language`
   字段从结构上就不成立；
3. 前端当时还拿「会被后端拒绝 422 SITE_LANGUAGE_MISMATCH」挡人 —— 而后端根本不校验，
   等于用一句假话拦用户。

**删除的东西**（`PublishDialog.tsx`）：`exchangeRate` state（界面上从来没有输入框）、
`language` state、`allowMismatch` state 与开关、`mismatch` 计算、提交体里的
`allowMismatch`。站点差异由每个店铺各自的定价模板承担（`targets[].template`）。

**保留的东西**：后端 `check_language` / `PublishRequest.allowMismatch` 字段不删，
留给将来真要做站点语言强校验的路径（定时任务、开放接口），只是发布弹窗不再依赖它。

---

## 定时任务一次发多个店铺（2026-11-19）

### 数据模型

`schedules` 表加了一列 `targets`（JSON，可空），迁移 `20261119_0001`：

```
targets = [{"shopId": "111", "template": "马来模板"},
           {"shopId": "222", "template": "菲律宾模板"}]
```

- **空值 = 老任务**，执行时回落到 `shop_id` + `template_name` 这一对，行为完全不变；
- **非空 = 多店铺**，第一个店铺同时写回 `shop_id` / `template_name`，
  列表、日志、回执这些只认老字段的地方一行都不用改。

`schedule_service.targets_of(row)` 是唯一取值入口：targets 为空就回落单店铺，
脏数据（连 shopId 都没有）返回空列表，不会凭空造一个店铺出来。

### 为什么不是一个任务拆多条 schedule

拆成 N 条之后：「改一次频率要改 N 条」「删任务要删 N 条」「算不算跑过」都要按组重算，
比多一个字段复杂得多，而且组内不一致（改了一半）没法自愈。

### 执行

`_execute_locked` 选品**只选一次**，然后依次把同一批商品发到每个店铺：

```
pick_products()  →  items（比如 10 件）
for target in targets_of(task):
    publish_service.publish(..., shopId=target.shopId,
                            template=target.template or task.template_name)
```

**所有店铺共用一个 batchId**，这是刻意的：回执核对 `_apply_receipt` 按 batchId
匹配发布记录，每家店换一个新 id 的话，上一轮没回来的回执就匹配不上，
任务会被当成「已提交」长期卡在 pending。

摘要按「件 × 店铺数」累计（`selected = 件数 × 店铺数`），多店铺时额外带
`summary["shops"]` 逐店结果。

### 前端

- 账号卡片里：店铺从「单选下拉」改成**复选**，必须带站点后缀
  （同一账号下常有同名的不同国家店铺，只显示店名分不清）；
- 单账号模式：店铺数 > 1 时同样给复选，= 1 时仍是下拉；
- 每个店铺套**它自己站点**的模板（`templateNameBySite`），
  找不到对应站点模板才回落到全局选的模板 —— 拿马来模板发菲律宾店必算错价；
- 列表「店铺」列多一行「共 N 个店铺」。
