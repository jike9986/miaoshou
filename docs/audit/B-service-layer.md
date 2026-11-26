# 审计 B：服务层核心链路

> 审计范围（task-2）：`server/services/` 里五个最大的文件
> `warehouse_service.py`（4551 行）、`carrier_service.py`（2322）、`publish_service.py`（1850）、
> `schedule_service.py`（1812）、`collect_box_service.py`（1561），共 **12096 行**。
> 因价格口径横跨模块，证据链延伸核对到 `product_service.py` / `price_template.py` /
> `product_standard.py` / `exchange_rate.py` / `pre_save_check.py` / `readback_check.py` /
> `transform_service.py` / `common_box_service.py` / `api/warehouse.py` / `api/collect_box.py`。
> 审计时点：2026-11-26，HEAD = `4c9f32d`，版本 0.1.54。
> 审计方式：**全部结论用可重放脚本实证**（真实调用服务层函数、真实 `save_from_collect_box`
> 端到端跑到 `save_shop`），不靠读代码猜。**未修改任何源码。**
>
> 复现脚本都在 `docs/audit/_verify_b_service.py`（可直接 `python -X utf8` 跑）。
> 脚本开头强制把 `AOHUANG_DATA_DIR` 指向临时目录 —— **不会碰**开发库
> `%LOCALAPPDATA%\aohuang-desktop\app.db`（写台账/批次的两个实证段需要真库）。

---

## 结论摘要

| 级别 | 条数 | 说明 |
|---|---|---|
| **P0** | **6** | 数据错误 / 资金 / 状态不一致 / 重复发布 |
| **P1** | **8** | 门禁失效、台账被覆写、状态机非法值、并发不安全 |
| **P2** | **6** | 重复实现、可维护性、测试红灯 |

> 全部结论由 `docs/audit/_verify_b_service.py` 可重放验证（17 个实证段），
> 服务层**未改动一行代码**。20 条问题的性质分布：
> **根因 9 条 / 补丁叠补丁 8 条 / 跷跷板 2 条 / 混合 1 条**
> （「混合」指 `except Exception` 的分类：合理降级与吞掉状态不一致在代码里长得一模一样，
> 无法用同一个标签概括）。
>
> **最需要立刻处理的是 B-P0-1**：它不是「偶发失败」，而是让「自动化处理 → 入仓」
> 这条主链路在规则层必然返回 422；且现有测试全部绕过了 `enforce_pre_save=True`，
> 所以 CI 是绿的。B-P0-2 / B-P0-3 会真的把钱算错或写错价，B-P0-4 是这两条的共同根。
> **B-P0-6 是唯一一条已实证会「重复发布同一批商品」的链路**，资金影响最直接。

### 最严重的 6 条（P0 全部）

**B-P0-1 —— 成品仓库入仓 100% 被自己的校验拦死（422 `one or more price fields changed`）。**
`warehouse_service.py:2802` 把 `template` 无条件置 `None`（只验证模板存在、不定价），于是
`:2939-2940` 的 `if template is None: payload = _price_warehouse_at_purchase(payload)` **每次都执行**；
该函数在 `:2631` 往 payload 里写一个**本地记账键** `_warehousePriceRate`。
`pre_save_check.py:121-122` 认定「key 里含 `price` 就算价格字段」，`:346-348` 在
`pricing_enabled=False` 时一律报错，而 `:3155`/`:3262` 因为 `template` 恒 `None` 传进去的就是 `False`。
**实测（真实 `save_from_collect_box`）**：源草稿价格一个字不改、只多这个键，也返回
`422 one or more price fields changed`；把该键删掉立刻 `prices_unchanged=True` 放行。
性质：**跷跷板**（修「仓库只记拿货价」时把「没开定价就不许改价」这条不变量打成了恒假）。

**B-P0-2 —— 成本线抬价用「源站点」汇率给「目标站点」算价，实测把 38.41 MYR 抬成 259.71 MYR。**
`carrier_service.py:164` 取 `site = payload.get("site")`，那是商品**采集地**的站点；
认领到跨站点店铺时必须用**目标店**站点。实测：源 `site="PH"`、目标店 MY、拿货价 23 CNY、
妙手报成本线 38.40 MYR → 抬价结果 **259.71 MYR**（应为 38.41，虚高 6.76 倍）；
反方向（源 MY、目标 PH）→ `target <= current` 直接 `continue`，`:215-216` 静默
`return None`，价格一分不动，认领照样失败。
性质：**根因**（函数签名里没有目标站点这个入参，调用方也没人补）。

**B-P0-3 —— `_COST_FLOOR_VALUE` 正则仍然只认一种写法，失败一律静默 `return None`。**
`carrier_service.py:71-72` 要求「数字 + 三个大写字母」，且不允许千分位。
实测四种真实写法：`225.50PHP` ✅、`1,280.00THB` ❌（千分位）、`225.50`（无币种）❌、
`0PHP` ✅（把 0 当合法下限）。任一处不匹配就走到 `:153-158 floor=None` →
`:203` 不抬下限 → 可能 `:215-216 return None`，调用方拿到 `None` 就把原始报错抛给用户，
表现为「某几件商品永远传不上去」，日志里没有任何关于「为什么没抬价」的记录。
性质：**补丁叠补丁**（2026-11-25 修掉写死 MYR 之后，剩下的脆弱面没清）。

**B-P0-4 —— 同一个「售价」概念有 4 套实现、3 个汇率来源、3 种数据类型。**
`carrier_service.py:193-213`（CNY×1.18÷rate，Decimal→`str`）、
`collect_box_service.py:1420-1432`（同上，另一份拷贝）、
`warehouse_service.py:2624-2628`（÷rate 不乘 1.18，Decimal→`str`）、
`price_template.py:795-814`（`display_price`，→`float`），
外加 `product_standard.py:214-221 / 520-531` 第五份（写死 `EXCHANGE_RATE=1.633`、`+2.00 MYR`，
且**只改 `priceIncludeVat` 不改 `price`**）。
实测 23 CNY 同一件货：`14.03 / 16.56 / 16.08` 三个不同结果；
实测 PH 店商品被 `product_standard.repair` 写入一个**马币口径**的 `12.41` 到 PHP payload。
性质：**根因**（概念没有单一实现，每处各自演化）。

**B-P0-5 —— `draft_verified_revision` 同步规则（CLAUDE.md 规则 4）在 10 个改商品的地方漏了。**
`warehouse_service.py:1729`（`batch_category`）、`:1792`（`delete`）、
`:3443`（`apply_transformed_payload`）、`:1252`（`admit_automation_results`，**连
`bump_revision` 都没有**）、`:1477`（`apply_category`）、`:475`（`reconcile_member_claims`，
仓库页加载时无条件触发）、`collect_box_service.py:130`（`_record_carrier_claim`）、
`:766`（`claim`，bump 在 `:876`）、`:1476`（`release`，bump 在 `:1514`）、
`publish_service.py:1505`（认领分支 bump）；`api/warehouse.py:569` 的 `translate_colors`
则是**故意不推版本号**来规避，反向破坏了规则 5。
后果：`automation_run_service.py:496/566/667/780` 与 `publish_service.py:207` 都以
`draft_verified_revision == revision` 为门禁 → 商品从「自动化结果」页消失、
发布报 `409 自动化商品没有与当前版本匹配的妙手草稿读回核验`。
性质：**根因**（规则靠人肉在每个调用点抄一遍，没有单一收口点）。

**B-P0-6 —— 定时任务多店摘要把件数乘了店铺数，`partial` 又被当成失败记账：
勾选式多店任务每轮重发同一批已成功的商品。**
`schedule_service.py:1559-1563` `requested=requested * shop_count`、
`selected=selected * shop_count`，而 `published/submitted/failures` 是累加实际结果。
`summarize()` 用 `selected - published - submitted` 反推失败 ⇒ **「没发」被算成「失败」**，
`status` 从 `ok` 变 `partial`。接着 `_store`（`:1295-1298`）的
`success=status in (RUN_OK, RUN_SUBMITTED)` **不含 `partial`** ⇒
`note_result` 走 else 分支：`attempts += 1` 且 `last_run_date` 不写
（实测 `status=partial → attempts=1, last_run_date=''`）——
docstring 写的「含 partial 算当天已跑」根本没实现，于是当天被反复触发。
同时 `_store` 只在 `status == RUN_OK` 时 `completed_runs += 1`，
而 `pick_products(run_index=completed_runs)`（`:943-945`）正是勾选式任务唯一的去重手段
（它**故意**跳过 `skip_published` 过滤）⇒ 下一轮仍切同一片商品。实测：
```
第 1 轮 run_index=0 选中: ['D2', 'D1']（D1 台账里已 success）
第 2 轮 run_index 仍为 0 → 再次选中: ['D2', 'D1']
```
性质：**补丁叠补丁**（「多店铺」加在单店摘要口径上时只改了分子没改分母，
`summarize()` 的反推公式把漏改升级成假失败，`_store` 的 docstring 又是另一次修复里
写下但与 `success=` 集合不同步的期望）。

---

## P0 问题（数据错误 / 资金 / 状态不一致）

### B-P0-1 入仓价格改写让「没开定价就不许改价」变成恒假：每次入仓必被 422 拦死

**证据 1：`template` 被无条件置 `None`。**
`server/services/warehouse_service.py:2802-2809`
```python
    template = None
    if price_template:
        raw = setting_service.price_templates(
            db, operator_id=operator_id).get(str(price_template))
        if not raw:
            raise errors.AppError(errors.TEMPLATE_INVALID,
                                  f"定价模板「{price_template}」不存在",
                                  status=422, details={"field": "template"})
        # 只验证存在，不用它定价（负责人 2026-11-25）。
```
`price_template` 传了也只用于查存在性，`template` 恒等于 `None`。

**证据 2：因此改写分支每次执行，并写入一个本地记账键。**
`warehouse_service.py:2939-2940`
```python
    if template is None:
        payload = _price_warehouse_at_purchase(payload)
```
`warehouse_service.py:2630-2634`
```python
    result["skuMap"] = new_map
    result["_warehousePriceRate"] = {"site": site or "MY",
                                     "currency": currency,
                                     "rate": str(rate_d),
                                     "rateSource": source}
    return result
```

**证据 3：pre-save 用「key 里含 price 就算价格字段」，并对新增 key 直接判失败。**
`server/services/pre_save_check.py:121-122`
```python
def _is_price_field(key: Any) -> bool:
    return "price" in str(key).casefold()
```
`server/services/pre_save_check.py:346-348`
```python
    if not prices_unchanged(original, payload):
        if not pricing_enabled:
            issues.append("one or more price fields changed")
```
`server/services/pre_save_check.py:153-158`（新增 key 一律 False）
```python
    for path in after.keys() - before.keys():
        if path.startswith("skuMap."):
            sku_key = path.split(".", 2)[1]
            if sku_key not in (original.get("skuMap") or {}):
                continue
        return False
```

**证据 4：`pricing_enabled` 因为 `template` 恒 `None` 而恒 `False`。**
`warehouse_service.py:3155` → `pricing_enabled=template is not None,`
`warehouse_service.py:3262` → `pricing_enabled=template is not None,`

**证据 5：实证 —— 只多一个记账键、价格一个字不改，也被拦。**
```python
info  = {'site':'MY', 'skuMap':{';1;2;':{'price':'10.46','priceIncludeVat':'10.46','originPrice':'17',...}}}
priced = warehouse_service._price_warehouse_at_purchase(info)
# 输出：
# 改写后价格与上游逐字相同？ True
# before 价格字段: ['skuMap.;1;2;.price', 'skuMap.;1;2;.priceIncludeVat', 'skuMap.;1;2;.originPrice']
# after  价格字段: [... , '_warehousePriceRate']
# → prices_unchanged = False
# → issues = ['one or more price fields changed']
# 删掉 _warehousePriceRate 后 prices_unchanged = True
# → issues = []
# _is_price_field('_warehousePriceRate') = True
```

**证据 6：实证 —— 真实 `save_from_collect_box(enforce_pre_save=True)` 端到端 422。**
（FakeClient 模拟 `shop_detail`/`save_shop`，草稿是完整合格商品：标题、主图、详情图、
类目 cid、重量、包裹尺寸、尺码图、颜色/尺码规格、COD 全齐）
```
--- 调用 save_from_collect_box（不传模板，与前端一致）
BLOCKED code= UNPROCESSABLE status= 422
message= 保存前校验未通过：one or more price fields changed
issues= ['one or more price fields changed']
```
再补一次对照：把草稿上的 `price` 事先改成与 `originPrice/汇率` 完全相同的值，仍然 422 ——
证明拦截与「价格算得对不对」无关，只与那个记账键有关。

**证据 7：`automation_result=True`（自动化入仓的正式入口）同样被拦。**
```
automation_result=True 路径：拦下 code=UNPROCESSABLE status=422
issues=['one or more price fields changed']
```
即 `api/collect_box.py:265` 传 `automation_result=True` 的那条路径与普通入仓一样必败。

**影响**
- 前端 `AutomationPage.tsx:1275` 发送 `save: true` → `api/collect_box.py:265`
  `enforce_pre_save=bool(payload.save)` → 必然走到 `warehouse_service.py:3211-3216` 抛 422。
  也就是说 **0.1.54 的「自动化处理 → 入仓」在规则层是必然失败的**，能成功只可能是
  走了别的入口或当时 payload 没有 `skuMap`。
- 同一处矛盾还挂在读回校验上：`readback_check.py:408-417`
  ```python
    if pricing_enabled:
        comparisons["prices"] = _prices_match(payload, actual)
        comparisons["originPriceUnchanged"] = pricing_preserves_origin_price(original, actual)
    elif payload.get("_costFloorAdjustments"):
        comparisons["prices"] = _prices_match(payload, actual)
    else:
        comparisons["prices"] = _prices_unchanged(original, actual)
  ```
  `pricing_enabled=False` 且没触发成本下限时，读回也必须与**上游原价**一致 ——
  而我们已经主动改写了价格。实测：提交 `14.15`、读回 `14.15`、原始 `38.41` →
  `readback_issues(...)` 返回 `['prices']`。只有触发过 `_costFloorAdjustments`
  的那几件才走 `_prices_match` 侥幸通过。
- **为什么没被发现**：`grep -rn "enforce_pre_save" server/tests/` 零命中 ——
  没有任何测试真正用 `enforce_pre_save=True` 跑过 `save_from_collect_box`；
  `server/tests/test_member_automation.py:148` 直接
  `monkeypatch.setattr(warehouse_service, "save_from_collect_box", save)` 把它整只替换掉了。
  这正是 `docs/project/fixed-download-link-2026-11-26.md` 记的那条教训：
  **校验没覆盖到的入口，等于没校验**。

