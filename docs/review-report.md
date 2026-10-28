# 审查报告

审查对象：`E:\codex工作台\p006\tk-desktop\`（新版 0.1.0，Electron + React + FastAPI）
对照基准：`E:\codex工作台\p006\tk-product-automation\`（老版 0.1.4）
审查日期：2026-10-27
审查方式：读代码 + 实际调接口（127.0.0.1:8812，355 件真实商品 / 3 分组 / 24 违规词 / 1 账号）
审查性质：只读审查，未修改任何业务代码。测试期间的写操作已全部还原（见文末「测试数据还原记录」）。

---

## 结论摘要

- **P0 问题：7 个**
- **P1 问题：11 个**
- **P2 问题：10 个**
- **总体评价：后端仓库/分组/词库/发布校验等「纯本地」部分质量不错，但采集箱整个模块（6 条接口 + 2 处服务层函数）因变量未声明 100% 崩溃，parity-checklist 里 25 项 ✅ 至少有 6 项名不副实；核心的「处理入仓」链路（翻译→定价→违规词→颜色）在后端存在但前端完全没入口，老版 25 条接口中有 15 条新版无对应实现。当前状态不具备交付条件。**

### 关于「25 项标 ✅」的核对结论

`docs/parity-checklist.md` 里标 ✅ 的共 25 项，逐项实测后：

| 结果 | 数量 | 项目 |
|---|---|---|
| 真实实现且验证通过 | 14 | A5、C13、C14、C15、C16、D2、E4、E5、E6、F3、F4、G1、G4、I4 |
| 有实现但存在缺陷 / 执行链路失败 | 9 | A8、D1、D4、F5、F6、F9、G2、G3、G6 |
| 标 ✅ 但根本不可用（接口 500） | 1 | B9 |
| 标 ✅ 但语义不符 | 1 | I3 |

> 说明：
> - **有缺陷的 9 项**里，`D1`（翻译）与 `D4`（颜色）是「服务层实现了但前端无入口」；`F5`（载体重建）是「缺链接池入口」；`F6`（模板重算）是「direct 模式静默不生效且 repriced 无 UI」；`F9`（语言校验）是「当前数据下恒不触发」；`A8`（多人 key）是「仅部分路由支持」；`G2/G3/G6`（定时发布）是「调度判定本身正确，但执行入口 `run_due()` 必崩」（见 P0-4）。
> - **`H4` 不在 ✅ 之列**（清单里 H4「启用开关」未标 ✅），实际情况是：`enabled` 字段存在、`matcher()` 会过滤，但**后端没有任何接口能切换单条词的 enabled**；前端用「停用 = `remove-many`」的变通实现（`WordsPage.tsx:413-420` 有注释说明），语义上「停用」等于「删除」，与老版「保留词条但跳过匹配」不同。这一项属「清单未标 ✅，且实现方式与老版语义不同」，不计入上表，另见 P2-10。
> - 逐项证据见下文各问题条目与「已验证正确的部分」。

---

## P0 问题

### 1. 采集箱整个模块 100% 崩溃：`credentials` 变量未声明（NameError）

- **现象**：`/collect-box` 全部 6 条接口一调就 500，返回 `INTERNAL_ERROR ... (NameError)`。采集箱是「扫描 → 认领 → 处理入仓」的入口，此模块挂了等于核心流程全断。
- **证据**：
  - 实测（`GET /api/v1/collect-box?pageSize=2`）：
    ```json
    {"error":{"code":"INTERNAL_ERROR","message":"服务器内部错误，请把 requestId 反馈给开发者（NameError）","details":{},"requestId":"req_48b858dc74984e6d94b3f10d7f"}}
    ```
  - 服务端日志：`NameError: name 'credentials' is not defined` + `"GET /api/v1/collect-box?pageSize=2 HTTP/1.1" 500 Internal Server Error`
  - 代码位置（`server/api/collect_box.py`）：路由函数签名里**没有** `credentials: CredentialsHeader = None` 参数，但函数体里用了它：
    - `collect_box.py:29`（`list_collect_box`）→ `account_service.resolve_account(db, accountId, credentials)`
    - `collect_box.py:44`（`collect_box_detail`）
    - `collect_box.py:59`（`claim`）
    - `collect_box.py:109`（`claim_batch`）
    - `collect_box.py:138`（`delete_from_collect_box`）
  - 同类问题在 `server/api/categories.py:74`（`sync_categories`）。
  - 静态检查确认（`python -m pyflakes server/`）：
    ```
    .\api\categories.py:74:70: undefined name 'credentials'
    .\api\collect_box.py:29:62: undefined name 'credentials'
    .\api\collect_box.py:44:62: undefined name 'credentials'
    .\api\collect_box.py:59:70: undefined name 'credentials'
    .\api\collect_box.py:109:70: undefined name 'credentials'
    .\api\collect_box.py:138:70: undefined name 'credentials'
    ```
  - 受影响的 6 条接口实测响应全部 500：
    - `GET /collect-box` → 500
    - `GET /collect-box/{detailId}` → 500
    - `POST /collect-box/{detailId}/claim` → 500
    - `POST /collect-box/claim` → 500
    - `POST /collect-box/delete` → 500
    - `POST /categories/sync` → 500
- **影响**：
  - 采集箱页面（前端 `CollectBoxPage.tsx`）永远只能显示「采集箱查询失败」，列表、详情抽屉、单条认领、批量认领、删除全部不可用。
  - `parity-checklist.md` 的 B1–B9 共 9 项、D3 全部作废，其中 B9 明确标了 ✅。
  - 由于前端「处理入仓」入口也不存在（见 P0-3），实际后果是**整条「从采集箱到成品仓库」的链路在 UI 上完全断掉**，只能靠手工 curl。
- **建议**：给这 6 个函数补 `credentials: CredentialsHeader = None` 形参（`categories.sync_categories` 同）。补完后建议在 CI 里加 `pyflakes`/`ruff --select F821`，这一类未定义名是纯静态可查的，不该进到验收阶段。

---

### 2. 采集箱删除函数引用 4 个不存在的名字（即使修好 #1 也照样崩）

- **现象**：`collect_box_service.delete()` 函数体里用了 `AppError`、`client_for`、`upstream_error` 三个未导入的名字，修好 P0-1 的 `credentials` 后这条接口仍然会 NameError。
- **证据**：
  - 代码位置 `server/services/collect_box_service.py:304-331`：
    - `:314` `raise AppError("VALIDATION_ERROR", ...)` — 文件顶部只 `from core import errors`，没有 `AppError`
    - `:316` `raise AppError("VALIDATION_ERROR", "请选择要删除的商品")`
    - `:318` `raise AppError("VALIDATION_ERROR", "一次最多删除 200 件")`
    - `:320` `client = client_for(account)` — 该文件只从 `services.account_service` 导入了 `Account, account_client, map_upstream_error, require_shop`
    - `:324` `raise upstream_error(exc) from None` — 同样未导入
  - pyflakes 确认：
    ```
    .\services\collect_box_service.py:314:19: undefined name 'AppError'
    .\services\collect_box_service.py:316:15: undefined name 'AppError'
    .\services\collect_box_service.py:318:15: undefined name 'AppError'
    .\services\collect_box_service.py:320:14: undefined name 'client_for'
    .\services\collect_box_service.py:324:15: undefined name 'upstream_error'
    ```
  - 另外 `AppError` 的构造签名也不对：`core/errors.py:56` 是 `AppError(code, message, *, status, details)`，而这里传的是 `AppError("VALIDATION_ERROR", "...")`（缺 status，虽然 keyword-only 有默认值 400，勉强能跑，但和项目里其它地方一律用 `errors.validation(...)` 的风格不一致）。
- **影响**：`POST /collect-box/delete`（parity B9，标 ✅）永远不可用；这是清理「带水印废品」的唯一入口，废品会一直堆在采集箱里。
- **建议**：改为 `from core import errors` + `raise errors.validation(...)`，客户端用 `account_service.client_for`，上游错误用 `map_upstream_error`。

---

### 3. 「处理入仓」链路（D1 翻译 / D4 颜色 / 违规词 / 定价 / 主图截断）后端有实现，但前端完全没有入口

- **现象**：`POST /collect-box/{detailId}/process` 是唯一把商品从采集箱搬进成品仓库的接口，`parity-checklist` D1/D4/D7 都挂在它上面；但前端代码里**没有任何地方调用它**，也没有「处理」「入仓」按钮。
- **证据**：
  - 前端全仓搜索 `process`：`grep -rn "collect-box.*process\|/process" web/src` → 无匹配。
  - 前端全仓搜索 `入仓` 按钮：`web/src/features/collect-box/CollectBoxPage.tsx` 里 `入仓` 只出现在文本展示（`:378` `{item.inWarehouse ? " · 已入仓" : ""}`、`:393` 状态徽标、`:676` 徽标），没有任何触发处理的 onClick。
  - `web/src/lib/queries.ts` 里也没有 `useProcess` 之类的 hook（`/collect-box` 相关只有 `useCollectBoxList`、`useCollectBoxDetail`、`useClaimItem`、`useBatchClaim`）。
  - 后端该接口本身可用（除上游不可达外）：实测 `POST /collect-box/3482714692/process` 返回 `502 UPSTREAM_ERROR ConnectError`（假凭据导致，属预期），说明路由和签名是通的。
  - 而且 `POST /collect-box/{detailId}/process` **在 `docs/api.md` 里完全没有记载**（`grep -n "process" docs/api.md` 只命中 `processing` 状态词），契约文档漏了这条核心接口。
- **影响**：
  - D1「标题翻译」、D4「颜色规范化」、D2「违规词」、D7「保存草稿到妙手」这 4 项虽然在服务层实现了，但**用户点不到**，标 ✅ 属名不副实。
  - 新商品无法通过 UI 进仓库；355 件数据是靠 `scripts/import_warehouse.py` 从老版 `warehouse.json` 直接灌进去的，掩盖了这个缺口。
  - 顺带：D7「保存草稿到妙手」在服务层也没实现 —— `warehouse_service.save_from_collect_box()` 全程没有调用 `client.save_shop(...)`（`grep -n "save_shop" server/services/warehouse_service.py server/api/collect_box.py` 无结果），只做了「拉详情 → 翻译 → 定价 → 违规词 → 入库本地」。老版 `product_workflow.py` 是会把结果写回妙手草稿的。
- **建议**：前端在采集箱行操作里加「处理入仓」（可选目标分组 / 语言 / 模板），并补 `api.md` 的 process 章节；D7 需要补 `save_shop` 调用或把该项从清单里降级。

---

### 4. 定时任务执行必失败：`int(result["published"])` 对 list 做 int()

- **现象**：`schedule_service.run_due()` 在发布成功后取 `int(result.get("published") or 0)`，但 `publish()` 返回的 `published` 是 **list**（`publish_service.py:460-475` 的 `"published": published`，`published: list[str] = []`）。每轮定时任务都会在这里抛 TypeError，被外层 `except Exception` 吞掉，然后 `note_result(success=False)`。
- **证据**：
  - 代码 `server/services/schedule_service.py:519`：
    ```python
    ok = int(result.get("published") or 0)
    ```
  - `publish_service.publish()` 返回体（`publish_service.py:466`）：`"published": published,`，其中 `published` 在 `:364` 声明为 `published: list[str] = []`，`:399` 赋值为 `[str(value) for value in detail_ids]`。
  - 实测复现（纯内存，不写库）：
    ```
    result = {"published": ["800901", "800902"], ...}
    int(result.get("published") or 0)
    → TypeError: int() argument must be a string, a bytes-like object or a real number, not 'list'
    ```
  - 后果链：`except Exception as exc:`（`schedule_service.py:523`）捕获 → `note_result(success=False)` → 该任务当天 `attempts += 1`。
- **影响**：
  - 定时发布（parity G1–G6，全部标 ✅）实际上**从来没有成功过一次**。`status.md:75` 写的「定时任务执行 → 走通：选品 → 模板 → 发布请求 → 上游（假凭据 ConnectError，预期）」是**假阳性**：那次失败发生在 `publish()` 内部（ConnectError），根本没走到 `int(...)` 这一行，所以没暴露。只要上游能通、发布真成功，这一行必崩。
  - 更糟的是失败会**累积重试**：`SCHEDULE_MAX_ATTEMPTS_PER_DAY = 3`，`should_run` 判定 `attempts < 3`。实测：
    ```
    should_run(attempts=1): True
    should_run(attempts=2): True
    should_run(attempts=3): False
    ```
    也就是说，**一次成功的发布会被记为失败，然后当天再重跑 2 轮 —— 同一批商品被发布 3 次**。
- **影响等级说明**：之所以列为 P0 而不是 P1，是因为它会导致「重复上架」（不可撤销操作）和「任务永不成功」两个数据级后果。
- **建议**：改为 `ok = len(result.get("published") or [])`。同时建议 `run_due` 的 `except Exception` 至少把 `type(exc).__name__` 打进 `last_result`（现在只打 `str(exc)[:200]`，TypeError 的消息会被记成 `int() argument must be...`，看不出是代码 bug 还是上游错误）。

---

### 5. 载体重建部分失败时，发布日志的 detailId 按位置错配（张冠李戴）

- **现象**：跨店铺发布时如果载体只写入了一部分，`_pair_entries()` 会把「发布后的新 detailId」按**数组下标**配给 entries，而 `fill_carriers` 返回的 `saved` 只包含成功的那些。结果：日志里 A 商品的 detailId 被记成 B 商品的。
- **证据**：
  - `publish_service.py:478-490`：
    ```python
    def _pair_entries(entries, published, claimed):
        ids = claimed or published
        if not ids: return []
        if len(ids) == len(entries):
            return list(zip(entries, ids))
        paired = []
        for index, detail_id in enumerate(ids):
            if index < len(entries):
                paired.append((entries[index], detail_id))   # ← 按下标配，忽略哪些成功了
        return paired
    ```
  - 而 `carrier_service.fill_carriers()`（`carrier_service.py:325-346`）明确返回 `saved`（成功列表）与 `saved_sources`（成功项在 payloads 里的下标），失败项只在 `failures` 里：
    ```python
    return {"saved": saved, "savedSources": saved_sources, "failures": failures, ...}
    ```
  - 实测复现（调用真实函数，纯内存）：
    ```
    entries: ['111', '222']   published: ['900']     # 只有第 2 个载体写入成功
    _pair_entries 结果: [('111', '900')]
    → 日志把 detailId=900 记在 entries[0]（原 111）名下，应为 222
    ```
  - 注意 `publish_service.py:430-434` 明明已经把 `savedSources` 用来回填 `entry["originDetailId"]`，却没有把它传给 `_pair_entries`。
- **影响**：
  - 发布记录（`/publish/logs`、商品详情的 `publishHistory`）显示的 detailId 是错的，操作者按记录去平台核对会核到另一件商品。
  - `_upsert_log` 按 `(account_id, detail_id, shop_id)` 去重（`publish_service.py:132-135`），错配会导致**覆盖掉另一件商品的发布记录**，台账数据丢失。
- **建议**：`_pair_entries` 接收 `savedSources`，按 `savedSources` 的下标配对，而不是按 `published` 的顺序。

---

### 6. 载体重建部分失败时，未发布成功的商品也被标成 `published`

- **现象**：`_mark_published()` 无条件遍历**全部** entries 把 `status` 改成 `published`、`claimed = True`，不检查该 entry 是否真的发布成功。
- **证据**：
  - `publish_service.py:493-508`：
    ```python
    def _mark_published(db, entries, shop_id, account):
        for entry in entries:              # ← 全部 entries，不是成功的那些
            product = entry.get("product")
            if not isinstance(product, Product): continue
            product.claimed = True
            ...
            product.status = "published"
    ```
  - 调用点 `publish_service.py:449-450`：
    ```python
    if published and not claim_only:
        _mark_published(db, entries, target_shop_id, account)
    ```
    条件是「至少有一件成功」，不是「这一件成功」。
  - 实测复现（用真实 `Product` 模型，纯内存）：
    ```
    成功项 status: published claimed: True
    失败项 status: published claimed: True   ← 也被标成 published
    ```
- **影响**：
  - 载体部分失败时，没上架的商品在仓库里显示「已上架」，`stats.totalPublished` 虚高。
  - `schedule_service.pick_products()`（`schedule_service.py:448`）用 `Product.status != "published"` 过滤候选，被误标成 published 的商品**再也不会被定时任务选中**，等于静默丢件。
- **建议**：把成功项（按 `savedSources` / `published` 配对出的 entries）单独传给 `_mark_published`。

---

### 7. 颜色规范化写入两个字段、值不一致（界面显示原始脏值，发布用清洗值）

- **现象**：`save_from_collect_box()` 先调 `build_product()` 生成落库字段（此时 `sku_properties` 已经快照了**原始** `attrValue`），之后才就地清洗 `info["skuPropertyList"]`。结果是 `products.sku_properties`（列表/详情展示）存脏值，`products.payload.skuPropertyList`（发布用）存干净值。
- **证据**：
  - 代码 `warehouse_service.py:571-594`：
    ```python
    fields = product_service.build_product(   # :571  ← 这里 sku_properties 已快照原始值
        info=payload, ...)
    existing = ...
    props = info.get("skuPropertyList") or []  # :581  ← 之后才清洗
    for prop in props:
        if str(prop.get("attrName") or "").casefold() in {"颜色","色彩","color","colour","warna"}:
            for value in prop.get("attrValueList") or []:
                value["attrValue"] = product_service.clean_color(value["attrValue"])   # :588
    ...
    if isinstance(fields["payload"], dict):
        if props:
            fields["payload"]["skuPropertyList"] = props    # :594  ← 只有 payload 拿到清洗值
    ```
  - `product_service.build_product()` 在 `product_service.py:296` 已经把 `"sku_properties": sku_properties(info)` 写进 fields。
  - 实测复现（纯内存）：
    ```
    落库的 sku_properties（列表/详情展示用）: ['Y18[beige]', 'Beige + Black [Two-Piece Set]']
    落库的 payload.skuPropertyList（发布用）:  ['Beige', 'Beige Black']
    → 两处不一致
    ```
  - 真实数据佐证（`GET /warehouse/355`）：`skuProperties` 里 `Warna` 的值仍是 `Ankle-lengthpantsblack`、`Nine-pointstylenordicgray` 这类未规范化文本。
- **影响**：
  - D4 标 ✅ 但实际效果是「界面看得见脏颜色，发出去是干净颜色」，两处不一致会让操作者以为清洗没生效而反复操作。
  - 列表/详情/导出只要读 `sku_properties` 就拿到脏值。
- **建议**：把清洗移到 `build_product()` 之前（对 `info` 就地清洗后再 build），或在 build 之后同步回写 `fields["sku_properties"]`。

---

## P1 问题

### 1. 老版 25 条接口中，新版缺 15 条（含 OCR 校验、图片上传、尺寸表、标题预览、价格预览）

- **现象**：老版 `src/web.py` 暴露的 51 条 `/api/*` 路由里，有 15 条在新版没有对应实现。
- **证据**：老版路由清单（`grep -oE '"/api/[a-z0-9_/{}.-]+"' src/web.py | sort -u`）与新版 `server/api/*.py` 对照：

| 老版接口 | 老版实现 | 新版状态 |
|---|---|---|
| `/api/analyze-images` | `image_review.py` OCR 中文残留校验 | **缺失**（parity E8 自己标了 ⏸ P2） |
| `/api/image-plan` | `title_editor.update_image_plan` 图片处理计划 | **缺失** |
| `/api/save-images` | 保存处理后的图片 | **缺失** |
| `/api/image-service`、`/api/image-service/reconcile` | 图片服务状态与对账 | 部分对应 `/images/limits`、`/images/reconcile` |
| `/api/size-charts`、`/api/upload-size-chart`、`/api/delete-size-chart` | `size_chart_store.py` 尺寸表库 | **缺失**（`settings.workflow.defaults.sizeChartUrl` 存在但无消费方） |
| `/api/preview` | `title_editor.preview()` 标题清理预览 | **缺失** |
| `/api/save-title` | 单标题编辑保存 | **缺失**（新版只有 `PATCH /warehouse/{id}` 改本地标题，不写回妙手） |
| `/api/title-words` | 标题词增删改 | 对应 `/violation-words`（语义已改） |
| `/api/price-preview` | 定价预览（不发布先看价） | **缺失** |
| `/api/price-sync` | 定价模板从妙手同步 | **缺失**（`miaoshou.price_templates()` 客户端方法存在，但无路由调用） |
| `/api/sync-source-links` | 货源链接池同步 | 内部有 `carrier_service.harvest_from_payloads`，但无独立路由 |
| `/api/save-connection`、`/api/connect-saved`、`/api/rename-connection` | 凭据库 | 对应 `/auth/connect`、`/auth/accounts`、`PATCH /auth/accounts/{id}` |
| `/api/bootstrap`、`/api/state`、`/api/control`、`/api/start`、`/api/scan`、`/api/select`、`/api/confirm-save` | 老版单会话状态机 | 新版改为无状态 REST，设计上合理 |
| `/api/add-warehouse-category`、`/api/update-warehouse-category`、`/api/delete-warehouse-product`、`/api/claim-warehouse-product`、`/api/publish-warehouse-batch` | 仓库操作 | 对应 `/groups`、`/warehouse/*`、`/publish` |

- **重点缺失（有业务后果的）**：
  - **`price-preview`**：老版能在不发布的情况下先算价确认；新版只能「发布出去才知道价格」。这对不可撤销的发布是实质性风险。
  - **`size-charts` 三件套**：老版有尺寸表库（`size_chart_store.py`，含 `remember_size_chart` / `forget_size_chart`），新版 `workflow.defaults.sizeChartUrl` 是个空字符串且**没有任何代码读它**（`grep -rn "workflow_defaults\|WORKFLOW_DEFAULTS_KEY" server/` 只有定义和 `system/settings` 的透出）。`product_service.detail_summary()` 只从上游 `info.get("sizeChart")` 读，等于没有本地尺寸表管理能力。
  - **`analyze-images` OCR 校验**：老版用 Windows OCR（`windows_ocr.ps1`）在翻译后二次检查中文残留，`image_review.py:191` `"Reject a translated image when local OCR still finds Chinese text."`。新版完全无此能力，parity E8 标了 ⏸ 可接受，但意味着**图片翻译的中文残留无人把关**，会直接发到平台上。
  - **`image_upload.py`**：老版能把本地图片上传到自有 HTTPS 图床（`DEFAULT_SFTP_PUBLIC_BASE = "https://aohuang888.com/tk-product-automation/translation-temp/"`），新版无上传能力，`/images/translate` 只接受 `url` 或 `imageBase64`（`schemas/requests.py:84-86`），本地图片必须先自己找地方放。
- **影响**：老版操作者的若干习惯路径在新版无对应，迁移时会掉功能。
- **建议**：至少补 `price-preview` 与 `size-charts`；OCR 若确定不做，应在 parity E8 明确写「能力缺失，接受风险」，而不是标 ⏸ P2。

---

### 2. `_mark_published` 之外的第二处：`claimOnly` 分支的认领结果不做失败校验

- **现象**：`claimOnly=true` 时，只要 `client.claim_to_shop()` 没抛异常就认为全部认领成功，把 `claimed_ids` 全量返回；妙手「部分成功」时前端会显示「已认领 N 件」，实际少于 N。
- **证据**：
  - `publish_service.py:371-390`：
    ```python
    if claim_only:
        detail_ids = [int(entry["detailId"]) for entry in entries if str(entry["detailId"]).isdigit()]
        if not detail_ids:
            raise errors.validation("没有可认领的明细", field="items")
        try:
            client.claim_to_shop(detail_ids, [int(target_shop_id)])
        except MiaoshouError as exc:
            raise map_upstream_error(exc) from None
        claimed_ids = [str(value) for value in detail_ids]     # ← 全量，未解析上游返回
    ```
  - 对比 `carrier_service.claimed_detail_ids()`（`carrier_service.py:165-180`）是有解析上游 `platformCollectBoxDetailIdMap` 的能力的，但这条分支没用。
- **影响**：认领结果虚报；操作者以为已认领，实际漏件。
- **建议**：解析 `claim_to_shop` 的返回体（`platformCollectBoxDetailIdMap` / `detailIds`），以实际返回的 id 作为 `claimed`。

---

### 3. `parity F9 站点语言校验`：当前数据下永远不触发（店铺无 `site` 字段）

- **现象**：`check_language()` 依赖 `target_shop.get("site")`，但当前库里店铺缓存只有 `shopId` 和 `name`，没有 `site`。`site_language("")` 返回 `[]`，函数直接 return，校验形同虚设。
- **证据**：
  - 实测 `GET /shops` 返回：
    ```json
    {"accountId":"1","accountName":"罗蔓","shops":[{"shopId":"18077147","name":"C lm shop","syncedAt":null}],...}
    ```
    **没有 `site` / `siteName` / `languages` 字段**，与 `api.md` §2.1 的契约示例（含 `site: "MY"`, `siteName: "马来西亚"`, `languages: ["ms","en"]`）不符。
  - 数据库佐证（`accounts.shops` 列）：
    ```
    [{"shopId": "18077147", "name": "C lm shop"}]
    ```
    `shops` 表本身是**空的**（`SELECT * FROM shops` 无行）。
  - `publish_service.check_language()`（`publish_service.py:163-179`）：
    ```python
    expected = site_language(site)      # site_language("") -> []
    if expected and wanted not in expected:   # [] 是 falsy，直接跳过
    ```
  - 实测：`POST /publish` 带 `language: "zh"`（MY 站点应只接受 ms/en）→ 返回 `502 UPSTREAM_ERROR ConnectError`，说明**语言校验没拦住**，一路走到上游去了。
- **影响**：
  - F9 标 ✅ 但实际是空转。一旦真实连接，中文商品会被发到马来站点。
  - 前端 `PublishDialog.tsx:98` 的 `isLanguageMismatch(targetShop?.site ?? "", ...)` 同样因为 `site` 为空而恒返回 false，界面上也不会提示。
- **建议**：修数据（重连一次让 `shop_rows()` 拿到 site）不够，根因在 `account_service.shop_rows()`（`:82-103`）依赖上游返回 `site`/`siteCode` 字段。当前这份缓存是历史遗留（`scripts/import_warehouse.py` 之前手工写的？），建议在 `fetch_shops` 时对缺失 site 的店铺用 `account.site` 兜底，并在 `check_language` 里把「站点未知」当作显式警告而不是静默放过。

---

### 4. `parity G3 每次数量 perRun`：定时任务的 `perRun` 语义与发布结果对不上

- **现象**：`run_due()` 用 `perRun` 选品（`pick_products(count=perRun)`），但发布时把这些商品一次性全塞进一个 `publish()` 请求；而 `run_schedule`（手动执行）用 `len(published)` 与 `requested` 比较算 `status`。两者对「短少」的判定不一致。
- **证据**：
  - `schedule_service.py:500-521`（`run_due`）：`items = pick_products(count=per_run)` → `publish({"items": [{"warehouseId": ...} for item in items]})` → `ok = int(result.get("published") or 0)`（见 P0-4）。
  - `api/schedules.py:102-105`（手动执行）：
    ```python
    count = len(published)
    status = "ok" if count >= requested else ("short" if count else "failed")
    reason = "" if status == "ok" else f"实际只处理了 {count} 件，少于计划的 {requested} 件"
    ```
    `requested = int(task.per_run or 10)`。若仓库只剩 3 件可发，`perRun=10` 会稳定报 `short`，即使这 3 件全部成功。
  - 实测（创建 `perRun=5` 的任务后 `POST /run`）：
    ```json
    {"taskId":"task-344f3bb526ae","status":"failed","requested":5,"count":0,"failed":5,
     "reason":"发布总开关未打开：请先在设置里开启发布", ...}
    ```
    注意 `failed: 5` —— 一个都没发，却报「5 件失败」，把 `len(items)` 当成失败数（`api/schedules.py:90` `"failed": len(items)`）。
- **影响**：定时任务的结果统计不可信；`status` 的 `ok/short/failed` 三态语义混乱（「没货可发」和「发了但失败」都归到 failed/short）。
- **建议**：`perRun` 语义定为「本轮最多取多少件」，`status` 应区分 `empty`（无货）/`partial`（部分成功）/`ok`；失败数用实际 failures 长度。

---

### 5. `parity G6 后台定时扫描`：worker 会吞掉所有异常且无告警

- **现象**：`main.py:73-90` 的 `_schedule_loop` 每 60 秒调一次 `run_due()`，`except Exception` 只 `logger.warning`。配合 P0-4，定时发布永远失败但**只在日志里留一行 warning**，前端看不到任何异常。
- **证据**：
  - `main.py:78-90`：
    ```python
    while not stop.is_set():
        try:
            with session_scope() as db:
                summaries = schedule_service.run_due(db)
                db.commit()
                if summaries: logger.info("定时发布执行：%s", summaries)
        except Exception as exc:
            logger.warning("定时发布扫描失败：%s", exc)   # ← 吞掉
        await asyncio.wait_for(stop.wait(), timeout=60)
    ```
  - `logs/server.log` 目前是空文件（`-rw-r--r-- 0 bytes`），而代码里 `log_path()` 定义了 `server.log` 但 `main.py` 从未配置 logging handler 写到该文件 —— 日志只打到 stdout。Electron 生产模式下 `spawn(bundled, [], {stdio: "ignore"})`（`app/main.ts:47-51`）**把 stdout 直接丢弃**，所以**打包后连 warning 都看不到**。
- **影响**：定时发布静默失败无法排查。
- **建议**：`main.py` 里加 `logging.FileHandler(settings.logs_dir / "server.log")`；`_schedule_loop` 的异常要写进任务的 `last_result`（现在 `run_due` 内部 catch 了，但 worker 层的异常没有）。

---

### 6. `parity A8 多人各自 key`：仅 3 条路由支持，采集箱/仓库等主力接口全不支持

- **现象**：`X-Miaoshou-AppKey` / `X-Miaoshou-AppSecret` 请求头只在 `shops.py`、`images.py`、`publish.py`、`schedules.py`、`warehouse.py` 声明了 `CredentialsHeader`，而采集箱与类目路由**没有声明**（也正是 P0-1 的 NameError 来源）。
- **证据**：
  - `grep -n "credentials\|CredentialsHeader" server/api/*.py`：
    - `collect_box.py`：`import` 了 `CredentialsHeader`，但 6 个路由函数签名里都没有 `credentials` 形参（只有 `process` 有）
    - `categories.py`：同样 import 了但 `list_categories` 没有形参；`sync_categories` 用了未声明的 `credentials`
  - 实测：带请求头调 `GET /shops` → 走到上游（`502 ConnectError`，说明临时账号生效）；带同样请求头调 `GET /collect-box` → `500 NameError`。
- **影响**：A8 标 ✅ 但只有一半接口支持；共享服务器模式下采集箱完全不可用。
- **建议**：修 P0-1 时一并补齐所有需要上游凭据的路由。

---

### 7. `parity F5 跨店铺载体重建`：链接池为空时直接 422，且没有 UI 可以补充链接

- **现象**：`collect_carriers()` 在链接池取不到链接时抛 `CARRIER_UNAVAILABLE`，提示「请先按来源链接采集一件商品，或把采集箱里的商品链接加入链接池」；但新版**没有任何接口或 UI 能往链接池里加链接**（`add_links` 只在 `harvest_from_payloads` 内部被调用）。
- **证据**：
  - `carrier_service.py:193-199`：
    ```python
    link = take_link(db)
    if link is None:
        raise CarrierError("链接池里没有可用的 1688 链接了。请先按来源链接采集一件商品，或把采集箱里的商品链接加入链接池", ...)
    ```
  - `add_links()`（`carrier_service.py:44-58`）是 public 函数，但 `grep -rn "add_links" server/` 只有 `harvest_from_payloads` 一处调用，无路由暴露。
  - `harvest_from_payloads`（`:70-95`）从 payload 的 `sourceList` / `sourceItemId` / `oriItemId` / `skuMap[].sourceItemId` 回收。若商品的 payload 里没有这些字段（355 件真实数据里是否有待确认），链接池永远空。
  - 前端也没有链接池管理页（`grep -rn "链接池\|carrier" web/src` 无匹配）。
- **影响**：F5 标 ✅ 但实际是「只有在恰好有可回收货源 id 时才能用」，操作者遇到 422 后无处可去（提示文字指向的功能不存在）。
- **建议**：补 `GET/POST /carrier/links` 路由 + 前端入口；或在错误 message 里去掉不存在的操作指引。

---

### 8. `parity F6 定价模板重算`：`mode=direct` 时模板静默不生效（仅返回 warning）

- **现象**：同店铺直发时模板不生效，服务端返回 `warning` 文案。这个设计在 `api.md` §8.1 有写，但**前端只是弹一个 8 秒 toast**，且 `repriced` 数组在前端完全没有 UI。
- **证据**：
  - 后端 `publish_service.py:400-403`：
    ```python
    if template is not None:
        warning = (f"这 {len(entries)} 件就在目标店铺，发布的是平台里已有的草稿，价格已经固定；"
                   f"模板「{template.get('name')}」没有生效。要改价请先在处理页按该模板重新保存这些商品。")
    ```
  - 前端 `PublishDialog.tsx:179-180`：
    ```tsx
    if (result.warning) {
      toast.warning("提示", { description: result.warning, duration: 8000 });
    }
    ```
    只是 toast，用户关掉就没了；而且提示里说的「处理页」就是 P0-3 里**不存在的页面**。
  - `repriced` 字段（后端 `publish_service.py:470` 返回）在前端**零引用**：`grep -rn "repriced" web/src` 只命中 `lib/types.ts:639` 的类型声明。
- **影响**：F6 标 ✅，但改价结果对操作者不可见；`repriced` 数据白算。
- **建议**：发布成功后在弹窗/结果页常驻展示 `repriced` 明细（改前/改后/币种/重量），并把 `warning` 做成可回看的提示而不是 8 秒 toast。

---

### 9. `api.md` 契约与实际实现的多处差异

- **现象**：契约文档与代码/实测有 9 处不一致。
- **证据**：

| # | api.md 位置 | 契约 | 实际 | 证据 |
|---|---|---|---|---|
| 1 | §0.3 | `Idempotency-Key` 支持 `POST /publish`、`/images/translate`、`/schedules`，24h 内同 key 返回首次结果（`replayed: true`） | **完全未实现**：后端无任何 `Idempotency` 处理 | `grep -rn "Idempotency\|idempotency" server/api server/core server/services` → 0 匹配；实测同 key 两次请求返回两个不同 `requestId`，无 `replayed` 字段 |
| 2 | §0.5 | `429 RATE_LIMITED`：本地令牌桶限流（默认 60 req/s，突发 120） | **未实现**（只有图片任务有 429，走 `image_service._reserve_slot`） | `grep -rn "limiter\|RateLimit" server/api server/core` → 0 匹配 |
| 3 | §0.5 | `403 FORBIDDEN_ORIGIN`：Origin/Host 不在白名单 | 只在 `system/update` 的 url host 校验里用；CORS 用的是 `allow_origin_regex`，非法 Origin 不会报 403 | `main.py:139-147` CORSMiddleware；`errors.py:18` 定义但仅 `system_service.py:122` 使用 |
| 4 | §1.2 | `GET /auth/status` 响应含 `accounts[].secretHint` | 契约示例里没有，实现里有（多了个字段，向后兼容） | `api/auth.py:54` → `account_service.account_summaries()` 含 `secretHint` |
| 5 | §3.1 | `pageSize` 上限 20（妙手上限） | 路由声明 `le=100`，实际由 service 层拦（`COLLECT_BOX_PAGE_MAX=20`） | `api/collect_box.py:21` `pageSize: int = Query(default=20, ge=1, le=100)`；`collect_box_service.py:109` 才校验 20 |
| 6 | §3.1 响应 | `total` 为上游总数 | 实现是 `max(total, len(items))`，且 `hasMore = len(items) >= page_size`（不是 `page*pageSize < total`） | `collect_box_service.py:158-159` |
| 7 | §4.3 | `title` 约束 25–255 字符 | 实际 1–255 | `warehouse_service.py:361` `if not 1 <= len(text) <= 255`；实测 `PATCH /warehouse/355 {"title":"A"}` → 200 |
| 8 | §7.1 | 错误码 `402 IMAGE_BUDGET_EXCEEDED` | 一致，但 `IMAGE_KEY_MISSING`（503）未在契约里列出 | `image_service.py:138`；实测 `POST /images/translate` → `503 IMAGE_KEY_MISSING` |
| 9 | 全文 | 未记载 `POST /collect-box/{detailId}/process` | 接口存在且是核心链路 | `api/collect_box.py:70`；`grep -n "process" docs/api.md` 只命中 `processing` |

- **另有一处契约与 Electron 实现不符**：
  - `api.md:4` 写「端口被占用时 8812–8822 递增，实际端口由 Electron 健康检查确定」，但 `app/src/main.ts:13` 是 `const API_PORT = 8812;` 硬编码，`config.py:25` 的 `api_port_max: int = 8822` **从未被读取**。8812 被占用时 Electron 会一直等 60 秒超时（`waitFor` tries=120 × 500ms），然后照常开窗但接口全不可用。
- **影响**：前端若按契约实现幂等重试会重复发布；端口递增的承诺不成立会导致「多开」场景直接不可用。
- **建议**：要么实现幂等与限流，要么从 `api.md` 删除；端口递增要么实现（读 `api_port_max` 循环探测），要么改文档。

---

### 10. `PATCH /schedules/{id}` 的 `exclude_none=True` 导致「清空字段」不可达

- **现象**：`api/schedules.py:37` 用 `exclude_unset=True, exclude_none=True` 双重过滤，客户端传 `null` 想清空 `days`/`dayOfMonth`/`startDate` 会被静默丢弃。
- **证据**：
  - `api/schedules.py:37`：
    ```python
    body = payload.model_dump(by_alias=False, exclude_unset=True, exclude_none=True)
    ```
  - `schedule_service.normalize()`（`:96`）`merged.update({key: value for key, value in raw.items() if value is not None})` —— 即使传了 `null` 也会被过滤。
  - 对比 `warehouse.py:59`（`PATCH /warehouse/{id}`）用的是 `exclude_unset=True` **没有** `exclude_none`，所以仓库那边能传 `null` 清空 `groupId`/`categoryId`（实测 `{"groupId": null}` 生效）。
- **影响**：定时任务从 `weekly` 改 `daily` 时，旧的 `days` 清不掉（虽然 `normalize` 里 `frequency == "daily"` 时会重置为 `[]`，属于侥幸）；但 `startDate`/`endDate` 无法清空。
- **建议**：去掉 `exclude_none=True`，在 `normalize` 里区分「未提供」与「显式 null」。

---

### 11. 前端 7 个页面中，图片翻译页未连接时仍可点「提交翻译」

- **现象**：`ImagesPage` 的「提交翻译」按钮 `disabled={selectedIds.length === 0}`，**没有判断 `connected`**；页面下方虽然有 `!auth?.connected` 的错误提示，但按钮本身仍可点，点了必然拿到 401/403。
- **证据**：
  - `web/src/features/images/ImagesPage.tsx:177-184`：
    ```tsx
    <Button variant="primary" icon={<Languages />} loading={translate.isPending}
      onClick={submit} disabled={selectedIds.length === 0}>提交翻译</Button>
    ```
  - 同文件 `:458-465` 有未连接提示，但只是展示：
    ```tsx
    {!auth?.connected && (<ErrorState title="未连接妙手" ... onRetry={() => navigate("/unlock", ...)} />)}
    ```
  - 对比 `CollectBoxPage.tsx:187,192` 的刷新/批量认领按钮都带了 `disabled={... || !connected}`。
  - 另外 `submit()` 内部（`:99-107`）也没检查 `connected`。
- **影响**：游客模式下点「提交翻译」→ 后端 `IMAGE_KEY_MISSING`（503）或上游错误，提示语与真实原因（未连接）不符，误导操作者。
- **建议**：按钮加 `disabled={selectedIds.length === 0 || !auth?.connected}`，或在 `submit()` 里先判连接态并给准确提示。

---

## P2 问题

### 1. 错误提示暴露内部异常类型名，对操作者无意义

- **现象**：`main.py:174` 把 `type(exc).__name__` 拼进用户可见的 message。
- **证据**：`main.py:172-175`：
  ```python
  response = _error_response(errors.INTERNAL_ERROR,
      f"服务器内部错误，请把 requestId 反馈给开发者（{type(exc).__name__}）", 500, request_id)
  ```
  实测返回：`"服务器内部错误，请把 requestId 反馈给开发者（NameError）"`。
- **影响**：`NameError` / `KeyError` / `TypeError` 对操作者毫无意义，反而暴露内部实现细节。
- **建议**：生产模式只回 requestId，异常类型名只写日志。

### 2. 请求体解析失败被归类为 `INTERNAL_ERROR` 且状态码 400，语义错乱

- **现象**：非法 JSON body 返回 `400` + `INTERNAL_ERROR`，而契约里 `BAD_JSON` 应该对应 400。
- **证据**：实测 `POST /violation-words` 带中文 body（curl 未指定 charset 时）：
  ```json
  {"error":{"code":"INTERNAL_ERROR","message":"There was an error parsing the body","details":{},"requestId":"..."}}
  ```
  HTTP 400。`errors.py:15` 定义了 `BAD_JSON = "BAD_JSON"` 但**全项目零引用**（`grep -rn "BAD_JSON" server/` 只命中定义）。
- **影响**：前端按 `code` 分支时，`INTERNAL_ERROR` 会被当成服务端故障；且 message 是英文原文。
- **建议**：`main.py` 里捕获 body 解析异常 → `errors.AppError(BAD_JSON, "请求体不是合法 JSON", status=400)`。

### 3. `warehouse_service.facets()` 每次列表请求都全表扫描 3 次

- **现象**：`GET /warehouse` 每次都要算 facets（全表取 5 列）+ stats（3 次 count）+ account_counts（再一次全表）。355 件时 37-45ms 尚可，2000 件上限（`WAREHOUSE_MAX_ITEMS`）会线性放大。
- **证据**：
  - `warehouse_service.py:174-235`（facets）：`active_query(db).with_entities(...).all()` 全表
  - `:226`：`for (acc_id,) in active_query(db).with_entities(Product.account_id).all()` 又一次全表
  - `:238-244`（stats）：`base.count()` + 两次 `filter(...).count()`
  - `:163-171`：`list_products` 返回体里同时塞 `facets(db)` 和 `stats(db)`
  - 实测耗时：
    ```
    real 0m0.045s / real 0m0.037s   (355 件)
    ```
- **影响**：数据量上去后列表页会明显卡；且 `facets` 里的 `-abs(hash(label)) % 100000`（`:187`、`:223`）用 Python 内置 `hash()`，**跨进程不稳定**（`PYTHONHASHSEED` 随机化），同一分组在不同进程里 id 不同。
- **建议**：facets/stats 加缓存或改为 SQL 聚合；去掉 `hash()` 生成 id 的写法（用分组名本身或持久化 id）。

### 4. `sort=savedDesc` 在采集箱里按「是否入仓」排序，与参数名语义不符

- **现象**：`collect_box_service.list_items()` 里 `savedDesc` 排的是 `row["inWarehouse"]`，不是「保存时间」。
- **证据**：`collect_box_service.py:152-153`：
  ```python
  if sort == "savedDesc":
      items.sort(key=lambda row: row.get("inWarehouse", False), reverse=True)
  ```
- **影响**：参数名 `savedDesc` 让调用方以为按时间倒序，实际是布尔排序。
- **建议**：改名为 `inWarehouseFirst`，或真的按时间排。

### 5. `clean_color()` 对已翻译的马来语/英语颜色词无效，且会误伤非颜色文本

- **现象**：`clean_color()` 的词表只含中文颜色词和英文颜色词，对马来语（`hitam`/`kelabu`/`ungu`/`putih`）完全不识别；对 `Ankle-lengthpantsblack` 这类无空格的拼接词也识别不到（因为用了 `\b` 词边界）。
- **证据**：实测 `clean_color`：
  ```
  'Y18[beige]'                     -> 'Beige'          ✅
  'Beige + Black [Two-Piece Set]'  -> 'Beige Black'    ✅
  '1987白色'                        -> '白色'            ✅
  'Ankle-lengthpantsblack'         -> 'Ankle-lengthpantsblack'   ❌ 未识别
  'dark green'                     -> 'Green'          ⚠️ 丢了 dark
  'ABC123'                         -> 'Abc123'         ❌ 非颜色文本被首字母大写
  ```
  真实数据佐证（`GET /warehouse/355`）：`Warna` 属性值是 `'Ankle-lengthpantsblack'`、`'Nine-pointstylenordicgray'`、`'Capri seluar klasik berwarna hitam.'` —— 全是未处理的脏值。
- **影响**：D4 对「已翻译成马来语的商品」实际无效；`ABC123 -> Abc123` 会破坏型号。
- **建议**：词表补马来语；对纯字母数字且无颜色词命中时原样返回而不是首字母大写。

### 6. 图片翻译的 `retryOcr` 参数实际不生效

- **现象**：`ImageTranslateRequest.retryOcr` 只用于「是否允许重试 unknown 状态任务」和「是否跳过复用」，并没有任何 OCR 行为（OCR 能力未实现，见 P1-1）。
- **证据**：`image_service.py:435` `retry_ocr = payload.get("retryOcr") is True`；`:451-460` 只用于 `RETRY_UNSAFE` 判定；`:496-499` 用于跳过 unknown 复用。全文无 OCR 调用。
- **影响**：前端 `ImagesPage.tsx` 有个 `retryOcr` 开关（`:63`），勾选后用户以为会做 OCR 校验，实际只是「允许重试」。
- **建议**：UI 文案改为「允许重试未确认任务」，或实现 OCR。

### 7. 硬编码与魔法数字

- **证据**：
  - `server/core/constants.py:79` `health_interval_ms = 200` —— **小写下划线命名**，与文件里其它全大写常量风格不一致，且全项目无引用（`grep -rn "health_interval_ms" server/` 只命中定义）。
  - `server/main.py:68` `timeout=20`（图片回收）、`:88` `timeout=60`（定时扫描）—— 硬编码，未走常量。
  - `server/main.py:178` `if elapsed > 800:` —— 慢请求日志阈值魔法数字。
  - `server/services/carrier_service.py:25-26` `COLLECT_SETTLE_SECONDS = 3.0` / `COLLECT_SETTLE_ROUNDS = 12` —— 硬编码等待 36 秒，无配置项。
  - `server/services/carrier_service.py:344` `time.sleep(0.25)` —— 载体逐个写入的间隔魔法数字。
  - `server/services/collect_box_service.py:126` `while request_page <= 10` —— 最多翻 10 页的魔法数字，未走常量（`COLLECT_BOX_MAX_ITEMS=100` 是另一个维度）。
  - `server/services/warehouse_service.py:187,223` `-abs(hash(label)) % 100000` —— 见 P2-3。
  - `web/src/lib/api.ts:11` `FALLBACK_API_BASE = "http://127.0.0.1:8812"` —— 前端硬编码端口（与 Electron 的 `API_PORT` 重复定义）。
  - `web/src/lib/queries.ts:479` `idempotencyKey: body.batchId ?? idempotencyKey("imgb")` —— 每次调用生成新 key，而后端根本不支持幂等（见 P1-9），等于白传 header。
- **影响**：调参与维护成本。
- **建议**：统一到 `core/constants.py`，命名规范统一。

### 8. `status.md` 与实测状态不符（自述失真）

- **现象**：`docs/status.md` 声称「50 条接口，49 条路径，全部按 docs/api.md 契约」「113 项冒烟测试通过」「7 个业务页面全部挂路由」「tsc 零错误 / vite build 通过」，但实际接口数是 **61 条**（含 alias），且有 6 条 500。
- **证据**：
  - 路由统计（AST 扫描 `server/api/*.py` 的 `@router.*` 装饰器）：`auth 6, categories 3, collect_box 6, groups 4, images 7, publish 5, schedules 5, shops 3, system 9, warehouse 7, words 6 = 61`（另有 `words.alias_router` 的 2 条）。
  - `status.md:7` 写「50 条接口，49 条路径」→ 与实际 61 不符。
  - `status.md:8` 写「113 项冒烟测试通过」→ 但采集箱 6 条接口全 500，任何覆盖采集箱的冒烟测试都不可能通过。冒烟脚本 `scripts/smoke.mjs` 存在，但没有证据表明它覆盖了采集箱成功路径（`.smoke/report.json` 里 7 个页面的 `dom.url` 全是 `/unlock`，即**都被重定向到解锁页，实际业务页一个都没测到**）。
  - `.smoke/report.json` 铁证：7 条记录（warehouse/collect/schedules/words/publish/images/unlock）的 `dom.url` 全是 `"/unlock"`，`cards: 0`，`rows: 0`。
  - `.smoke/` 目录里 13 张截图 MD5 完全相同（`5f8684ab8056b5b655d5e22821359f57`）：`collect.png / images.png / publish.png / root.png / schedules.png / unlock.png / warehouse.png / words.png` —— 说明这批「页面截图」其实是同一张解锁页。
  - `status.md:36-43` 声称「仓库页：355 件真实渲染，24/24 图片加载成功（截图 f-warehouse.png）」，但 `.smoke/` 里**没有 f-warehouse.png 这个文件**（`ls .smoke/` 只有 `flow-warehouse.png`、`warehouse-*.png`）。
  - `tsc --noEmit` 实测确实 0 错误（`exit=0`），这一项是真的。
- **影响**：`status.md` 作为交付凭证不可信；「113 项冒烟测试通过」会误导验收方跳过验证。
- **建议**：冒烟脚本要在已连接状态下跑业务页（当前全被 guard 重定向）；`status.md` 的数字要从实际代码统计生成。

### 9. 版本号三处不一致，且更新链路没有闭环

- **现象**：后端 `APP_VERSION = "1.0.0"`，`app/package.json` 和 `web/package.json` 都是 `0.1.0`；`system/version` 返回 `latest: "0.1.4"`（老版版本号）却 `updateAvailable: false`。
- **证据**：
  - `server/core/constants.py:7` `APP_VERSION = "1.0.0"`
  - `app/package.json:3` `"version": "0.1.0"`；`web/package.json:3` `"version": "0.1.0"`
  - 实测 `GET /system/version`：
    ```json
    {"current":"1.0.0","latest":"0.1.4","updateAvailable":false, ...,
     "url":".../AOHUANG-Listing-Setup-0.1.4.exe","sha256":"1bfac9b4...","sizeBytes":null}
    ```
    `parse_version("1.0.0") > parse_version("0.1.4")` → `(1,0,0) > (0,1,4)` → True，所以 `updateAvailable` 为 false 是**对的**，但意味着新版比线上老版「版本号更高」，永远收不到更新。
  - `POST /system/update`（`api/system.py:43-74`）**只做校验和路径拼接，不下载**：返回 `"accepted": True, "file": "<data_dir>/updates/xxx.exe"` 但该文件不存在。契约 §11.2 说「桌面模式下由 Electron 主进程执行下载与静默安装」，但 `app/src/main.ts` 里**没有任何 update 相关 IPC**（`grep -n "update" app/src/main.ts app/src/preload.ts` 无匹配），前端也没有调用 `/system/update` 的地方（`grep -rn "system/update" web/src` 无匹配）。
  - `DIGEST_MISMATCH`（`errors.py:44`）定义了但零引用 —— 因为根本没有下载校验环节。
- **影响**：I2「软件内下载安装」实际不存在；I3「版本号展示」展示的是后端版本 1.0.0 而非安装包版本 0.1.0，用户看到的版本号与实际不符。
- **建议**：统一版本号来源（从 `package.json` 注入）；要么实现 Electron 侧下载+校验+安装，要么把 I2 从清单里删掉。

### 10. 词库「启用/停用」用「删除/重加」变通，语义与老版不同

- **现象**：`violation_words.enabled` 字段存在，`matcher()` 也只取 `enabled=True` 的词，但**没有任何接口能改这个字段**。前端把「停用」实现成 `remove-many`（把词删掉），「撤销」实现成 `add`（再加回来）。
- **证据**：
  - 后端：`api/words.py` 全文无 `enabled` 相关参数；`word_service.apply_action()`（`:202-239`）的 5 个 action 都不碰 `enabled`；`word_service.matcher()`（`:258-262`）过滤 `enabled.is_(True)`，但写入路径永远写 `enabled=True`（`:123`、`:181`、`:251`）。
  - 前端 `web/src/features/words/WordsPage.tsx:412-420` 注释明确承认这个变通：
    ```tsx
    /*
      接口（api.md §10.2）没有独立的启用/停用字段，enabled 只能读。
      停用 = 从词库移除（remove-many），重新启用 = 再次 add；
      Toast 提供「撤销」把词加回来，避免误操作丢词。
    */
    <Switch checked={item.enabled} onChange={(checked) => {
      if (checked) return;
      mutate.mutate({ action: "remove-many", words: [item.word] }, ...)
    }} />
    ```
  - 而且 `Switch` 的 `checked` 永远读到 `true`（因为所有词都是 enabled），所以开关**永远是「开」状态且点了就删词**，界面上看不出「停用」和「删除」的区别。
- **影响**：
  - 老版操作者「临时停用某个词，过几天再启用」的用法在新版等于「删掉再手动打一遍」，且删掉后 `source` 会从 `seed` 变成 `manual`、`createdAt` 重置。
  - 误点开关会直接删词（虽然有 toast 撤销，但 toast 消失后就找不回了）。
  - parity H4 未标 ✅ 是对的，但清单里写的是「enable/disable」，读起来像已支持。
- **建议**：补 `POST /violation-words` 的 `action: "enable" / "disable"`（或 `PATCH /violation-words/{id} {"enabled": false}`），前端开关改为真切换。

---

## 已验证正确的部分

以下项目经实测确认没问题（给证据）：

### 后端：仓库与分组

| 项目 | 结论 | 证据 |
|---|---|---|
| warehouse_key 唯一性 | ✅ | DB 查询 `SELECT warehouse_key, COUNT(*) c FROM products GROUP BY warehouse_key HAVING c>1` → `[]`；模型层 `models/product.py:30` `unique=True` + `:80` `UniqueConstraint("account_id","detail_id","shop_id")` |
| 主图 9 张限制（C13） | ✅ | DB 查询所有 355 件 `json_array_length(images)` 均 ≤9，`images>9` 结果为空；`product_service.py:120-121` `main_images(limit=MAIN_IMAGE_LIMIT)`，`constants.py:42` `MAIN_IMAGE_LIMIT = 9` |
| 详情图硬限 30 校验 | ✅ | `product_service.py:132-141` `validate_image_limits()` 在 `build_product():276` 最先调用 |
| 价格区间（C14） | ✅ | `product_service.py:59-70` `purchase_range()` 取有效 SKU 的 `originPrice`（回退 `price`）min/max；`serialize()` 输出 `purchasePriceMin/Max`（实测 `GET /warehouse/355` → `"purchasePriceMin":"17.00","purchasePriceMax":"21.00"`） |
| 统计（C16） | ✅ | 实测 `GET /warehouse/stats` → `{"totalProducts":355,"totalClaimed":0,"totalPublished":0}`，与 DB `COUNT(*)` 一致 |
| 分组删除后商品解绑 | ✅ | 实测：创建分组 → 挂 1 件商品 → 删分组 → 返回 `productsUnassigned: 1`，商品 `groupId: null, group: ""`；`group_service.py:125-126` 显式 update |
| 分组改名同步商品缓存 | ✅ | `group_service.py:103-104` 更新 `Product.group_name` |
| 软删除后的关联 | ✅ | 实测软删 `id=1` 后按 id 和按 key 查均 `NOT_FOUND`；DB 检查 `group/category/account` 孤儿引用均为 0；`warehouse_service.py:457-458` 把 `publish_logs.product_id` 置 NULL |
| 并发写 | ✅ | 10 线程并发 `PATCH /warehouse/{id}` 全部 200（`[(1,200),(2,200),...,(10,200)]`）；`db/session.py:28-31` 开了 WAL + `busy_timeout=5000` |
| 分页边界 | ✅ | `pageSize=500` → 400 `VALIDATION_ERROR`；`pageSize=0` → 400；`page=99999` → 200 + `items:[]` + `hasMore:false`（不崩） |
| 非法 sort | ✅ | `sort=bogus` → 400 `"排序方式无效"` |
| 搜索词长度 | ✅ | `q=` 70 字符 → 400 `"搜索词不能超过 64 个字符"` |
| 批量改分类单事务 | ✅ | 实测 `POST /warehouse/batch-category {"ids":["355","354"],"groupId":"1"}` → `updated:2, failed:[]`；`warehouse_service.py:406-417` 有 try/rollback |
| 批量操作上限 | ✅ | `warehouse_service.py:386-387`（200 条）、`:495-496`（删除 200 条） |

### 后端：违规词

| 项目 | 结论 | 证据 |
|---|---|---|
| 词库 CRUD（H1/H2/H3/H5） | ✅ | 实测 add/remove/import/remove-many/clear 全部可用（`add` → 200 含新 id；`clear` 无 confirm → 400 `CONFIRM_REQUIRED`）；`word_service.py:202-239` |
| 标题清理（D2 remove 类） | ✅ | 实测 `clean_title("【包邮】2026新款碳板跑鞋 厂家直销 正品保证", words)` → `'2026新款碳板跑鞋'`，removed `['包邮','厂家直销','正品保证']`；空括号清理 `word_service.py:280` 生效 |
| 大小写不敏感 | ✅ | 实测 `"Wholesale Running Shoes"` → `"Running Shoes Best Quality"`（词表里是小写 `wholesale`） |
| 重复词/冲突词检测 | ✅ | `word_service.py:111-120` 分 `WORD_CONFLICT` 与 `DUPLICATE_WORD` |
| 上限 500 | ✅ | `word_service.py:92-102` `_check_limit`；实测计数 `{"remove":24,"review":0,"limit":500,"remaining":476}` |
| 种子幂等 | ✅ | `word_service.py:242-255` 按 normalized 跳过已存在；实测重启后仍是 24 条 |
| 未启用词不参与匹配 | ✅ | `word_service.py:260-262` `filter(ViolationWord.enabled.is_(True))` |

### 后端：发布三重保险

| 项目 | 结论 | 证据 |
|---|---|---|
| confirm 必须字面量 true（F3） | ✅ | 实测 `confirm:false` → `400 CONFIRM_REQUIRED`；`publish_service.py:319` `if payload.get("confirm") is not True`；`api/publish.py:59` 再落一次 `body["confirm"] = payload.confirm is True` |
| 总开关（PUBLISH_DISABLED） | ✅ | 实测开关关闭时 → `403 PUBLISH_DISABLED`；`publish_service.py:323-326` |
| 显式列表非空 | ✅ | 实测 `items:[]` → `400 "items 必须是非空数组"`；`publish_service.py:328-329` |
| 上限 200 | ✅ | `publish_service.py:330-332`；`constants.py:49` `PUBLISH_MAX_ITEMS = 200` |
| 店铺归属校验 | ✅ | 实测 `shopId:"99999"` → `422 SHOP_NOT_IN_ACCOUNT`，`details:{"shopId":"99999","accountId":"1"}`；`account_service.py:200-209` |
| 商品不存在 | ✅ | 实测 `warehouseId:"999999"` → `404 NOT_FOUND`，`details:{"missing":["999999"]}`；`publish_service.py:230-232` |
| 模板不存在 | ✅ | 实测开关打开后 `template:"不存在的模板"` → `422 TEMPLATE_INVALID`，`details:{"field":"template"}`；`publish_service.py:302-314` |
| 汇率校验 | ✅ | `publish_service.py:349-357` `0 < rate <= 1000` |
| mode 判定 | ✅ | `publish_service.py:360-361` `same_shop = all(entry["shopId"] == target)` |
| direct + template 必返 warning | ✅ | `publish_service.py:400-403` 有 warning 文案（前端展示不足见 P1-8） |
| 上游失败保留 savedCount | ✅ | `publish_service.py:419-426` `details={"savedCount": len(filled["saved"])}` |

### 后端：图片翻译

| 项目 | 结论 | 证据 |
|---|---|---|
| 同图去重不重复扣费（E4） | ✅ | `image_service.py:490-503` 按 `sha256(原图+model+prompt+size)` 命中 `ready`/`unknown`/`queued`/`submitted` 直接复用并标 `reused: true` |
| 预算上限 10 元（E5） | ✅ | 实测 `GET /images/limits` → `{"budgetMilliyuan":10000,...}`；`image_service.py:507-516` 在写事务内检查并抛 402 |
| 限流（E6） | ✅ | 实测 `GET /images/limits` 返回 `{"minute":600,"hour":6000,"day":60000}`；`image_service.py:113-125` 三窗口校验 |
| 五态状态机 | ✅ | `image_service.py:49` `STATUSES = ("queued","submitted","ready","unknown","failed")`；前端 `constants.ts` `IMAGE_STATUS_META` 五态齐全 |
| unknown 不自动重试 | ✅ | `image_service.py:496-499` 复用不重提；`retryOcr` 时才允许（`:451-460` 先拦 `RETRY_UNSAFE`） |
| 15MB 上限 | ✅ | `image_service.py:157-160`、`:171-174`；`constants.py:57` `IMAGE_MAX_BYTES = 15*1024*1024` |
| 结果文件缺失不重提 | ✅ | `image_service.py:627-641` 抛 `RESULT_FILE_MISSING` |
| 密钥未配置提前报错 | ✅ | 实测 `POST /images/translate` → `503 IMAGE_KEY_MISSING`（不是等到下载原图才报连接错误）；`image_service.py:431` 先调 `_api_key()` |
| 回收 worker 不重复提交 | ✅ | `image_service.py:644-677` `reconcile()` 只查状态不重提；`main.py:49-70` 20 秒循环 |

### 后端：鉴权与基础设施

| 项目 | 结论 | 证据 |
|---|---|---|
| Bearer 鉴权 | ✅ | 无 token → 401；错 token → 401；`X-Local-Token` 兼容 → 200（实测三种情况） |
| health 免鉴权（I4） | ✅ | 实测 `GET /api/v1/system/health` 无 token → 200 `{"ok":true,"name":"aohuang-listing-server","version":"1.0.0","db":"ok","uptimeSeconds":58.1}` |
| requestId 回传 | ✅ | 实测带 `X-Request-Id: review-req-42` → 响应头 `x-request-id: review-req-42`；`main.py:153,176` |
| 错误信封格式 | ✅ | 所有错误都是 `{"error":{"code","message","details","requestId"}}`，与 `api.md` §0.5 一致 |
| 参数校验统一 400 | ✅ | `main.py:209-216` 把 FastAPI 默认 422 转成 400 `VALIDATION_ERROR`（实测 `pageSize=500` → 400） |
| openapi 生产关闭 | ✅ | 实测 `/api/openapi.json` 与 `/api/v1/openapi.json` 均 404（`settings.dev=False`） |
| 凭据加密存储（A7） | ✅ | `services/crypto.py` 用本机 secret 派生 Fernet key；`accounts.app_secret_enc` 存密文；响应只有 `appIdHint`/`secretHint`（`crypto.py:33-39` 只回末 4 位） |
| 重命名连接（A5） | ✅ | 实测 `PATCH /auth/accounts/1 {"name":"罗蔓-重命名测试"}` → 200；空名 → 400；超长 → 400；不存在 → 404 |
| 上游错误脱敏 | ✅ | `core/utils.py` `redact()`；`account_service.py:393-406` `map_upstream_error` 带 `secrets` 参数 |

### 前端

| 项目 | 结论 | 证据 |
|---|---|---|
| tsc 零错误 | ✅ | `npx tsc --noEmit` → `exit=0` |
| 构建产物存在 | ✅ | `web/dist/` 含 `assets` + `index.html`（621K） |
| 路由完整 | ✅ | `App.tsx:135-150` 覆盖 `/warehouse /collect /collect-box(别名) /schedules /words /publish /images /unlock` + 404 |
| 未连接自动跳解锁（只跳一次） | ✅ | `guard.ts:67-88` `redirectedRef` 防重复跳；`sessionStorage` 存 guest 标记 |
| mutation 错误提示覆盖 | ✅ | 逐文件统计 `onError` 数 = `mutate(` 数：WarehousePage 3/3、PublishPage 1/1、SchedulesPage 5/5、ImagesPage 1/1、WordsPage 6/6 |
| 加载态/空状态 | ✅ | `CollectBoxPage` 有 `SkeletonTable`（`:272`）、`ErrorState`（`:276`）、`EmptyState`（`:288`）；`WarehousePage`、`ProductDetailDrawer` 同构 |
| 轮询有上限且可停止 | ✅ | `useImageJobPolling.ts:22` `IMAGE_POLL_MAX_MS = 5*60*1000`；终态 `ready/failed/unknown` 停止；卸载 `cancelled=true` + `clearTimers()`；`stop()`/`pollNow()` 可手动控制 |
| 图片 Referer 问题已修 | ✅ | `RemoteImage.tsx` 用 `referrerPolicy="no-referrer"`（`status.md:16,39` 自述，代码存在） |
| 词库动作全覆盖 | ✅ | `WordsPage.tsx` 覆盖 `import`(:146)、`remove-many`(:173, :423)、`clear`(:189) |
| 发布 warning 有展示 | ⚠️ 部分 | `PublishDialog.tsx:179-180` 有 toast，但 8 秒后消失（见 P1-8） |

---

## 附：测试数据还原记录

审查期间为验证写接口做了若干写操作，**已全部还原**，最终状态与审查开始时一致：

| 检查项 | 审查开始时 | 最终状态 | 还原方式 |
|---|---|---|---|
| 商品总数 | 355 | 355 | — |
| 商品 355 标题 | `Seluar yoga wanita ini menampilkan...` | 原文 | `PATCH /warehouse/355 {"title": 原文, "groupId": null}` |
| 商品 354 groupId | null | null | `PATCH /warehouse/354 {"groupId": null}` |
| 商品 301-310 | 原样 | 原样 | 并发测试只改 groupId，已逐个 PATCH 回 null |
| 分组数 | 3 | 3 | 测试分组（id=4）已 DELETE |
| 违规词数 | 24 | 24 | 测试词 `ce-shi-ci-XYZ`、`测试词中文TMP` 已 remove |
| 定时任务数 | 0 | 0 | 测试任务 `task-344f3bb526ae` 已 DELETE |
| 发布记录数 | 0 | 0 | 未产生（发布被开关/上游拦截） |
| 发布总开关 | false | false | 测试后已 `POST /publish/settings {"publishEnabled": false}` |
| 账号名 | 罗蔓 | 罗蔓 | 重命名测试后已 PATCH 回原名 |
| 软删商品 id=1 | 正常 | 正常 | 测试软删后直接改库 `deleted_at=NULL, status='ready'` |

> 唯一直接改库的一处：软删除测试（`DELETE /warehouse/1` 后恢复）。因为软删后 API 无「恢复」入口 —— 这本身也是一个观察：**软删除不可逆，误删只能改库**（建议加 `POST /warehouse/{id}/restore`）。

## 附：审查方法说明

- 静态：`python -m pyflakes server/`、AST 扫描路由与未声明变量、`grep` 交叉核对。
- 动态：`curl` + `urllib` 实测 30+ 组请求，覆盖正常路径、边界（0/超限/非法枚举/不存在 id）、并发（10 线程）、鉴权（无 token/错 token/兼容头）。
- 数据库：`sqlite3` 直查 `app.db` 校验唯一性、孤儿引用、图片数量分布。
- 逻辑：对无法通过接口触发的路径（如 `run_due` 的 `int(list)`、`_pair_entries` 错配、`_mark_published` 全量标记），**直接 import 服务层函数做纯内存验证**，未写库。
- 前端：`tsc --noEmit`、逐文件 `onError`/`mutate` 计数、交互路径人工走查（未起浏览器，`.smoke/` 截图全为解锁页，不可作为界面证据）。
