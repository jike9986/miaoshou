# 奥皇上品 · 桌面端接口契约

版本：v1.0（2026-10-27 按真实实现校正，见 §13）
Base URL：`http://127.0.0.1:8812/api/v1`
端口：**不要假设一定是 8812**。后端 `server/main.py:371 pick_port()` 会从 `api_port`(8812) 逐号探测到 `api_port_max`(8822)，取第一个可 bind 的端口（全占用则 `SystemExit(2)`）；但打包入口 `server/run_server.py:16` 与 Electron `app/src/main.ts:13` 仍写死 8812、不做递增。客户端以健康检查（`GET /api/v1/system/health`）实际连通的端口为准，两处不一致属待办（见 `docs/status.md`「已知未修复」）。
协议：HTTP/1.1 + JSON（`Content-Type: application/json; charset=utf-8`）
文档生成：FastAPI 自动 OpenAPI 3.1，`GET /openapi.json` / `GET /docs`（仅在 `AOHUANG_DEV=1` 时开启）

---

## 0. 通用约定

### 0.1 命名与类型

- JSON 字段一律 **camelCase**；数据库列名 snake_case，由 Pydantic `alias_generator=to_camel` 自动转换。
- id 一律字符串传输（`detailId`、`shopId`、`cid`、`groupId`），避免 JS 大整数精度问题。
- 时间：`*At` 字段为 **Unix 秒（浮点，带毫秒）**；`startDate`/`endDate` 为 `YYYY-MM-DD`；`time` 为 `HH:MM`（本地时区）。
- 金额：字符串化十进制（`"12.34"`），不用浮点；货币字段后缀 `Currency`（`"CNY"`/`"MYR"`）。
- 布尔：`true`/`false`，不用 0/1。
- 可空：字段始终出现，无值时 `null`（数组用 `[]`），前端不必做 `in` 判断。

### 0.2 认证

```
Authorization: Bearer <token>
```

- 兼容旧客户端：`X-Local-Token: <token>`（等价，二者取其一）。
- 兼容旧客户端：`X-Session-Id` 已废弃，服务端忽略。
- 例外：`GET /system/health` 不需要 token（供 Electron 探测端口）。
- token 由 Electron 首次启动生成，`safeStorage` 加密存储；渲染进程从 `window.__DSH_BOOT__.token` 取，不落 localStorage。

### 0.3 请求追踪与幂等

| 头 | 方向 | 说明 |
|---|---|---|
| `X-Request-Id` | 请求→响应 | 客户端可选；服务端总会回传，缺失时生成 `req_<26位>` |
| `Idempotency-Key` | 请求 | **当前版本未实现幂等**。后端不读这个头（`grep -rn "Idempotency" server/api server/core server/services` 零命中），重复请求会重复执行、各自生成新的 `requestId`，响应里也没有任何「重放命中」标记字段。客户端不得对发布类请求（`POST /publish`、`POST /schedules/{id}/run`）做自动重试 |
| `X-Client-Version` | 请求 | 桌面端版本，服务端在 `system/version` 中比对 |

> 幂等目前只能靠调用方保证「同一批次只提交一次」。真要重试，先用 `GET /publish/logs?since=<时间>` 或 `GET /warehouse/{id}` 的 `publishHistory` 确认上一批是否已经产生记录。
>
> 共享服务器模式下可选的妙手凭据头（不落库）：`X-Miaoshou-AppKey` / `X-Miaoshou-AppSecret`（`server/api/deps.py:89-104`）。桌面单机模式不用传。

### 0.4 分页

请求：`?page=1&pageSize=20`（`page` 从 1 起，`pageSize` 上限 100，默认 20）
响应统一信封：

```json
{
  "items": [],
  "page": 1,
  "pageSize": 20,
  "total": 137,
  "hasMore": true
}
```

### 0.5 错误信封

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "每页数量必须在 1 到 100 之间",
    "details": {"field": "pageSize", "value": 500},
    "requestId": "req_01HB7Z3K9Q7M2X4T"
  }
}
```

`message` 是给人看的，`code` 是给程序判断的。前端分支只看 `code`。

**全局错误码**

| HTTP | code | 含义 |
|---|---|---|
| 400 | `VALIDATION_ERROR` | 参数缺失、类型错误、越界 |
| 400 | `BAD_JSON` | body 不是合法 JSON 或不是对象 |
| 401 | `AUTH_REQUIRED` | 缺 token |
| 401 | `TOKEN_INVALID` | token 不符 |
| 403 | `FORBIDDEN_ORIGIN` | **只在 `POST /system/update` 用**：更新包 URL 的 host 不在白名单（`system_service.py:118-125`）。CORS 不返回 403 —— 非法 `Origin` 不会拿到 `Access-Control-Allow-Origin` 响应头，由浏览器拦截，服务端本身仍回 200 |
| 403 | `READ_ONLY_MODE` | 只读模式下执行写操作 |
| 404 | `NOT_FOUND` | 资源不存在或已软删除 |
| 409 | `CONFLICT_BUSY` | 互斥操作（批次运行中、任务执行中） |
| 409 | `DUPLICATE_NAME` | 名称唯一约束冲突 |
| 422 | `UNPROCESSABLE` | 语义校验失败（如结束日期早于开始日期） |
| 429 | `RATE_LIMITED` | **仅 `/images/translate` 有配额限制**（分钟/小时/天三窗口，见 §7.1）。没有全局令牌桶、没有 req/s 限流 |
| 500 | `INTERNAL_ERROR` | 未捕获异常，响应不含堆栈 |
| 502 | `UPSTREAM_ERROR` | 妙手/图片服务返回业务错误 |
| 502 | `UPSTREAM_UNKNOWN_RESULT` | 上游结果未确认（可能已产生费用） |
| 503 | `UPSTREAM_RATE_LIMIT` | 妙手限流且重试耗尽 |
| 503 | `IMAGE_KEY_MISSING` | 图片服务密钥未配置（见 §7.1） |
| 504 | `UPSTREAM_TIMEOUT` | 上游超时 |

**上游错误透传规则**：妙手的 `code`/`msg` 放在 `error.details.upstream`，`message` 用中文重写；任何包含 AppKey/AppSecret 的原文必须脱敏为 `***`。

---

## 接口索引

**快照**：2026-10-27（UTC+8，在本机进程内导出路由表）。后端在并行开发，路由随时会增加 —— **以命令输出为准，不要相信下表里的数字**。

```bash
# ① 路由对象数（1 个对象可承载多个 HTTP 方法）
cd server && python -c "import main; print(len([r for r in main.app.routes if getattr(r,'path','').startswith('/api/v1')]))"
# → 78

# ② 方法+路径对数（当「接口数」用这个）
cd server && python -c "import main; rs=[r for r in main.app.routes if getattr(r,'path','').startswith('/api/v1')]; print(sum(len(r.methods-{'HEAD'}) for r in rs))"
# → 81

# ③ 全 app 路由 = ① + 2 条非 /api/v1 路由（GET / 静态页兜底、GET /api/system/health）
cd server && python -c "import main; print(len(main.app.routes))"
# → 80（= 78 + 2）。别把这个数当接口数用