**性质：跷跷板**。为落实负责人「仓库只记拿货价、不套模板」，把 `template` 硬置 `None`
（`:2802`），于是 `pricing_enabled` 这个**由 `template` 推导出来的**布尔值恒为 `False`，
而「`pricing_enabled=False` ⇒ 价格必须一字不改」这条不变量是另一条独立规则留下的。
两条规则各自都对，组合起来就成了「入仓必然被自己拦死」。修一边（让入仓改价）就必然破另一边
（让门禁相信价格没变），这正是跷跷板：**根子在于「价格为什么变」这件事没有被表达出来，
只被一个推导出来的布尔值间接表达。**

**根因修复方案（不改本轮代码，只给方向）**
1. 「价格是否允许变化」不能靠 `pricing_enabled` 这个**由另一个开关推导出来的**布尔值，
   要显式区分三种语义：`prices_locked`（发布时的固定价）/ `prices_from_purchase`（仓库
   拿货价口径）/ `prices_from_template`（发布套模板）。仓库入仓属于第二种，
   `pre_save_issues` 必须允许「`price == originPrice / rate`」这个**受约束的变化**，
   而不是二选一。
2. 本地记账键必须进 `_LOCAL_ONLY_PAYLOAD_FIELDS`（`warehouse_service.py:4138-4143`
   现在有 `colorMap/sizeMap/_sourceWeight/translation/...`，**没有** `_warehousePriceRate`
   和 `_costFloorAdjustments`），并且 `_is_price_field` 必须排除 `_` 开头的本地键。
   实测 `_strip_local_only_fields` 保留 `_warehousePriceRate`：
   `'_warehousePriceRate' in _strip_local_only_fields(priced) → True`，即这个纯本地
   记账信息会被一起推给妙手。
3. `template = None` 这个「只验证不定价」的写法应该改成显式的
   `template_used_for_pricing: bool`，让下游不再靠 `template is not None` 反推语义。

**回归测试**
```python
def test_warehouse_save_password_gate_accepts_purchase_price_rewrite():
    # 真跑 save_from_collect_box(enforce_pre_save=True)，草稿含 originPrice
    # 断言：不抛 422；断言上游 save_shop 收到的 price == originPrice / rate
    # 断言：上游收到的 payload 里没有 _warehousePriceRate / _costFloorAdjustments
def test_warehouse_price_rewrite_is_not_reported_as_unexpected_change():
    info = {"site": "MY", "skuMap": {"k": {"price": "38.41", "originPrice": "23", ...}}}
    payload = warehouse_service._price_warehouse_at_purchase(info)
    assert pre_save_check.pre_save_issues(
        info, payload, {}, pricing_enabled=False, ...) == []   # 现在返回 1 条
def test_readback_accepts_rewritten_purchase_price_without_cost_floor():
    # pricing_enabled=False 且无 _costFloorAdjustments 时，
    # actual == 提交给妙手的那份 → readback_issues 必须为空
```

---

### B-P0-2 成本线抬价用源站点汇率算目标站点价格：实测 38.41 MYR 被抬到 259.71 MYR

**证据：`site` 取自 payload（采集地），不是目标店铺。**
`server/services/carrier_service.py:164-165`
```python
    site = str(payload.get("site") or "").strip().upper()
    rate = _claim_site_rate(site)
```
`server/services/carrier_service.py:107-118`
```python
def _claim_site_rate(site: str) -> Decimal | None:
    """站点币种的汇率（1 CNY = N 目标币种）。取不到返回 None。"""
    from services import exchange_rate

    currency = exchange_rate.currency_for_site(site) if site else "MYR"
```
`carrier_service.fill_carriers(...)` 的签名（`:1940-1949`）里只有 `shop_id`，**没有目标站点**，
所以函数内部根本无从知道要发到哪个国家的店；`payload["site"]` 是成品入库时从上游
`shopCollectItemInfo.site` 带下来的**采集地**站点（`product_service.py:314`、`warehouse_service.py:2814`）。
同一 batch 被认领到另一国店铺时两者不一致。

**实证 1（虚高）**：源商品采集于 PH，认领到 MY 店铺。`_reset_claim_prices_to_purchase`
已按目标店 MY 折算好 16.70 MYR：
```
汇率 CNY->MYR = 1.62550086 ; CNY->PHP = 0.1045
payload = {"site":"PH","skuMap":{"k":{"price":"16.70","originPrice":"23","..."}}}
_raise_price_to_reported_floor(payload, "本地展示价16.70MYR，无法覆盖产品基础成本 38.40MYR")
→ 抬价后 price = 259.71 MYR
   adjustments = {'oldPrice': '16.70', 'newPrice': '259.71', 'costFloor': '38.40', 'usedFloor': False}
```
应为 38.41 MYR（`38.40 + 0.01`），实际 **259.71 MYR = 6.76 倍**。
`usedFloor=False` 说明它连「抬到成本线」都没做，而是按 PHP 汇率算出一个天文数字后
认为「已经超过成本线」直接用了。

**实证 2（静默失败）**：源 MY、目标 PH，拿货价 34 CNY，目标价已折好 383.90 PHP：
```
payload = {"site":"MY","skuMap":{"k":{"price":"383.90","originPrice":"34",...}}}
_raise_price_to_reported_floor(payload, "本地展示价383.90PHP，无法覆盖产品基础成本 225.50PHP")
→ 返回值 = None ，价格仍为 383.90
```
按 MY 汇率算出的 target = 16.70，`16.70 <= 383.90` → `:207-208 continue` → `:215-216 return None`。
调用方（`carrier_service.py:2095-2099`）拿到 `None` 就 `raise`，用户看到的是原始报错，
认领永远失败 —— 正是 2026-11-25 那批「9 件一直失败」的同类症状，只是换了个触发条件。

**影响**：跨站点认领（MY↔PH/TH/VN/SG）一旦触发成本线，要么挂一个 6 倍以上的价格上去
（必然卖不动，且如果后续有人在妙手参照这个价改模板，就是真实的定价污染），
要么永久失败。`docs/features/warehouse/price-and-claim-2026-11-25.md` 第 5 条只修了
「正则不写死 MYR」，**没修「币种该由目标店决定」**。

**性质：根因**。汇率币种必须由**目标**决定，这是汇率换算的第一性原理；
但 `_raise_price_to_reported_floor` 的函数签名里根本没有「目标店」这个入参，
于是它只能退而取 `payload["site"]`（采集地）。调用链上 `publish()`（`publish_service.py:1200`）
明明已经拿到了 `target_shop["site"]`，却没有往下传 ——
**根子在于「谁负责知道目标站点」这件事从未被定下来，每一层都以为自己不需要知道。**

**根因修复方案**
1. `fill_carriers` / `collect_and_fill_carriers` / `_raise_price_to_reported_floor`
   全部显式接收 `target_shop_site`，由 `publish()`/`claim_carrier_batch()` 从
   `target_shop["site"]` 传下来（`publish_service.py:1200-1201` 已经有
   `str(target_shop.get("site") or "")`，只是没传进载体层）。
2. 函数内部若有 `target_shop_site` 就用它，`payload["site"]` 只作为**没有目标站点时**的兜底，
   并在兜底时打 warning（现在既没兜底标记也不打日志）。
3. `_raise_price_to_reported_floor` 增加「币种交叉校验」：解析出的成本线币种（正则第三段）
   与目标币种不同时直接返回结构化失败并记 error，不允许继续抬价。

**回归测试**
```python
@pytest.mark.parametrize("source_site,target_site,origin,floor,expected", [
    ("PH", "MY", "23", "38.40MYR", "38.41"),     # 现在得到 259.71
    ("MY", "PH", "34", "225.50PHP", "383.92"),   # 现在得到 None / 不动
])
def test_cost_floor_raise_uses_target_shop_currency(source_site, target_site, ...):
    ...
```

---

### B-P0-3 `_COST_FLOOR_VALUE` 依旧脆弱 + 全链路静默 `return None`

**证据 1：正则本体。**
`server/services/carrier_service.py:71-72`
```python
_COST_FLOOR_VALUE = re.compile(
    r"基础成本[^0-9]{0,24}([0-9]+(?:\.[0-9]+)?)\s*[A-Z]{3}", re.IGNORECASE)
```

**证据 2：所有「解析不出来」的分支都不留痕。**
```python
carrier_service.py:149-150   if "无法覆盖产品基础成本" not in message: return None
carrier_service.py:152-158   floor_match = ...; if floor_match is not None: floor = Decimal(...)
                             # 不匹配时 floor 保持 None，继续往下算
carrier_service.py:160-162   if not isinstance(sku_map, dict): return None
carrier_service.py:174-179   try: current = Decimal(str(sku.get("price")))
                             except (...): continue
                             if current <= 0: continue
carrier_service.py:183-191   purchase 缺失 / 非法 / <=0 一律 continue
carrier_service.py:207-208   if target <= current: continue
carrier_service.py:215-216   if not changed: return None
```
`return None` 在调用方等于「这不是成本线问题」→ 原样 `raise`。
全函数**没有一行 logger**，所以生产上只能看到妙手那句报错，
看不到「正则没匹配上」「SKU 缺 originPrice」「目标价没超过当前价」这三类完全不同的原因。

**证据 3：实证四种真实写法。**
```
✅ '38.40'        <- 本地展示价16.70MYR，无法覆盖产品基础成本 38.40MYR
✅ '225.50'       <- 本地展示价 933.19PHP，无法覆盖产品基础成本(跨境物流运费+关税+增值税)：225.50PHP
❌ None          <- 本地展示价 100.00THB，无法覆盖产品基础成本(...)：1,280.00THB        # 千分位
❌ None          <- 本地展示价 100.00PHP，无法覆盖产品基础成本(...)：225.50              # 无币种尾巴
✅ '0'           <- 本地展示价 100.00PHP，无法覆盖产品基础成本(...)：0PHP                # 把 0 当下限
✅ '3861250000'  <- 本地展示价 100.00VND，无法覆盖产品基础成本：3861250000VND
```
`1,280.00THB` 是泰铢/越南盾报价的**正常书写方式**（妙手自己在别处也用千分位），
一旦上游这么写，抬价机制彻底失效，而错误信息看起来与「这个修法没用」完全无关。

**证据 4：`0` 被当合法下限。**
`:203-205`
```python
        if floor is not None and target < floor:
            target = (floor + Decimal("0.01")).quantize(Decimal("0.01"))
```
`floor=0` 时把目标价设成 `0.01`。如果上游真报过 `0`，我们会把一个 SKU 的售价写成
「0.01 元」，这是**资金事故级**的写入（比报错严重得多）。

**性质：补丁叠补丁**。2026-11-25 修的是「正则写死 `MYR`」，改法是「把币种变成
`[A-Z]{3}` 占位符」—— 只把那一次遇到的写法容纳进来，没有把「从一句自然语言报错里
提取金额」这件事当成**不可靠输入**来处理（容错解析 + 合理性校验 + 失败必须留痕）。
于是每遇到一种新写法就要再来一次补丁，而每次补丁都没有解决「解析失败时静默 `return None`」
这个真正的故障放大器。

**影响**：与 2026-11-25 那次「写死 MYR 导致 PH/TH/VN/SG 全失败」是同一类问题 ——
**一个脆弱的字符串解析 + 静默失败 = 问题可以在生产上活很多个版本**。
`docs/project/fixed-download-link-2026-11-26.md` 里记的「6 个版本」就是这个模式。

**根因修复方案**
1. 正则改成「容错解析」而不是「精确匹配」：
   `[^0-9]{0,24}([0-9][0-9,\s]*(?:\.[0-9]+)?)\s*([A-Z]{3})?`，命中后先
   `value.replace(",", "").replace(" ", "")`，币种段允许缺失（缺失时取目标币种）。
2. `floor is not None and floor <= 0` → 视为解析失败并 `logger.error`，
   绝不参与抬价（现在会把价格写成 0.01）。
3. 每个 `return None` / `continue` 前都加 `logger.warning(reason=...)`，
   并把 `reason` 带进返回结构（成功/失败都返回结构化，而不是 `None`）。
4. 抬价成功后校验「新价 ≤ floor × 1.5」这类合理性上限，超过就拒发（防 P0-2 那种 6 倍）。

**回归测试**
```python
@pytest.mark.parametrize("message,expected", [
    ("...基础成本 38.40MYR", "38.40"),
    ("...基础成本(...)：225.50PHP", "225.50"),
    ("...基础成本(...)：1,280.00THB", "1280.00"),   # 现在 None
    ("...基础成本(...)：225.50", "225.50"),          # 现在 None
])
def test_cost_floor_regex_tolerates_thousands_separator_and_missing_currency(...): ...

def test_cost_floor_zero_is_rejected_not_used_as_price():
    # floor=0 必须返回结构化失败，且不得把任何 SKU 价格改成 0.01
```

---

### B-P0-4 「本地售价」有 4 套实现、3 个汇率来源、3 种数据类型：同一个概念各算一遍

**证据 1：四处主实现（写入点全清单见文末专章）。**
| # | 位置 | 公式 | 类型 |
|---|---|---|---|
| 1 | `carrier_service.py:193-213` | `originPrice × 1.18 ÷ rate`，低于成本线抬到 `floor+0.01` | `Decimal` → `str` |
| 2 | `collect_box_service.py:1420-1432` | `originPrice × 1.18 ÷ rate`（第 2 份拷贝） | `Decimal` → `str` |
| 3 | `warehouse_service.py:2624-2628` | `originPrice ÷ rate`（**不乘 1.18**） | `Decimal` → `str` |
| 4 | `price_template.py:795-814` | `display_price()` 全成本反解 | `Decimal` → **`float`** |
| 5 | `product_standard.py:214-221 / 520-531` | `originPrice ÷ 1.633 + 2.00`（写死常量） | `Decimal` → `str`，**只写 `priceIncludeVat`** |

`collect_box_service.py:1420` 引用 `carrier_service.CLAIM_PRICE_BUMP` 是唯一做对的一处
（系数共用），但**公式本身仍是一份拷贝**；`:1413-1419` 的注释说明加价必须在人民币层面，
这条规则只写在两处注释里，没有写成函数。

**证据 2：三个汇率来源，且数值不一致。**
```
exchange_rate.FALLBACK_RATES['MYR'] = 1.6393     (exchange_rate.py:41)
price_template.FALLBACK_EXCHANGE_RATE = 1.6393   (price_template.py:17)
product_standard.EXCHANGE_RATE = 1.633           (product_standard.py:26)
```
同一件 23 CNY 的货，四处算出四个价（同一时刻同一汇率环境）：
```
warehouse(_price_warehouse_at_purchase, 兜底 1.6393): 14.03
price_template(display_price,        兜底 1.6393): 14.03
product_standard(×1.633 + 2.00 MYR):              16.08
carrier/collect_box(×1.18 后 ÷1.6393):            16.56
```
`product_standard` 的常量还带死板区间 `RATE_MIN=1.2 / RATE_MAX=2.2`（`:27-28`），
**只对 MYR 成立**；对 PHP（实盘 0.1045）、THB（0.1971）、VND（0.000259）全在区间外。

