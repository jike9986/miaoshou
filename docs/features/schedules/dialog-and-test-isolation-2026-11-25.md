# 定时任务弹窗与测试隔离（2026-11-25）

本文记录 2026-11-25 这一轮「新建定时任务」弹窗的全部改动，以及顺带查出的
测试环境污染问题。后续再动这块先读这里。

---

## 一、弹窗最终形态

从「任务名称」往下依次是：

```
任务名称
发布账号          （九宫格，多账号；勾账号自动勾其下店铺）
定价模板          （独占一行；只列当前店铺国家的，可多选）
选择发布商品      （分组栏 + 图片网格 + 认领状态筛选 + 分页）
频率 / 执行时间 / 每次数量
```

**没有「目标店铺」这一项。** 曾经有过下拉框、也有过只读摘要行，
负责人都明确要求去掉 —— 上面账号卡片里已经把每个账号的店铺勾好了，
再放一处能决定「发哪个店」的控件必然和它打架。

---

## 二、四条业务规则（都是负责人口径，不要改回去）

### 1. 勾账号要自动勾其下店铺

负责人 2026-11-23。`toggleAccount(accId, checked)`：
勾上账号 → 拉该账号店铺 → 全部勾上；取消账号 → 删掉它名下的店铺选择。

### 2. 新建任务不预选任何账号、不预选任何模板

负责人 2026-11-24（账号）、2026-11-25（模板，「为什么打开自动勾选了马来模板」）。

- 账号：表单重置时 `setCheckedAccountIds([])`
- 模板：`setPickedTemplates([])`

注意区分：**新建**不预选，**用户主动勾账号**仍然自动勾店铺。这两件事不冲突，
曾经为了满足前者把后者删掉，导致「勾账号下面店铺不动」。

### 3. 模板只列当前店铺国家的，不是置灰

负责人 2026-11-25：「不是置灰而是不能选择懂不懂 / 像上面只有马来和菲律宾，
其他国家的定价模板根本不能选择」。

`templateOptions` 用 **filter** 而不是禁用：

```ts
if (selectedSites.size === 0) return templates;          // 没勾店不收窄
const matched = templates.filter(/* site 命中 */);
return matched.length > 0 ? matched : templates;          // 兜底防空列表
```

勾了 MY + PH 两个店 → 列表里只有「马来」「菲律宾」两个按钮，
泰国/越南/新加坡**根本不渲染**。

`site` 为空的模板（老数据没记站点）视为通用，保留。

### 4. 模板可以多选

负责人 2026-11-25：「怎么只能单选定价模板」。

`pickedTemplates: string[]`。`templateForShop(shop)` 按店铺自己的 site
从勾选的模板里挑，挑不到按 site 自动取，再回落到第一套。
提交时的 `targets: [{shopId, template}]` 后端本来就支持每店各一套。

---

## 三、`selectedSites` 的数据源（踩过两次坑）

正确写法是遍历**所有已勾账号**的店铺：

```ts
for (const accId of checkedAccountIds) {
  const accShops = shopsByAccount[accId] ?? [];
  const picked = selectedShopIdsByAccount[accId] ?? [];
  for (const shop of accShops) if (picked.includes(shop.shopId)) { /* 收 site */ }
}
```

两次踩坑记录：

| 错误写法 | 后果 |
|---|---|
| 只读 `selectedShopIdsByAccount[runAccountId]` | 「目标店铺」下拉选了马来，模板不收窄 |
| 拿 shopId 去 `shopOptions`（当前登录账号的店铺）里查 | 老师勾的是**别的**账号，店铺不在 `shopOptions` 里 → `selectedSites` 恒空 → 模板退回全列，五国全出现 |

`shopOptions` 是**当前登录账号**的店铺；`shopsByAccount` 才是所有已勾账号的。
这两个绝不能混用。

---

## 四、发布时会自动按店铺国家纠正模板

`publish_service.py:1200` → `_template_name_for_site()`。
发布前按目标店铺的 `site` 把模板换成同国家那套再算价。注释原文：

> 拿马来模板去发菲律宾店，算出来的数字套的是 MYR 的税率结构，报价一定不对。

所以界面约束和后端纠正是同一套逻辑，选错也不会算错价。

---

## 五、选择发布商品：分组筛选

负责人 2026-11-25：「选择发布产品这里应该也加上像产品仓库一样的分组可以选择，
学员不能发的三个分组依旧看不到也选不了」。

实现：弹窗新增 `groups` / `groupId` / `onGroupChange` 三个 prop，
`SchedulesPage` 传入 `productsQuery.data.facets.groups`，
查询带 `groupId` 参数。

