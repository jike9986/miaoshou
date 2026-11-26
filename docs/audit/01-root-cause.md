# 根因分析：为什么这套代码的 bug 越修越多

> 这不是又一份问题清单。清单在 [00-summary.md](00-summary.md)（124 条）。
> 这份回答的是另一个问题：**为什么修了这么多轮，问题反而在长？**
>
> 审计基准 `4c9f32d`（2026-11-26，0.1.54）。所有结论都有可复跑证据。

---

## 一、先给结论

审计出 124 条问题，按性质分类：根因 38 / **补丁叠补丁 52（42%）** / 跷跷板 26（21%）/ 文档债 8。

**42% 是补丁叠补丁，这个比例不正常。** 它说明历史修法有一个稳定的坏模式，而不是偶发失误。

把这个模式拆开，是三个可以命名的机制：

| # | 机制 | 一句话 | 可量化证据 |
|---|---|---|---|
| 1 | **手工白名单** | 用枚举表达「哪些算 X」，漏了就再枚举一份 | 8 处独立实例，横跨前后端（见 §二） |
| 2 | **修了机制，没接通路** | 加了参数/校验/隔离，但调用方没传 → 空转 | `account_id` 23 个调用点 0 个传（见 §三） |
| 3 | **闸门不在必经之路上** | 校验写对了，但发布流程可以绕过它 | 4 处（见 §四） |

这三条比任何单条 P0 都更值得先修 —— **单条 P0 修完就没了，这三条不修，下一轮还会长出新 P0。**

---

## 二、机制 1：手工白名单 —— 每一份枚举都是一次未来的漏

### 同一个「价格字段」概念，三套判定

| 位置 | 写法 | 结果 |
|---|---|---|
| `services/pre_save_check.py:120` | `return "price" in str(key).casefold()` | **子串匹配** |
| `services/readback_check.py:26` | `_PRICE_FIELDS = ("price", "priceIncludeVat", "shopPrice", "salePrice")` | 显式白名单（**正确写法**） |
| `services/warehouse_service.py:4138` | `_LOCAL_ONLY_PAYLOAD_FIELDS = (...)` | 手工枚举本地字段 |

**这三处都在回答「这个 key 是不是价格字段」，给了两个不同答案。**

### 后果实证：入仓 100% 被自己拦死（B-P0-1）

```
_is_price_field 判定：
  price               → True     ← 正确
  originPrice         → True     ← 正确
  _warehousePriceRate → True     ← 错！这是纯本地记账键
  skuMap              → False

只加记账键 _warehousePriceRate、价格数字一个字没改：
  prices_unchanged = False       ← 被判成「价格字段变了」
```

完整链条：

```python
# warehouse_service.py:2802  「仓库不套模板」（负责人 2026-11-25 决策）
template = None

# warehouse_service.py:2940    每次入仓都写记账键
if template is None:
    payload = _price_warehouse_at_purchase(payload)   # 写入 _warehousePriceRate

# warehouse_service.py:3155    → pricing_enabled 恒 False
pricing_enabled=template is not None,

# pre_save_check.py:346-348    → 必然报错
if not prices_unchanged(original, payload):
    if not pricing_enabled:
        issues.append("one or more price fields changed")   # 422
```

**这是跷跷板的教科书案例**：11-25 的「仓库不套模板」和 11-09 的「关定价就不许改价」都是正确需求，但中间夹着一个把本地元数据误判成业务字段的子串匹配。**两个正确需求 + 一个错误的判定方式 = 核心功能完全不可用。**

而 `_LOCAL_ONLY_PAYLOAD_FIELDS`（本该排除本地字段的那份枚举）漏了 `_warehousePriceRate` —— 所以这个键既没被排除在「价格字段」之外，也没被排除在「发往上游」之外。

### 全部 8 处实例