# ④ 只看源码装饰器（含未挂载的，交叉核对用）
grep -hcE "^@(router|alias_router|health_router)\.(get|post|patch|put|delete)" server/api/*.py | awk '{s+=$1} END {print s}'
# → 77
```

① 与 ② 差 3：`GET/PUT/PATCH/DELETE /warehouse/price-preview` 是**一个对象四个方法**的占位路由（让非 POST 返回 405 `VALIDATION_ERROR`，而不是掉进 `GET /warehouse/{ident}` 被当成商品标识报 404）。
④ 比 ② 少 4：那条占位路由是 `@router.api_route(..., methods=[...])`，不在 grep 的动词表里（少 1 条装饰器），却贡献 4 个方法+路径对。③ 比 ① 多 2：`GET /`（前端静态页兜底）与 `GET /api/system/health`（无版本前缀）不在 `/api/v1` 之下。

**分模块（对象数 / 方法+路径对数）**

| 模块 | 前缀 | 对象 | 对数 | 章节 |
|---|---|---|---|---|
| 认证 | `/auth` | 6 | 6 | §1 |
| 店铺 | `/shops` | 3 | 3 | §2 |
| 采集箱 | `/collect-box` | 6 | 6 | §3 |
| 仓库 | `/warehouse` | 9 | 12 | §4 |
| 分组 | `/groups` | 4 | 4 | §5 |
| 类目 | `/categories` | 3 | 3 | §6 |
| 图片 | `/images` | 8 | 8 | §7 |
| 发布 | `/publish` | 5 | 5 | §8 |
| 定时 | `/schedules` | 5 | 5 | §9 |
| 词库 | `/violation-words` + `/words` | 7 | 7 | §10 |
| 系统 | `/system` | 9 | 9 | §11 |
| 载体链接池 | `/carrier` | 3 | 3 | §12 |
| 尺寸表 | `/size-charts` | 6 | 6 | §13 |
| 标题 | `/titles` | 2 | 2 | §16 |
| 同步 | `/sync` | 2 | 2 | §17 |
| **合计** | | **78** | **81** | |

「章节」列写「未成节」的，表示接口已实现但本契约没有单独成节，字段以代码为准（已标注文件）。

| 模块 | 方法与路径 | 章节 |
|---|---|---|
| 认证 | POST `/auth/connect` | §1.1 |
| 认证 | GET `/auth/status` | §1.2 |
| 认证 | DELETE `/auth/disconnect` | 未成节（`api/auth.py:69`） |
| 认证 | GET `/auth/accounts` | 未成节（`api/auth.py:84`） |
| 认证 | DELETE `/auth/accounts/{account_id}` | 未成节（`api/auth.py:90`） |
| 认证 | PATCH `/auth/accounts/{account_id}` | 未成节（`api/auth.py:102`） |
| 店铺 | GET `/shops` | §2.1 |
| 店铺 | GET `/shops/languages` | 未成节（`api/shops.py`） |
| 店铺 | GET `/shops/publishable` | 未成节（`api/shops.py`） |
| 采集箱 | GET `/collect-box` | §3.1 |
| 采集箱 | GET `/collect-box/{detailId}` | §3.2 |
| 采集箱 | POST `/collect-box/{detailId}/claim` | §3.3 |
| 采集箱 | POST `/collect-box/{detailId}/process` | **§3.4（本次补）** |
| 采集箱 | POST `/collect-box/claim`（批量认领，body `keys`） | 未成节（`api/collect_box.py:109`） |
| 采集箱 | POST `/collect-box/delete`（从采集箱删除） | 未成节（`api/collect_box.py:140`） |
| 仓库 | GET `/warehouse` | §4.1 |
| 仓库 | GET `/warehouse/{id}` | §4.2 |
| 仓库 | PATCH `/warehouse/{id}` | §4.3 |
| 仓库 | DELETE `/warehouse/{id}` | §4.4 |
| 仓库 | POST `/warehouse/batch-category` | §4.5 |
| 仓库 | POST `/warehouse/batch-delete` | 未成节（`api/warehouse.py`） |
| 仓库 | GET `/warehouse/stats` | 未成节（`api/warehouse.py`） |
| 仓库 | POST `/warehouse/price-preview` | **§4.6（新增）** |
| 仓库 | GET/PUT/PATCH/DELETE `/warehouse/price-preview` | §4.6（占位，405） |
| 分组 | GET `/groups` | §5.1 |
| 分组 | POST `/groups` | §5.2 |
| 分组 | PATCH `/groups/{id}` | 未成节（`api/groups.py`） |
| 分组 | DELETE `/groups/{id}` | §5.3 |
| 类目 | GET `/categories` | §6.1 |
| 类目 | POST `/categories/sync` | 未成节（`api/categories.py`） |
| 类目 | GET `/categories/{cid}/path` | 未成节（`api/categories.py`） |
| 图片 | POST `/images/translate` | §7.1 |
| 图片 | POST `/images/analyze` | **§7.4（新增）** |
| 图片 | GET `/images/jobs/{id}` | §7.2 |
| 图片 | GET `/images/jobs/{id}/file` | §7.2 末段 |
| 图片 | GET `/images/jobs` | §7.3 |
| 图片 | GET `/images/limits` | 未成节（`api/images.py:81`，§7.1 引用） |
| 图片 | GET `/images/usage` | 未成节（`api/images.py:62`） |
| 图片 | POST `/images/reconcile` | 未成节（`api/images.py:67`） |
| 发布 | POST `/publish` | §8.1 |
| 发布 | GET `/publish/logs` | §8.2 |
| 发布 | POST `/publish/logs/refresh` | 未成节（`api/publish.py`） |
| 发布 | GET `/publish/settings` | 未成节（`api/publish.py`，读发布总开关） |
| 发布 | POST `/publish/settings` | 未成节（`api/publish.py`，写发布总开关） |
| 定时 | GET `/schedules` | §9.1 |
| 定时 | POST `/schedules` | §9.2 |
| 定时 | PATCH `/schedules/{id}` | §9.3 |
| 定时 | DELETE `/schedules/{id}` | §9.4 |
| 定时 | POST `/schedules/{id}/run` | §9.5 |
| 词库 | GET `/violation-words` | §10.1 |
| 词库 | POST `/violation-words` | §10.2 |
| 词库 | GET `/violation-words/counts` | 未成节（`api/words.py:65`） |
| 词库 | POST `/violation-words/seed` | 未成节（`api/words.py:57`） |
| 词库 | PATCH `/violation-words/enabled` | **§10.3（新增）** |
| 词库 | GET `/words`（别名） | §10.1（同一处理函数） |
| 词库 | POST `/words`（别名） | §10.2（同一处理函数） |
| 系统 | GET `/system/health`（免鉴权） | §11.3 |
| 系统 | GET `/system/version` | §11.1 |
| 系统 | POST `/system/update-check` | 未成节（`api/system.py:33`，等价 `GET /system/version`） |
| 系统 | POST `/system/update` | §11.2 |
| 系统 | GET `/system/settings` | 未成节（`api/system.py:77`） |
| 系统 | POST `/system/settings` | 未成节（`api/system.py:92`） |
| 系统 | GET `/system/templates` | 未成节（`api/system.py:110`） |
| 系统 | POST `/system/templates` | 未成节（`api/system.py:127`） |
| 系统 | DELETE `/system/templates/{name}` | 未成节（`api/system.py:144`） |
| 载体链接池 | GET `/carrier/links` | **§12（新增）** |
| 载体链接池 | POST `/carrier/links` | **§12（新增）** |
| 载体链接池 | DELETE `/carrier/links` | **§12（新增）** |
| 尺寸表 | GET `/size-charts` | **§13（新增）** |
| 尺寸表 | POST `/size-charts` | **§13（新增）** |
| 尺寸表 | GET `/size-charts/resolve` | **§13（新增）** |
| 尺寸表 | GET `/size-charts/{id}` | **§13（新增）** |
| 尺寸表 | PATCH `/size-charts/{id}` | **§13（新增）** |
| 尺寸表 | DELETE `/size-charts/{id}` | **§13（新增）** |

> 挂载关系：`server/api/__init__.py:33-43` 把 `auth / shops / collect_box / warehouse / groups / categories / images / publish / schedules / words / carrier / size_charts` 全部挂上（`words.alias_router` 单独挂），`system.health_router` 免鉴权、`system.router` 带 token。全部实测可达（`GET /api/v1/carrier/links` → 200、`GET /api/v1/size-charts` → 200）。

---

## 1. 认证

### 1.1 POST /auth/connect

连接妙手账号。传 `accountId` 表示使用已保存的账号；否则用 `appId` + `appSecret` 现场连接并可选保存。

**请求**

```json
{
  "accountId": null,
  "name": "妙手罗蔓",
  "appId": "ak_7f3c9a1b2d4e",
  "appSecret": "sk_9c1f0e2a7b6d4f8a",
  "save": true,
  "autoScan": true
}
```

| 字段 | 类型 | 必填 | 约束 | 说明 |
|---|---|---|---|---|
| accountId | string\|null | 否 | 已存在且未删除 | 有值时忽略 appId/appSecret |
| name | string\|null | 否 | 1–60 字符 | 缺省 `妙手{appId 后 4 位}` |
| appId | string | 条件必填 | 1–256 | AppKey |
| appSecret | string | 条件必填 | 1–512 | AppSecret，仅上行，永不下行 |
| save | boolean | 否 | 默认 `true` | 是否落库为已保存账号 |
| autoScan | boolean | 否 | 默认 `true` | 连接后是否预取采集箱首屏 |

**响应 200**

```json
{
  "connected": true,
  "accountId": "1",
  "connectionName": "妙手罗蔓",
  "appIdHint": "…4e",
  "secretHint": "…4f8a",
  "site": "MY",
  "shopsCount": 6,
  "autoScan": true,
  "lastVerifiedAt": 1761612345.812,
  "shops": [
    {"shopId": "18077147", "name": "LuoMan Store", "site": "MY", "siteName": "马来西亚", "languages": ["ms", "en"], "status": "1"}
  ]
}
```

**错误**

| HTTP | code | 条件 |
|---|---|---|
| 400 | `VALIDATION_ERROR` | appId 为空或超长 |
| 401 | `AUTH_REQUIRED` | 无 token |
| 404 | `NOT_FOUND` | `accountId` 不存在 |
| 409 | `CONFLICT_BUSY` | 商品批次运行中，不能换连接 |
| 422 | `UNPROCESSABLE` | `save=true` 但 name 重复 |
| 502 | `UPSTREAM_ERROR` | 妙手拒绝凭据（`details.upstream.code = "invalid_app_key"`） |
| 503 | `UPSTREAM_RATE_LIMIT` | 重试 5 次仍限流 |

> 说明：连接会调用一次 `shop_list` 验证凭据；`autoScan=true` 时再拉一页 `collect_box_list(status="notPublished")`，两处都成功才返回 `connected: true`。

### 1.2 GET /auth/status

当前连接状态。无参数。

**响应 200**

```json
{
  "connected": true,
  "accountId": "1",
  "connectionName": "妙手罗蔓",
  "appIdHint": "…4e",
  "readOnly": false,
  "publishEnabled": false,
  "accounts": [
    {"id": "1", "name": "妙手罗蔓", "appIdHint": "…4e", "secretHint": "…4f8a", "site": "MY", "isDefault": true, "status": "active", "lastVerifiedAt": 1761612345.812}
  ],
  "languages": ["ms", "en", "th", "vi", "zh"],
  "serverVersion": "0.1.0",
  "serverName": "aohuang-listing-server",
  "dataDir": "C:\\Users\\Administrator\\AppData\\Local\\aohuang-desktop",
  "limits": {"mainLimit": 9, "detailLimit": 9, "detailHardLimit": 30, "concurrency": 3, "collectBoxMaxItems": 100}
}
```

`accounts[]` 来自 `account_service.account_summaries()`（`server/services/account_service.py:395-408`）：

| 字段 | 说明 |
|---|---|
| id / name / site / isDefault / status / lastVerifiedAt | 账号基本信息；`lastVerifiedAt` 可能为 `null`（从未验证过） |
| appIdHint | AppKey 末 4 位（形如 `…4e`），不是完整 key |
| **secretHint** | **AppSecret 末 4 位**（形如 `…4f8a`），由 `services/crypto.py:33-39` 生成，**只回末 4 位**；密钥长度不足 4 时回 `…`，从未保存过则回 `""` |

`serverName` 也在这条响应里（`api/auth.py:57`），示例已列出。

**错误**

| HTTP | code | 条件 |
|---|---|---|
| 401 | `AUTH_REQUIRED` / `TOKEN_INVALID` | token 缺失或不符 |

> `GET /auth/status` 不校验 Origin，也不会返回 403；`FORBIDDEN_ORIGIN` 只在 `POST /system/update` 的更新包 host 校验里出现（见 §0.5、§11.2）。

`connected=false` 时 `accountId`/`connectionName` 为 `null`，`accounts` 仍返回已保存列表。

---

## 2. 店铺

### 2.1 GET /shops

返回某账号下的店铺。无缓存时不落库直接问上游；落库的缓存用于离线展示。

**Query**

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| accountId | string | 否 | 当前连接 | 指定账号时用该账号自己的凭据 |
| refresh | boolean | 否 | `false` | `true` 强制回源 |

**响应 200**

```json
{
  "accountId": "1",
  "accountName": "妙手罗蔓",
  "shops": [
    {
      "shopId": "18077147",
      "name": "LuoMan Store",
      "site": "MY",
      "siteName": "马来西亚",
      "languages": ["ms", "en"],
      "status": "1",
      "syncedAt": 1761612345.812
    },
    {
      "shopId": "18077152",
      "name": "LuoMan MY 2",
      "site": "MY",
      "siteName": "马来西亚",
      "languages": ["ms", "en"],
      "status": "1",
      "syncedAt": 1761612345.812
    }
  ],
  "siteLanguages": {"MY": ["ms", "en"], "SG": ["en", "zh"], "PH": ["en"], "TH": ["th"], "VN": ["vi"], "ID": ["id"]},
  "siteNames": {"MY": "马来西亚", "SG": "新加坡", "PH": "菲律宾", "TH": "泰国", "VN": "越南", "ID": "印尼"},
  "degraded": false
}
```

**错误**

| HTTP | code | 条件 |
|---|---|---|
| 401 | `AUTH_REQUIRED` | 未连接且未指定 accountId |
| 404 | `NOT_FOUND` | accountId 不存在 |
| 502 | `UPSTREAM_ERROR` | 妙手返回错误 |
| 503 | `UPSTREAM_RATE_LIMIT` | 限流 |

`degraded=true` 表示回源失败，`shops` 是上次缓存。

---

## 3. 采集箱

### 3.1 GET /collect-box

分页查询采集箱（默认只查未发布的店铺商品）。**扫描即查询**：不再一次性拉全部 100 条。

**Query**

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| page | integer | 否 | 1 | ≥1 |
| pageSize | integer | 否 | 20 | **1–100，但超过 20 一律按 20 处理**：路由声明 `le=100`（`api/collect_box.py:21`），服务层用 `COLLECT_BOX_PAGE_MAX=20` 卡（`collect_box_service.py:109-111`），`21..100` 直接 **400 `VALIDATION_ERROR`**（message「每页数量必须在 1 到 20 之间」，`details.field=pageSize`），不是静默截断 |
| status | string | 否 | `notPublished` | `notPublished` / `published` / `all` |
| keyword | string | 否 | `""` | 标题/明细 id 模糊匹配，服务端过滤 |
| shopId | string | 否 | null | 只看某店铺 |
| site | string | 否 | null | MY/SG/PH/TH/VN/ID |
| sort | string | 否 | `default` | `default` / `savedDesc`（注意：`savedDesc` 实际按「是否已入仓」排序，不是按时间，见 §3.1 末尾） |
| accountId | string | 否 | 当前连接 | 指定账号时用该账号自己的凭据 |

实测（`pageSize=50`）：

```json
{"error":{"code":"VALIDATION_ERROR","message":"每页数量必须在 1 到 20 之间","details":{"field":"pageSize","value":50},"requestId":"req_…"}}
```

HTTP 400。所以 `pageSize=100` 虽然能通过路由层的 `le=100`，仍会在服务层被拒。

**响应 200**

```json
{
  "items": [
    {
      "key": "778812:18077147",
      "detailId": "778812",
      "shopId": "18077147",
      "shopName": "LuoMan Store",
      "site": "MY",
      "title": "Women Running Shoes Breathable Mesh Sneakers",
      "imageUrl": "https://img.91miaoshou.com/oss/778812/main.jpg",
      "sourceId": "681234567890",
      "status": "未发布",
      "editModel": "shop",
      "inWarehouse": false,
      "hasDraftIn": []
    }
  ],
  "page": 1,
  "pageSize": 20,
  "total": 43,
  "hasMore": true,
  "scannedAt": 1761612400.221,
  "degraded": false,
  "scanned": 20
}
```

| 字段 | 说明 |
|---|---|
| key | `{detailId}:{shopId}`，前端选择集用它 |
| sourceId | 1688 货源 id，可能为 `null` |
| editModel | 上游 `editModel`，`shop` 才处理 |
| inWarehouse | 是否已在成品仓库 |
| hasDraftIn | 已采集进哪些账号的采集箱（`["妙手罗蔓"]`） |
| **total** | **`max(上游 total, len(items))`**，不是纯上游总数（`collect_box_service.py:158`）。上游没给 `total` 或给了 0 时，就等于本页条数 |
| **hasMore** | **`len(items) >= pageSize`**，不是 `page*pageSize < total`（`collect_box_service.py:159`）。因此「刚好取满一页」时 `hasMore=true`，即使已经是最后一页 |
| scanned | 本轮实际向上游扫过的条数（`collect_box_service.py:132,162`） |
| degraded | 当前实现恒为 `false`（`collect_box_service.py:161`），回源失败时抛 502 而不是降级 |

> `sort=savedDesc` 的实现是 `items.sort(key=lambda row: row.get("inWarehouse", False), reverse=True)`（`collect_box_service.py:152-153`），即「已入仓的排前面」，**不是按保存时间倒序**。

**错误**

| HTTP | code | 条件 |
|---|---|---|
| 400 | `VALIDATION_ERROR` | pageSize 不在 1–20、status 非法、sort 非法、page < 1 |
| 401 | `AUTH_REQUIRED` | 未连接 |
| 502 | `UPSTREAM_ERROR` | 上游不可达或结构异常（`details.reason = "detailList_missing"`） |

### 3.2 GET /collect-box/{detailId}

单件详情（含 `ossMd5`、SKU、图片、诊断结果）。

**Path / Query**

| 位置 | 参数 | 必填 | 说明 |
|---|---|---|---|
| path | detailId | 是 | 数字字符串 |
| query | shopId | 是 | 明细只在店铺维度唯一 |
| query | mode | 否 | `shop`（默认）/ `site` |

**响应 200**

```json
{
  "detailId": "778812",
  "shopId": "18077147",
  "mode": "shop",
  "ossMd5": "9f2c1d7e4a5b6c8d9e0f1a2b3c4d5e6f",
  "editModel": "shop",
  "title": "Women Running Shoes Breathable Mesh Sneakers",
  "site": "MY",
  "cid": "601234",
  "categoryPath": "女鞋/运动鞋/跑步鞋",
  "language": "zh",
  "images": [
    "https://img.91miaoshou.com/oss/778812/main-1.jpg",
    "https://img.91miaoshou.com/oss/778812/main-2.jpg"
  ],
  "imageCount": 2,
  "detailImages": [
    "https://img.91miaoshou.com/oss/778812/detail-1.jpg"
  ],
  "skuCount": 6,
  "skuProperties": [
    {
      "attrName": "颜色",
      "attrValueList": [
        {"attrValueId": "c1", "attrValue": "黑色", "imgUrl": "https://img.91miaoshou.com/oss/778812/black.jpg"},
        {"attrValueId": "c2", "attrValue": "白色", "imgUrl": "https://img.91miaoshou.com/oss/778812/white.jpg"}
      ]
    },
    {
      "attrName": "尺码",
      "attrValueList": [
        {"attrValueId": "39", "attrValue": "39", "imgUrl": null},
        {"attrValueId": "40", "attrValue": "40", "imgUrl": null}
      ]
    }
  ],
  "skus": [
    {"skuKey": ";39;c1;", "price": "35.90", "priceIncludeVat": "39.49", "stock": 120, "weight": "0.65", "isDelete": "0"},
    {"skuKey": ";40;c2;", "price": "35.90", "priceIncludeVat": "39.49", "stock": 80, "weight": "0.65", "isDelete": "0"}
  ],
  "purchasePrice": {"min": "35.90", "max": "35.90", "currency": "CNY"},
  "weight": "0.65",
  "packageDimensions": {"length": 30, "width": 20, "height": 10},
  "sizeChartUrl": "https://aohuang888.com/assets/size-chart-my.jpg",
  "notes": "<p><img src=\"https://img.91miaoshou.com/oss/778812/detail-1.jpg\"></p>",
  "diagnostics": [
    {"severity": "error", "code": "missing_category", "message": "类目为空"},
    {"severity": "warning", "code": "title_too_short", "message": "清理后标题长度为 22，不符合 25 到 255 字符"}
  ],
  "readyToProcess": false,
  "inWarehouse": false,
  "warehouseId": null
}
```

**错误**

| HTTP | code | 条件 |
|---|---|---|
| 400 | `VALIDATION_ERROR` | detailId 非数字 / shopId 缺失 |
| 401 | `AUTH_REQUIRED` | 未连接 |
| 404 | `NOT_FOUND` | 上游查无此明细 |
| 422 | `UNPROCESSABLE` | 返回缺少 `ossMd5` 或身份字段不一致 |
| 502 | `UPSTREAM_ERROR` | 上游错误 |

### 3.3 POST /collect-box/{detailId}/claim

把采集箱商品认领进目标店铺的采集箱。**只认领，不发布。**

**Path / Query**

| 位置 | 参数 | 必填 | 说明 |
|---|---|---|---|
| path | detailId | 是 | |
| query | shopId | 是 | 来源店铺（定位明细） |

**请求**

```json
{
  "targetShopId": "18077152",
  "accountId": "1",
  "markCollected": true
}
```

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| targetShopId | string | 是 | — | 认领到哪个店铺 |
| accountId | string | 否 | 当前连接 | 用哪个账号的凭据 |
| markCollected | boolean | 否 | `true` | 是否记录 `collected` 状态 |

**响应 200**

```json
{
  "claimed": true,
  "detailId": "778812",
  "fromShopId": "18077147",
  "targetShopId": "18077152",
  "mode": "direct",
  "count": 1,
  "detailIds": ["778812"],
  "failures": [],
  "collected": {"妙手罗蔓": {"at": 1761612512.441, "detailId": "778812"}}
}
```

`mode=carrier` 表示来源店铺与目标店铺不同，已通过公共采集箱重建载体（`carrier.fill_carriers`），`detailIds` 是新生成的明细 id。

**错误**

| HTTP | code | 条件 |
|---|---|---|
| 400 | `VALIDATION_ERROR` | 缺 targetShopId |
| 403 | `READ_ONLY_MODE` | 只读模式 |
| 404 | `NOT_FOUND` | 明细或目标店铺不存在 |
| 409 | `ALREADY_CLAIMED` | 该明细已在目标店铺的采集箱 |
| 409 | `CONFLICT_BUSY` | 同一商品正在处理 |
| 422 | `CARRIER_UNAVAILABLE` | 载体池不足（`details.available`, `details.required`） |
| 502 | `UPSTREAM_ERROR` | 上游拒绝 |

> 这条接口不校验 Origin，不会返回 `FORBIDDEN_ORIGIN`（该码只属于 §11.2）。

### 3.4 POST /collect-box/{detailId}/process（处理入仓）

把采集箱里的一件商品拉下来、翻译、定价、过违规词、颜色规范化，然后写进**成品仓库**。这是「采集箱 → 仓库」的唯一入口，路由在 `server/api/collect_box.py:73-101`，服务层是 `warehouse_service.save_from_collect_box()`（`server/services/warehouse_service.py:525` 起）。

**Path / Query**

| 位置 | 参数 | 必填 | 说明 |
|---|---|---|---|
| path | detailId | 是 | 采集箱明细 id（数字字符串） |
| query | accountId | 否 | 与 body 里的 `accountId` 等价，body 优先（`collect_box.py:82`） |

**请求**（字段定义见 `server/schemas/requests.py:174-183` `ProcessRequest`）

```json
{
  "shopId": "18077147",
  "accountId": "1",
  "language": "ms",
  "groupId": "3",
  "template": "马来",
  "exchangeRate": "0.6521",
  "save": true
}
```

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| shopId | string | **是** | — | 明细所在店铺（定位明细必需，缺了直接 400 `shopId Field required`） |
| accountId | string\|null | 否 | 当前连接 | 用哪个账号的凭据 |
| language | string\|null | 否 | `""`（不翻译） | 目标语言，透传给翻译；取值参考 `/auth/status` 的 `languages` |
| groupId | string\|null | 否 | null | 入仓后挂到哪个分组；不存在 → 404 `NOT_FOUND` |
| template | string\|null | 否 | null | 定价模板名；不存在 → 422 `TEMPLATE_INVALID` |
| exchangeRate | string\|number\|null | 否 | null | 覆盖模板汇率 |
| save | boolean | 否 | `true` | **`false` 时只做预览**：整笔事务 `db.rollback()`，库里不留任何东西 |

**响应 200**

```json
{
  "saved": true,
  "product": {
    "id": "1042",
    "key": "778812:18077147",
    "detailId": "778812",
    "shopId": "18077147",
    "accountId": "1",
    "title": "Kasut Sukan Wanita Breathable Mesh Sneakers",
    "language": "ms",
    "site": "MY",
    "cid": "601234",
    "categoryId": "88",
    "categoryName": "跑步鞋",
    "groupId": "3",
    "group": "精品",
    "images": ["https://img.91miaoshou.com/oss/778812/main-1-ms.jpg"],
    "skuCount": 6,
    "purchasePriceMin": "35.90",
    "purchasePriceMax": "35.90",
    "purchaseCurrency": "CNY",
    "status": "ready",
    "savedAt": 1761613001.207
  }
}
```

| 字段 | 说明 |
|---|---|
| saved | `true` 已提交；`false` 表示 `save=false` 的预览，**没有写库**（`collect_box.py:97-99`） |
| product | 与 `GET /warehouse/{id}` 的 `product` 同构（`warehouse_service.serialize()`），预览态下 `id` 是本次 flush 出来的临时 id，回滚后不可再用它查询 |

**错误**

| HTTP | code | 条件 |
|---|---|---|
| 400 | `VALIDATION_ERROR` | 缺 `shopId`（`details.field = "shopId"`） |
| 403 | `READ_ONLY_MODE` | 只读模式（`require_writable`，`collect_box.py:81`） |
| 404 | `NOT_FOUND` | 上游查无此明细（`warehouse_service.py:541`）或 `groupId` 不存在 |
| 422 | `UNPROCESSABLE` | 详情缺 `ossMd5`（`warehouse_service.py:542-543`） |
| 422 | `TEMPLATE_INVALID` | `template` 指定的模板不存在（`warehouse_service.py:551-555`） |
| 502 | `UPSTREAM_ERROR` | 上游不可达/拒绝（实测假凭据 → `ConnectError`） |

> 已实现的链路：`shop_detail` 拉详情 → 翻译 → `process_price` 定价 → 扁平违规词库清理（所有启用且命中的词均从标题删除）→ 颜色规范化 → `build_product` 落库 → 主图截断 9 张 → 可选挂分组。
> **未实现**：老版 `product_workflow.py` 在入仓后还会 `save_shop()` 把草稿写回妙手，新版没有这一步（`grep -n "save_shop" server/services/warehouse_service.py server/api/collect_box.py` 零命中），只写本地库。
> **前端入口**：已接入。`web/src/lib/queries.ts:450-468` 的 `useProcessCollectItem()`（120s 超时，成功后失效采集箱/仓库/分组三个 query），页面在 `web/src/features/collect-box/CollectBoxPage.tsx:917` 起的单条/批量「处理入仓」表单，字段与本节一致（来源店铺只读、语言、汇率、分组、模板、`save`）。

---

## 4. 成品仓库

### 4.1 GET /warehouse

分页 + 类目 + 分组 + 搜索。

**Query**

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| page | integer | 否 | 1 | |
| pageSize | integer | 否 | 24 | 1–100 |
| categoryId | string | 否 | null | 本地类目 id |
| cid | string | 否 | null | 平台 cid（与 categoryId 二选一） |
| groupId | string | 否 | null | 自定义分组 id |
| group | string | 否 | null | 分组名（兼容旧前端） |
| q | string | 否 | null | 标题全文检索（≤64 字符；2 字符以下退化为 LIKE） |
| accountId | string | 否 | null | 来源账号 |
| status | string | 否 | null | `ready` / `claimed` / `published` / `archived` |
| claimed | boolean | 否 | null | |
| sort | string | 否 | `savedDesc` | `savedDesc` / `savedAsc` / `priceAsc` / `priceDesc` / `titleAsc` |

**响应 200**

```json
{
  "items": [
    {
      "id": "1042",
      "key": "778812:18077147",
      "detailId": "778812",
      "shopId": "18077147",
      "shopName": "LuoMan Store",
      "account": "妙手罗蔓",
      "accountId": "1",
      "title": "Kasut Sukan Wanita Breathable Mesh Sneakers",
      "language": "ms",
      "site": "MY",
      "images": [
        "https://img.91miaoshou.com/oss/778812/main-1-ms.jpg",
        "https://img.91miaoshou.com/oss/778812/main-2-ms.jpg"
      ],
      "image": "https://img.91miaoshou.com/oss/778812/main-1-ms.jpg",
      "imageCount": 2,
      "skuCount": 6,
      "purchasePriceMin": "35.90",
      "purchasePriceMax": "35.90",
      "purchaseCurrency": "CNY",
      "cid": "601234",
      "categoryName": "跑步鞋",
      "categoryId": "88",
      "group": "精品",
      "groupId": "3",
      "savedAt": 1761613001.207,
      "claimed": false,
      "claimedAt": null,
      "claimedShopId": null,
      "collected": {},
      "salesLast30": 42,
      "stock": 200,
      "status": "ready"
    }
  ],
  "page": 1,
  "pageSize": 24,
  "total": 312,
  "hasMore": true,
  "facets": {
    "categories": [
      {"name": "跑步鞋", "cid": "601234", "categoryId": "88", "count": 96},
      {"name": "板鞋", "cid": "601235", "categoryId": "91", "count": 43}
    ],
    "groups": [
      {"id": "3", "name": "精品", "color": "amber", "count": 28},
      {"id": "4", "name": "待改价", "color": "rose", "count": 11}
    ],
    "accounts": [
      {"accountId": "1", "name": "妙手罗蔓", "count": 210},
      {"accountId": "2", "name": "妙手蒋新宇", "count": 102}
    ]
  },
  "stats": {"totalProducts": 312, "totalClaimed": 61, "totalPublished": 88}
}
```

**错误**

| HTTP | code | 条件 |
|---|---|---|
| 400 | `VALIDATION_ERROR` | pageSize > 100 / sort 非法 |
| 401 | `AUTH_REQUIRED` | |

### 4.2 GET /warehouse/{id}

**Path / Query**

| 位置 | 参数 | 必填 | 说明 |
|---|---|---|---|
| path | id | 是 | 数字 id 或 `warehouseKey`（`778812:18077147`） |
| query | includePayload | 否 | 默认 `false`；`true` 返回完整 `payload` |

**响应 200**

```json
{
  "product": {
    "id": "1042",
    "key": "778812:18077147",
    "detailId": "778812",
    "shopId": "18077147",
    "accountId": "1",
    "account": "妙手罗蔓",
    "title": "Kasut Sukan Wanita Breathable Mesh Sneakers",
    "language": "ms",
    "site": "MY",
    "cid": "601234",
    "categoryId": "88",
    "categoryName": "跑步鞋",
    "groupId": "3",
    "group": "精品",
    "images": ["https://img.91miaoshou.com/oss/778812/main-1-ms.jpg"],
    "skuCount": 6,
    "purchasePriceMin": "35.90",
    "purchasePriceMax": "35.90",
    "purchaseCurrency": "CNY",
    "stock": 200,
    "salesLast30": 42,
    "claimed": false,
    "claimedAt": null,
    "claimedShopId": null,
    "collected": {},
    "status": "ready",
    "savedAt": 1761613001.207,
    "updatedAt": 1761613001.207,
    "payload": null
  },
  "publishHistory": [
    {"logId": "512", "shopId": "18077147", "shopName": "LuoMan Store", "status": "success", "requestedAt": 1761614000.112, "platformItemId": "1729384756"}
  ],
  "imageJobs": [
    {"id": "3f9a…c1b2", "source": "main", "status": "ready", "reservedMilliyuan": 24, "createdAt": 1761612900.331}
  ]
}
```

**错误**

| HTTP | code | 条件 |
|---|---|---|
| 404 | `NOT_FOUND` | id 不存在或 `deleted_at` 非空 |

### 4.3 PATCH /warehouse/{id}

修改单个商品的类目/分组/标题（标题改动会刷新 `updated_at`，不重算价格）。

**请求**

```json
{
  "categoryId": "91",
  "cid": "601235",
  "name": "板鞋",
  "groupId": "4",
  "title": "Kasut Sukan Wanita Breathable Mesh Sneakers 2026"
}
```

| 字段 | 类型 | 必填 | 约束 | 说明 |
|---|---|---|---|---|
| categoryId | string\|null | 否 | 存在 | 传 `null` 表示清除类目 |
| cid | string | 否 | 数字 | 指定平台类目；与 categoryId 同时给时以 cid 为准并回填 categoryId |
| name | string | 否 | 1–255 | `cid` 的中文名，仅用于展示缓存 |
| groupId | string\|null | 否 | 存在 | 传 `null` 表示移出分组 |
| title | string | 否 | **1–255**（按字符计，中英文均可） | 实际校验 `if not 1 <= len(text) <= 255`（`warehouse_service.py:367-369`）。**不是 25–255**：25 只是入仓诊断里的建议长度（`product_service.py:158-162`，`TITLE_MIN_LENGTH`），PATCH 不受它约束，实测 `PATCH /warehouse/355 {"title":"A"}` → 200 |

至少提供一个字段，否则 `400 VALIDATION_ERROR`。

**响应 200**

```json
{
  "product": {
    "id": "1042",
    "key": "778812:18077147",
    "title": "Kasut Sukan Wanita Breathable Mesh Sneakers 2026",
    "cid": "601235",
    "categoryId": "91",
    "categoryName": "板鞋",
    "groupId": "4",
    "group": "待改价",
    "updatedAt": 1761615122.907
  }
}
```

**错误**

| HTTP | code | 条件 |
|---|---|---|
| 400 | `VALIDATION_ERROR` | 空 body / 标题长度不在 1–255 |
| 403 | `READ_ONLY_MODE` | 只读模式 |
| 404 | `NOT_FOUND` | 商品/类目/分组不存在 |
| 422 | `CATEGORY_NOT_LEAF` | 选了非叶子类目 |

> `title` 长度与入仓诊断的 25 字符下限无关：`PATCH` 只校验 1–255。改标题会同时写 `products.title` 和 `products.payload.title`，并刷新 `updated_at`，不重算价格。

### 4.4 DELETE /warehouse/{id}

软删除。同时删除本地图片结果文件（`image-results/` 中该商品的任务产物），`publish_logs` 保留并置 `product_id = NULL`。

**响应 200**

```json
{
  "deleted": true,
  "id": "1042",
  "filesRemoved": 4,
  "stats": {"totalProducts": 311}
}
```

**错误**

| HTTP | code | 条件 |
|---|---|---|
| 403 | `READ_ONLY_MODE` | |
| 404 | `NOT_FOUND` | |
| 409 | `CONFLICT_BUSY` | 该商品正在发布或图片任务进行中（`details.runningJobs = 2`） |

### 4.5 POST /warehouse/batch-category

批量改类目/分组。单次上限 200 条，全部成功或全部失败（单事务）。

**请求**

```json
{
  "ids": ["1042", "1043", "1050"],
  "categoryId": "91",
  "cid": "601235",
  "name": "板鞋",
  "groupId": null
}
```

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| ids | string[] | 是 | 1–200，去重 |
| categoryId | string\|null | 否 | |
| cid | string | 否 | |
| name | string | 否 | |
| groupId | string\|null | 否 | |

`ids` 之外至少给一个目标字段。

**响应 200**

```json
{
  "updated": 3,
  "requested": 3,
  "failed": [],
  "products": [
    {"id": "1042", "cid": "601235", "categoryId": "91", "categoryName": "板鞋", "groupId": null, "group": null},
    {"id": "1043", "cid": "601235", "categoryId": "91", "categoryName": "板鞋", "groupId": null, "group": null},
    {"id": "1050", "cid": "601235", "categoryId": "91", "categoryName": "板鞋", "groupId": null, "group": null}
  ]
}
```

**错误**

| HTTP | code | 条件 |
|---|---|---|
| 400 | `VALIDATION_ERROR` | ids 为空 / 超过 200 / 去重后为空 |
| 403 | `READ_ONLY_MODE` | |
| 404 | `NOT_FOUND` | 任一 id 不存在（`details.missing = ["1099"]`），事务回滚 |

### 4.6 POST /warehouse/price-preview（价格试算）

**不发布、不写库、不调上游、不看发布开关**——只按模板把本地 payload 重算一遍售价，用于「发布前先看价」（发布是不可撤销操作）。
算法 100% 复用 `services.price_template.apply_template()`（与 `publish_service` 的重算同源），所以试算结果与真实发布一致。路由：`api/warehouse.py:88`（POST）；占位路由在 `api/warehouse.py:75-86`。

**只支持 POST**。同路径的 `GET`/`PUT`/`PATCH`/`DELETE` 是一个占位路由，返回 **405 `VALIDATION_ERROR`**「价格试算请用 POST /api/v1/warehouse/price-preview」（实测）。这样就不会掉进 `GET /warehouse/{ident}` 被当成商品标识、回一个误导性的 404。

**请求**

```json
{
  "items": [
    {"warehouseId": "355"},
    {"key": "3482714692:18077147"},
    {"detailId": "3482714692", "shopId": "18077147"}
  ],
  "template": "默认模板",
  "exchangeRate": "1.62"
}
```

| 字段 | 类型 | 必填 | 约束 | 说明 |
|---|---|---|---|---|
| items | array | 是 | 1–200 | 每项给 `warehouseId` 或 `key`，或 `detailId`+`shopId` 配对；都不给 → 该条进 `warnings`（`INVALID_ITEM`），不影响其它条 |
| template | string | 是 | 已存模板名 | 空或不存在 → `422 TEMPLATE_INVALID` |
| exchangeRate | string\|number\|null | 否 | >0 且合法 | 覆盖模板汇率；非法 → 400 |

**响应 200**（实测，`products.id=355`，模板「默认模板」）

```json
{
  "template": {"name": "默认模板", "site": "MY", "sellingCurrency": "MYR"},
  "exchangeRate": null,
  "items": [
    {
      "warehouseId": "355",
      "warehouseKey": "3482714692:18077147",
      "title": "A",
      "detailId": "3482714692",
      "shopId": "18077147",
      "purchasePrice": {"min": "17.00", "max": "21.00", "currency": "CNY"},
      "priceBefore": {"min": "10.41", "max": "12.86", "currency": "MYR"},
      "priceAfter": {"min": "36.63", "max": "41.25", "currency": "MYR"},
      "currency": "MYR",
      "templateName": "默认模板",
      "templateApplied": true,
      "rate": 1.6330171371886406,
      "rateSource": "product",
      "weightKg": 0.7,
      "skuCount": 10,
      "changedCount": 24,
      "calculation": {"price": 41.25, "currency": "MYR", "rate": 1.6330171371886406, "purchasePriceCny": 21.0, "packageWeightKg": 0.7, "templateName": "默认模板", "lines": {"purchasePrice": 12.86, "merchantFreight": 8.22, "vat": 3.75, "paymentCharge": 1.9441902346641662, "platformCommission": 0.0, "withdrawCharge": 0.2548930976533583, "bcpCharge": 1.3365, "sfpCharge": 0.0, "affiliateCharge": 0.0, "lastMileTax": 0.0, "platformSupport": 0.51, "otherCharge": 0.0, "profit": 12.375, "pretaxPrice": 29.28}, "linesCny": {"purchasePrice": 21.000600384245917, "...": "省略"}}
    }
  ],
  "total": 1,
  "repriced": 1,
  "skipped": 0,
  "warnings": []
}
```

| 字段 | 说明 |
|---|---|
| priceBefore / priceAfter | 改价前 / 改价后的 SKU 价格区间（币种见 `currency`） |
| rate / rateSource | 实际使用的汇率；`product` 表示取自商品 payload，`override` 表示来自请求 `exchangeRate` |
| changedCount | 被改写的价格字段个数（SKU 价格 + 含税价等，不是 SKU 数） |
| calculation | 模板算价的逐项拆解（采购价/运费/税/佣金/提现/利润/税前价），排错用 |
| warnings[].code | `INVALID_ITEM` / `NOT_FOUND` / `NO_PAYLOAD` / `PRICE_UNAVAILABLE`；**单条失败只进 warnings，整体仍 200** |
| total / repriced / skipped | 请求条数 / 成功条数 / 跳过条数 |

**错误**

| HTTP | code | 条件 |
|---|---|---|
| 400 | `VALIDATION_ERROR` | items 空 / >200 / 汇率非法 |
| 405 | `VALIDATION_ERROR` | 用了非 POST 方法（占位路由） |
| 422 | `TEMPLATE_INVALID` | `template` 为空或不存在/不可用（`details.field = "template"`） |

> 只读模式（`READ_ONLY_MODE`）下**也能用**：它不写库，所以不走 `require_writable`。

---

## 5. 自定义分组

### 5.1 GET /groups

**响应 200**

```json
{
  "groups": [
    {"id": "3", "name": "精品", "color": "amber", "sort": 0, "count": 28, "createdAt": 1761000000.0, "updatedAt": 1761000000.0},
    {"id": "4", "name": "待改价", "color": "rose", "sort": 1, "count": 11, "createdAt": 1761000100.0, "updatedAt": 1761000100.0}
  ],
  "total": 2
}
```

### 5.2 POST /groups

**请求**

```json
{
  "name": "春季新款",
  "color": "emerald",
  "sort": 2
}
```

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| name | string | 是 | 1–64 字符，去首尾空格，唯一（大小写不敏感） |
| color | string | 否 | 枚举：`slate` `rose` `amber` `emerald` `sky` `violet`，默认 `slate` |
| sort | integer | 否 | 0–999，默认追加到末尾 |

**响应 201**

```json
{
  "group": {"id": "5", "name": "春季新款", "color": "emerald", "sort": 2, "count": 0, "createdAt": 1761616000.0, "updatedAt": 1761616000.0}
}
```

**错误**

| HTTP | code | 条件 |
|---|---|---|
| 400 | `VALIDATION_ERROR` | name 为空/超长/颜色非法 |
| 403 | `READ_ONLY_MODE` | |
| 409 | `DUPLICATE_NAME` | 同名分组已存在 |

### 5.3 DELETE /groups/{id}

删除分组，组内商品 `group_id` 置空（**不删除商品**）。

**响应 200**

```json
{
  "deleted": true,
  "id": "5",
  "productsUnassigned": 12
}
```

**错误**

| HTTP | code | 条件 |
|---|---|---|
| 403 | `READ_ONLY_MODE` | |
| 404 | `NOT_FOUND` | |

---

## 6. 类目

### 6.1 GET /categories

返回类目树。默认只返回根节点一层，`depth` 控制展开深度。

**Query**

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| site | string | 否 | 当前账号主站点 | MY/SG/PH/TH/VN/ID |
| accountId | string | 否 | 当前连接 | 类目树按账号缓存 |
| rootCid | string | 否 | null | 只取某子树 |
| depth | integer | 否 | 2 | 1–6 |
| leafOnly | boolean | 否 | `false` | 只返回叶子（保存商品时选类目用） |
| refresh | boolean | 否 | `false` | 强制回源 |

**响应 200**

```json
{
  "site": "MY",
  "rootCid": null,
  "syncedAt": 1761610000.5,
  "stale": false,
  "tree": [
    {
      "cid": "600000",
      "parentCid": null,
      "name": "Women Shoes",
      "nameZh": "女鞋",
      "path": "女鞋",
      "depth": 0,
      "isLeaf": false,
      "children": [
        {
          "cid": "601000",
          "parentCid": "600000",
          "name": "Sports Shoes",
          "nameZh": "运动鞋",
          "path": "女鞋/运动鞋",
          "depth": 1,
          "isLeaf": false,
          "children": [
            {
              "cid": "601234",
              "parentCid": "601000",
              "name": "Running Shoes",
              "nameZh": "跑步鞋",
              "path": "女鞋/运动鞋/跑步鞋",
              "depth": 2,
              "isLeaf": true,
              "children": []
            }
          ]
        }
      ]
    }
  ],
  "stats": {"total": 1284, "leaf": 962, "cached": true}
}
```

**错误**

| HTTP | code | 条件 |
|---|---|---|
| 401 | `AUTH_REQUIRED` | 未连接（且无缓存） |
| 404 | `NOT_FOUND` | `rootCid` 不在树里 |
| 502 | `UPSTREAM_ERROR` | 上游错误 |
| 503 | `UPSTREAM_RATE_LIMIT` | 限流 |

`stale=true` 表示缓存超过 7 天且本次回源失败，仍返回旧树。

---

## 7. 图片翻译

### 7.1 POST /images/translate

批量提交图片翻译。服务端按 `sha256(原图 + model + prompt + size)` 幂等：已有 `ready` 结果直接复用，不扣费。

**请求**

```json
{
  "batchId": "imgb_20261028_01",
  "size": "1:1",
  "retryOcr": false,
  "items": [
    {
      "productId": "1042",
      "detailId": "778812",
      "source": "main",
      "url": "https://img.91miaoshou.com/oss/778812/main-1.jpg"
    },
    {
      "productId": "1042",
      "detailId": "778812",
      "source": "detail",
      "url": "https://img.91miaoshou.com/oss/778812/detail-1.jpg"
    },
    {
      "productId": "1042",
      "detailId": "778812",
      "source": "sku",
      "url": "https://img.91miaoshou.com/oss/778812/black.jpg",
      "size": "16:9"
    }
  ]
}
```

| 字段 | 类型 | 必填 | 约束 | 说明 |
|---|---|---|---|---|
| batchId | string\|null | 否 | ≤32 | 缺省自动生成 `imgb_<uuid12>` |
| size | string | 否 | `1:1`/`16:9`/`9:16` | 默认 `1:1`；item 可覆盖 |
| retryOcr | boolean | 否 | 默认 `false` | 仅对已有失败/未确认任务显式重试 |
| items | array | 是 | 1–200 | |
| items[].productId | string\|null | 否 | 存在 | |
| items[].detailId | string\|null | 否 | | |
| items[].source | string | 是 | `main`/`detail`/`sku` | 只用于归类，不影响计价 |
| items[].url | string | 条件必填 | https，≤15MB | 与 `imageBase64` 二选一 |
| items[].imageBase64 | string\|null | 条件必填 | ≤15MB 原图 | 上传本地图片时用 |
| items[].size | string\|null | 否 | 同上 | 覆盖批次值 |

**响应 202**

```json
{
  "batchId": "imgb_20261028_01",
  "accepted": 3,
  "reused": 1,
  "jobs": [
    {"id": "3f9a1c7d2b8e4f60a1c3d5e7f9b0a2c4d6e8f0a1b2c3d4e5f60718293a4b5c6d", "productId": "1042", "source": "main", "status": "ready", "filename": "3f9a…c6d.png", "reservedMilliyuan": 24, "reused": true},
    {"id": "7b2d4f6a8c0e1b3d5f7091a2c4e6b8d0f2a4c6e8b0d2f4a6c8e0b2d4f6a8c0e1", "productId": "1042", "source": "detail", "status": "submitted", "taskId": "task_9f8e7d6c", "reservedMilliyuan": 24, "reused": false},
    {"id": "a1c3e5b7d9f0a2c4e6b8d0f2a4c6e8b0d2f4a6c8e0b2d4f6a8c0e1b3d5f7091", "productId": "1042", "source": "sku", "status": "queued", "reservedMilliyuan": 24, "reused": false}
  ],
  "budget": {"limitMilliyuan": 10000, "reservedMilliyuan": 624, "remainingMilliyuan": 9376, "unitMilliyuan": 24},
  "jobsUrl": "/api/v1/images/jobs?batchId=imgb_20261028_01"
}
```

**错误**

| HTTP | code | 条件 |
|---|---|---|
| 400 | `VALIDATION_ERROR` | items 空 / >200 / size 非法 / 既无 url 也无 imageBase64 |
| 400 | `IMAGE_TOO_LARGE` | 原图 > 15MB |
| 402 | `IMAGE_BUDGET_EXCEEDED` | 累计预扣费将超过 10000 毫央（10 元）；`details.requiredMilliyuan`、`details.reservedMilliyuan` |
| 403 | `READ_ONLY_MODE` | |
| 409 | `CONFLICT_BUSY` | 同 `batchId` 正在处理 |
| 422 | `RETRY_UNSAFE` | `retryOcr=true` 但存在结果未确认任务，拒绝重复扣费 |
| 429 | `RATE_LIMITED` | **图片配额用尽**（不是 req/s 限流）：分钟 600 / 小时 6000 / 天 60000，任一窗口满即拒（`image_service.py:113-125 _reserve_slot`，`constants.py:60`）。`details.budgets`、`details.nextSlotSeconds` |
| 502 | `UPSTREAM_ERROR` | 提交被拒（未产生费用） |
| 502 | `UPSTREAM_UNKNOWN_RESULT` | 请求结果未确认，可能已扣费，任务置 `unknown`，**不会自动重试** |
| 503 | `IMAGE_KEY_MISSING` | **图片服务密钥未配置**：`_api_key()` 在下载原图之前就抛（`image_service.py:134-141`，抛点在 `:138`），message 提示设置 `AOHUANG_IMAGE_KEY` 或写入数据目录 `image-key.txt`。这条是配置错误，重试无用 |

实测未配密钥时：

```json
{"error":{"code":"IMAGE_KEY_MISSING","message":"图片翻译服务未配置密钥：请设置 AOHUANG_IMAGE_KEY 或写入数据目录的 image-key.txt","details":{},"requestId":"req_…"}}
```

HTTP 503。当前配额见 `GET /images/limits`（实测 `{"limits":{"minute":600,"hour":6000,"day":60000,"budgetMilliyuan":10000},...}`）。

> 全站只有这一条路由有 429。其它接口没有限流、没有令牌桶，靠上游妙手自己的 QPS 限制（`UPSTREAM_PACE_SECONDS = 1.5`，`constants.py:74`）。
>
> `retryOcr` 这个参数名容易误解：**当前没有 OCR 能力**，它只用于「是否允许重试结果未确认的任务」和「是否跳过 unknown 复用」（`image_service.py:435,451,496`），不触发任何文字识别。

> 说明：`reused=true` 的任务不占新的费用额度；`reservedMilliyuan` 是历史预扣值，原样返回。

### 7.2 GET /images/jobs/{id}

查询单个任务。`{id}` 传 64 位 sha256 或 `batchId`（用 `?batchId=` 查列表，见 7.3）。

**Path / Query**

| 位置 | 参数 | 必填 | 说明 |
|---|---|---|---|
| path | id | 是 | 任务 id |
| query | refresh | 否 | `true` 时向图片服务查询 task 状态并落库 |

**响应 200**

```json
{
  "job": {
    "id": "7b2d4f6a8c0e1b3d5f7091a2c4e6b8d0f2a4c6e8b0d2f4a6c8e0b2d4f6a8c0e1",
    "batchId": "imgb_20261028_01",
    "productId": "1042",
    "detailId": "778812",
    "source": "detail",
    "sourceUrl": "https://img.91miaoshou.com/oss/778812/detail-1.jpg",
    "status": "submitted",
    "model": "gpt-image-2",
    "size": "1:1",
    "reservedMilliyuan": 24,
    "taskId": "task_9f8e7d6c",
    "upstreamStatus": "processing",
    "filename": null,
    "previewUrl": null,
    "message": "图片任务已提交，正在查询上游结果",
    "retryOf": null,
    "attempt": 0,
    "createdAt": 1761617000.442,
    "updatedAt": 1761617002.118,
    "readyAt": null
  },
  "progress": {"polledAt": 1761617002.118, "elapsedSeconds": 1.7, "estimateSeconds": 25}
}
```

`status` 五态：

| 值 | 含义 | 前端行为 |
|---|---|---|
| `queued` | 等本地并发槽 | 显示排队，轮询 |
| `submitted` | 已提交，等上游 | 显示进度，轮询 |
| `ready` | 成功，`previewUrl` 可取 | 停止轮询 |
| `unknown` | 结果未确认，可能已扣费 | 停止轮询，人工核对，禁止自动重试 |
| `failed` | 上游明确失败 | 停止轮询，可显式重试 |

`GET /images/jobs/{id}/file` 返回 PNG/JPEG/WebP 二进制（`Content-Disposition: inline`），仅 `ready` 状态存在。

**错误**

| HTTP | code | 条件 |
|---|---|---|
| 404 | `NOT_FOUND` | 任务 id 不存在 |
| 404 | `RESULT_FILE_MISSING` | 状态 `ready` 但本地文件缺失（`details.id`），禁止自动重新提交 |
| 502 | `UPSTREAM_ERROR` | `refresh=true` 时上游查询失败（任务状态不改） |

### 7.3 GET /images/jobs（补充）

`?batchId=&productId=&status=&page=1&pageSize=50`，返回分页信封，`items` 结构同 7.2 的 `job`。

### 7.4 POST /images/analyze（主图文字识别 / 本地 OCR）

选主图前先看图上有没有中文，**不消耗图片翻译预算**。路由 `api/images.py:21-36`，服务层 `services/image_review_service.py`。

> **不是老版 `analyze-images` 的 1:1 移植**：老版返回 `classify_image` 的完整字段（含 `obviousChinese`、`words[]` 逐词位置），新版只给 `chineseText` + `text`，够用来决定「这张图要不要翻译/换掉」。若前端要逐词坐标，得再扩字段。

**图片来源白名单**（`allowed_source()`，`:86-97`，防 SSRF）

- 必须 `https`、端口空或 443、URL 里不能带 `user:pass@`；
- host 必须在 `ALLOWED_HOSTS`（`cbu01/img/gd1..gd4.alicdn.com`、`img/earth-rt/static.chengji-inc.com`）或后缀命中 `.alicdn.com` / `.chengji-inc.com` / `.taobao.com` / `.tmall.com`；
- 再过 `_is_private_host()`：解析出的 IP 里只要有私有网段 / 回环 / 链路本地 / 保留地址就拒（防把 OCR 当内网探测器）。**唯一例外是 `198.18.0.0/15`（RFC 2544 benchmark 网段）**——本机代理把公网域名解析到这里，一并拒绝会导致所有图床不可用。

**OCR 前置条件**：调用 `server/services/windows_ocr.ps1` 走 `powershell.exe`（**Windows PowerShell 5.1**）。**PowerShell 7 不支持 WinRT，会报 `Operation is not supported on this platform (0x80131539)`**；Mac/Linux 上没有这套 OCR，一律落到 `error=true`。

**限制**：单图 ≤15 MB、OCR 超时 30s、并发 3、一次 ≤ `ANALYZE_LIMIT=20` 张；结果按 URL 的 `sha256` 缓存在 `%LOCALAPPDATA%/aohuang-desktop/images/ocr/`（`OCR_CACHE_VERSION` 变更即失效）。

**请求**

```json
{"images": ["https://earth-rt.chengji-inc.com/picture/12807302/2026-07-28/342127e8-….png"], "retry": false, "limit": 20}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| images | string[] | 是 | 非空、≤20；空数组 → 400（`field=images`）；超过 20 张 → 400 |
| retry | boolean | 否 | `true` 忽略缓存重跑 OCR |
| limit | integer | 否 | 实际处理张数上限，默认 20 |

**响应 200**（实测：一张真实商品主图 `earth-rt.chengji-inc.com`，OCR 正常执行但**图上没识别到文字**）

```json
{
  "items": [
    {"index": 1, "url": "https://earth-rt.chengji-inc.com/picture/12807302/2026-07-28/342127e8-….png",
     "action": "review", "reason": "图上没识别到文字，可保留或人工看图",
     "hasChinese": null, "chineseText": "", "text": "", "error": false}
  ],
  "summary": {"total": 1, "keep": 0, "translate": 0, "review": 1, "withChinese": 0, "errors": 0}
}
```

**响应 200**（实测：白名单外的域名 `example.com`，逐张失败但整体 200）

```json
{
  "items": [
    {"index": 1, "url": "https://example.com/a.jpg", "action": "review",
     "reason": "该图片来源暂未接入本地识别（只支持 cbu01.alicdn.com 原图）",
     "hasChinese": null, "chineseText": "", "text": "", "error": true}
  ],
  "summary": {"total": 1, "keep": 0, "translate": 0, "review": 1, "withChinese": 0, "errors": 1}
}
```

> 注意上面这条 `reason` 文案是**过时的**：代码白名单其实已扩到 alicdn/chengji-inc/taobao/tmall 多个域，但提示语还写着「只支持 cbu01.alicdn.com」。判断能不能识别请看 `error` 字段，别按文案猜。

| 字段 | 说明 |
|---|---|
| items[].action | `keep`（无中文，可直接用）/ `translate`（识别到中文，建议翻译）/ `review`（没识别到文字或 OCR 不可用，人工看图） |
| items[].hasChinese | `true` / `false` / `null`（`null` = 没结论） |
| items[].chineseText | 抽出的中文片段 |
| items[].text | OCR 原文 |
| items[].error | 该张识别失败/被拒（不影响整体 200） |
| summary | `total` / `keep` / `translate` / `review` / `withChinese` / `errors` |

**错误**

| HTTP | code | 条件 |
|---|---|---|
| 400 | `VALIDATION_ERROR` | `images` 为空（实测「images 必须是非空数组」）或超过 20 张 |

> 这**不是** `POST /images/translate` 的前置校验：`translate` 不会自动调用它，也不会因为图上有中文就拒绝翻译。`retryOcr`（§7.1）与这里无关。
> 前端入口：图片翻译页的「识别主图」按钮（`web/src/features/images/ImagesPage.tsx` → `useAnalyzeImages()`）。

---

## 8. 发布

### 8.1 POST /publish

单个或批量发布。不可撤销，三重保险：总开关 + `confirm` + 显式列表。

**请求**

```json
{
  "items": [
    {"warehouseId": "1042"},
    {"detailId": "778812", "shopId": "18077147"}
  ],
  "shopId": "18077152",
  "accountId": "1",
  "template": "马来",
  "exchangeRate": "0.6521",
  "language": "ms",
  "allowMismatch": false,
  "confirm": true,
  "claimOnly": false,
  "titles": {"778812": "Kasut Sukan Wanita Breathable Mesh Sneakers"}
}
```

| 字段 | 类型 | 必填 | 约束 | 说明 |
|---|---|---|---|---|
| items | array | 是 | 1–200 | 每项给 `warehouseId` 或 `detailId`+`shopId` |
| items[].warehouseId | string | 条件 | 存在且状态 `ready`/`claimed` | 仓库来源，允许跨店铺（走载体重建） |
| items[].detailId | string | 条件 | 数字，采集箱存在该明细 | 直接发布（同店铺草稿） |
| items[].shopId | string | 条件 | 与 detailId 配对 | 明细所在店铺 |
| shopId | string | 是 | 目标店铺 | 必须属于 `accountId` |
| accountId | string | 否 | 默认当前连接 | |
| template | string\|null | 否 | 已存模板名 | 给定时会对跨店铺项重算售价 |
| exchangeRate | string\|null | 否 | >0 且 ≤1000 | 覆盖模板里的汇率 |
| language | string\|null | 否 | `ms`/`en`/`th`/`vi`/`zh` | 用于站点语言校验 |
| allowMismatch | boolean | 否 | 默认 `false` | 明确接受语言与站点不匹配 |
| confirm | boolean | 是 | 必须字面量 `true` | |
| claimOnly | boolean | 否 | 默认 `false` | 只认领进采集箱，不上架 |
| titles | object | 否 | `{detailId: title}` | 写发布日志用，避免首屏显示空标题 |

**响应 200**

```json
{
  "publishRequested": true,
  "mode": "carrier",
  "batchId": "pub_01HB7ZQ1KM",
  "targetShopId": "18077152",
  "published": ["800901", "800902"],
  "claimed": [],
  "failures": [],
  "warning": null,
  "repriced": [
    {"warehouseId": "1042", "priceBefore": "35.90", "priceAfter": "59.90", "currency": "MYR", "weightKg": "0.65"}
  ],
  "records": {
    "total": 2,
    "items": [
      {"logId": "521", "detailId": "800901", "shopId": "18077152", "shopName": "LuoMan MY 2", "status": "processing", "reason": "已提交，平台尚未返回结果", "requestedAt": 1761618000.113}
    ]
  }
}
```

| 字段 | 说明 |
|---|---|
| mode | `direct`：全部明细就在目标店铺，直接发布草稿；`carrier`：经公共采集箱重建载体后发布 |
| published | 平台接受的明细 id（载体重建后是新 id） |
| claimed | `claimOnly=true` 时被认领的明细 id |
| warning | `mode=direct` 且传了 `template` 时提示「模板未生效，价格已固定」 |
| repriced | 跨店铺且传模板时的改价明细 |

`mode=direct` 时 `template` 不生效，必须返回 `warning`：

```json
{"warning": "这 1 件就在目标店铺，发布的是平台里已有的草稿，价格已经固定；模板「马来」没有生效。要改价请先按该模板重新保存商品。"}
```

**错误**

| HTTP | code | 条件 |
|---|---|---|
| 400 | `VALIDATION_ERROR` | items 空/>200/字段不合法 |
| 400 | `CONFIRM_REQUIRED` | `confirm` 不是 `true` |
| 403 | `READ_ONLY_MODE` | 只读模式 |
| 403 | `PUBLISH_DISABLED` | `settings['publish.enabled'] = false` |
| 404 | `NOT_FOUND` | warehouseId/detailId/店铺不存在（`details.missing`） |
| 409 | `CONFLICT_BUSY` | 该批商品已有发布在途 |
| 422 | `SITE_LANGUAGE_MISMATCH` | 语言与目标站点不符且 `allowMismatch != true` |
| 422 | `TEMPLATE_INVALID` | 模板不存在或校验失败（`details.field` 指出哪一项） |
| 422 | `CARRIER_UNAVAILABLE` | 载体池不足 |
| 422 | `SHOP_NOT_IN_ACCOUNT` | 目标店铺不属于该账号 |
| 502 | `UPSTREAM_ERROR` | 妙手拒绝（`details.upstream.msg`） |

> 已经写入载体但发布失败时，返回 `502 UPSTREAM_ERROR`，`details.savedCount` 给出已写入数量，避免操作者重复操作。

### 8.2 GET /publish/logs

只返回**本机发起**的发布记录，按 `requestedAt` 倒序；平台尚未回执的显示 `processing`。

**Query**

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| page | integer | 否 | 1 | |
| pageSize | integer | 否 | 20 | 1–100 |
| status | string | 否 | null | `processing`/`success`/`fail`/`cancel` |
| accountId | string | 否 | 当前连接 | |
| shopId | string | 否 | null | |
| scheduleId | string | 否 | null | |
| since | number | 否 | null | Unix 秒，只取该时间之后 |
| refresh | boolean | 否 | `true` | 是否回源平台更新状态 |

**响应 200**

```json
{
  "items": [
    {
      "logId": "521",
      "detailId": "800901",
      "warehouseId": "1042",
      "warehouseKey": "778812:18077147",
      "shopId": "18077152",
      "shopName": "LuoMan MY 2",
      "title": "Kasut Sukan Wanita Breathable Mesh Sneakers",
      "status": "success",
      "reason": "",
      "mode": "carrier",
      "batchId": "pub_01HB7ZQ1KM",
      "scheduleId": null,
      "accountId": "1",
      "platformItemId": "1729384756",
      "itemEditUrl": "https://seller.tiktokglobalshop.com/product/1729384756",
      "attempts": 1,
      "requestedAt": 1761618000.113,
      "finishedAt": 1761618032.554
    }
  ],
  "page": 1,
  "pageSize": 20,
  "total": 2,
  "hasMore": false,
  "benchOnly": true,
  "platformTotal": 483,
  "degraded": false
}
```

| 字段 | 说明 |
|---|---|
| benchOnly | 恒为 `true`：平台历史（手工发布）不在此列表 |
| platformTotal | 平台侧该账号的发布记录总数，仅供对照 |
| degraded | `refresh=true` 但回源失败时 `true`，`status` 保持库里原值 |

**错误**

| HTTP | code | 条件 |
|---|---|---|
| 400 | `VALIDATION_ERROR` | status 非法 |
| 401 | `AUTH_REQUIRED` | 未连接且库中无记录 |
| 502 | `UPSTREAM_ERROR` | `refresh=true` 且上游失败（`degraded=true` 时不报错） |

---

## 9. 定时发布

### 9.1 GET /schedules

**Query**：`accountId`（可选）、`enabled`（可选，布尔）。

**响应 200**

```json
{
  "items": [
    {
      "id": "task-9f2c1d7e4a5b",
      "name": "罗蔓 每天 09:30",
      "enabled": true,
      "accountId": "1",
      "account": "妙手罗蔓",
      "shopId": "18077147",
      "shopName": "LuoMan Store",
      "template": "马来",
      "frequency": "daily",
      "days": [],
      "dayOfMonth": [],
      "startDate": "",
      "endDate": "",
      "time": "09:30",
      "perRun": 10,
      "claimOnly": false,
      "lastRunDate": "2026-10-27",
      "attemptDate": "2026-10-27",
      "attempts": 0,
      "nextDueAt": 1761619800.0,
      "nextDueText": "2026-10-28 09:30",
      "createdAt": 1761500000.0,
      "updatedAt": 1761610000.0,
      "lastResult": {"status": "ok", "count": 10, "failed": 0, "at": 1761619800.0}
    }
  ],
  "total": 1,
  "serverTime": 1761618600.44
}
```

### 9.2 POST /schedules

**请求**

```json
{
  "name": "罗蔓 每天 09:30",
  "enabled": true,
  "accountId": "1",
  "shopId": "18077147",
  "template": "马来",
  "frequency": "daily",
  "days": [],
  "dayOfMonth": [],
  "startDate": "",
  "endDate": "",
  "time": "09:30",
  "perRun": 10,
  "claimOnly": false
}
```

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| name | string\|null | 否 | 1–120；缺省 `{account} 定时发布` |
| enabled | boolean | 否 | 默认 `true` |
| accountId | string | 是 | 已保存账号 |
| shopId | string | 是 | 必须属于该账号（保存时校验一次，执行前再校验） |
| template | string | 是 | 已存模板名 |
| frequency | string | 是 | `daily`/`weekly`/`monthly`/`range` |
| days | integer[] | 条件 | `weekly` 必填，1–7，至少 1 个 |
| dayOfMonth | integer[] | 条件 | `monthly` 必填，1–31，至少 1 个 |
| startDate | string | 条件 | `range` 必填，`YYYY-MM-DD` |
| endDate | string | 条件 | `range` 必填，≥ startDate，跨度 ≤180 天 |
| time | string | 是 | `HH:MM`，00:00–23:59 |
| perRun | integer | 是 | 1–200 |
| claimOnly | boolean | 否 | 默认 `false` |

**响应 201**

```json
{
  "task": {
    "id": "task-9f2c1d7e4a5b",
    "name": "罗蔓 每天 09:30",
    "enabled": true,
    "accountId": "1",
    "shopId": "18077147",
    "template": "马来",
    "frequency": "daily",
    "days": [],
    "dayOfMonth": [],
    "startDate": "",
    "endDate": "",
    "time": "09:30",
    "perRun": 10,
    "claimOnly": false,
    "lastRunDate": "",
    "attemptDate": "",
    "attempts": 0,
    "nextDueAt": 1761619800.0,
    "nextDueText": "2026-10-28 09:30",
    "createdAt": 1761618600.44,
    "updatedAt": 1761618600.44
  }
}
```

**错误**

| HTTP | code | 条件 |
|---|---|---|
| 400 | `VALIDATION_ERROR` | 任一约束不满足（`details.field` 指明） |
| 403 | `READ_ONLY_MODE` | 只读模式 |
| 404 | `NOT_FOUND` | accountId/shopId/模板不存在 |
| 422 | `UNPROCESSABLE` | endDate < startDate、跨度 >180 天 |
| 422 | `SHOP_NOT_IN_ACCOUNT` | 店铺不属于该账号 |

### 9.3 PATCH /schedules/{id}

局部更新，只传要改的字段；`id` 不可改。校验规则同 POST，但有两处差异：

- 用 `exclude_unset=True`（**没有** `exclude_none`，`api/schedules.py:37`），所以**可以显式传 `null` 清空** `startDate` / `endDate` / `days` / `dayOfMonth`。
- **不强制**重传 `template`：局部更新时沿用原值，原值为空才校验；只改 `time`/`perRun` 不会被模板校验拦住。

**请求**

```json
{
  "enabled": false,
  "time": "21:15",
  "perRun": 20
}
```

**响应 200**

```json
{
  "task": {
    "id": "task-9f2c1d7e4a5b",
    "enabled": false,
    "time": "21:15",
    "perRun": 20,
    "nextDueAt": null,
    "nextDueText": "已停用",
    "updatedAt": 1761618900.12
  }
}
```

**错误**

| HTTP | code | 条件 |
|---|---|---|
| 400 | `VALIDATION_ERROR` | 空 body 或字段非法 |
| 403 | `READ_ONLY_MODE` | |
| 404 | `NOT_FOUND` | 任务不存在 |
| 409 | `CONFLICT_BUSY` | 该任务正在执行（`details.runningSince`） |
| 422 | `SHOP_NOT_IN_ACCOUNT` | 改后的店铺不属于账号 |

### 9.4 DELETE /schedules/{id}

**响应 200**

```json
{
  "deleted": true,
  "id": "task-9f2c1d7e4a5b",
  "logsKept": 37
}
```

历史 `publish_logs` 保留（`schedule_id` 置 `NULL`）。

**错误**

| HTTP | code | 条件 |
|---|---|---|
| 403 | `READ_ONLY_MODE` | |
| 404 | `NOT_FOUND` | |
| 409 | `CONFLICT_BUSY` | 正在执行 |

### 9.5 POST /schedules/{id}/run

立即执行一次（演练用）。**同步**执行完才返回（`api/schedules.py:52-69` 调 `schedule_service.execute(..., raise_on_error=True, with_results=True)`），没有 120s 超时 + `202 runId` 那一套（`process` 的前端调用给了 120s 客户端超时，仅此而已）。

**响应 200**

```json
{
  "taskId": "task-9f2c1d7e4a5b",
  "status": "partial",
  "requested": 10,
  "selected": 7,
  "published": 6,
  "failed": 1,
  "count": 6,
  "reason": "计划 10 件，实际选中 7 件，成功 6 件，失败 1 件",
  "results": [
    {"detailId": "800901", "status": "published", "title": "Kasut Sukan Wanita …"},
    {"detailId": null, "status": "failed", "reason": "店铺 18077147 已不可用"}
  ],
  "at": 1761619100.5
}
```

| 字段 | 含义（`schedule_service.summarize()`，`:554-593`） |
|---|---|
| requested | 计划的 `perRun`（**本轮最多取多少件，不是必须发满**） |
| selected | 实际选中（可为 0，也可小于 `requested`） |
| published | 成功数；`claimOnly=true` 时等于认领成功数 |
| failed | 实际失败数 = `publish.failures` 条数 + `(selected - published)`，上限收敛到 `selected`；抛异常时按选中件全失败计。**不会用 `len(items)` 当失败数** |
| count | 与 `published` 相同，只是给旧前端留的兼容字段 |
| status | `empty`（没货可发，`selected=0`）/ `ok`（全成功）/ `partial`（有成功也有失败或少于选中）/ `failed`（`published=0` 或抛异常）。判定见 `run_status()`（`:529-539`） |
| results | 逐条明细（成功项 + 失败项） |

> 与后台 `run_due` 共用同一套摘要字段与同一条 `execute()`，所以手动演练的数字和定时执行的口径一致。
  ],
  "at": 1761619100.5
}
```

`status`：`ok` / `short` / `failed` / `empty`。

**错误**

| HTTP | code | 条件 |
|---|---|---|
| 403 | `READ_ONLY_MODE` | |
| 404 | `NOT_FOUND` | |
| 409 | `CONFLICT_BUSY` | 同一任务已在执行 |
| 422 | `SHOP_NOT_IN_ACCOUNT` | 执行前校验失败 |

---

## 10. 违规词库

### 10.1 GET /violation-words

**Query**

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| action | string | 否 | null | 仅支持 `remove`；省略时不过滤 |
| q | string | 否 | null | 词面模糊匹配 |
| page | integer | 否 | 1 | |
| pageSize | integer | 否 | 200 | 1–500 |

**响应 200**

```json
{
  "items": [
    {"id": "1", "word": "免邮", "normalized": "免邮", "action": "remove", "source": "seed", "enabled": true, "createdAt": 1761000000.0},
    {"id": "2", "word": "全网最低价", "normalized": "全网最低价", "action": "remove", "source": "seed", "enabled": true, "createdAt": 1761000000.0},
    {"id": "3", "word": "wholesale", "normalized": "wholesale", "action": "remove", "source": "manual", "enabled": true, "createdAt": 1761619200.1}
  ],
  "page": 1,
  "pageSize": 200,
  "total": 187,
  "hasMore": false,
  "counts": {"remove": 187, "review": 0, "limit": 500, "remaining": 313}
}
```

词库是全局扁平列表：`action` 固定为 `remove`，`review` 仅为兼容计数字段且始终为 0；传 `action=review` 会返回参数错误。`source` 只说明录入渠道：`seed`（内置）、`import`（批量导入）、`manual`（手动添加），不记录或证明官方规则出处，也不表示适用的站点/市场。当前数据没有规则链接、版本或市场范围，不能据此认定词条已由平台政策核验。

### 10.2 POST /violation-words

支持单条增删、批量导入/删除和清空。词条只有删除这一种匹配动作；新建与导入时不选择 `review` 分类。

**请求（单条新增）**

```json
{"action": "add", "word": "厂家直销", "category": "remove"}
```

**请求（单条删除）**

```json
{"action": "remove", "word": "厂家直销"}
```

**请求（批量导入）**

```json
{"action": "import", "words": ["秒杀", "限时特价", "drop shipping"], "category": "remove", "mode": "append"}
```

**请求（批量删除 / 清空）**

```json
{"action": "remove-many", "words": ["秒杀", "限时特价"]}
```

```json
{"action": "clear", "category": "remove", "confirm": true}
```

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| action | string | 是 | `add` / `remove` / `import` / `remove-many` / `clear` |
| word | string | 条件 | `add`/`remove` 必填；1–64 可见字符（无控制字符） |
| words | string[] | 条件 | `import`/`remove-many` 必填；1–500，去重 |
| category | string | 否 | 仅支持 `remove`（默认）；其他值返回参数错误 |
| mode | string | 否 | `append`（默认）/`replace` |
| confirm | boolean | 条件 | `clear` 必须 `true` |

**启停**

- 通过 **`PATCH /violation-words/enabled`（§10.3）**切换 `enabled`；停用不会删除词条，也不会改变 `source` 或 `createdAt`。
- `POST /violation-words` 不支持 `enable` / `disable` action；启停不改变词条来源或规则出处，因为库中没有保存规则证据。

**响应 200（`POST /violation-words`）**

```json
{
  "items": [
    {"id": "188", "word": "厂家直销", "action": "remove", "source": "manual", "enabled": true, "createdAt": 1761619300.0}
  ],
  "added": 1,
  "removed": 0,
  "skipped": 0,
  "counts": {"remove": 188, "review": 0, "limit": 500, "remaining": 312}
}
```

**错误**

| HTTP | code | 条件 |
|---|---|---|
| 400 | `VALIDATION_ERROR` | action 非法、word 为空/超长/含控制字符 |
| 403 | `READ_ONLY_MODE` | |
| 409 | `DUPLICATE_WORD` | `add` 时词已存在（`details.existing = "remove"`） |
| 409 | `CONFLICT_BUSY` | 商品批次运行中，词库不可改 |
| 422 | `WORD_LIMIT_EXCEEDED` | 词库超过 500 条（`details.limit`、`details.current`） |

### 10.3 PATCH /violation-words/enabled

只翻 `enabled` 位，不删词、不改 `source` / `createdAt`。停用后该词不参与标题匹配；重新启用后仍按删除词处理。

**请求**

```json
{"enabled": false, "words": ["免邮", "全网最低价"]}
```

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| enabled | boolean | 是 | `true` 启用 / `false` 停用 |
| words | string[] | 是 | 至少 1 个；找不到的词进 `missing`，不报错 |

**响应 200**（实测：`{"enabled":false,"words":["zzz-not-exist"]}`）

```json
{"updated": 0, "missing": ["zzz-not-exist"], "enabled": false, "counts": {"remove": 24, "review": 0, "limit": 500, "remaining": 476}}
```

| 字段 | 说明 |
|---|---|
| updated | 实际改了 `enabled` 的词数（`missing` 里的不算） |
| missing | 库里不存在的词（规范化后比对） |
| enabled | 本次设成的值 |
| counts | 词库计数与上限 |

**错误**

| HTTP | code | 条件 |
|---|---|---|
| 400 | `VALIDATION_ERROR` | `words` 为空（「请提供要启停的词」） |
| 403 | `READ_ONLY_MODE` | 只读模式 |

---

## 11. 系统

### 11.1 GET /system/version

本地版本 + 更新清单比对。不需要连接妙手。

**Query**：`refresh`（布尔，默认 `true`）。注意：`system_service.check_update()` 里 `fetch_manifest(url) if refresh else fetch_manifest(url)`（`system_service.py:87`）两个分支是同一个调用，**`refresh=false` 并不会用缓存，仍会回源**。清单地址来自 `settings['update.channel'].manifestUrl`，为空时回落到常量 `UPDATE_MANIFEST_URL`（`system_service.py:55-56`）。

**字段来源**

| 字段 | 来源 |
|---|---|
| `current` | **后端 `APP_VERSION`**，其值由 `core/constants.py:9-30` 的 `_package_version()` 从**仓库根 `package.json` 的 `version`** 读出来（找不到文件时回退 `"0.1.0"`）。不是远端版本，也不读 `app/package.json` |
| `latest` / `url` / `sha256` / `sizeBytes` / `notes` / `publishedAt` | **远端更新清单** `version.json`（`system_service.py:91-103`）；拉不到时 `latest=""`、`ok=false`、带 `error` |
| `updateAvailable` | `bool(latest and url and is_newer(latest, APP_VERSION))`（`system_service.py:96`），纯版本号比较 |

> `current` 与安装包版本的一致性依赖「根 `package.json` 的 version == Electron 安装包版本」这个约定。`app/package.json` 单独写了一份 0.1.0，二者靠人工同步，没有构建期校验 —— 这仍是待办（见 `docs/status.md`）。

实测（`GET /system/version`，2026-10-27 本机，清单可达）：

```json
{"name":"奥皇上品","nameEn":"AOHUANG Listing","current":"0.1.0","latest":"0.1.4","updateAvailable":true,"ok":true,"channel":"stable","manifestUrl":"https://aohuang888.com/tk-product-automation/update/version.json","platform":"win32-x64","checkedAt":1793087111.157,"url":"https://aohuang888.com/tk-product-automation/update/AOHUANG-Listing-Setup-0.1.4.exe","sha256":"1bfac9b4b28348b9c053203bc760e990c4ca7640ef9ba385802f3cdb1d99c7cd","sizeBytes":null,"notes":"软件内一键更新：自动下载、静默安装、自动重开","publishedAt":"2026-10-27"}
```

也就是说：清单里 `latest=0.1.4` 比本地 `current=0.1.0` 新，`updateAvailable=true`。但清单的 `notes` 写着「自动下载、静默安装、自动重开」——**这个能力当前不存在**（见 §11.2），`sizeBytes` 也是 `null`（清单没给）。

**响应 200（示例：清单里有一个更新的版本）**

```json
{
  "name": "奥皇上品",
  "nameEn": "AOHUANG Listing",
  "current": "1.0.0",
  "latest": "1.0.2",
  "updateAvailable": true,
  "ok": true,
  "url": "https://aohuang888.com/tk-product-automation/update/AOHUANG-Listing-1.0.2-setup.exe",
  "sha256": "9f2c1d7e4a5b6c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d",
  "sizeBytes": 87432112,
  "notes": "修复定时任务在跨月时的下一次执行时间显示",
  "publishedAt": "2026-10-27T09:00:00Z",
  "channel": "stable",
  "manifestUrl": "https://aohuang888.com/tk-product-automation/update/version.json",
  "platform": "win32-x64",
  "checkedAt": 1761619400.1
}
```

清单不可达时返回 200 + 错误信息，不抛 5xx（旧行为保持一致）：

```json
{
  "name": "奥皇上品",
  "current": "1.0.0",
  "latest": "",
  "updateAvailable": false,
  "ok": false,
  "error": "无法获取更新信息（URLError）",
  "manifestUrl": "https://aohuang888.com/tk-product-automation/update/version.json",
  "checkedAt": 1761619400.1
}
```

**错误**

| HTTP | code | 条件 |
|---|---|---|
| 400 | `VALIDATION_ERROR` | refresh 非布尔 |
| 401 | `AUTH_REQUIRED` | 无 token |

### 11.2 POST /system/update

**当前只做校验和路径拼装，不下载、不安装、不重启。**

流程（`api/system.py:43-74`）：`validate_update_request()` 校验 url 是 https 且 host 在白名单、sha256 是 64 位十六进制 → 拼出 `<data_dir>/updates/<文件名>` 返回。**这个文件不会存在**（没有任何代码去下载它）。

- `app/src/main.ts` 里没有任何 update 相关 IPC（`grep -n "update" app/src/main.ts app/src/preload.ts` 零命中），只有 `app:boot` 和 `window:*` 几个通道。
- `web/src` 也没有调用 `/system/update` 的地方（`grep -rn "system/update" web/src` 零命中）。
- `DIGEST_MISMATCH`（`errors.py:44`）定义了但零引用 —— 因为没有下载校验环节。
- 所以**「软件内自动下载安装」目前不存在**：更新信息由 `GET /system/version` 返回，下载安装由安装包/浏览器手工完成。

**请求**

```json
{
  "url": "https://aohuang888.com/tk-product-automation/update/AOHUANG-Listing-1.0.2-setup.exe",
  "sha256": "9f2c1d7e4a5b6c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d",
  "restart": true
}
```

| 字段 | 类型 | 必填 | 约束 | 说明 |
|---|---|---|---|---|
| url | string | 是 | https，host 必须在白名单（`aohuang888.com` / `www.aohuang888.com`，`constants.py:10`） | 只校验，不下载 |
| sha256 | string | 是 | 64 位小写十六进制 | **只做格式校验**，没有下载后比对 |
| restart | boolean | 否 | 默认 `true` | 原样回显为 `willRestart`，没有重启动作 |

**响应 200（桌面模式，`accepted=true`）** —— 注意 `version` 恒为 `""`、`sizeBytes` 恒为 `null`、`file` 指向一个不存在的路径：

```json
{
  "ok": true,
  "accepted": true,
  "version": "",
  "file": "C:\\Users\\Administrator\\AppData\\Local\\aohuang-desktop\\updates\\AOHUANG-Listing-1.0.2-setup.exe",
  "sha256": "9f2c1d7e4a5b6c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d",
  "sizeBytes": null,
  "willRestart": true,
  "startedAt": 1761619500.4,
  "url": "https://aohuang888.com/tk-product-automation/update/AOHUANG-Listing-1.0.2-setup.exe"
}
```

**响应 200（共享模式 `AOHUANG_SHARED_MODE=1`，`accepted=false`）**

```json
{
  "ok": true,
  "accepted": false,
  "reason": "shared-mode",
  "url": "https://aohuang888.com/tk-product-automation/update/AOHUANG-Listing-1.0.2-setup.exe",
  "sha256": "9f2c1d7e4a5b6c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d"
}
```

**错误（实际会发生的）**

| HTTP | code | 条件 |
|---|---|---|
| 400 | `VALIDATION_ERROR` | url 缺失/非 https/sha256 格式错 |
| 403 | `FORBIDDEN_ORIGIN` | url host 不在白名单（`system_service.py:118-125`）。实测：`{"url":"https://evil.example.com/x.exe",...}` → 403 `FORBIDDEN_ORIGIN`，`details.host = "evil.example.com"` |
| 403 | `READ_ONLY_MODE` | 只读模式 |

> `422 DIGEST_MISMATCH`、`502 UPSTREAM_ERROR`、`409 UPDATE_IN_PROGRESS` 这三个码**当前不可能出现**（没有下载与并发控制）。清单里保留但它们只是预留。

### 11.3 GET /system/health（无鉴权）

供 Electron 探测端口。

```json
{"ok": true, "name": "aohuang-listing-server", "version": "0.1.0", "db": "ok", "uptimeSeconds": 12.4}
```

`version` 与 `GET /system/version` 的 `current` 同源（`APP_VERSION`，见 §11.1）。`db` 为 `ok`/`migrating`/`error`。此接口不返回任何账号或路径信息。

---

## 12. 载体链接池

跨店铺发布（`POST /publish` 的 `mode=carrier`）需要从「公共采集箱」重建载体，而载体来自池子里的 1688 货源链接。池子空了 `collect_carriers()` 就抛 `422 CARRIER_UNAVAILABLE`。这三条接口是补池子/看清池子的唯一入口（路由 `server/api/carrier.py`，服务层 `services/carrier_service.py`）。

池子存在 `settings` 表的一个 JSON 键里（`carrier_service.LINK_POOL_KEY`），不是独立表。

### 12.1 GET /carrier/links

**Query**：`limit`（1–500，默认 200；只截 `items`，不影响 `total`/`usable`）。

**响应 200**（实测空池：`{"total":0,"usable":0,"items":[],"limit":200}`）

```json
{
  "total": 3,
  "usable": 2,
  "items": [
    {"url": "https://detail.1688.com/offer/681234567890.html", "addedAt": 1761620000.5, "failed": 0, "ok": 4},
    {"url": "https://detail.1688.com/offer/681234567891.html", "addedAt": 1761620001.1, "failed": 3, "ok": 0}
  ],
  "limit": 200
}
```

| 字段 | 说明 |
|---|---|
| total | 池子里全部链接数 |
| usable | `failed < 3` 的条数（失败 3 次的链接不会再被取用） |
| items[].url / addedAt / failed / ok | 链接、入池时间、失败次数、成功次数 |

### 12.2 POST /carrier/links

**请求**：`{"urls": ["https://detail.1688.com/offer/681234567890.html"]}`

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| urls | string[] | 是 | 1–200 条；空数组 → 400；>200 → 400 |

**校验**（`carrier_service.valid_source_link()`，`:63-71`）：必须是 `http(s)`，host 为 `1688.com` 或其子域，且路径里能取到 **≥6 位数字**的 offer id（`/offer/123456.html` 或 `detail.1688.com` 形式）。

**非法链接不会让整批失败**：合法的照加，非法的原样回在 `invalid[]` 里。

**响应 200**

```json
{"added": 2, "skipped": 1, "invalid": ["https://example.com/x"], "requested": 4, "total": 5}
```

| 字段 | 说明 |
|---|---|
| added | 实际入池数 |
| skipped | 合法但池里已有（`url` 去重） |
| invalid | 不满足 1688 规则、被丢弃的原始字符串 |
| requested / total | 请求条数 / 入池后池子总数 |

**错误**：`400 VALIDATION_ERROR`（urls 为空或 >200）、`403 READ_ONLY_MODE`。

### 12.3 DELETE /carrier/links

**请求**：`{"urls": ["https://detail.1688.com/offer/681234567890.html"]}`（空数组 → 400）

**响应 200**

```json
{"removed": 1, "requested": 1, "total": 4}
```

**错误**：`400 VALIDATION_ERROR`、`403 READ_ONLY_MODE`。

> 三个写接口都走 `require_writable`，只读模式 403。

---

## 13. 尺寸表

老版的三件套（`size_charts` / `upload-size-chart` / `delete-size-chart`）在新版的等价实现：**只存书签**（已上传到图床的尺码图 URL），不存图片本体。落 SQLite 的 `size_charts` 表（`name + site` 唯一，大小写不敏感；`site` 为空表示通用）。
它同时是 `settings.workflow.defaults.sizeChartUrl` 的真实消费方：`resolve` 先查本地库，再回落 settings，最后空串。
路由 `server/api/size_charts.py`，服务层 `services/size_chart_service.py`。

### 13.1 GET /size-charts

**Query**：`site`（可选，如 `MY`，服务端大写归一；给了 `site` 就只列该站点的记录，`site` 为空的「通用」记录不混进来）。

**响应 200**（实测空库：`{"items":[],"charts":[],"total":0,"sites":[]}`）

```json
{
  "items": [
    {"id": "1", "name": "MY 女鞋尺码", "site": "MY", "url": "https://aohuang888.com/assets/size-chart-my.jpg", "contentJson": null, "createdAt": 1761620000.1, "updatedAt": 1761620000.1}
  ],
  "charts": ["同上，老版字段名，保留兼容"],
  "total": 1,
  "sites": ["MY"]
}
```

按 `updated_at` 倒序。`charts` 与 `items` 内容相同（兼容老字段名）。

### 13.2 GET /size-charts/resolve

**Query**：`site`（可选）。

**响应 200**（三种 `source`）

```json
{"site": "MY", "url": "https://aohuang888.com/assets/size-chart-my.jpg", "name": "MY 女鞋尺码", "chartId": "1", "source": "local"}
```

| source | 含义 |
|---|---|
| `local` | 命中 `size_charts` 表（站点优先，其次通用记录） |
| `settings` | 本地没有，回落到 `settings.workflow.defaults.sizeChartUrl` |
| `none` | 都没有，`url=""`、`chartId=null` |

### 13.3 GET /size-charts/{id}

**响应 200**：`{"chart": {...}}`（结构同 13.1 的 item）。

**错误**：`400 VALIDATION_ERROR`（id 非数字）、`404 NOT_FOUND`。

### 13.4 POST /size-charts

**请求**

```json
{"name": "MY 女鞋尺码", "site": "MY", "url": "https://aohuang888.com/assets/size-chart-my.jpg", "contentJson": {"rows": [["36", "23.0"]]}}
```

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| name | string | 是 | 1–128，去首尾空格 |
| site | string\|null | 否 | 字母数字、≤8、自动大写；空表示通用 |
| url | string | 是 | `http(s)` 且 host 非空，≤2048 |
| contentJson | object\|array\|string\|null | 否 | 原样存；字符串按 JSON 解析 |

**响应 201**：`{"chart": {...}, "items": [...]}`

**错误**：`400 VALIDATION_ERROR`（name/url/site 非法）、`403 READ_ONLY_MODE`、`409 DUPLICATE_NAME`（同 `name+site` 已存在）、`422`（超过 `MAX_CHARTS=500`）。

### 13.5 PATCH /size-charts/{id}

只改 body 里出现的字段；`contentJson: null` 表示清空。**响应 200**：`{"chart": {...}, "items": [...]}`。
**错误**：`400 VALIDATION_ERROR`（空 body 或字段非法）、`403 READ_ONLY_MODE`、`404 NOT_FOUND`、`409 DUPLICATE_NAME`。

### 13.6 DELETE /size-charts/{id}

**只忘书签，不删图片**（早期入库的商品还在引用同一个 URL）。

**响应 200**

```json
{"deleted": true, "id": "1", "name": "MY 女鞋尺码", "site": "MY", "url": "https://…", "imageKept": true, "items": []}
```

**错误**：`403 READ_ONLY_MODE`、`404 NOT_FOUND`。

---

## 14. 前端调用示例

```ts
// 真实实现在 web/src/lib/api.ts（不是 api/client.ts）
const boot = window.__DSH_BOOT__;

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`${boot.apiBase}/api/v1${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${boot.token}`,
      "X-Client-Version": boot.version,
      ...(init.headers ?? {}),
    },
  });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const err = body?.error ?? { code: "INTERNAL_ERROR", message: "请求失败" };
    throw Object.assign(new Error(err.message), { code: err.code, details: err.details, status: res.status });
  }
  return body as T;
}
```

```ts
// 采集箱勾选后认领
await api(`/collect-box/${detailId}/claim?shopId=${shopId}`, {
  method: "POST",
  body: JSON.stringify({ targetShopId, markCollected: true }),
});
```

```ts
// 采集箱处理入仓（新增章节 §3.4 的调用示例）
const { saved, product } = await api(`/collect-box/${detailId}/process`, {
  method: "POST",
  body: JSON.stringify({
    shopId,              // 必填：明细所在店铺
    language: "ms",
    groupId: "3",        // 可空
    template: "马来",     // 可空
    exchangeRate: "0.6521",
    save: false,         // 先预览：后端会 rollback，不写库
  }),
});
// 确认无误后同样的请求带 save: true 再发一次
```

```ts
// 批量发布（注意：不要再传 Idempotency-Key —— 后端不实现幂等，传了也不会生效）
const result = await api("/publish", {
  method: "POST",
  body: JSON.stringify({
    items: ids.map((warehouseId) => ({ warehouseId })),
    shopId,
    template: "马来",
    language: "ms",
    confirm: true,
  }),
});
```

---

## 16. 标题清理与保存

老版 `/api/preview`（标题清理预览）与 `/api/save-title`（单标题保存）的等价实现。
代码：`server/api/titles.py`、`server/services/title_service.py`。

### 16.1 `POST /titles/preview` — 标题清理预览（不写库、不调上游）

直接复用词库的 `word_service.matcher()` + `clean_title()`：所有启用且命中的词都会从标题里删除。响应保留兼容字段 `reviewWords`，当前始终为空数组，不代表存在复核类词条。

**请求**

| 字段 | 类型 | 说明 |
|---|---|---|
| `titles` | string[] | 待预览的标题；与 `warehouseIds` 二选一，同时给时以 `titles` 为准 |
| `warehouseIds` | string[] | 拿仓库里已有商品的标题来预览；查不到的标识**静默跳过** |
| `limit` | int | 默认 50、上限 200；超了截断，非法值回落默认 |

**响应**

```json
{
  "items": [
    {"index": 0, "original": "好鞋 包邮 秒杀", "cleaned": "好鞋",
     "removedWords": ["包邮", "秒杀"], "reviewWords": [], "changed": true}
  ],
  "total": 1,
  "changedCount": 1
}
```

`titles` 与 `warehouseIds` 全空 → 400 `VALIDATION_ERROR`（`details.field = "titles"`）。

### 16.2 `PATCH /titles/{ident}` — 保存单个商品标题

> **只改本地库，不回写妙手草稿。** 要同步回妙手需要另走妙手的保存接口，当前没有该能力。

`ident` 可以是数据库 id，也可以是 `{detailId}:{shopId}` 形式的 `warehouseKey`。

**请求**：`{"title": "新标题", "clean": false}`

| 字段 | 说明 |
|---|---|
| `title` | 必填，长度 1–255；超范围 400 `VALIDATION_ERROR`（`details.field = "title"`） |
| `clean` | 默认 `false`；为 `true` 时先按词库清理再落库，响应里带 `removedWords` |

**响应**：`{"id", "detailId", "shopId", "title", "previousTitle", "changed", "removedWords"}`

错误：商品不存在或已软删除 → 404 `NOT_FOUND`；只读模式 → 403 `READ_ONLY_MODE`。

---

## 17. 同步

老版 `/api/price-sync` 与 `/api/sync-source-links` 的等价实现。
代码：`server/api/sync.py`、`server/services/sync_service.py`。

### 17.1 `POST /sync/price-templates` — 从妙手拉定价模板列表

请求：`{site?, name?, pageNo?, pageSize?, accountId?}`

- `site` 默认 `"MY"`，大写归一
- `pageSize` 1–20（妙手上限），超范围 → 400 `VALIDATION_ERROR`
- `accountId` 可选；不传用默认账号的凭据，也支持多人各自 key（`X-Miaoshou-AppKey/Secret`）

响应：`{"items": [...], "total": n, "site": "MY", "pageNo": 1, "pageSize": 20}`，`items` 保留上游字段名。

**只读同步、不落库**：用途是让前端下拉选模板，不缓存到本地。

上游不可达 → 502 `UPSTREAM_ERROR`（本机妙手凭据是假的，实测就是这个结果，属预期）。

### 17.2 `POST /sync/source-links` — 从已有成品回收货源链接进池子

请求：`{warehouseIds?: string[], all?: boolean, limit?: number}`

- `warehouseIds` 与 `all=true` 二选一；都没给 → 400 `VALIDATION_ERROR`
- `all=true` 时扫描全部未软删除商品，`limit` 默认 500、最大 2000

响应：`{"scanned": 扫描数, "added": 新增数, "total": 池子总数, "usable": 可用数}`

写操作，需要 `require_writable`；只读模式 → 403 `READ_ONLY_MODE`。
回收逻辑复用 `carrier_service.harvest_from_payloads()`，只收 1688 货源（`valid_source_link()` 校验）。

---

## 18. 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-10-27 | 首版：认证、店铺、采集箱、仓库、分组、类目、图片、发布、定时、词库、系统 |
| v1.0-r2 | 2026-10-27 | **按真实实现校正契约**：删除未实现的幂等（§0.3）与全局令牌桶限流（§0.5，429 仅属图片配额）；`FORBIDDEN_ORIGIN` 改为只在 `POST /system/update` 使用、CORS 不返回 403；`/auth/status` 补 `accounts[].secretHint` 与 `serverName`；采集箱 `pageSize` 改为「1–100 声明、超 20 报 400」、`total`/`hasMore` 按 `max(total,len)` 与 `len>=pageSize` 写实；仓库 `title` 约束改 1–255；§7.1 补 `IMAGE_KEY_MISSING`(503) 与 429 语义；**新增 §3.4 `POST /collect-box/{detailId}/process`**；新增接口索引；端口描述改为后端探测 vs 打包写死 8812 的现状；§11 说明 `current` 来自 `APP_VERSION`（读根 package.json）、`latest` 来自更新清单、更新只返回信息不下载安装 |
| v1.0-r3 | 2026-10-27 | **补并行落地的新接口**：新增 §4.6 `POST /warehouse/price-preview`（含 GET/PUT/PATCH/DELETE 占位 405）、§7.4 `POST /images/analyze`（本地 OCR）、§10.3 `PATCH /violation-words/enabled`、§12 载体链接池（`/carrier/links` ×3）、§13 尺寸表（`/size-charts` ×6）；§9.3 改为「PATCH 支持显式 null、不强制重传 template」；§9.5 摘要字段改为 `requested/selected/published/failed/count/status(empty&#124;ok&#124;partial&#124;failed)`，删掉不存在的 202+runId 说法；接口总数改为 78 个路由对象 / 81 个方法+路径对（附复核命令）；删除「carrier 未挂载」的过时结论 |
| v1.0-r4 | 2026-10-27 | **补齐 P1-1 剩余老版接口**：新增 §16 标题（`POST /titles/preview`、`PATCH /titles/{ident}`，对应老版 `/api/preview` + `/api/save-title`）、§17 同步（`POST /sync/price-templates`、`POST /sync/source-links`，对应老版 `/api/price-sync` + `/api/sync-source-links`）；接口总数 78 对象 / 81 方法+路径对。另：图片翻译完成时新增「译文中文残留」本地 OCR 复查，检出中文只写进 `job.message` 提示、不改状态（老版是直接拒绝，但新版图已扣费，判失败会导致重复付费）；`WordAction` 放开 `enable`/`disable` 两值 |