**权限闭环在服务端，不是前端藏起来：**

```
/warehouse → warehouse_service.facets()
           → group_access.visible_group_ids(db, viewer)
           → 学员: {1,2,3}，老师/管理员: None
           → facets.groups 里根本没有受限三组
```

写入层还有 `group_access.guard_products` 二次拦截，绕过前端直接调 API 会 403。

受限三组（`group_access.RESTRICTED_GROUP_NAMES`）：自营海外仓 / 海外仓定制款 / 精品。
`RESTRICTED_GROUP_IDS` 用 id 1/2/3 兜底，防改名放开权限。

实测验证（`tools/source_match/verify_group_facets.py`）：

```
受限分组 id: [1, 2, 3]

学员: may_access_restricted = False
  visible_group_ids = {1, 2, 3}
  分组栏实际显示: ['普通款']
  ✓ 受限三组被剔除，学员选不到

老师: may_access_restricted = True
  visible_group_ids = None
  分组栏实际显示: ['自营海外仓', '海外仓定制款', '精品', '普通款']
```

---

## 六、测试环境污染（重要）

**原来的测试跑的是桌面端真实数据库：**
`C:\Users\Administrator\AppData\Local\aohuang-desktop\app.db`

后果：

1. 各测试用固定 `app_key`（`claim-job-test`、`price-foreign-key`…），
   第一次跑成功、第二次撞 `UNIQUE constraint failed: accounts.app_key`。
   **之前统计的「84 个失败」绝大多数就是这个，跟业务代码无关。**
2. 测试会读到真实商品/账号/认领数据，结论不可信。
3. 任何带删除逻辑的测试都可能清掉真实数据。

修法在 `server/tests/conftest.py`：在 import 任何 server 模块**之前**
把 `AOHUANG_DATA_DIR` 指向 `tempfile.mkdtemp()`，atexit 清理。
必须在模块顶层设，不能放 fixture —— `Settings.data_dir` 是类定义阶段
求值的默认值，fixture 跑起来时模块早 import 完了。

隔离后失败数 **92**，全部是历史遗留的测试代码过期（桩函数签名不匹配、
`KeyError: 'notes'` 等），与本次改动无关：

| 文件 | 数量 | 原因 |
|---|---|---|
| test_student_publish.py | 44 | `fill_carriers()` 桩不认 `category_trees` 参数 |
| test_parity.py | 20 | ERROR，外部探测依赖 |
| test_bulk_schedule_acceptance.py | 10 | 同类桩过期 |
| test_carrier_copy.py | 5 | `KeyError: 'notes'` |
| test_price_template.py | 4 | 模板字段演进 |
| 其余 | 9 | 分散 |

`test_claim_jobs.py` **9 个全过**。

---

## 七、认领卡死的真根因（2026-11-25）

生产库实测两个认领批次 `running` 卡 19 小时，进度 0/203、0/9，
`batch_items` 里 100+9 件全 `running`，且无法重新认领。

根因：`claim_job_service.recover_interrupted_on_startup()` 第一行

```python
if os.getenv("AOHUANG_CLAIM_RECOVER_SINGLE_WORKER") != "1":
    return 0
```

这个变量**从来没被设置过**（确认 `/etc/aohuang-listing-v2.env` 里没有），
所以这个「重启后收尾残留批次」的函数一次都没执行过。

而 `create()` 里有 `Batch.status.in_(["pending","running"])` 的去重检查，
于是重新认领被永久拦住。

修法两层：

1. 默认就恢复，只有显式设 `0` 才跳过（当前 `--workers 1`）
2. 新增 `reap_stale()`：进程一直活着但 worker 线程自己死了的情况，
   启动恢复管不到；超 `STALE_JOB_SECONDS = 30 分钟` 没进展的批次，
   下次创建时判定失败

已在生产验证：两个批次变 `failed`、`finished_at` 已写，
`running/pending` 的认领批次剩 0 个。

---

## 八、布局规则（血泪）

- 不要对 Tailwind 布局做算术推理，用 `tools/source_match/probe_layout.py`
  / `compare_layout.py`（Playwright + 真实构建 CSS + `getBoundingClientRect`）
  实测。探针 DOM 必须包含**所有祖先 grid**，否则测量无意义。
- `sm:` / `md:` / `lg:` 是**视口**断点，不是容器断点。640px 对话框在
  960px 窗口里按窗口宽度评估断点。
- 子元素要占满外层 grid 的一整行，必须显式写 `sm:col-span-2`。
  这个问题导致模板按钮改了三版都堆成三行。