| 手工列表 | 位置 | 漏了什么 | 后果 |
|---|---|---|---|
| `_is_price_field` 子串匹配 | `pre_save_check.py:120` | 把 `_warehousePriceRate` 判成业务字段 | 入仓 100% 422 |
| `_LOCAL_ONLY_PAYLOAD_FIELDS` | `warehouse_service.py:4138` | 漏 `_warehousePriceRate` | 本地键发给上游 |
| `_PRICE_FIELDS` | `readback_check.py:26` | （本身正确，但与上面两套口径不一致） | 同一概念三个答案 |
| `FILES` 部署清单 | `deploy_server.py:11-37` | 158 个文件只列 21 个 | 新增文件默认不部署 |
| `ALLOWED_HOSTS` | `updateDownload.ts:10` | 5 个真实主机名只列 2 个 | 校验盲区 |
| `API_ERROR_TEXT` | `api.ts:140` | 37 个错误码只覆盖 11 个 | 27 个错误无文案 |
| 9 个 `_ensure_sqlite_*` | `db/session.py` | 漏第 4 个（`short_id`） | 老库唯一约束永久缺失 |
| `DYNAMIC_WRITE_ROUTES` | `test_role_permissions.py` | 漏 `system/templates` | 结构性守护变红 |

### 修复方向：不是补全列表，是消除列表

```python
# ❌ 现在：枚举，漏一个就再补一个
_LOCAL_ONLY_PAYLOAD_FIELDS = ("colorMap", "sizeMap", "_sourceWeight", ...)

# ✅ 改法：用前缀做约定，不需要枚举
def _is_local_only(key: str) -> bool:
    return key.startswith("_")          # _sourceWeight / _warehousePriceRate / 以后新增的全都自动覆盖
```

```python
# ❌ 现在：子串匹配，任何含 price 的键都中招
def _is_price_field(key): return "price" in str(key).casefold()

# ✅ 改法：唯一权威，从上游协议推导
from services.readback_check import _PRICE_FIELDS   # 已有正确实现，直接复用
def _is_price_field(key): return key in _PRICE_FIELDS
```

### 我已经验证过这个修法能成立（而不是只提出方案）

`docs/audit/_verify_b_p0_1_fix.py` 实测结果：

```
基准：originPrice=23 CNY, rate=1.6255 → 期望本币价 14.15

场景                                     期望     实际    判定
正常入仓改写（价格=origin/rate）            True    True    ✓
价格被改高                               False   False   ✓
价格被改低                               False   False   ✓
originPrice 被篡改                       False   False   ✓
SKU 被删除（整张 skuMap 清空）             False   False   ✓

对照（修复前）：正常入仓改写 → prices_unchanged = False → 422
```

**关键是这张表证明了「不是靠放松校验通过的」**：放行的是「价格 = 拿货价 ÷ 汇率」这一种确定的改写，改高、改低、改拿货价、删 SKU 全部仍然被拦住。

#### 这个过程本身值得记录：我自己写错了两次，都是脚本抓出来的

第一次：只遍历 `original` 的 SKU 去比价。**删掉整张 skuMap 时循环体一次都不执行 → 返回 True → 放行。** 典型的「只为『值变了』设防，忘了『东西没了』」。

第二次：改成三态判定后，`False`（确定不一致）仍会回落到原始校验，而原始校验对空 skuMap 返回 True → **一个明确的「不一致」被另一个更弱的校验推翻**。

两次都是同一种思维惯性：**默认「放行」是安全的缺省值**。而在这条链路上，缺省值必须是「拒绝」——价格校验漏放比误拦的代价大得多。

> 如果我只写方案不写验证脚本，这两个洞会直接进生产。**审计报告里的每一个「修复方案」，都应该是跑过的，不是想出来的。**

---

## 三、机制 2：修了机制，没接通路

### 实例：账号隔离加了参数，23 个调用点一个都没传（C-P0-6）

`services/warehouse_service.py:653`：

```python
def resolve_product(db: Session, ident: Any, *, account_id: int | None = None) -> Product:
    """...
    A legacy ``detailId:shopId`` key can now occur in multiple Miaoshou accounts,
    so callers with account context should pass ``account_id``.
    """
```

注释写得很清楚：「有账号上下文的调用方**应该**传 account_id」。

实测：

```
resolve_product 调用点（排除测试）：23 处
其中显式传 account_id：0 处
唯一命中 account_id 的那行：就是函数定义本身
```

**`account_id` 在整个仓库的其它地方被显式传了 381 次**，唯独这个函数没人传。所以这不是「项目惯例如此」，是一次孤立遗漏。

### 后果：跨租户发布

`services/publish_service.py` 两条分支都不校验商品归属：

```python
# :536  仓库 id 分支
product = warehouse_service.resolve_product(db, warehouse_id)   # 没传 account_id

# :560-562  detailId 分支：裸 ORM，连归属字段都没碰
product = (db.query(Product)
           .filter(Product.warehouse_key == key, Product.deleted_at.is_(None))
           .one_or_none())
```