**证据 3：兜底汇率与实盘差到 17.7%。**
```
币种    兜底 FALLBACK_RATES      实盘(open.er-api)     偏差
MYR          1.6393              1.62550086          0.8%
PHP          0.1230              0.1045             17.7%
THB          0.2160              0.19708752          9.6%
VND          0.00029             0.000259           12.0%
SGD          5.3300              5.18035403          2.9%
```
（`exchange_rate.py:40-47`；代码一律**除**，即 rate = 「1 单位本币 = N CNY」的倒数写法。
名字容易读反，见 `:74` `_per_cny` 的注释与 `:126` 的 `1.0 / per_cny`。走兜底时 PHP 报价会**偏低 17.7%**，
这是真实的亏本方向。）

**证据 4：`product_standard.repair` 把马币数字写进菲律宾 payload，且只改一个字段。**
`product_standard.py:528-534`
```python
            target = (base + PRICE_BUMP_MYR).quantize(Decimal("0.01"),
                                                      rounding=ROUND_HALF_UP)
            if vat is None or vat < target:
                sku["priceIncludeVat"] = format(target, ".2f")
                changed += 1
        if changed:
            done.append("价格+2MYR(%d条)" % changed)
```
实测（同一份 payload，只换 `site`）：
```
PH: 原 vat=1.00 -> 修复后 priceIncludeVat=12.41   notes=['价格+2MYR(1条)']
TH: 原 vat=1.00 -> 修复后 priceIncludeVat=12.41   notes=['价格+2MYR(1条)']
MY: 原 vat=1.00 -> 修复后 priceIncludeVat=12.41   notes=['价格+2MYR(1条)']
```
12.41 是「17 CNY ÷ 1.633 + 2.00」= 一个**马币**数字，被写进 PHP/THB 商品，
而 `price` 字段保持原值 → `price ≠ priceIncludeVat`。妙手按含税价展示，于是同一件商品
出现「卖价 1.00、含税价 12.41」的自相矛盾数据。
**并且它被 `warehouse_service.py:3176-3180` 在 `payload.clear(); payload.update(_repaired)` 里
整份写回** —— 也就是说这条路径会真实落库。

顺带一个反方向证据：MY 店拿货价 17 CNY 的正确仓库价是 10.46 MYR，
`product_standard.validate` 却报「售价低于标准(1条SKU)」（它认为标准价是 12.41）。

**证据 5：`exchangeRate` 参数是一根断掉的线。**
`api/warehouse.py:410-425`（`price_preview`）和 `api/collect_box.py:264`
都把用户填的 `exchangeRate` 一路传下去，
`warehouse_service.py:2930` 也 `process_price(..., exchange_rate=exchange_rate)`，
但 `product_service.py:373-374` 在 `template` 为空时直接 `return dict(payload), None`，
而 `_price_warehouse_at_purchase` 的签名里根本没有这个参数：
```python
>>> inspect.signature(warehouse_service._price_warehouse_at_purchase)
(payload: 'Mapping[str, Any]') -> 'dict[str, Any]'
```
**用户手填的汇率在仓库路径上永远不生效**（只有走模板的 `price-preview`
和 `apply_template` 才生效）。同一个「汇率」输入在两条路径上行为不同，没有任何提示。

**影响**
- 「一个售价」在四个地方算出四个数，任何一次「按哪个口径核对」的判断都会有两个以上答案。
  实测 23 CNY：仓库 14.03 / 模板 14.03 / 成本标准 16.08 / 认领 16.56 ——
  差价 18%，足够让「价格没变」的判定翻面（见 B-P0-1）。
- 仓库价 14.03 与认领价 16.56 的差额（1.18 倍）是**同一件商品**在两个页面上的显示价，
  用户会觉得其中一个错了，而代码里两处都「对」。
- `product_standard.repair` 的 `priceIncludeVat=12.41` 会真实发到妙手
  （`warehouse_service.py:3176-3180` 整份写回），造成含税价与售价自相矛盾。
- `exchangeRate` 入参在仓库路径上被丢弃（实测签名不含该参数）→
  用户手填的汇率「有时生效有时不生效」，且没有任何提示。
- `FALLBACK_RATES` 落后实盘：PHP 偏差 17.7%、VND 12.0%、THB 9.6%，
  一旦兜底生效，报价方向是**系统性偏低**（亏本）。

**性质：根因**。这不是「某处写错了」，而是「售价」这个概念从来没有被收敛成一个
带明确前置条件（哪个币种、哪层加价、什么汇率源、什么类型）的单一实现。

**根因修复方案**
1. 建一个 `services/pricing.py`（唯一口径），导出三个明确命名的函数：
   - `warehouse_price(origin_cny, target_site)` → 仓库口径（÷rate，不加价）
   - `claim_price(origin_cny, target_site)` → 认领口径（×1.18÷rate）
   - `listed_price(origin_cny, weight, template, target_site)` → 发布口径（`display_price`）
   全部返回 `Decimal`（量化到币种最小单位），**只在这一个模块写 `sku["price"]`**。
2. 汇率来源收成一个：删掉 `price_template.FALLBACK_EXCHANGE_RATE` 与
   `product_standard.EXCHANGE_RATE`，统一走 `exchange_rate.fallback_rate(currency)`；
   `product_standard` 的 `+2.00 MYR` 规则若确实要保留，必须改成「按站点币种的等值加价」
   并从站点取币种，且 `price`/`priceIncludeVat` 同时写。
3. `_price_warehouse_at_purchase` / `_reset_claim_prices_to_purchase` /
   `_raise_price_to_reported_floor` 三个函数体全部改成调用 `pricing.py`。
4. `exchange_rate.FALLBACK_RATES` 需要与实盘对齐一次并加单测（当前 PHP 差 17.7%）。

**回归测试**
```python
def test_single_pricing_source_of_truth():
    # 同一 originPrice/站点，warehouse/claim/template 三个入口必须都由 pricing.py 计算
    # 断言没有任何其它模块给 sku["price"] 赋值（AST 扫描断言）
def test_pricing_currency_follows_target_site_not_source_site(): ...
def test_product_standard_price_repair_never_writes_mvr_into_php_payload(): ...
def test_user_exchange_rate_override_takes_effect_on_warehouse_path():
    # 现在被丢弃
def test_price_and_price_include_vat_stay_equal_in_warehouse_payload(): ...
```

---

### B-P0-5 `draft_verified_revision` 同步漏了 10 处：商品从「自动化结果」消失 / 发布 409

**证据 1：规则原文**（`CLAUDE.md` 规则 4）：
> 编辑商品的任何字段（图片、重量、尺寸、分类、标题）后，必须同步 `draft_verified_revision`：
> `if product.automation_result and product.draft_verified_revision is not None: ...`
> 否则该商品会从自动化结果页消失。

**证据 2：门禁（谁在读这个字段）**
- `services/automation_run_service.py:496` `and product.draft_verified_revision == product.revision`
- `services/automation_run_service.py:566 / 667 / 780` `or product.draft_verified_revision != product.revision`
- `services/publish_service.py:207-209`
  ```python
    if any(product.draft_verified_revision != product.revision for product in products):
        raise errors.AppError(
            "CONFLICT", "自动化商品没有与当前版本匹配的妙手草稿读回核验", status=409)
  ```
- `services/warehouse_service.py:1219` `_verified_automation_product_ids` 的过滤条件

**证据 3：做对的（有 `revision_guard` + 同步）**：`warehouse_service.py:1664`（`update`，`:1717-1718`）、
`:1623`（`review_product_quality`，`:1657-1658`）、`api/warehouse.py:370`（改货源链接，`:382-383`）、
`api/warehouse.py:1164`（改图，`:1218-1219`）、`publish_service.py:1806`（`_mark_published`，条件同步 `:1827-1828`）。

**证据 4：做漏的（AST 全量扫描 `bump_revision|revision_guard|product.payload =` 且函数体内无同步）**

| # | 位置 | 函数 | 改了什么 | 有 bump | 有 draft 同步 |
|---|---|---|---|---|---|
| 1 | `warehouse_service.py:1252-1338` | `admit_automation_results` | `automation_result=False`、`group_id`、`payload`（图片 promote）、`images/cover` | ❌ 无 | ❌ |
| 2 | `warehouse_service.py:1729-1789` | `batch_category` | `cid/category_id/group_id/payload` | ✅ `:1769` | ❌ |
| 3 | `warehouse_service.py:1792-1823` | `delete` | `status=archived`、`deleted_at` | ✅ `:1813` | ❌ |
| 4 | `warehouse_service.py:3443-3469` | `apply_transformed_payload` | 整份 `payload`、`title`、`images/cover` | ✅ `:3468` | ❌ |
| 5 | `collect_box_service.py:130-145` | `_record_carrier_claim` | `collected/claimed/claimed_at/claimed_shop_id` | ✅ `:145` | ❌ |
| 6 | `collect_box_service.py:766-891` | `claim` | 同上（`:869-876`） | ✅ `:876` | ❌ |
| 7 | `collect_box_service.py:1476-1527` | `release` | `collected/claimed` | ✅ `:1514` | ❌ |
| 8 | `publish_service.py:1489-1505` | `publish`（claimOnly 分支） | `collected/claimed` | ✅ `:1505` | ❌ |
| 9 | `warehouse_service.py:475-560` | `reconcile_member_claims` | `collected`（清掉上游已不存在的认领标记） | ✅ `:556` | ❌ |
| 10 | `warehouse_service.py:1477-1520` | `apply_category` | `cid/category_id/category_name/payload` | ❌ 无（由调用方 `batch_category:1769` 补） | ❌ |
| 11 | `api/warehouse.py:569-608` | `translate_colors` | `payload`、`sku_properties` | ❌ 无（**注释明确说不推版本号**，见下） | ❌ |

第 11 条是**有意的例外**，`api/warehouse.py:600-604` 写明了理由：
```python
    # 注意：不推进 revision，也不清掉 draft_verified_revision。
    # 颜色翻译只改本地展示字段，不影响妙手草稿——推进 revision 会导致
    # draft_verified_revision != revision，商品从自动化结果列表消失，无法入库。
```
这个理由本身成立（改的确实是本地展示字段），但它暴露了规则 4 的结构问题：
**「改本地字段要不要推版本号」和「推了版本号要不要同步草稿版本」被绑成了一个动作**，
于是想「不丢商品」就只能「不改版本号」，而这又违反规则 5（任何改商品都要 `revision_guard`）。
正确做法是拆成两件事：推版本号（必须）+ 标记「草稿仍有效」（按内容是否涉及草稿决定）。

**证据 5：最严重的是第 1 条**：`admit_automation_results` 是「自动化结果 → 成品仓库」的唯一入口，
它把 `automation_result` 从 `True` 改成 `False`、还整份替换了 `payload`（图片从 auto-temp
promote 到 products，`warehouse_service.py:1322-1331`），却**既没 `bump_revision` 也没同步**。

**证据 8：代码路径。**
`warehouse_service.py:1288-1293`
```python
    for product in products:
        product.automation_result = False
        if group is not None:
            product.group_id = group.id
            product.group_name = getattr(group, "name", "") or ""
    db.flush()
```
`warehouse_service.py:1325`
```python
                product.payload = payload          # 图片 URL 全换成图床地址
```
这两个赋值都在同一个循环里，全程没有 `bump_revision(product)`、没有
`revision_guard`、没有 `draft_verified_revision` 同步。
按 `warehouse_service.py:1542-1551` 自己的注释：
> 所有会改商品行的路径都必须推进版本号：漏一处，那一处就成了「改了也不换版本」，
> 别人手上的旧版本号还能继续通过校验 —— 防护直接破一个洞。

**影响**
- 第 1 条：入仓后 `revision` 不变，如果这件商品曾经被 `review_product_quality` 或
  `update` 改过（`draft_verified_revision` 已是旧值），入仓替换 payload 后仍能通过
  `_verified_automation_product_ids` 的 `draft_verified_revision == revision` 检查 ——
  即**未经核验的 payload 被当成已核验**，发布时会把没核对过的内容发出去（fail-open）。
- 第 2-8、9 条：`revision` 变了但 `draft_verified_revision` 没变 → 门禁 fail-close，
  商品从自动化结果页消失 / 发布 409。这正是 `CLAUDE.md`「快速诊断」第一条写的那种症状。
- 第 9 条（`reconcile_member_claims`，`api/warehouse.py:140` 在仓库页加载时**无条件**调用一次）：
  它只清「别人已从采集箱消失」的认领标记，`revision` 一推，商品就掉出自动化结果页 ——
  而这一条路径用户完全无感（页面加载触发），是最难归因的一种。
- 第 11 条是反例：为了避免掉出列表，`translate_colors` 干脆**不推版本号**，
  于是规则 5（任何改商品都要 `revision_guard`）被破了 —— 想满足其中一条就只能违反另一条。
- 两批规则（`bump_revision` 与 `draft_verified_revision`）被当成两件独立的事，
  实际上它们必须**同时**发生；把「必须同时发生」写成两个独立函数，
  就注定了每一处新代码都可能只记得一个。第 11 条就是这个设计逼出来的绕过。

**性质：根因**。规则 4（同步 `draft_verified_revision`）与规则 5（`revision_guard`）
被实现成两个独立函数，于是「必须同时发生」这件事只能靠每个调用点的人肉记忆。
10 处漏掉的地方里，有 1 处（`admit_automation_results`）是 **fail-open**
（漏 bump 导致未核验内容被当成已核验），其余 9 处是 **fail-close**（漏同步导致商品消失）。
fail-open 那一处比 fail-close 危险得多，因为它没有任何可见症状。

**根因修复方案**
1. 把两件事合成一个不可能只做一半的 API：
   ```python
   def touch_product(product, *, reason: str) -> None:
       product.revision = int(product.revision or 0) + 1
       if product.automation_result and product.draft_verified_revision is not None:
           product.draft_verified_revision = product.revision
   ```
   `bump_revision` 改名/收敛为它，`revision_guard` 内部也调用它。
2. 对「草稿内容真的变了」的路径（`admit_automation_results` 的图片 promote、
   `apply_transformed_payload` 的整份 payload 替换）**必须**显式
   `product.draft_verified_revision = None`（草稿已不再与本版一致），
   而不是「同步到新版本」——同步到新版本是错的，因为我们没有重新向妙手核对过。
   现在的 `update`（`:1717-1718`）无条件同步，其实也是 fail-open：
   它改了本地 payload 却没重写妙手草稿。这条要单独评估。
3. 加一条 CI 静态检查：`server/services/**.py` 里任何 `product.<field> =` 或
   `product.payload =` 的函数体内若既无 `touch_product` 也无 `revision_guard`，直接失败。
   （本轮审计用的就是这个办法，`docs/audit/_verify_b_service.py` 里的 `scan_mutations()`。）

**回归测试**
```python
@pytest.mark.parametrize("mutator", [
    warehouse_service.reconcile_member_claims, warehouse_service.batch_category,
    warehouse_service.delete, warehouse_service.apply_transformed_payload,
    warehouse_service.admit_automation_results,
    collect_box_service._record_carrier_claim, collect_box_service.claim,
    collect_box_service.release,
])
def test_every_product_mutation_advances_revision_and_draft_sync(mutator, ...):
    # 造 automation_result=True 且 draft_verified_revision == revision 的商品
    # 执行 mutator 后断言：revision 增加，且 draft_verified_revision 要么 == revision
    # （内容未变草稿）要么 == None（内容变了，等重新核验）
```