学员 A 可以把自己的私有商品挂到学员 B 的账号/店铺下发布。

### 时间线证明这是「没接通」而非「没做」

```
3c2028f  2026-11-03  "fix account isolation and schedule ownership"
         改了 14 个文件，为 resolve_product 加了 account_id 形参
         改了 batch_service / collect_box_service / schedule_service / warehouse_service
         ★ 没有改 publish_service.py（grep 命中 0）
```

**隔离机制在发布路径上从第一天起就是空转的。** 测试全绿，因为 `test_account_ownership.py` 不在 CI 范围里（见 §四）。

### 这一类还有多少？

同类形状（加参数/加字段/加开关，但消费方没接）：

- `pending.json` 里写了 `sha256` 字段，**无任何消费者**（E-P0-2）
- `warehouse_service` 的 `exchange_rate` 参数在仓库路径被静默丢弃（B-P0-4）
- `_already_published_blocker`（`schedule_service.py:154`）**零调用**，`:1303` 只剩 `pass  # 不再阻止重复发布`
- `parse_snippet_target()` 写好了、测试覆盖了、**生产代码零调用**（F）

### 修复方向：让「没接通」变成编译/测试错误

```python
# ❌ 可选参数 = 可以永远不传 = 可以永远空转
def resolve_product(db, ident, *, account_id: int | None = None) -> Product: ...

# ✅ 必传 + 显式放行
def resolve_product(db, ident, *, account_id: int | None) -> Product:
    """account_id 必须传。确实不需要账号上下文的调用方传 None，
    但必须在调用点写出来 —— 让「我没考虑隔离」变成可见的代码。"""
```

配套测试：**断言「所有调用 resolve_product 的地方都显式传了 account_id」**（用 AST 扫，不是 grep）。这类结构性断言一旦有了，就不需要每轮审计重新发现一遍。

---

## 四、机制 3：闸门不在必经之路上

审计发现 4 处「校验写对了，但可以被绕过」：

| 闸门 | 位置 | 为什么绕得过去 |
|---|---|---|
| 逐列漂移检查 | `migrations/_verify_baseline.py` | **能精确检出漂移（exit 1）**，但 docstring 自己写「不需要进 CI」 |
| 鉴权矩阵测试 | `tests/test_role_permissions.py` | 不在 `-m unit` 集合里，CI 不跑 |
| 多租户隔离测试 | `test_account_ownership.py` 等 3 个 | 同上 |
| 发布校验 | `verify_release.py` | `build.ps1` 全流程不调用它，靠人记得跑 |

### 最刺眼的一条：CI 只跑了 6% 的测试

```
全量测试    1243 项
CI 实际跑     78 项（-m unit，6 个文件）
被静默排除  1165 项
```

而 CI 跑的 78 项里还有 4 项是红的（运费表旧值）→ **CI 早就是红的，只是没人看。**

### 96 个失败的真相

全量 96 个失败，逐个归类后：

| 类别 | 数量 | 是 bug 吗 |
|---|---:|---|
| 测试桩没跟上生产签名 | 49 | ❌ 测试债 |
| fixture 从未存在 | 20 | ❌ 环境缺失 |
| **测试对、生产错** | **1** | ✅ **真 bug** |
| 待人工判定 | 26 | ？ |

**只有 1 个是真 bug。** 但这个分布本身就是灾难：**96 个红灯里 95 个是噪音，没人会再从里面找那 1 个真的。** 测试套件已经失去了「红灯 = 有 bug」的信号价值。

而那 1 个真的，恰好证明了信号价值的重要性：

```
tests/test_student_publish.py:1120   assert verify_readback is True, "发布路径必须开启读回核验"
services/publish_service.py:1547     verify_readback=False
```

`ff5178f`（11-07）建立契约并写断言 → `fd826fa`（11-19）改成 False，**commit message 里一个字都没提**。行为改了、测试没改、决策没写进 commit、没进 rules.md。这是**静默的行为回退**，只能靠测试红灯发现 —— 而红灯早就没人看了。

---

## 五、已验证可行的修法（不是提案，是跑过的）

如果按「P0 严重度」排序，会先修 `short_id`、`shared_mode`、`version` 穿越这些。但它们修完，机制还在，下一轮照样长。

### 已验证可行的修法（不是提案，是跑过的）