---

### B-P0-6 定时任务多店摘要把件数乘了店铺数：`partial` 被当成失败记账，勾选式任务重复发布同一批

**证据：合并摘要时把件数乘了店铺数。**
`schedule_service.py:1521-1561`
```python
        shop_targets = targets_of(task)
        ...
        shop_count = len(shop_targets)
        for target in shop_targets:
            outcome = publish_service.publish(db, {
                ...
                "items": items_payload,
                ...
            }, ...)
            ...
        summary = summarize(
            task_id=str(task.id), requested=requested * shop_count,
            selected=selected * shop_count,
            published=merged["published"], submitted=merged["submitted"],
            failures=merged["failures"], results=merged["results"])
```
`items_payload` 每一轮都是**同一份**（同一批商品发到 N 个店），
`published/submitted/failures` 是**累加实际结果**，唯独 `requested`/`selected`
又被乘了一次 `shop_count`。于是摘要里会出现 `selected=30, published=9`（3 店 × 10 件，
其中 2 个店失败），`run_status()` 据此算出 `partial` 并把失败率显示成 70%。
单店任务 `shop_count=1` 时恰好正确 —— 这个错误只在「一次发多个店铺」时出现，
而这是 `targets` 字段 2026-11-2x 新加的能力，测试覆盖不到。

**证据 2：实证 —— 分母虚高会让 `status` 从 `ok` 变 `partial`，并连带卡住 `completed_runs`。**
场景：2 个目标店 × 本轮 10 件；A 店 10 件全发成功，B 店本来就没有新商品
（`selected=0`、`failures=0`，这不是失败）。
```
现在（selected/requested × 店数）:
  selected=20 published=10 failed=10 status=partial
  「计划 20 件，实际选中 20 件，成功 10 件，待回执 0 件，失败 10 件」
若不乘店数:
  selected=10 published=10 failed=0 status=ok
```
`summarize()`（`:1142`）用 `raw_failed = max(failures, selected - published - submitted)`
反推失败数 —— `selected` 一虚高，**「没发」就被算成「失败」**。
接着 `_store`（`:1311-1314`）只在 `summary["status"] == RUN_OK` 时
`completed_runs += 1`，而 `pick_products(run_index=int(task.completed_runs))`
（`:944-945`）正是用 `run_index * count` 切 `selected_product_ids`：
```
completed_runs 卡住 = 下一轮仍切同一片商品 = 同一批再次发布
```
多店铺 + 勾选了商品的任务，因此会在每一轮重复发同一批（与 B-P1-1 无防护叠加 =
店铺里出现重复商品）。这是本报告里唯一一条**已证实会重复发布**的链路。

**证据 3：实证 —— `_store` 的 docstring 与代码互相矛盾，`partial` 被当成失败记账。**
`schedule_service.py:1294-1298`
```python
def _store(db: Session, task: Schedule, summary: dict[str, Any],
           *, moment: datetime | None = None) -> dict[str, Any]:
    """记账：只有 failed 才累加当天重试次数，其余（含 partial）算当天已跑。"""
    saved = note_result(db, str(task.id), moment=moment,
                        success=summary["status"] in (RUN_OK, RUN_SUBMITTED),
                        result=summary)
```
docstring 说「含 partial 算当天已跑」，但 `success=` 的集合里**没有** `RUN_PARTIAL`，
而 `note_result`（`:851-859`）的 `else` 分支会 `row.attempts += 1` 且**不写**
`last_run_date`。实测（真跑 `_store`，每轮先清空 `attempts`/`attempt_date`/`last_run_date`/`completed_runs`）：
```
status=partial  published=10 failures=10 -> attempts=1 last_run_date=''            completed_runs=0
status=ok       published=10 failures= 0 -> attempts=0 last_run_date='2026-11-26'  completed_runs=1
status=failed   published= 0 failures=10 -> attempts=1 last_run_date=''            completed_runs=0
```
`partial` 与 `failed` 的记账效果**完全一致**（`attempts+1`、`last_run_date` 不写、
`completed_runs` 不前进），docstring 描述的「含 partial 算当天已跑」在代码里不存在。
`is_due`（`:335-345`）的第一条判据是 `lastRunDate == today → False`，
所以 partial 之后 `last_run_date` 仍为空 ⇒ **当天会被反复触发**，直到
`attempts` 吃满 `SCHEDULE_MAX_ATTEMPTS_PER_DAY = 3`（`core/constants.py:129`）。
注意 `failed` 这样记账是**有意的**（失败要重试）；问题在于 `partial` 被无差别地
并入了 `failed` 这一支。

**证据 4：实证 —— 勾选式任务在 `completed_runs` 不前进时会重发已成功的商品。**
`pick_products` 对「显式勾选」**故意跳过** `status != published` 过滤和 `skip_published`
（`:993-1005` 与 `:1006-1009` 的注释说明理由：运营点名要发就得允许重发）。
这意味着勾选式任务的唯一去重手段就是 `completed_runs` 推进 `run_index`（`:943-945`）。
实测（4 件商品、勾选前 2 件）：
```
第 1 轮 run_index=0 选中: ['AUDITD2', 'AUDITD1']
第 2 轮 run_index=0 选中: ['AUDITD2', 'AUDITD1']
```
`pick_products` 在「显式勾选」时不看 `status != published`、也不看
`_published_identities`（`:1000-1009` 的注释说明理由：运营点名要发就得允许重发），
所以 `run_index` 是**唯一**的去重手段；`completed_runs` 一卡住，这一批就被原样再发一遍，
而 B-P1-1 的两道防重复发布都已失效 —— 没有任何东西会拦住它。

**性质：补丁叠补丁**（`requested/selected` 从单店语义扩展到多店时，
一边乘了店铺数、另一边没有）。它是「多店铺」这个新能力加在既有单店摘要口径上时
漏改的一半，而 `summarize()` 里 `max(failures, selected-published-submitted)`
这个反推公式让「漏改」直接升级成「算出假失败」；`_store` 的 docstring 又是在
另一次修复里写下的、与 `success=` 集合不同步的期望。

**影响**：`run_status()` 按 `published/selected` 算完成度，分母虚高 ⇒ 明明全部成功也显示
`partial`；`_default_reason` 会打印「失败 10 件」这种根本不存在的失败；
`note_result(success=...)` 因此把一次成功执行记成「未成功」——
`attempts += 1` 且 `last_run_date` 不写，当天被反复触发，吃满
`SCHEDULE_MAX_ATTEMPTS_PER_DAY=3` 后整个任务当天停机。
再加上面证据 4 的链路，勾选式多店任务会一直重发同一批已成功的商品。

**根因修复方案**：`requested`/`selected` 保持「**本批商品件数**」的单店语义不动，
多店的成功/失败用嵌套结构表达（`per_shop: [{shopId, published, failures}]`），
`run_status()` 改为按「店铺 × 商品」的笛卡尔积算完成度；
`summarize()` 的 `selected - published - submitted` 反推必须区分
「没选中（不是失败）」与「选中但没发出去（是失败）」——
现在这两件事共用一个减法，是多店铺场景下唯一的错误来源。
同时 `_store` 的 `success=` 集合必须与 docstring 一致（把 `RUN_PARTIAL` 算成「已跑」），
并且 `RUN_PARTIAL` 只有在真的存在失败件时才允许不推进 `completed_runs`。

**回归测试**
```python
def test_multi_shop_summary_counts_items_once_not_per_shop():
    # 2 店 × 10 件，A 店全成功、B 店 selected=0 → status 必须是 ok，failed 必须是 0
def test_multi_shop_partial_failure_still_counts_failed_per_shop():
    # 2 店 × 10 件，A 成功 10、B 失败 10 → failed == 10（不是 20），status == partial
def test_completed_runs_advances_when_all_shops_succeed():
    # 勾选了商品 + 多店铺全成功 → completed_runs 必须 +1（否则下一轮重发同一批）
def test_partial_run_records_today_as_run_and_does_not_burn_attempts():
    # partial 后 last_run_date 必须是今天（现在为空 → 当天反复触发）
def test_explicit_selection_never_republishes_same_shop_without_confirmation():
    # 勾选式任务第二轮不得再选中第一轮已 success 的商品（同店）
```

## P1 问题

### B-P1-1 三道「防重复发布」里有两道是死的，注释还在引用已经不存在的函数

**证据 1：`_already_published_blocker` 定义了，从来没被调用。**
`publish_service.py:154-158`
```python
def _already_published_blocker(db: Session, *, account: Account,
                               entries: list[dict[str, Any]],
                               target_shop_id: str,
                               operator_id: int | None) -> None:
    """拒绝将同一成品重复发布到同一店铺；失败/取消和其他店铺仍可重试。"""
```
调用点被 `pass` 顶掉了 —— `publish_service.py:1303`
```python
        if not claim_only:
            pass  # 不再阻止重复发布到同一店铺
```
`grep -rn "_already_published_blocker" server/` → 只有定义（`:154`）和两个测试
（`tests/test_student_publish.py:1400/1407`）。**生产路径零调用。**
也就是说：同一件成品可以无限次发布到同一店铺，每次都在妙手那边生成一个新商品。
唯一还活着的只剩 `_find_in_flight`（`:88-132`），它只看 `status == "processing"` ——
只要记录一旦变成 `success`，第二道就不管了。

**证据 2：`_in_flight_blocker` 同样只在测试/探针里出现。**
`publish_service.py:135` 定义，`grep` 结果只有 `tests/test_student_publish.py`
和 `_meas/probe_concurrency.py` 引用。

**证据 3：注释引用了一个不存在的函数。**
`schedule_service.py:1619`
```
    就被 `_pending_publish_blocker` 拦住，表现为「任务到点不执行」。
```
`schedule_service.py:1639`
```
    而 `_pending_publish_blocker` 看到 processing 就拒绝再次发布——
```
`schedule_service.py:1733`
```
    # 先回源刷新 + 释放卡住的记录，否则 _pending_publish_blocker 会把到点任务全拦下。
```
`grep -rn "def _pending_publish_blocker" server/` → **无此函数**。
后继者读这些注释会去找一个幽灵，然后得出「防重复发布有三道」的错误结论。

**性质：补丁叠补丁**。`ALREADY_PUBLISHED` 这个错误码、`PUBLISH_STATUSES` 里的 `success`
分支、前端对 409 的处理、两个测试用例全都还在，只有那一行接线被删了，
**测试因为用的是同一个函数对象所以继续绿**（`tests/test_student_publish.py:1546` 断言
`duplicate.value.code == "ALREADY_PUBLISHED"` 现在跑不通，见 B-P2-5）。

**影响**：重复发布 = 同一件货在店铺里挂两个链接、两套库存和两个价格，
后来者按模板改价只改到一个，是真实的运营事故面（也是 `docs/project/rules.md` 里
「批量×逐件」教训的同一类：**并发/重复路径必须逐条验证**）。

**修复方案**：要么把 `_already_published_blocker` 接回 `:1303`（并在
`claimOnly` / 「定向换店」等合法重发场景留显式开关），要么删掉它 + 删掉
`ALREADY_PUBLISHED` 码 + 删掉那三条幽灵注释 + 改用例。**不能留中间态。**

### B-P1-2 台账被就地覆写：同一商品重发一次，上一条 `success` 记录当场变回 `processing`

**证据：`_upsert_log` 命中旧行时不新建，而是无条件复写状态字段。**
`publish_service.py:363-390`
```python
    row = _find_log(db, account_id=account.id, detail_id=str(detail_id),
                    shop_id=str(shop_id), warehouse_key=stable_key,
                    operator_id=operator_id)
    if row is None:
        row = PublishLog(account_id=account.id, detail_id=str(detail_id),
                         shop_id=str(shop_id), batch_id=batch_id,
                         attempts=1, requested_at=stamp, last_requested_at=stamp)
        db.add(row)
    else:
        row.attempts = int(row.attempts or 1) + 1
        row.batch_id = batch_id
        ...
        row.detail_id = str(detail_id)
    row.product_id = product.id if product is not None else None
    ...
    row.status = status
    row.reason = ""
    row.finished_at = None
```
`:354` 的默认值是 `status: str = "processing"`，`:1593` 调用时也显式传 `status="processing"`。
`_find_log`（`:400-430`）的判据正是「账号 + operator + detailId + shopId」
（第一级）或「账号 + operator + warehouse_key + shopId」（第二级）——
**也就是「同一件商品发同一家店」**，恰恰是重复发布时必然命中的组合。

**实证**
```
第一次: success 发布成功 12345.0 attempts= 1
第二次: processing '' None attempts= 2 同一行? True
库里行数: 1
→ 上一条 success 记录被就地改写成 processing，历史不可回查
```

**影响**
- **发布历史不可回查**。商品 A 昨天发到店铺 S 成功，今天再发一次（`_already_published_blocker`
  已死，见 B-P1-1，重发合法且常见），昨天那条 `success` + `finished_at` + 失败原因字符串
  一起消失，只剩 `attempts=2`。用户问「这件到底发出去几次、哪次成功的」时，本地台账答不出来。
- **`reason` 被无条件清空**（`row.reason = ""`），`_find_log` 第二级判断用的
  `warehouse_key` 也被改成新值（`:384`）。按 `status` 筛「成功」时，
  一条本该在 success 里的历史会跳到 processing。
- 与 `_find_in_flight`（`:111-115` 只查 `status == "processing"`）叠加：
  重发把旧行改回 `processing` 之后，这一行立刻成了「在途未确认」，
  **下一次**发布就会被 `_find_in_flight` 剔掉 —— 用户会看到「这件商品的发布结果尚未确认」
  而实际上两次都成功了。这是「覆写台账」与「在途检测」互相喂错。
- 唯一没被覆盖的是 `requested_at`（`:374-376` 的注释专门解释了为什么不覆盖），
  说明作者知道「覆写会丢信息」这件事，只是只对这一个字段做了保护。

**性质：补丁叠补丁**（`PublishLog` 早期当「一行 = 一次发布」用，后来又当
「一行 = 一件商品在某店的最新状态」用。`requested_at`/`last_requested_at` 双字段
就是这两种语义打架留下的疤。要查历史必须新增 `PublishAttempt` 子表，
或把 `_find_log` 的命中范围收窄到 `status in ('processing',)`。）

**修复方案**：状态更新与「插入一次尝试」拆开。`_upsert_log` 只负责「创建/复用
一条代表当前在途的记录」，历史一律 append 到独立表（或用 event/audit 表），
列表查询按 attempt 表聚合。至少要先把 `row.status = status` 改成
「仅当旧行不是 success 才覆盖」，并在覆盖前把旧行快照写进 `row.detail["previous"]`。

**回归测试**
```python
def test_republish_does_not_erase_previous_success_record():
    # 发成功一次 → 再发一次 → 断言仍能查到那条 success（新行或历史表）
def test_republish_does_not_make_success_look_in_flight():
    # 第二次发布后 _find_in_flight 不得因覆写把已成功的商品报成未确认
```

### B-P1-3 发布批次写了非法状态 `processing`：既不终态、又不能删、还永不更新

**证据 1：写进 Batch 的 `status` 不在状态机白名单里。**
`publish_service.py:1695-1701`
```python
    status = "done" if all(r["status"] == "success" for r in results) else (
        "failed" if results and all(r["status"] in ("fail", "failed") for r in results)
        else "processing" if results and any(r["status"] == "processing" for r in results)
        else "done")
    batch = Batch(
        id=batch_id,
        status=status if results else "done",
```
`batch_service.py:54-61`
```python
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_STOPPED = "stopped"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
BATCH_STATUSES = (STATUS_PENDING, STATUS_RUNNING, STATUS_PAUSED,
                  STATUS_STOPPED, STATUS_DONE, STATUS_FAILED)
```
`batch_service.py:70` `_TERMINAL = (STATUS_DONE,)`；`:77` `_ACTIVE = (pending, running, paused)`；
`:80` `_DELETABLE = (done, stopped, failed)`。

**证据 2：建批次时状态就固化，之后永不更新。**
`publish_service.py:1686-1688`
```python
    existing = db.get(Batch, batch_id)
    if existing is not None:
        return
```
台账的 `status` 是**创建的那一刻**的值，而创建发生在提交后（`:1585-1590`
先 `_upsert_log(status="processing")`，`:1618` 才提交上游，`:1643` 才落批次）。
所以正常发布一定写进 `processing`，后续无论成功失败都不会再改这一行。

**实证**
```
落库 batch.status = 'processing'  合法？ False
finished_at 已设？ True （已设置 finished_at 却仍是未终态）
删除：被拒 -> 批次状态是 processing，只有 done / stopped / failed 可以删除
→ 每次发布都会生成一条既不能删、也不算终态、也不在活跃集合里的批次

首次 batch.status = processing
台账已 success、再次记录后 batch.status = processing （早退，永不更新）
batch.results[0].status = processing
→ 批次状态永远停在建批次那一刻的快照
```

**证据 3：多店铺任务共用一个 `batchId`，批次只记下第一家店。**
`schedule_service.py:1517-1521` 的注释解释「共用一个 batchId 是刻意的」（回执按
batchId 匹配），但 `_record_publish_batch` 的第二行就是 `existing is not None: return`。
实证（同一 batchId 依此发 3 个店铺，A 店 success、B/C 店 processing）：
```
batch.status = done            合法？ True（第一批恰好 success → done）
batch.shop_id = SHOP_A          只记了第一个店
batch.results 条数 = 1          3 个店铺 × 1 件，应为 3 条
```
即「认领到采集箱」面板里，多店铺任务永远只显示第一家店的结果，
B/C 店发没发、成没成，这个面板上完全看不到。

**影响**
- `batch_service.py:829-832` 的 `delete` 用 `_DELETABLE` 卡住 → 这些批次**永远删不掉**，
  库会随发布次数单调增长，且界面上显示一个既非「完成」也非「失败」的状态。
- `_ACTIVE` 不含 `processing` → 同一账号 × 店铺的「未完成批次唯一」这条约束
  （`batch_service.py:72-77` 的注释专门解释为什么 `pending` 必须算进来）
  对发布批次完全不生效。
- `results[0].status` 也停在 `processing`，前端「认领到采集箱」面板看到的是永久的「进行中」。
- 多店铺任务的面板只显示第一家店，其余店铺的结果**没有任何界面可看**
  （只能去发布记录页逐个 detailId 查）。

**性质：根因**（`Batch.status` 有两套状态机：`batch_service` 的六态机、
`publish_service` 自己拼的 `done/failed/processing`。写状态的地方没有校验，
`Batch.status` 列上也没有 `CHECK` 约束 —— 见 `models/batch.py:44`
`status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)`）。

**修复方案**：`_record_publish_batch` 只允许写 `BATCH_STATUSES` 里的值
（`processing` → `running`），并且改成 upsert（命中已有行就更新 `status`/`results`/
`finished_at`，而不是早退）；多店铺共用 batchId 时，`results` 要按
`(shopId, detailId)` 累积而不是只留第一批。同时给 `Batch.status` 加 `CHECK` 约束或
在 `models` 层做 `@validates`，让非法值在写入时就炸而不是留在库里。

**回归测试**
```python
def test_publish_batch_status_is_always_a_legal_batch_status():
    # 扫全仓 row.status/batch.status 的字面量，必须在对应白名单内
def test_publish_batch_record_is_updated_after_ledger_reaches_terminal():
    # 台账 success 后再记录一次，batch.status 必须变 done
def test_publish_batches_are_deletable():
    # 任何完工的发布批次都必须能删
def test_multi_shop_batch_records_every_shop():
    # 同一 batchId 依次发 3 个店铺，batch.results 必须有 3 条且 shopId 齐全
```

### B-P1-4 发布记录状态机：`claimed` / `unknown` 两个状态写进去了，但列表筛选和前端都不认

**证据 1：两个新状态的写入点。**
`schedule_service.py:1706-1711`
```python
        if _is_claim_only_log(db, row):
            row.status = "claimed"
            row.reason = "认领已完成（认领不走发布回执），自动归档"
        else:
            row.status = "unknown"
            row.reason = "超时未收到平台回执，已释放以免阻塞后续发布"
```
- 后端筛选白名单不认：`publish_service.py:64` `PUBLISH_STATUSES = ("processing", "success", "fail", "cancel")`，
  `:284-285`
  ```python
    if status is not None and status != "" and status not in PUBLISH_STATUSES:
        raise errors.validation("发布状态筛选无效", field="status", value=status)
  ```
  → 用户**无法筛选**出这些记录（`?status=unknown` 直接 422）。而 `list_logs` 的
  `status` 参数是唯一按状态过滤的手段，不传就全量返回（后端会返回这些行，只是筛不了）。
- 前端下拉框也没有：`web/src/features/publish/PublishPage.tsx:32-38` 只有
  `all/processing/success/fail/cancel`；而 `web/src/lib/constants.ts:76-79` 的
  `PUBLISH_STATUS_META` **有** `claimed`/`unknown` 的文案。
  → 徽章能显示（"已认领"/"结果未确认"），但用户没法筛，也不知道这两个状态是怎么来的。
- `main.py:220-251` 的调度循环、`schedule_service.py:1614-1632`
  `refresh_processing_logs` 只看 `processing` —— 一旦被标成 `unknown`，
  平台侧后来真的成功了也不会再回源确认（`release_stale_publish_logs`
  已把它们从 `processing` 池里拿走了）。`docs/features/publish/receipt-confirmation-2026-11-19.md`
  里记的「实测 119 条 unknown 全部其实是 success」，这条通道还在。

**证据 2：四处状态定义各自为政。**
| 位置 | 定义 | 含 `claimed`/`unknown` |
|---|---|---|
| `publish_service.py:64` `PUBLISH_STATUSES` | `("processing","success","fail","cancel")` | ❌ |
| `web/src/lib/constants.ts:65-79` `PUBLISH_STATUS_META` | 六项（含 claimed/unknown 文案） | ✅ |
| `web/src/features/publish/PublishPage.tsx:32-38` `STATUS_OPTIONS` | `all/processing/success/fail/cancel` | ❌ |
| `models/publish_log.py` `status` 列 | `String(16)`，无 `CHECK` | —（无约束） |

**证据 3：`_is_claim_only_log` 的判据依赖 `row.detail` 里的 `claimOnly` 标记。**
`publish_service.py:75-85`
```python
def _is_claim_only_log(db: Session, row: PublishLog) -> bool:
    detail = row.detail if isinstance(row.detail, dict) else {}
    if "claimOnly" in detail:
        return detail["claimOnly"] is True
```
`row.detail["claimOnly"]` 是 `publish_service.py:1597`
```python
            row.detail = {**(row.detail or {}), "claimOnly": claim_only}
```
写的 —— 只有走 `publish()` 才会有这个键。老数据没有，只能靠 `batch.settings.kind`
兜底，而 `_record_publish_batch` 又把 `kind` 写死成 `"warehouse_claim"`
（`publish_service.py:1706`），于是「发布批次」的 `kind` 也是 `warehouse_claim`。

**影响**：运营在「发布记录」页把状态筛到「进行中」时，403 条 `unknown` 一条都看不到，
以为台账是干净的；而它们其实是「可能已发成功但本地记成未确认」的那批，
正是 `docs/features/publish/receipt-confirmation-2026-11-19.md` 记录的事故来源。
更严重的是 `unknown` 之后不再回源 —— 平台侧真的成功了本地永远不知道，
用户会因为「结果未确认」而重复发布（B-P1-1 的两道防护已死，没有东西拦）。

**性质：跷跷板**（为了让「认领记录不显示成发布失败」新增两个状态，没有同步
「状态集合」这一处唯一定义 —— `PUBLISH_STATUSES`、`PUBLISH_STATUS_META`、
`STATUS_OPTIONS`、DB 无约束，四处各写一份）。

**根因修复方案**：状态集合收敛成唯一枚举（后端 `Enum` + DB `CHECK`），
前端 META/OPTIONS 由后端一处生成；`release_stale_publish_logs` 标 `unknown`
时必须同时把它放进「下一轮仍要回源」的队列（或直接标成
`unknown_pending_recheck` 并让 `refresh_processing_logs` 也扫它），
不允许出现「进入某状态后永不再核对」。

**回归测试**
```python
def test_every_written_publish_status_is_filterable_and_renderable():
    """扫全仓所有 row.status = "..." 字面量 → 必须在 PUBLISH_STATUSES 里；
    前端 STATUS_OPTIONS 与 PUBLISH_STATUS_META 的键集合必须相等。"""
def test_unknown_status_eventually_rechecked_or_flagged():
    """标 unknown 的记录必须仍可回源（不能被 release 之后彻底遗忘）。"""
def test_claim_only_log_detection_survives_missing_detail_marker():
    """老数据（无 detail.claimOnly）也必须能正确区分认领/发布。"""
```

### B-P1-5 `_is_already_claimed` 关键词启发式：7 条真实失败里 5 条被判成「已认领成功」

**证据 1：判据本体。**
`collect_box_service.py:690-702`
```python
def _is_already_claimed(message: str) -> bool:
    text = str(message or "").lower()
    has_already = ("已" in text or "已经" in text
                   or "already" in text or "exist" in text)
    has_target = ("采集箱" in text or "店铺" in text or "存在" in text
                  or "shop" in text or "collect" in text)
    return has_already and has_target
```
**证据 2：实证七例（`_verify_b_service.py::repro_already_claimed`）。**
```
判成功 <- 该商品已在采集箱中          ✅ 正确
判成功 <- 已存在相同商品              ✅ 正确
判成功 <- 该商品已被删除，记录不存在   ❌ 错（真失败）
判成功 <- 商品已下架，店铺记录不存在   ❌ 错（真失败）
判成功 <- 已达店铺上限，商品不存在     ❌ 错（真失败）
判成功 <- 该账号已有其他店铺，商品不存在 ❌ 错（真失败）
判成功 <- Item already exists in another shop  ❌ 错（在别的店，不是目标店）
```
「已」+「不存在」几乎命中了中文报错的全部子集，因为它把「已」当成
already-exists 的判据（中文里「已删除」也带「已」）。后果：`claim_site_batch`
（`:600-702`）把这些件计成 skipped/成功，运营看到「全部成功」，实际一件都没进采集箱。

**影响**：认领任务的**成功率是假的**。7 条真实报错里 5 条被判成功，
而认领失败的真实原因（店铺上限、商品已删除、账号已有其他店铺）全部被吞成「已认领」，
用户既不会重试也找不到原因 —— 与 B-P0-1 那种「明确报错」相反，这是「静默错报成功」。

**性质：根因**（用自然语言子串判断业务语义，且 `has_target` 里包含「存在」这个
在否定句里同样出现的词）。

**根因修复方案**：改成结构化判据 —— 优先用上游错误码（`exc.code`）白名单，
退而用「肯定式关键词 + 排除否定式关键词」（`"不存在" not in text and "已删除" not in text`）。
长线做法是把「这件是否已在目标采集箱」改成一次真实的查询（`collect_box_list` 里找 detailId），
而不是从报错文案反推。

**回归测试**
```python
@pytest.mark.parametrize("message,expected", [
    ("该商品已在采集箱中", True),
    ("已存在相同商品", True),
    ("该商品已被删除，记录不存在", False),
    ("商品已下架，店铺记录不存在", False),
    ("已达店铺上限，商品不存在", False),
    ("该账号已有其他店铺，商品不存在", False),
    ("Item already exists in another shop", False),
])
def test_is_already_claimed_does_not_treat_failures_as_success(message, expected):
    assert collect_box_service._is_already_claimed(message) is expected
```

### B-P1-6 模块级可变全局：全部无锁，且全部是进程内状态

**证据 1：五处模块级可变状态全都没有锁（AST + 源码核对）。**

| 位置 | 内容 | 枷锁 | 并发写者 |
|---|---|---|---|
| `exchange_rate.py:59` `_cache`，`:68-71` `_bucket` 的 `setdefault` | 各币种汇率桶 | ❌ 无 | `live_rate` 被 `ThreadPoolExecutor(max_workers=40)` 里的任意线程调 |
| `carrier_service.py:1360` `_CATEGORY_ATTR_CACHE` | 类目属性缓存 | ❌ `_TtlCache`（`:1340-1352`）内部无锁，`get` 里还会 `pop` | 载体流程多条线程 |
| `collect_box_service.py:55-64` `_CACHE` | 采集箱列表缓存 | ❌ 双重检查无锁（TTLCache 自身有锁，但 `_CACHE` 的赋值可能做两次） | API 线程池 |
| `sync_service.py:166-183` `_TEMPLATE_CACHE` | 上游模板缓存 | ❌ 同上 | 同步任务 + API |
| `schedule_service.py:671-676` `_scheduler_heartbeat` / `_running_tasks` | 调度心跳 + 执行中标记 | ❌ 普通 dict | 调度线程 + `_run_plan_in_thread` 的多账号线程 + API 读 `task_running` |

**证据 2：并发是真的（线程模型）。**
- `main.py:627` `uvicorn.run(app, host=host, port=port, log_level="info")` —— 单 worker，
  所以**目前**没有多进程问题。
- 但 `main.py:321` `_futures.ThreadPoolExecutor(max_workers=40, thread_name_prefix="dsh-io")`
  加上 `schedule_service.run_due` 里按账号再开一层 `ThreadPoolExecutor`（`:1756-1763`），
  同一进程内的真并发是常态。