每个根因类 P0 的修法都写成了可复跑脚本，**且都验证了「不误伤正常业务」**：

| 问题 | 验证脚本 | 修法验证结果 |
|---|---|---|
| A-P0-1 `short_id` 无约束 | `_verify_a_shortid_fix.py` | ✅ 三件套缺一不可：迁移 + 补偿函数 + **先查重再建索引**。关键细节：必须用部分唯一索引 `WHERE short_id IS NOT NULL`，否则历史 NULL 记录会互相冲突 |
| B-P0-1 入仓 422 | `_verify_b_p0_1_fix.py` | ✅ 放行合法改写，改价/改拿货价/删 SKU 全部拦住（见第二节那张表） |
| C-P0-1 `shared_mode` 双源 | `_verify_c_p0_1_fix.py` | ✅ 三个鉴权函数**都拿得到 db**，改 2 行即可，不必动 40+ 个端点 |
| C-P0-2 分组闸门零接入 | `_verify_c_p0_2_fix.py` | ✅ `guard_group` 角色矩阵**本来就对**（member 拦、teacher/admin 放行、普通组不误伤），只需接线 |
| E-P0-1 version 路径穿越 | `_verify_e_p0_1_fix.mjs` | ✅ 正向白名单拦住 8 种攻击，5 个真实版本号全放行 |

#### 验证过程中发现的、审计报告里没有的三个新风险

这是「验证修法」比「提修法」多出来的价值：

**① `short_id` 用普通唯一索引会让升级崩溃**
历史记录（没生成过 short_id 的老账号）的 `short_id` 是 NULL。SQLite 的普通唯一索引允许多个 NULL，但**如果实现时加了 `NOT NULL` 或用了错的谓词，老库升级会直接 IntegrityError**。必须用 `WHERE short_id IS NOT NULL AND trim(short_id) <> ''` 的部分索引。

**② `guard_group(None)` 会被拦住 → 会打死定时任务**
```
=== operator 为 None（内部调用/定时任务）时的行为 ===
  None → 拦住 (FORBIDDEN)
```
定时任务、后台线程、认领任务这些内部路径很可能不传 operator。直接接线会让它们全部 403。**接线前必须确认那 4 个调用点的 operator 真的有值。**

**③ `RESTRICTED_GROUP_IDS = {1,2,3}` 的 id 兜底会误判新库**
```
新插入的普通组 id=1 → is_restricted_group = True（误判成精品组！）
                         id=2 → True
                         id=3 → True
                         id=4 → False（正常）
```
生产库没问题（1/2/3 确实是精品组），但**新部署从头建库时，前三个普通组会被当成受限组，学员全部看不到**。我在写验证脚本时自己就踩到了这个坑（造的「普通款」拿到 id=1 被拦）。

> 这三条都说明：**审计发现的「问题」和「修法」之间还有一段路。** 不跑验证就接线，等于用新补丁盖旧补丁 —— 正是这个项目 42% 补丁叠补丁的成因。

---

## 六、修复顺序（按依赖关系排，不按严重度）

**建议的顺序：**

### 第 0 步（半天）：让红灯重新有信号价值
1. `ruff --fix`（24 个可自动修）+ 手工修剩余 16 个
2. 4 个运费测试对齐官方运价 `0.06/0.08`
3. 20 个 `probe_runner` 缺失的测试：补 fixture 或明确 `@pytest.mark.skip(reason=...)`
4. 38 个 `fill_carriers` 桩补 `category_trees` 参数；7 个 lambda 补 `operator_id`
5. `verify_readback` 那条：**确认 11-18 口径是否仍有效**，然后让测试和代码一致（不是改测试让它过）

> 做完这一步，96 → 0。**不做这一步，后面每一步修复都无法验证是否真的修好了。**

### 第 1 步（一天）：把已有的闸门搬上必经之路
6. `_verify_baseline.py` 进 CI，替换 `ci_migration_check.py`
7. CI 的 `-m unit` 改成全量（或至少加入 3 个鉴权/多租户测试文件）
8. `build.ps1` 末尾强制调用 `verify_release.py`
9. 加结构性断言：AST 扫「所有 `resolve_product` 调用都显式传 `account_id`」