- `_running_tasks` 的语义是「同一任务不能同时跑两轮」，但它只在 `execute()`
  （`:1421`）里写、`task_running()`（`:689`）里读，**从来没有被用来做互斥** ——
  它只喂给序列化输出 `:419` 的 `"running"` 字段。真正的互斥只有 `_batch_guard`
  （`:1321-1339`，对词库）和 `hold_for_session`（`core/locks.py:161`，对商品键）。
  调度器每 60s 扫一次，`run_due` 的超时是 `RUN_DUE_TIMEOUT_SECONDS=1800`
  （`:1767`），**上一轮还没跑完就会开始下一轮**，两次 `run_due` 会为同一个
  `task_id` 各起一条线程 → 同一个定时任务被并发执行两遍（发两遍货）。
  `:1768-1772` 的 `future.cancel()` 对被放弃的线程**无效**（已经在跑）。

**性质：根因**（并发模型从未被写下来：单 worker + 40 线程 + 嵌套线程池 + 允许重叠的
定时扫描，四者叠加，而所有共享状态都假设单线程）。多 worker 部署的那一天，
`exchange_rate` 缓存、`_running_tasks`、`_scheduler_heartbeat` 会同时失效，
调度器会每个 worker 各跑一份。

**影响**：眼下最实际的后果不是「缓存脏了」，而是 `_running_tasks` **看起来像互斥锁、
实际不是** —— 后继者读 `mark_task_running` / `task_running` 会以为调度器已经防了重入，
从而不去补真正的锁。真正发生的重入路径是「`run_due` 超时 1800s，扫描间隔 60s」：
一轮没跑完下一轮就开始了，同一个 `task_id` 两条线程并发发同一批货。

**根因修复方案**
1. 明确写下并发模型（README/architecture.md）：单 worker、进程内线程池、
   哪些状态是进程内的、为什么可以。
2. `_running_tasks` 要么升级成真互斥（`threading.Lock` + 检查后再置位，
   或 `core/locks.py` 的 `hold_for_session`），要么改名成 `_task_started_at`
   并在 docstring 里写明「仅用于展示，不做互斥」，避免误导。
3. 缓存统一用 `services/cache.py` 的 `TTLCache`（有锁 + deepcopy），
   删掉 `carrier_service._TtlCache`。
4. `run_due` 加重入保护：上一轮 `future` 未完成时直接跳过本轮并记 warning。

**回归测试**
```python
def test_run_due_overlapping_scans_do_not_double_publish():
    """并发调用两次 run_due(same now)：同一 task 只能执行一次。"""
def test_exchange_rate_cache_concurrent_access():
    """20 线程并发 live_rate/cached_rate/fallback_rate，无异常、无脏桶。"""
def test_ttl_caches_are_single_instance_under_concurrency():
    """_cache()/_template_cache() 并发首次调用只创建一个实例。"""
def test_running_tasks_marker_actually_prevents_reentry():
    """mark_task_running 之后，第二次 execute(same task) 必须被拒（或明确记录当前不防重入）。"""
```

### B-P1-7 重试与幂等：认领路径在上游调用前**没有持久化台账**

**证据 1：`not claim_only` 把认领路径整条排除出「先提交台账」。**
`publish_service.py:1617-1620`
```python
        db.flush()
        if published and not claim_only:
            _submit_persisted_publish(
                db, client, detail_ids=published, shop_id=str(target_shop_id), logs=logged)
```
`_submit_persisted_publish` 的注释（`:436-440`）说得很清楚：
> Persist the exact submission before an irreversible upstream request.
> A caller rollback or process exit after submission must not erase the only
> evidence that the platform may already have accepted these products.

并且 `:449-451`
```python
    # The committed processing rows take over from the session locks when this
    # checkpoint releases them. Claim-only operations never enter this helper.
    db.commit()
```
**`not claim_only` 这个条件把认领路径整条排除了**。而认领路径的上游动作
（`collect_and_fill_carriers` → `collect_carriers` 建载体 + `fill_carriers` 写入）
是**不可撤销**的、且**会真的在妙手那边产生商品**（`collect_box_service.py:1363-1371`
的注释承认「认领直接沿用 payload」）。它只 `db.flush()`，没有 `commit()` ——
如果这个请求超时/进程退出/`audit_service.guard` 回滚，台账里那一行 `processing`
一起没了，用户重试就会在妙手那边**再建一批载体**（重复商品）。

**证据 2：重试循环清单（逐个判定）**

| 位置 | 形态 | 判定 |
|---|---|---|
| `carrier_service.py:2089-2119` | `while True` + `cost_floor_retries<12` + `version_refresh_count<5` | 有上限，但**无退避累加**（md5 冲突固定 `sleep(1.2)`），且每次都重读 `ossMd5` 后**重新提交**；如果上游其实已经保存成功只是回了错，会重复写 |
| `carrier_service.py:900` | `for attempt in range(3)`（单件写入） | 有上限 |
| `carrier_service.py:838-946` | `item_round` 外层重试 | 有上限 |
| `carrier_service.py:2231` | `while True`（生产者/消费者 drain） | 由 `queue` 的 `None` 哨兵终止，正常 |
| `publish_service.py:1374-1394` | `for attempt in range(3)` 包 **`collect_and_fill_carriers`** | ⚠️ **每次重试都重新 `collect_carriers`（新建载体）**。第一次若已写入成功但返回 `saved` 为空（`:1381-1382` 用 `filled.get("saved")` 判断），会再建一批 → 重复商品。这是本清单里**唯一会产生重复上游对象**的重试 |
| `warehouse_service.py:4366` | `while True` + 12 次成本下限 | 有上限，抬价不滚雪球（`:181-182` 注释说明用 originPrice 而非当前价做基准） |
| `warehouse_service.py:4417` | `for attempt in range(2)` 读回重试 | 有上限，且「取不一致项更少的那次」 |
| `warehouse_service.py:4080` | `for attempt in range(3)` | 有上限 |
| `collect_box_service.py:364` | `for attempt in range(retries+1)`，翻页 + 重试 | 有上限 |
| `collect_box_service.py:419` | `while True`（翻页） | 由 `page_size`/`total` 终止，正常 |

**影响**
- 认领是「用本地 payload 在目标店新建载体 + 写入商品」，**不可撤销**且会在妙手侧
  真实产生商品。台账那一行随请求回滚一起消失后，用户重试 → 目标店里出现两批同款商品。
- `_find_in_flight`（`:111-115`）只查 `status == "processing"` 的行，认领路径既然
  没 commit，第二次调用完全看不到第一次，**在途检测对认领全域失效**。
- 下面清单里 `publish_service.py:1374-1394` 的重试每次都会重新 `collect_carriers`，
  与这条叠加：一次超时会自己再造一批载体，不需要用户重试。

**性质：根因**（「重试一个不可撤销的上游写」这件事没有统一的幂等键策略：
`PublishLog` 的 `uq_publish_logs_request_*` 只覆盖发布，认领/载体没有对应台账）。

**根因修复方案**
1. 认领路径也走「先 commit 台账再打上游」：把 `_submit_persisted_publish` 的
   commit-before-upstream 模式抽成 `checkpoint_before_upstream(db, logs)`，
   发布和认领共用。`claim_only` 不是豁免理由 —— 它比发布更需要在途台账
   （发布有平台侧回执可查，认领只在妙手侧留一个商品）。
2. 重试必须带幂等键：`collect_and_fill_carriers` 接收 `request_id`
   （来自台账行 id），上游调用前先查「这个 request_id 是否已经建过载体」。
3. `PublishLog` 增加 attempt 子表（同 B-P1-2 的修复），一次尝试一行，
   重试是新增行而不是覆盖。

**回归测试**
```python
def test_claim_path_commits_ledger_before_upstream_write():
    """用会抛异常的 client 跑 claim：断言台账里已有一条 processing 行（已 commit），
    且第二次调用的 _find_in_flight 能看见它。"""
def test_publish_retry_does_not_create_duplicate_carriers():
    """第一次 collect_and_fill_carriers 已写入但返回 saved=[]：重试不得再次 collect_carriers。"""
```

### B-P1-8 `_save_processed_draft` 的读回校验可以被一次 `except` 整段跳过

**证据 1：明细生成失败被整段吞掉。**
`warehouse_service.py:4441-4445`
```python
        try:
            rows = readback_check.details(upstream_payload, actual, original,
                                          coverage, pricing_enabled=pricing_enabled)
        except Exception:
            rows = []
```
这里 `rows` 只是**给界面看的明细**，跳过了不影响 `raise`（真正阻断在前一行），
所以严格说不是门禁漏洞。但它把「为什么不一致」的唯一线索吞掉了 ——
`raise` 出去的 `details={"mismatches": mismatches, "rows": []}`，
前端 `renderDiagnostics` 拿到空数组只能显示「不一致」。
`warehouse_service.py:4446` 的 `logger.error` 还在，所以线上能查；但界面上等于没信息。
同类：`warehouse_service.py:4326-4328`（规格规范化失败退回原样 → 后续
`SKU不存在` 报错与真实原因隔了一层）、`:4441` 这条。

**影响**：用户看到的是「读回内容与提交内容不一致」但没有任何一项对不上，
只能把整件商品提给开发；开发只能去翻服务器日志。
`docs/project/rules.md` 里「报错要能指导行动」这条要求在这种场景下失效。

**性质：补丁叠补丁**（「明细失败不能阻断主流程」这个决定单独看是对的，
但它让「唯一诊断入口」变成可选的）。

**根因修复方案**：把 `rows` 的失败和 `mismatches` 的失败分开处理 ——
`mismatches` 已经算出来了就必须原样带出去（现在是），
`rows` 计算失败时在 `details` 里放 `{"rowsError": str(exc)}` 而不是空数组，
让前端能显示「明细生成失败：<原因>」。这样既不阻断也能诊断。

**回归测试**
```python
def test_readback_details_failure_is_reported_not_swallowed(monkeypatch):
    monkeypatch.setattr(readback_check, "details", lambda *a, **k: 1/0)
    # 断言抛出的 AppError.details 里含 rowsError，且 rows 不为静默空数组
```

---

## P2 问题

### B-P2-1 `except Exception` 全量清单与分类（AST 扫描，不是 grep 估数）

**证据 1：`ast` 逐文件统计（`docs/audit/_verify_b_service.py::scan_handlers`，不是 grep 估数）。**
```
services/warehouse_service.py     handlers=50  pass-only=2  broad=26  silent=[2620, 2847, 3053, 4444]
services/carrier_service.py       handlers=32  pass-only=0  broad=7   silent=[690]
services/publish_service.py       handlers=19  pass-only=1  broad=6   silent=[]
services/schedule_service.py      handlers=17  pass-only=0  broad=6   silent=[]
services/collect_box_service.py   handlers=19  pass-only=0  broad=2   silent=[1383]
TOTAL handlers = 137，其中 broad(Exception/BaseException) = 47，完全静默 6
```
- **裸 `except:`（`except: pass` 那种）在五个文件里是 0 处** —— 会话任务描述里的
  「9 处 `except: pass`」不在这五个文件里（在 `word_service.py:52-53` 等模块）。
- `pass` 作为唯一语句的只有 3 处，且都不是 broad：
  `warehouse_service.py:327-328`（`float(override)` 失败退回品类默认值，合理）、
  `warehouse_service.py:4346-4347`（`float(_src)` 失败，合理）、
  `publish_service.py:342-343`（`int(upstream_total)` 失败，合理）。**这 3 处无需改。**
- **无 log、无 raise、无 return 的「完全静默」broad handler 共 6 处**（AST 精确，
  `scan_handlers()` 的 `silent` 列）：
  | 位置 | 吞了什么 | 判定 |
  |---|---|---|
  | `warehouse_service.py:2620-2621` | `Decimal(originPrice)` 失败 → `continue`（价格不改写，且无痕） | 应 warn |
  | `warehouse_service.py:2847-2848` | 类目关键词兜底失败 → `kw_cid=""` | 可接受，建议 debug |
  | `warehouse_service.py:3053-3054` | 类目要求查询失败 → `requirements=None`（规则 11/12 静默失效） | **应 warn** |
  | `warehouse_service.py:4444-4445` | 读回明细生成失败 → `rows=[]` | 见 B-P1-8 |
  | `carrier_service.py:690-691` | 熔断器读取失败 → 默认停顿 | 可接受 |
  | `collect_box_service.py:1383-1384` | 目标店站点取不到 → `shop=None` → 站点退回 `MY` | **应 fail**，见下 |

  最后一条单独说：`collect_box_service.py:1381-1384`
  ```python
    try:
        shop = require_shop(db, account, target_shop_id)
    except Exception:  # noqa: BLE001 - 拿不到站点就退回 MYR，不阻断认领
        shop = None
    site = str((shop or {}).get("site") or "MY").strip().upper()
  ```
  退回 `MYR` 后整批按马币汇率换算，然后把结果发给菲律宾店 —— 与 B-P0-2 叠加会
  直接把错误币种的价格写进上游。这条 `except` 是「不阻断」写成「算错钱」的典型。
- 其余 **41 处** broad handler 都有 log 或 return，属于合理降级，不逐条列。
  抽检后仍值得收紧的：`warehouse_service.py:3196-3198`（产品标准自动修复失败 →
  `standard_report={"error":...}`，门禁就此放松，见 B-P0-4）、
  `warehouse_service.py:1230-1232`（采集箱载体校验失败 → `alive=set()` → 走载体重建，
  会多建一批载体）、`publish_service.py:1642-1648`（批次记录丢失但发布照常）、
  `schedule_service.py:1305-1308`（执行历史丢失但 `completed_runs` 已递增）。

**影响**：6 处完全静默的 handler 里，有 4 处会让一个**校验/门禁悄悄失效**
（`3053` 类目要求、`4444` 读回明细、`1383` 目标币种、`2620` 价格改写），
每一处的症状都是「流程正常结束，但结果不对」。这类问题无法从日志发现，
只能靠人工审计或用户投诉定位 —— 是本报告多条 P0 的共同放大器。

**性质：混合**。「合理降级」与「吞掉状态不一致」在代码里长得一模一样
（都是 `except Exception: # noqa: BLE001 - ...`），没有任何机制区分它们。

**根因修复方案**
1. 给降级分两类并显式标注：`# degrade: optional-enrichment`（允许静默）与
   `# degrade: guard-bypass`（**禁止静默**，必须 logger.warning 并带上下文）。
   两者能用 lint 规则区分（后者必须紧跟 logger 语句）。
2. 6 处静默 handler 全部补齐 `logger.warning(...)`；
   `collect_box_service.py:1383` 改成 fail-fast（拿不到目标站点就报错，不要退回 MYR）。
3. 把 `except Exception` 的宽窄当代码评审项：新代码若捕获 `Exception`，
   PR 必须说明「为什么不能收窄到具体异常」。

**回归测试**
```python
def test_no_silent_broad_except_handlers():
    """AST 扫描：services/**.py 里 except Exception 的 body 必须含
    logger.* / raise / return（带值）之一，否则测试失败。
    （本报告 scan_handlers() 的 silent 列就是原型）"""
def test_claim_aborts_when_target_shop_site_unknown():
    """require_shop 抛错时认领必须失败，不得退回 MYR 继续换算。"""
```