### 第 2 步（一周）：修根因类 P0
10. `short_id` 补迁移 + 第 4 个补偿函数（A-P0-1，生产起不来级）
11. `shared_mode` 统一到 `share_service.shared_mode(db)`（C-P0-1，人人管理员级）
12. `manifest.version` 路径校验（E-P0-1，开机自启级）
13. `resolve_product` 的 `account_id` 改必传（C-P0-6，跨租户级）

### 第 3 步（两周）：消除手工白名单
14. `_is_price_field` 复用 `readback_check._PRICE_FIELDS`，并写对照表证明没放松校验
15. `_LOCAL_ONLY_PAYLOAD_FIELDS` 改成 `key.startswith("_")`
16. 部署清单改成 `rglob` 推导
17. 前端错误文案从 `errors.py` 枚举生成
18. 建「补偿函数 ↔ 迁移」对照表 + 同步测试

### 第 4 步（一月）：划定唯一权威
19. 「售价」收拢到单一实现（B-P0-4：现在有 4 套实现 / 3 个汇率源 / 3 种类型）
20. 「币种由目标店决定」全链路传 `target_shop_site`（B-P0-2）
21. 单一事务契约：服务层永不 commit
22. `weight` / `payload.sourceUrl` / `qualityReview.verifiedRevision` 升为真实列（A-P1-3：塞在 JSON 里物理上无法索引）

---

## 七、一句话

> 这套代码的基础设施纪律高于平均水平（`_guarded` 依赖树、`_verify_baseline` 逐列比对、`validateUpdateUrl` 精确匹配、26 个迁移全有真实 downgrade）。
>
> **问题不在「不会写对的代码」，在「写对的东西没被强制使用」** ——
> 校验器写好了不进 CI，隔离参数加了没人传，白名单漏了就再枚举一份。
>
> 所以修的重点不是再写更多正确的代码，是**让已经正确的代码无法被绕过**。

---

## 附：本文所有结论的复跑命令

```bash
# ── 机制 1：手工白名单 ─────────────────────────────────────
cd server && python -X utf8 ../docs/audit/_verify_b_p0_1.py      # 入仓 422 复现
cd server && python -X utf8 ../docs/audit/_verify_b_p0_1_fix.py  # 修法验证（含反例）
grep -n "_is_price_field\|_PRICE_FIELDS\|_LOCAL_ONLY" \
     services/pre_save_check.py services/readback_check.py services/warehouse_service.py

# ── 机制 2：没接通路 ──────────────────────────────────────
grep -rn "resolve_product(" --include=*.py server/ | grep -v tests  # 23 处，0 处传 account_id
git show --stat 3c2028f | grep publish_service                      # 无输出

# ── 机制 3：闸门不在必经之路 ───────────────────────────────
cd server && python -m pytest tests -m unit -q --co | tail -1   # 78 项（全量 1243）
cd server && python migrations/_verify_baseline.py             # exit 1（正确检出，但不在 CI）
cd server && python -m ruff check .                            # 40 errors

# ── 96 个失败归类 ─────────────────────────────────────────
cd server && python -X utf8 ../docs/audit/_triage_tests.py

# ── 各 P0 的复现 + 修法验证（全部在 mkdtemp 临时库，不碰真实数据）──
cd server && python -X utf8 ../docs/audit/_verify_a_shortid.py       # A-P0-1 复现（三路径）
cd server && python -X utf8 ../docs/audit/_verify_a_shortid_fix.py   # A-P0-1 修法
cd server && python -X utf8 ../docs/audit/_verify_c_p0_1.py          # C-P0-1 复现
cd server && python -X utf8 ../docs/audit/_verify_c_p0_1_fix.py      # C-P0-1 修法（含可行性）
cd server && python -X utf8 ../docs/audit/_verify_c_p0_2.py          # C-P0-2 复现（AST）
cd server && python -X utf8 ../docs/audit/_verify_c_p0_2_fix.py      # C-P0-2 修法（含误伤检查）
node docs/audit/_verify_e_p0_1_fix.mjs                               # E-P0-1 修法
cd server && python -X utf8 ../docs/audit/_verify_b_service.py       # B 全部 17 段
cd .. && python -X utf8 docs/audit/_verify_f_deploy.py               # F 生产 vs 本地比对
```

> **修法验证脚本和复现脚本是两类东西。**
> 复现脚本证明「问题真实存在」；修法验证脚本证明「这样改能修好、且不误伤」。
> 后者更重要 —— 它抓出了 3 个审计报告里没有的新风险（见第五节）。