### B-P2-2 同一逻辑的多份实现（bug 温床清单）

**证据：同一概念的多份实现（逐个列）。**

| 概念 | 实现 1 | 实现 2 | 风险 |
|---|---|---|---|
| 读回比价 | `readback_check.py:247-263` `_prices_unchanged`（对比 original，`_number`） | `readback_check.py:266-289` `_prices_match`（对比 payload） | 两条分支 `:408-417` 二选一，判定口径不同（见 B-P0-1） |
| SKU 图保留判定 | `pre_save_check.py:382` `_retained_sku_images_unchanged` | `readback_check.py:320` `_retained_sku_images_unchanged` + `:326 _sku_image_slots_match` | save 前与 readback 后两套判据，注释（`readback_check.py:396-406`）承认改过一次 |
| 防重复发布 | `publish_service.py:88-132` `_find_in_flight`（在用） | `:135-151 _in_flight_blocker` + `:154-191 _already_published_blocker`（死） | 三份，一份活 |
| 价格修复 | `product_standard.py:206-226`（validate） | `:509-534`（repair） | 同一公式抄两遍，改一处漏一处 |
| 认领加价 | `carrier_service.py:193-200` | `collect_box_service.py:1420-1423` | 系数共用（好），公式各写一遍 |
| TTL 缓存 | `services/cache.py` `TTLCache`（有锁、deepcopy） | `carrier_service.py:1340-1352` `_TtlCache`（无锁、不 deepcopy） | 新写的轮子比现成的差 |
| 规格规范化 | `carrier_service.normalize_spec_for_save`（`:1459`） | 被 `warehouse_service._save_processed_draft:4324` 与载体流程 `:2124` 两处分别调用，参数形态不同 | 同一函数两套调用契约 |

**性质：根因**（没有「同一个概念只许有一个实现」的约定，也没有 CI 检查）。

**影响**：这 7 组重复里，前 5 组各自已经在生产上出过问题（读回比价 → B-P0-1；
SKU 图判据 → `readback_check.py:396-406` 注释记载的返工；防重复发布 → B-P1-1；
价格修复 → B-P0-4；认领加价 → B-P0-2 的跨站点）。**每一组重复都是一次
「改了一处、另一处照旧」的等待发生**，而且这类不一致在测试里同样测不出来
（测了 A 实现不代表 B 实现一致）。

**根因修复方案**：把这 7 组各收敛成一个函数，并把「被两处调用」写进 CI 检查：
对每个收敛后的函数，断言全仓只有一处定义（`grep -c "def 函数名"` == 1），
且调用点覆盖原先的分支。工程上更重要的是**先收敛读回/保存这两套比价判据**
（它们直接决定商品能不能入库）。

**回归测试**
```python
def test_no_duplicate_pricing_or_readback_implementations():
    """AST/grep 断言：_prices_unchanged、_prices_match、_retained_sku_images_unchanged、
    price 修复公式在全仓各只有 1 处定义。"""
def test_pre_save_and_readback_use_same_price_comparison():
    """同一份 (original, payload): pre_save 放行 ⇔ readback 放行。"""
```

### B-P2-3 本地记账键会漏给上游

**证据 1：剔除名单里没有新增的本地键。**
`warehouse_service.py:4138-4143`
```python
_LOCAL_ONLY_PAYLOAD_FIELDS = (
    "colorMap", "sizeMap", "colorPropName", "sizePropName",
    "_sourceWeight", "weightAutoFixed", "weightBeforeCap",
    "translation", "violations", "removedWords", "needsRecollect",
    "needsRecollectReason",
)
```
实测：
```python
'_warehousePriceRate' in _strip_local_only_fields(priced) → True
# 即这个纯本地记账字段会被一起写给妙手
```
另外 `_costFloorAdjustments` 是被**显式写进上游 payload** 的：
`warehouse_service.py:4384-4386`
```python
            payload["_costFloorAdjustments"] = price_adjustments
            if isinstance(upstream_payload, dict):
                upstream_payload["_costFloorAdjustments"] = price_adjustments
```
`normalize_spec_for_save`（`carrier_service.py:1478` `deepcopy(dict(payload))`）
也不会剔除它们。妙手对多余字段的敏感度在 `carrier_service.py:1475-1476` 有明确记载：
> 同时剔除文档没列的多余字段（imgUrls/imageUrl/supplementarySkuImageUrls），
> 上游对多余字段敏感。
本地记账键不在白名单里，属于同一类风险；同时也是 B-P0-1 的直接成因。

**性质：补丁叠补丁**。`_LOCAL_ONLY_PAYLOAD_FIELDS` 是一份「手工维护的剔除名单」，
每新增一个本地字段就得记得往里加一次。已有的 12 个条目说明这份名单被维护过多次，
而 `_warehousePriceRate`（2026-11-25 新增）与 `_costFloorAdjustments` 都没加进去 ——
**名单式白名单的失败模式就是「新字段默认泄漏」**。正确方向是反过来：
出口处只允许上游文档规定的字段通过（allowlist），而不是剔除已知的本地字段（denylist）。

**影响**：`_warehousePriceRate` 是 P0-1 的直接成因（被当成价格字段 → 入仓 422）；
`_costFloorAdjustments` 会连同抬价前后的价格一起推给妙手，属于「上游敏感字段」风险面
（`carrier_service.py:1475-1476` 明确说过上游对多余字段敏感）。
两者都只在本地有意义，泄漏出去既无用途又可能触发上游校验。

**根因修复方案**：`_strip_local_only_fields` 改成出口 allowlist
（以上游文档字段为准，`skuMap` 内部字段逐个白名单），并加一条断言：
「任何以 `_` 开头的 key 在发上游前必须已被剔除」。这条规则同时解决
`_warehousePriceRate` / `_costFloorAdjustments` 以及未来的本地字段。

**回归测试**
```python
def test_no_underscore_keys_reach_upstream_payload():
    """所有出口（save_shop / publish_items 的入参）不得含 '_' 开头的 key。"""
def test_strip_local_only_fields_removes_all_known_local_keys():
    payload = {**base, "_warehousePriceRate": ..., "_costFloorAdjustments": ...}
    assert not any(k.startswith("_") for k in _strip_local_only_fields(payload))
```

### B-P2-4 `_store` 的异常边界把「记账」和「计数」拆在两处

**证据：三件事的异常边界各不相同，`note_result` 落在无人区。**
`schedule_service.py:1294-1314`
```python
    saved = note_result(db, str(task.id), moment=moment,
                        success=summary["status"] in (RUN_OK, RUN_SUBMITTED),
                        result=summary)
    ...
    try:
        record_log(db, task, summary, moment=moment)
    except Exception:  # noqa: BLE001
        logger.warning("写入定时任务执行历史失败 taskId=%s", task.id, exc_info=True)
    if (task.selected_product_ids and task.planned_runs
            and summary.get("selected", 0) > 0
            and summary.get("status") == RUN_OK):
        task.completed_runs = int(task.completed_runs or 0) + 1
```
注释解释得很清楚（`completed_runs` 递增必须在 try 外，否则会重复发布），
但 `note_result` 在 try 外**没有**保护 —— 它一旦抛错（`get_schedule` 查不到、
`next_due` 抛错），`execute` 的 `except` 会把整轮记成 failed，
而 `mark_task_running` 的 `finally` 已经清了标记，于是**下一轮会重跑同一批**。
即「丢日志可以，丢计数不行」这个判断只对 `record_log` 做了，没对 `note_result` 做。

**影响**：`note_result` 写的是「今天已经跑过 / 今天失败几次」（`last_run_date`、
`attempt_date`、`attempts`），它抛错但事务没提交时，`execute` 会把整轮记成 failed，
而 `mark_task_running` 的 `finally` 已清标记 —— **下一轮扫描会重跑同一批商品**，
且 `attempts` 没累加，`SCHEDULE_MAX_ATTEMPTS_PER_DAY` 的熔断对它无效。

**性质：补丁叠补丁**（第一次「历史写入失败不能影响发布」时把 `record_log` 包进 try，
第二次「`completed_runs` 不能被 try 吞掉」时把它挪到 try 外 —— 两次都在同一个函数里
逐个语句地调整异常边界，从未把「哪些写入是可丢弃的、哪些是不可丢弃的」写下来。
`note_result` 恰好落在两者之间，两头都没被这次调整覆盖到）。

**根因修复方案**：在 `_store` 顶部把三件事的「可丢弃性」一次性写清并实现：
`record_log`（可丢弃，try 内）、`completed_runs`（不可丢弃，且必须与 publish 结果
同事务）、`note_result`（**不可丢弃且必须最先执行并提交** —— 它是防重复执行的唯一依据）。
顺序应为 `note_result` → `completed_runs` → `record_log`，前两者失败即整轮失败，
`record_log` 失败只记 warning。

**回归测试**
```python
def test_note_result_failure_does_not_allow_rerun_of_same_batch(monkeypatch):
    """note_result 抛错时，本轮必须 fail 且 attempts 已累加（或明确不重跑）。"""
def test_store_commits_run_marker_before_history_log(monkeypatch):
    """record_log 失败不得影响 last_run_date/attempts 的落库。"""
```

### B-P2-5 服务层测试是红的，最大一片是「stub 签名过期」，说明这条链路根本没在 CI 里跑

**证据 1：跑一次就知道。**
```
$ python -m pytest tests/test_student_publish.py tests/test_claim_price_currency.py -q
tests/test_student_publish.py      76 项 → 44 failed / 32 passed
tests/test_claim_price_currency.py  9 项 →  9 passed
合计 85 项，44 failed
```
44 个失败里 **38 个**是同一个原因：
```
TypeError: _stub_member_publish.<locals>.fill_carriers() got an unexpected
           keyword argument 'category_trees'
```
测试里的 stub（`tests/test_student_publish.py:1115-1116`）签名是
`fill_carriers(_client, _carriers, payloads, *, shop_id, verify_readback, strict_readback)`，
而生产代码（`publish_service.py:1547-1550`）传了 `category_trees=`、
且 `verify_readback=False`（`:1553`）。
也就是说：**`publish` 这条主链路在 CI 里 100% 抛异常**，任何回归都测不出来。
剩下 6 个失败还有：`KeyError: 'notes'`（`prepare_claim_payloads` 不返回 `notes` 字段）、
`assert 422 == 403`（学员发布缺模板的检查排在越权检查之前）、
`An already verified automation draft must not be rebuilt or repriced`。

顺带：`grep -rn "enforce_pre_save" server/tests/` **零命中** —— 见 B-P0-1。
`docs/audit/A-data-layer.md` 也记了 CI 主线是红的（`-m unit` 4 failed / ruff 40 errors）。

**影响**：服务层五个核心文件的回归防护**当前等于零**。
`publish` 是被 44 个用例里的 38 个覆盖的，它们全在第一步就 TypeError，
所以「发布逻辑改坏了」这件事 CI 发现不了 ——
本报告 B-P1-2 / B-P1-3 / B-P1-4 三条状态机问题能长期存在，就是这条的直接后果。

**性质：根因**（测试与实现之间没有契约约束；stub 靠 `monkeypatch.setattr` 手写签名）。

**根因修复方案**
1. 所有 stub 用 `**kwargs` 接收 + 测试开头断言 `inspect.signature` 兼容，
   签名变更时「签名断言」先红，而不是 38 个用例一起 TypeError。
2. 补一条真实 `save_from_collect_box(enforce_pre_save=True)` 端到端用例
   （FakeClient 记录 `save_shop` 入参），覆盖 B-P0-1 的回归。
3. CI 必绿子集：`pytest -k "pricing or state_machine or idempotency"` +
   本报告的 3 个 AST 扫描（handler / mutation / 状态字面量）。

**回归测试**：见文末「7. 测试基建」与各条 P0/P1 的测试片段。

### B-P2-6 部署清单与运行需要的文件不一致（跨审计项，仅记录）

**证据：`FILES` 是显式白名单，缺 6 个本轮关键文件。**

`scripts/deploy_server.py:11-28` 的 `FILES` 是显式白名单。本轮涉及的关键文件里
`services/product_standard.py`、`services/pre_save_check.py`、`services/readback_check.py`、
`services/exchange_rate.py`、`services/transform_service.py`、`services/common_box_service.py`
**都不在清单里**。也就是说：本报告 P0-4 里 `product_standard.EXCHANGE_RATE=1.633`
这类改动**改本地不会上线**。`docs/project/rules.md` 已记过同类事故
（`carrier_service.py` 曾漏传）。详细归 F 审计，此处只做 B 的关联提示。

**性质：补丁叠补丁**（`FILES` 显式白名单每漏一个文件就补一次，从不改成「按目录全量同步 +
排除」，于是漏传可以反复发生，且失败是静默的 —— 服务器上还跑着旧文件，没有任何报错）。

**影响**：上一条 P0-4 的任何修复（`product_standard` / `exchange_rate` / `pre_save_check` /
`readback_check`）**改了也不会到生产**，本报告的 P0-1 修复同样依赖 `pre_save_check.py`。
即：就算按本报告全部改完，线上仍会复现同样的 422 —— 这是最容易误判「修复无效」的陷阱。

**根因修复方案**：`FILES` 改成「`server/services/**.py` + `server/core/**.py` +
`server/models/**.py` 全量同步，仅排除 `tests/`、`_meas/`、`__pycache__`」，
并在部署后加一步「远端 import 自检 + 版本比对」（`scripts/deploy_server.py` 已有
`sha256` 基础，扩成「逐个文件核对远端 sha」即可）。

**回归测试**
```python
def test_deploy_manifest_covers_all_runtime_modules():
    """遍历 server/services/*.py 与 server/core/*.py，断言每个文件都在 FILES 里。"""
def test_deploy_verifies_remote_sha_after_upload():
    """部署脚本必须逐文件核对远端 sha256，不一致则非零退出。"""
```

---

## 价格计算位置全清单

「主口径」按 `docs/features/warehouse/price-and-claim-2026-11-25.md` 定义的三种语义：
- **仓库口径**：`price = priceIncludeVat = originPrice(CNY) ÷ rate`（不加价）
- **认领口径**：`price = priceIncludeVat = originPrice × 1.18 ÷ rate`
- **发布口径**：`price_template.display_price()`（全成本反解）

| 文件:行号 | 算什么 | 汇率来源 | 数据类型 | 是否与主口径一致 |
|---|---|---|---|---|
| `carrier_service.py:193-200` | 认领目标价（`quoted_cny = purchase×1.18`，`target = quoted_cny/rate`） | `_claim_site_rate(payload.site)`（`:107-118`）→ cached→live→fallback | Decimal→`str` | ✅ 公式一致，❌ 币种取源站点（B-P0-2） |
| `carrier_service.py:203-205` | 抬到成本线 `floor + 0.01` | 无（用解析值） | Decimal | ⚠️ `floor=0` 时把价写成 0.01（B-P0-3） |
| `carrier_service.py:199-200` | 拿不到汇率时的比例兜底 `current × (quoted_cny/purchase_d)` | 无 | Decimal | ⚠️ 第三条路径，与另两条不同口径 |
| `carrier_service.py:212-213` | 写入 `sku["price"]/['priceIncludeVat']` | — | `str` | — |
| `collect_box_service.py:1420-1423` | 认领价 `purchase×1.18÷rate`（**第 2 份实现**） | `exchange_rate.cached/live/fallback`，币种取**目标店**站点（`:1381-1386`，但异常时退 MYR） | Decimal→`str` | ✅ 公式与币种都对（这才是正确版本），❌ 异常兜底退 MYR |
| `collect_box_service.py:1431-1432` | 写入 `sku["price"]/['priceIncludeVat']` | — | `str` | — |
| `warehouse_service.py:2595-2601` | 仓库价汇率选择 | `cached→live→fallback`，币种取 `payload.site`（**缺 site 时 MYR，不看 account.site**） | Decimal | ❌ 币种取源站点且无账号兜底（实测 origin 17 无 site → 10.46 MYR，PH 店应为 162.68） |
| `warehouse_service.py:2624-2628` | 仓库价 `originPrice ÷ rate`（不加价） | 同上 | Decimal→`str` | ✅ 公式一致（这就是仓库口径的定义实现），❌ 币种 |
| `warehouse_service.py:2631-2634` | 写 `_warehousePriceRate` 记账键 | — | dict | ❌ 会被当价格字段并推给上游（B-P0-1 / B-P2-3） |
| `product_service.py:373-374` | `template=None` 时直接原样返回 | — | — | ⚠️ 让 `exchange_rate` 入参变死参数 |
| `price_template.py:530-543` | `display_price` 入口（运费/尾程/各项费率） | `rate` 由调用方给 | Decimal | ✅ 发布口径唯一实现 |
| `price_template.py:790-814` | `apply_template` 逐 SKU 重算并写入 | `resolve_rate_with_source`（`:387-441`）override→cached→live→fallback；币种 `_resolve_currency`（`:444-478`）site > freightTable > template.site > 默认 | Decimal→**`float`** | ⚠️ 公式一致，类型不一致（其它处都是 `str`） |
| `price_template.py:17` | `FALLBACK_EXCHANGE_RATE = 1.6393` | 常量 | Decimal | ❌ 第 2 个汇率常量 |
| `price_template.py:19-25` | `DEFAULT_FREIGHT_TABLE.currency = "MYR"` | 常量 | — | ⚠️ 旧模板遗留，`_resolve_currency` 已降级为第 2 优先级 |
| `product_standard.py:214-221` | 校验「售价是否低于标准」`origin/1.633 + 2.00` | `EXCHANGE_RATE = 1.633`（`:26`） | Decimal | ❌ 第 3 个汇率源、写死 MYR 加价、无站点概念 |
| `product_standard.py:520-531` | 修复「价格偏低」：**只写 `priceIncludeVat`** | 同上 | Decimal→`str` | ❌ 更严重：造成 `price ≠ priceIncludeVat` |
| `product_standard.py:25` | `PRICE_BUMP_MYR = 2.00` | 常量 | Decimal | ❌ 写死马币 |
| `product_standard.py:27-28` | `RATE_MIN=1.2 / RATE_MAX=2.2` | 常量 | Decimal | ❌ 只对 MYR 成立的区间 |
| `transform_service.py:108-132` | 批量改价（倍数/固定价），并按原比例折算含税价 | 不涉汇率 | `float` | ⚠️ 第 5 种类型；`_money()` 用 `float(quantize())` |
| `common_box_service.py:127-129` | `price` / `priceIncludeVat` 互相补 0 | 不涉汇率 | `str` | ⚠️ 缺值时把价补成 `"0"`，无告警 |
| `api/warehouse.py:1024` | `price-preview` 试算（调 `apply_template`） | 同上 override 可生效 | — | ✅ 与发布口径同源 |
| `api/warehouse.py:445-451` | 预览响应里 `"sellingCurrency": "MYR"` **写死** | — | — | ❌ PH/TH/VN/SG 模板也报 MYR |
| `exchange_rate.py:40-47` | `FALLBACK_RATES`（MYR/PHP/THB/VND/SGD） | 兜底常量 | Decimal | ⚠️ PHP 与实盘差 17.7% |
| `exchange_rate.py:88-131` | `live_rate`：`rate = 1.0/per_cny`（代码一律除） | open.er-api → exchangerate-api | Decimal | ✅ 唯一正确的活汇率源 |
| `exchange_rate.py:134-143` | `cached_rate`（TTL 3600s） | 进程内 `_cache`（无锁） | Decimal | ⚠️ 无锁（B-P1-6） |
| `exchange_rate.py:145-148` | `fallback_rate` | `FALLBACK_RATES` | Decimal | — |

**结论**：`sku["price"]` 的写入点共 **6 处**（`carrier_service:212`、`collect_box_service:1431`、
`warehouse_service:2627`、`price_template:813`、`transform_service:130`、
`product_standard:531` 只写含税价），汇率常量 **3 套**，数据类型 **3 种**
（`str` / `float` / `Decimal`），币种决定方式 **4 种**（payload.site / 目标店 site /
template site / 写死 MYR）。「同一个概念在多处各算一遍」这个判断成立，且是 P0 级。

---

## 建议的回归测试

按「先钉住已经破的不变量，再谈重构」排序。每一条都对应上面一个 P0/P1，
且**必须是跑真实函数的集成测试**（不是 stub 掉服务层）。

### 1. 价格口径（覆盖 B-P0-1 / P0-2 / P0-3 / P0-4）
```python
# tests/test_pricing_contract.py（新建）
def test_warehouse_save_end_to_end_does_not_422_on_price_rewrite():
    """真实 save_from_collect_box(enforce_pre_save=True)，FakeClient 记录 save_shop 收到的 payload。
    断言：不抛 422；上游收到的 price == originPrice/rate；
    上游 payload 不含 _warehousePriceRate / _costFloorAdjustments。"""

def test_pre_save_gate_allows_purchase_price_rewrite_but_blocks_other_price_edits():
    """三组输入：受约束改写（应放行）/ 手工改价（应拦）/ 只加记账键（应放行）。"""

@pytest.mark.parametrize("source_site,target_site,origin,upstream_floor,expected", [
    ("PH", "MY", "23", "38.40MYR", "38.41"),
    ("MY", "PH", "34", "225.50PHP", "383.92"),
    ("MY", "TH", "100", "1280.00THB", "1280.01"),
    ("MY", "MY", "5.50", "12.00MYR", "12.01"),
])
def test_cost_floor_raise_uses_target_shop_currency(...):
    """现在 PH→MY 得 259.71、MY→PH 得 None。"""

@pytest.mark.parametrize("message,floor", [
    ("...基础成本 38.40MYR", "38.40"),
    ("...基础成本(...)：1,280.00THB", "1280.00"),
    ("...基础成本(...)：225.50", "225.50"),      # 无币种
    ("...基础成本(...)：0PHP", None),            # 0 必须拒绝
])
def test_cost_floor_value_parsing_is_tolerant_and_rejects_zero(...): ...

def test_sku_price_is_written_only_by_services_pricing():
    """AST 扫描：除 services/pricing.py 外任何模块不得出现 sku["price"] = 赋值。
    这条是防回归的硬约束，比逐个 assert 数值更有效。"""

def test_user_exchange_rate_override_affects_warehouse_price():
    """exchangeRate=2.0 时，仓库价必须是 origin/2.0，现在被静默丢弃。"""

def test_product_standard_repair_keeps_price_and_include_vat_consistent():
    """PH/TH 店 payload 经 repair 后 price == priceIncludeVat，且等于该站点币种口径。"""
```

### 2. 乐观锁与草稿版本（覆盖 B-P0-5）
```python
# tests/test_product_mutation_invariants.py（新建）
@pytest.mark.parametrize("mutator", [batch_category, delete, apply_transformed_payload,
                                     admit_automation_results, _record_carrier_claim,
                                     claim, release, publish_claim_branch])
def test_every_mutation_either_bumps_and_syncs_or_invalidates(mutator):
    """造 automation_result=True, draft_verified_revision==revision 的商品。
    执行后断言：revision 必增；草稿内容变了的必须 draft_verified_revision is None；
    草稿内容没变的必须 == revision。"""

def test_admit_automation_results_invalidates_draft_verification_after_image_promote():
    """图片从 auto-temp promote 到 products 后，draft_verified_revision 必须失效。"""

def test_static_scan_all_product_mutations_are_guarded():
    """AST：任何给 product.<field>/product.payload 赋值的函数体内，
    必须出现 touch_product / revision_guard / bump_revision 之一。"""
```

### 3. 状态机（覆盖 B-P1-2 / P1-3 / P1-4）
```python
# tests/test_publish_state_machine.py（新建）
def test_definitively_rejected_publish_keeps_fail_status_and_reason():
    """上游明确 4xx 拒绝后，日志 status 必须是 fail + finished_at，reason 保留原文。"""

def test_republish_does_not_erase_previous_success_record():
    """同件同店再发一次，上一条 success 必须仍可查到（现在被就地覆写成 processing）。"""

def test_publish_batch_status_is_always_a_legal_batch_status():
    """_record_publish_batch 写进 Batch.status 的值必须在 BATCH_STATUSES 里
    （现在写 'processing'，不在六态机里 → 永不终态、不可删）。"""

def test_publish_batch_record_is_updated_after_ledger_reaches_terminal():
    """台账 success 后再记录一次，batch.status 必须从 running 变 done（现在早退，永不更新）。"""

def test_every_written_publish_status_is_filterable_and_renderable():
    """扫全仓：所有 row.status = "..." 的字面量必须在 PUBLISH_STATUSES 里；
    前端 STATUS_OPTIONS 与 PUBLISH_STATUS_META 的键集合必须相等。"""

def test_unknown_status_eventually_rechecked_or_flagged():
    """标 unknown 的记录必须仍可回源（不能被 release 之后彻底遗忘）。"""
```

### 4. 幂等与并发（覆盖 B-P1-1 / P1-6 / P1-7）
```python
# tests/test_idempotency.py（新建）
def test_already_published_shop_is_blocked_or_explicitly_allowed():
    """同一成品发同一店铺两次：要么第二次 409 ALREADY_PUBLISHED，
    要么文档化告警 + 用例断言当前行为。现在两道防护都是死的。"""

def test_claim_path_persists_ledger_before_upstream_write():
    """模拟 audit guard 回滚/进程退出：台账里必须已有 processing 行，
    重试时才能据此判断。现在 claim 路径只 flush 不 commit。"""

def test_publish_retry_does_not_create_duplicate_carriers():
    """第一次 collect_and_fill_carriers 已写入但返回 saved=[]：
    重试不得再次 collect_carriers（会产生第二批载体）。"""

def test_schedule_run_due_overlapping_scans_do_not_double_publish():
    """并发调用两次 run_due(same now)：同一 task 只能执行一次。
    现在 _running_tasks 不参与互斥，两条线程会各发一遍。"""
```

### 5. 全局状态与调度（覆盖 B-P0-6 / B-P1-6）
```python
# tests/test_schedule_summary.py（新建）
def test_multi_shop_summary_counts_items_once_not_per_shop():
    """2 店 × 10 件，A 店全成功、B 店 selected=0 → status 必须 ok、failed 必须 0。
    （现在得到 partial + failed=10）"""
def test_partial_run_records_today_as_run_and_does_not_burn_attempts():
    """partial 后 last_run_date 必须是今天（现在为空 → 当天反复触发）。"""
def test_completed_runs_advances_when_all_shops_succeed():
    """勾选式多店任务全成功 → completed_runs +1（否则下一轮重发同一批）。"""
def test_explicit_selection_never_republishes_same_shop_without_confirmation():
    """勾选式任务第二轮不得再选中第一轮已 success 的同店商品。"""

# tests/test_global_state_thread_safety.py（新建）
def test_exchange_rate_cache_concurrent_access():
    """20 线程并发 live_rate/cached_rate/fallback_rate，无异常、无脏桶。"""

def test_ttl_caches_are_single_instance_under_concurrency():
    """_cache()/_template_cache() 并发首次调用只创建一个实例。"""
```

### 6. 关键词启发式（覆盖 B-P1-5）
```python
@pytest.mark.parametrize("message,expected", [
    ("该商品已在采集箱中", True),
    ("该商品已被删除，记录不存在", False),
    ("商品已下架，店铺记录不存在", False),
    ("已达店铺上限，商品不存在", False),
    ("Item already exists in another shop", False),
])
def test_is_already_claimed_does_not_treat_failures_as_success(...):
    """建议直接改成结构化判据（错误码/关键字白名单），不再用子串。"""
```

### 7. 测试基建（覆盖 B-P2-5，先修这个才能跑上面的）
```python
# tests/conftest.py
def fill_carriers_stub(*, strict_signature=True):
    """所有 stub 用 **kwargs 接收，并在测试开头断言
    inspect.signature(carrier_service.fill_carriers) 与 stub 兼容；
    签名变更时这条断言先红，而不是让 38 个用例一起 TypeError。"""
```
另外建议在 CI 里加：
- `pytest tests -k "pricing or state_machine or idempotency"` 作为**必绿**子集；
- `python -X utf8 scripts/check_version.py`；
- 一条 AST 检查（本报告 B-P2-1/B-P0-5 的两个 `scan_*` 函数就是原型）。

---

## 附：本轮实证脚本清单

| 断言 | 复现方式 |
|---|---|
| 入仓必 422（真实 `save_from_collect_box`） | `docs/audit/_verify_b_service.py::repro_warehouse_save_blocked` |
| 只加记账键即被判「价格变了」 | `::repro_rule9_caused_by_metadata_key` |
| 读回比对报 `['prices']` | `::repro_readback_prices` |
| 成本线跨站点算错（259.71 / None） | `::repro_cost_floor_cross_site` |
| 正则四种写法 | `::repro_cost_floor_regex` |
| 报价 4 套实现差异 / 兜底汇率偏差 | `::repro_pricing_divergence` |
| `product_standard` 把 MYR 写进 PHP | `::repro_product_standard_cross_currency` |
| `exchangeRate` 被丢弃 | `::repro_exchange_rate_ignored` |
| 记录键漏给上游 | `::repro_local_only_leak` |
| `_is_already_claimed` 7 例 | `::repro_already_claimed` |
| handler / mutation 静态扫描 | `::scan_handlers` / `::scan_mutations` |
| 重发把 success 台账覆写成 processing | `::repro_ledger_overwrite` |
| 发布批次写入非法状态 `processing` | `::repro_batch_status` |
| 多店摘要分母虚高 → partial → 重发同一批 | `::repro_multishop_summary` |
| `_store` docstring 与 `success=` 集合不一致 | `::repro_partial_vs_failed` |
| 服务层测试红灯 | `python -m pytest tests/test_student_publish.py tests/test_claim_price_currency.py -q`（85 项 / 44 failed） |

一条命令重放全部：
```bash
cd server && python -X utf8 ../docs/audit/_verify_b_service.py
```
