# 审计 D：前端状态与数据流

> 审计范围：`web/src/`（138 个文件 / 36 349 行）
> 审计方式：只读静态审计，未修改任何源码
> 审计基准：`CLAUDE.md`（含「快速诊断」章节）、`docs/architecture.md` §4.6、`web/src/lib/queries.ts`（2 637 行）、`web/src/lib/queryKeys.ts`
> 审计日期：本次会话

---

## 结论摘要

这份前端在**基础设施层面是罕见的克制**：`queryKey.ts` 有正规的工厂函数、`main.tsx` 明确写了「为什么不加全局 onError」、`BatchPanel` 的定时器清理写得比大多数商业项目都严谨、`apiErrorMessage` 从不吞后端的 `message`。这些不是补丁，是有意识的设计。

但**基础设施的洁癖掩盖了业务层的失控**：

1. **`queries.ts` 是一个 2 637 行的 God File**。它同时是 types 模块（56 个内联 `export interface`/`type`）、HTTP client 封装、103 个 React hook 的集合（28 个 `useQuery` + 75 个 `useMutation`）、以及业务常量的堆放处。`types.ts` 另有 1 640 行，两份类型定义并存且**已经开始互相重复**（`PriceTemplateItem`、`SizeChart`、`SystemVersion`/`VersionPayload`、`WarehouseQuery`/`WarehouseListParams`）。这不是「文件大」，是**职责边界从未被划定**。

2. **缓存失效靠人肉记忆 key 字符串**。工厂函数 `queryKeys.ts` 存在，但全仓 105 处 `invalidateQueries` 里仍有 **11 处硬编码字面量**（`["product-detail", id]`、`["system", "templates"]`、`["images", "usage"]`……），其中 **2 处是彻底的死键**（`["product-detail", id]` / `["productDetail", id]` 从未有任何 query 使用）。没有类型约束能阻止这种漂移。

3. **最严重的单点缺陷是 `useProductDetail` 的 5 分钟 staleTime + 「写操作后不失效自己」的组合**。`usePatchProduct` / `usePatchProductImages` / `useTranslateProductColors` 全部只失效 `queryKeys.warehouse.all`，而详情用的是 `["warehouse", accountId, id, {includePayload}]` —— 前缀能命中，所以这一点侥幸成立；但两处显式写的 `["product-detail", id]` / `["productDetail", id]` 说明**开发者以为详情是另一套 key**，于是真正的补丁打在了空气上，而三份不同拼写的 key 在代码里共存至今。

4. **无虚拟化**。仓库支持每页 1000 件（`PAGE_SIZE_OPTIONS`），后端上限正好 1000（`server/api/warehouse.py:112` `le=1000`）。1000 张 `ProductCard` + 1000 个 `RemoteImage` + `framer-motion` 的 `AnimatePresence` 逐项变体同时挂载，且选中判断是 `selectedIds.includes(product.id)` —— **O(n²)**。

5. **`loadSettings` / `saveSettings` 直接读写 localStorage，绕过 TanStack Query**，导致「界面参数」和「服务端参数」两套状态并存，靠 `useLayoutEffect` 里手写的 800ms 防抖 + `syncedFromServer` ref 双向同步 —— 这是典型的**跷跷板**：改本地怕覆盖服务端，改服务端怕冲掉本地输入。

**P0 共 5 项，P1 共 9 项，P2 共 8 项。** 其中真正会「用户看得见地出错」的是 D-P0-1（详情刷新竞态）、D-P0-3（`assignedSiteRef` 跨账号不重置）、D-P0-4（幽灵按钮）。

---

## P0 问题（数据不一致 / 功能失效）

### D-P0-1 详情页 `useProductDetail` 的 staleTime 5 分钟 + `refetchOnMount: "always"` 自相矛盾，且三套 key 拼写共存导致失效打空

- **证据 1：** key 工厂只定义一种拼写

`web/src/lib/queryKeys.ts:37-38`
```ts
    detail: (accountId: string | null | undefined, id: string, includePayload = false) =>
      ["warehouse", accountId ?? "current", id, { includePayload }] as const,
```

- **证据 2：** 两个 mutation 却按完全不同的两种拼写去失效

`web/src/lib/queries.ts:565-569`
```ts
    onSuccess: (_, { id }) => {
      void client.invalidateQueries({ queryKey: queryKeys.warehouse.all });
      void client.invalidateQueries({ queryKey: ["product-detail", id] });
    },
```

`web/src/lib/queries.ts:625-629`
```ts
    onSuccess: (_data, { id }) => {
      void client.invalidateQueries({ queryKey: queryKeys.warehouse.all });
      void client.invalidateQueries({ queryKey: ["productDetail", id] });
    },
```

- **证据 3：** 全仓搜索确认这两个 key 从来没有对应的 query

```
=== product-detail / productDetail 全仓引用 ===
./lib/queries.ts:567:      void client.invalidateQueries({ queryKey: ["product-detail", id] });
./lib/queries.ts:627:      void client.invalidateQueries({ queryKey: ["productDetail", id] });
```
—— 只有失效，没有生产者。两行代码是纯 no-op。

- **证据 4：** staleTime 与 refetchOnMount 的语义冲突

`web/src/lib/queries.ts:512-523`
```ts
    // 详情页要秒开：打开过的商品直接用内存/磁盘里的上一份，后台静默核对。
    // 实测 payload 只有 8–35KB，缓存代价很低；但每次重进都串一次网络请求，
    // 在跨境链路上有明显等待感。
    //
    // staleTime 5 分钟：这段时间内再打开不再发请求，直接给缓存。
    // gcTime 30 分钟：离开页面后详情还留在内存里，来回切换商品不用重新拉。
    // placeholderData 用 keepPreviousData：切换商品时先显示上一件的骨架，
    // 避免白屏闪烁（数据到达后立刻替换）。
    staleTime: 5 * 60_000,
    gcTime: 30 * 60_000,
    refetchOnMount: "always",
```
注释说「5 分钟内再打开不再发请求」，但 `refetchOnMount: "always"` 的语义是**无视 staleTime，挂载即重取**。两者不能同时成立 —— `always` 赢。注释描述的是代码现在的行为之外的一个不存在的状态。

- **证据 5：** 注释里承诺的 `placeholderData` 根本不存在

同上代码块，注释写「placeholderData 用 keepPreviousData」，但该 hook 的 options 里**没有 `placeholderData` 字段**。切商品时不会显示上一件，会直接走骨架。这是注释与实现的直接矛盾。

- **性质：**补丁叠补丁。原本想解决「跨境请求慢 → 加 staleTime」，后来发现「缓存太旧 → 加 refetchOnMount:always」，两个补丁同时留下且互相抵消；两次拼错的 key 是第三个补丁（「mutation 后刷新详情」）打在空气上留下的疤。

- **影响：**
  1. 详情页的乐观并发基准 `revisionRef.current` 依赖 `product.revision`。`refetchOnMount:"always"` 让每次进入详情都重取，这个是对的；但**同一页面内的 `detail.refetch()` 会重置编辑中的输入**（见 D-P1-2）。
  2. 两行死 key 让维护者误以为「详情有独立缓存」，任何后续按这个思路写的失效逻辑都会继续打空。这是**误导性残留**，比 bug 本身更危险。
  3. 注释与实现三处不一致（staleTime 语义、placeholderData 不存在），下一个接手的人会基于假信息改代码。

- **根因修复方案：**
  1. 删掉 `queries.ts:567` 和 `queries.ts:627` 两行。它们什么都没做。
  2. 详情失效统一走 `queryKeys.warehouse.detail(accountId, id, includePayload)`，但**不要**在 mutation 里硬编码 `includePayload` —— 正确做法是依赖已有的 `queryKeys.warehouse.all` 前缀失效（`["warehouse", accountId, id, {…}]` 的第一个元素是 `"warehouse"`，前缀匹配成立），然后**删除那两行冗余代码**。
  3. 在 `queryKeys.ts` 加一条约束性注释 + 一个运行时断言（dev only）：所有 `invalidateQueries` 的 key 必须能在工厂里找到来源。最简单可行的落地是给 `queryKeys` 加 `Object.freeze` 并配一个 lint 规则禁止 `invalidateQueries({ queryKey: [` 的字面量写法。
  4. 修正注释，或补上 `placeholderData: keepPreviousData` 让注释变成真的。二者选一，不能继续两存。

---

### D-P0-2 `useAutomationResults` / `usePublishLogs` / `useImageJobs` 的 pageSize 挂在 key 尾部，失效时漏掉变体

- **证据 1：** `usePublishLogs` 的 key 形状

`web/src/lib/queries.ts:1703-1715`
```ts
export function usePublishLogs(params: PublishLogParams): UseQueryResult<PublishLogsResponse> {
  const { data: auth } = useAuthStatus(true);
  const accountId = params.accountId ?? auth?.accountId ?? null;
  return useQuery({
    queryKey: [
      ...queryKeys.publishLogs.list({
        accountId,
        page: params.page,
        status: params.status ?? undefined,
        shopId: params.shopId ?? undefined,
      }),
      { pageSize: params.pageSize },
    ],
```
而工厂是 `queryKeys.ts:57-58`：
```ts
    list: (params: { accountId?: string | null; page?: number; status?: string; shopId?: string; scheduleId?: string }) =>
      ["publish-logs", params] as const,
```
实际产出的 key 是 `["publish-logs", {...}, {pageSize: 20}]` —— **第二层对象里没有 pageSize，pageSize 在第三层**。

`queryKeys.publishLogs.all` = `["publish-logs"]`，前缀失效仍然命中，所以这里侥幸正确。

- **证据 2：** `useSchedules` 同一手法，但失效路径不同

`web/src/lib/queries.ts:1760`
```ts
    queryKey: [...queryKeys.schedules.list(params), { page: 1 }],
```
`queryKeys.schedules.list(params)` 已经含 `params`，再挂一个 `{page: 1}` 是冗余的常量尾巴 —— 它不参与任何筛选语义，纯粹把 key 撑成三层。`queryKeys.schedules.all` 前缀失效仍能命中，同样是侥幸。

- **证据 3：** 真正的问题在 `useImageJobs`，它把 `accountId` 塞进了第二层

`web/src/lib/queries.ts:1534-1545`
```ts
    queryKey: [...queryKeys.imageJobs.list({
      accountId,
      productId: params.productId ?? undefined,
      batchId: params.batchId,
      status: params.status,
      page: params.page ?? 1,
    }), { pageSize: params.pageSize ?? 50 }],
```
`queryKeys.imageJobs.all` = `["image-jobs"]`，前缀失效命中。**但 `queryKeys.imageJobs.detail(accountId, id)` 产出的 key 是 `["image-jobs", accountId, id]`，而 `useImageJobs` 的 key 是 `["image-jobs", "list", {...}, {...}]`。**

`web/src/lib/queryKeys.ts:48-54`
```ts
  imageJobs: {
    all: ["image-jobs"] as const,
    detail: (accountId: string | null | undefined, id: string) =>
      ["image-jobs", accountId ?? "current", id] as const,
    list: (params: {...}) =>
      ["image-jobs", "list", params] as const,
  },
```
`detail` 的第二段是 `accountId`，`list` 的第二段是字面量 `"list"`。**如果一个 accountId 恰好等于 `"list"`，两族会撞 key。** 这在现实中不会发生（accountId 是数字串），但它暴露了 key 空间未分区：`detail` 和 `list` 共用第二层但语义不同。

- **证据 4：** `queryKeys.imageJobs.detail` 从未被任何 query 使用

```
=== queryKeys.imageJobs.detail 全仓引用 ===
0
```
`queryKeys.ts:50-51` 定义的 `detail` 是死代码。

- **性质：**根因 —— 「key 工厂」只覆盖了每族的**第一层**，各 hook 自己在后面自由追加尾巴，导致 key 的形状事实上不受工厂控制。工厂给的是**建议**，不是**契约**。

- **影响：**
  1. 当前所有 `queryKeys.X.all` 前缀失效都能命中，功能上没有立刻报错的 bug。
  2. 但**精确失效永远失效不了**。任何想「只刷新当前这页」的优化都会失败，因为 pageSize 在第三层、accountId 埋在第二层的对象里。代码里所有 `exact: true` 失效只有 2 处能工作（`warehousePage.tsx:466` 对 claimJobs，`queries.ts:989` 对 collect-box），其余只能全量。
  3. `imageJobs.detail` 是死定义，`system.health`、`titles.preview`、`sync.priceTemplates` 同样是死定义（全仓 0 引用）。**`queryKeys.ts` 里 4 个工厂函数从未被调用**，但它们的**叙事**（「所有 key 都从这里出」）让审计和 review 反复误判。

- **根因修复方案：**
  1. `queryKeys` 每族的工厂函数必须**接收完整参数**并**返回单一形状**。`params` 对象里就该包含 `pageSize`；`usePublishLogs` / `useImageJobs` / `useSchedules` 里的 `...spread + {extra}` 全部收敛回工厂。
  2. 删除 4 个死工厂（`imageJobs.detail`、`system.health`、`titles.preview`、`sync.priceTemplates`），或补上对应 hook —— 现在它们只提供虚假的安全感。
  3. 给 `list` / `detail` 加固定命名空间段：`["image-jobs", "list", …]` / `["image-jobs", "detail", …]`，不要在第二层放 accountId。

---

### D-P0-3 `useWarehouseList` 的 key 里 `q` 恒为 `null` 但 `WarehouseListParams` 声明为 `string | null`，且 `WarehouseQuery` 与 `WarehouseListParams` 是两份定义

- **证据 1：** `WarehouseQuery`（types.ts）与 `WarehouseListParams`（queries.ts）字段几乎相同

`web/src/lib/types.ts:615-631`
```ts
export interface WarehouseQuery {
  claimAccountId?: string | null;
  page?: number;
  pageSize?: number;
  categoryId?: string | null;
  cid?: string | null;
  groupId?: string | null;
  group?: string | null;
  q?: string | null;
  accountId?: string | null;
  status?: ProductStatus | null;
  qualityStatus?: QualityReviewStatus | null;
  /** 货源筛选：missing＝没有货源链接/已失效，present＝有。仅老师及以上 */
  sourceStatus?: "missing" | "present" | null;
  claimed?: boolean | null;
  sort?: ProductSort;
}
```

`web/src/lib/queries.ts:385-398`
```ts
export interface WarehouseListParams extends PageParams {
  claimAccountId?: string | null;
  categoryId?: string | null;
  cid?: string | null;
  groupId?: string | null;
  q?: string | null;
  accountId?: string | null;
  status?: ProductStatus | null;
  qualityStatus?: QualityReviewStatus | null;
  /** 货源筛选：missing＝没有货源链接/已失效，present＝有。仅老师及以上 */
  sourceStatus?: "missing" | "present" | null;
  claimed?: boolean | null;
  sort?: ProductSort;
}
```

差异：`WarehouseQuery` 有 `group?: string | null`（**从未被使用**）、字段全可选、含 `page/pageSize`；`WarehouseListParams` 靠 `extends PageParams` 拿到 `page/pageSize` 且必填、没有 `group`。

**两份定义的注释完全相同**（`/** 货源筛选：missing＝... */`），说明是复制粘贴产生的，不是独立演化。

- **证据 2：** `q` 在唯一的调用点被硬编码为 `null`

`web/src/features/warehouse/WarehousePage.tsx:163-168`
```tsx
  // 搜索框已按界面精简要求移除；q 不再来自本页，避免旧状态残留影响列表
  const listQuery = useWarehouseList({
    page: query.page,
    pageSize: query.pageSize,
    q: null,
```

但 `useWarehouseStore` 里 `q` / `searchDraft` / `setQ` / `setSearchDraft` 全都还在（`features/stores.ts:83-85, 98-99, 130`），且 `resetOperatorSessionUiState` 还在重置它们（`stores.ts:208-209`）。**一个已死的功能留下了完整的 store 字段、setter、重置逻辑，而在唯一的消费者里被钉死为 null。**

- **性质：**根因（类型层）+ 补丁叠补丁（功能层）。搜索被移除时只改了调用点，没清理 store、没清理类型、没清理 `WarehouseQuery`。

- **影响：**
  1. **两套类型定义在编译期互不校验**。`queryKeys.warehouse.list(query: WarehouseQuery)` 接受 `WarehouseQuery`，`warehouseQueryKey(params: WarehouseListParams)` 接受另一份。任何字段增删都要改两处，改一处不会报错 —— **这正是 CLAUDE.md 快速诊断里「商品在自动化结果里消失」那类漂移的同一发病机制**（后端加字段，前端两份定义各改一半）。
  2. `group` 字段在 `WarehouseQuery` 里但**从无任何代码读取**；`useWarehouseList` 的 `compactQuery` 也没传它。这是纯粹的谎言字段。
  3. 搜索框已移除，但用户从 URL 或旧状态带进来的 `q` 会被静默丢弃，无法通过任何 UI 恢复 —— 如果还有别处依赖 `q`（当前没有），会静默失效。

- **根因修复方案：**
  1. **删掉 `WarehouseQuery`**，`queryKeys.warehouse.list` 改用 `WarehouseListParams`。一份定义。
  2. 如果确实保留 `q`（未来要恢复搜索），把它接回 `useWarehouseList`；如果不保留，删 store 字段 + 类型字段 + 重置逻辑，一次清干净。
  3. 删除 `WarehouseQuery.group`。
  4. **全局做法**：`queries.ts` 里 56 个内联 interface 全部迁出到 `types.ts` 或按域拆分（`types/warehouse.ts`、`types/publish.ts`……）。`queries.ts` 只留 hook。这是本报告 P2-1 的同一根因。

---

### D-P0-4 三个弹窗的开关状态有 setter 但**没有任何 UI 能打开** —— 幽灵功能

- **证据 1：** `transformOpen` 只被写 `false`，从未被写 `true`

`web/src/features/warehouse/WarehousePage.tsx:203`
```tsx
  const [transformOpen, setTransformOpen] = useState(false);
```
全仓 `setTransformOpen` 调用点（仅 1 处）：
```
1187:        open={transformOpen}
1191:        onClose={() => setTransformOpen(false)}
```
→ `<TransformDialog>` 永远不开。

- **证据 2：** `priceOpen` 同理

`WarehousePage.tsx:204`
```tsx
  const [priceOpen, setPriceOpen] = useState(false);
```
```
1215:        open={priceOpen}
1216:        onOpenChange={setPriceOpen}
```
→ `<PricePreviewDialog>` 的 `setPriceOpen(true)` 在任何地方都不存在。**用户永远看不到「价格试算」。**

- **证据 3：** `createBatchOpen` 同理

`WarehousePage.tsx:209` + `1233-1234`，`setCreateBatchOpen` 只在 `onOpenChange` 里出现，没有触发点。

- **证据 4：** 作为对照，`batchOpen` 是正常的

```
202:  const [batchOpen, setBatchOpen] = useState(false);
863:                  onClick={() => setBatchOpen(true)}   ← 有触发按钮
1222:        onOpenChange={setBatchOpen}
```

- **证据 5：** `BatchPanel` 整个组件无任何 import

```
=== features/batch 引用 ===
./features/batch/BatchPanel.tsx:1:// features/batch —— 批次编排面板（POST /batch 系列）
```
全仓只有它自己引用自己。`useBatchStep` / `useBatchControl` / `useBatchRetryFailed` / `useDeleteBatch` / `useBatches` **只有 `BatchPanel` 一个消费者**，因此这 5 个 hook + 833 行 UI + 自动推进的定时器逻辑（写得很好的那段）**全部是死代码**。

`WarehousePage.tsx:615-629` 的 `tab === "batch"` 渲染的是 `ClaimJobsPanel`，不是 `BatchPanel`。

- **性质：**补丁叠补丁。`WarehousePage` 在演进中把「批次」这个 tab 的语义从「batch 编排」换成了「claim-jobs 台账」，旧的 `BatchPanel` 组件被摘掉但没删；同时三个对话框的入口按钮在某次界面精简里被删除，state 和 Dialog 渲染留着。

- **影响：**
  1. **功能对用户不存在**：`POST /warehouse/transform`（批量改价/删图/清标题）、`POST /warehouse/price-preview`（价格试算）、`POST /batch`（批次创建）三个后端接口在前端**没有任何可达路径**。这不是「隐藏的调试入口」，是 Main 分支上的功能缺失。
  2. `TransformDialog.tsx`（13 KB）、`PricePreviewDialog.tsx`（10 KB）、`CreateBatchDialog.tsx`（14 KB）、`BatchPanel.tsx`（36 KB）共 **73 KB / 约 2 000 行**在打包产物里，`tsc` 也要检查，但不产生任何用户价值。
  3. `BatchPanel` 里那段**全仓质量最高的定时器清理逻辑**（注释详尽、`wantRef` / `inFlightRef` / `cancelled` 三重防护、明确引用了 StrictMode 双执行）是死代码。而活着的 `AutomationPage` 的执行循环（2 190 行）反而没有同等级别的防护说明。
  4. 三个 `useState` + Dialog 树每次渲染都参与 reconciliation。

- **根因修复方案：**
  1. **先决策，再动手**：这三个功能是要还是不要？
     - 要 → 在工具栏补回入口按钮（`setTransformOpen(true)` / `setPriceOpen(true)`），`BatchPanel` 挂回 `tab === "batch"` 或独立路由。
     - 不要 → 删除 4 个组件文件 + 3 个 state + `queries.ts` 里 5 个 batch hook + `useTransform` / `usePricePreview`。
  2. **禁止中间状态**。当前这种「state + Dialog 都渲染了，只是没有开启入口」的形态是最坏的：审计时看起来功能在，运行时永远不出现。
  3. 加一条 CI 检查：`useState(false)` 的 setter 若从未以 `true` 调用，报 warning。这条规则能一次性抓出本类全部幽灵功能。

---

### D-P0-5 `AutomationPage` 的 `assignedSiteRef` 在切换妙手账号后不重置 —— 自动分配只跑一次是「整个软件生命周期一次」

- **证据 1：** ref 不随账号重置

`web/src/features/automation/AutomationPage.tsx:211`
```tsx
  const assignedSiteRef = useRef(false);
```

- **证据 2：** 触发后永久置位

`AutomationPage.tsx:214-244`
```tsx
  useEffect(() => {
    if (assignedSiteRef.current) return;
    if (!connected || !currentShopId) return;
    // 等列表真正加载过一轮再判断，否则 items 是空的会误判「没有 site 商品」
    if (listQuery.isLoading || !listQuery.isFetchedAfterMount) return;
    if (!listQuery.data) return;
    if (items.length === 0 && total === 0) return;

    const hasSiteItems = items.some((item) => item.editModel === "site");
    if (!hasSiteItems) {
      assignedSiteRef.current = true;
      return;
    }

    assignedSiteRef.current = true;
    assignSiteItems
      .mutateAsync({ targetShopId: currentShopId, accountId: status?.accountId ?? null })
      ...
```

注意：**即使没有 site 商品，也立刻置 `true` 并永久放弃**。注释（第 205-206 行）说「每次启动只跑**一次**（assignedRef）—— 用户手动刷新列表不该反复触发上游写操作，认领是不可撤销的」，意图是**防止重复调用**。但实现是**整个组件实例生命周期只判一次**，而不是「每次列表变更最多判一次」。

- **证据 3：** 切换账号时的清理逻辑明确重置了 10 个 ref，唯独漏掉 `assignedSiteRef`

`AutomationPage.tsx:491-511`
```tsx
  useLayoutEffect(() => {
    const nextAccountId = status?.accountId ?? null;
    if (selectionScopeRef.current === nextAccountId && selectionOperatorRef.current === operatorId) return;
    selectionScopeRef.current = nextAccountId;
    selectionOperatorRef.current = operatorId;
    executionScopeRef.current += 1;
    stopRef.current = true;
    specificationResolver.current?.(null);
    specificationResolver.current = null;
    setSpecificationPrompt(null);
    setSelection(new Map());
    setStages({});
    setProgress({});
    setRun(IDLE_RUN);
    hydratedRunId.current = null;
    recoveringRunId.current = null;
    persistentRunId.current = null;
    persistentItems.current = new Map();
    startingRunRef.current = false;
    setPreparingRun(false);
  }, [status?.accountId, operatorId]);
```
10 个 ref + 7 个 setState 被重置，**`assignedSiteRef` 不在其中**。

- **证据 4：** `AccountSwitcher` 就在同一页，用户可以随时切账号

`UnlockPage.tsx:100-105` 提供 `selectAccount.mutate(accountId, ...)`，`AutomationPage` 顶部渲染了账号切换（`W1` 行为已证实：`AccountSwitcher` 是 `WarehousePage` 的 headerActions，`AutomationPage` 同样有账号/店铺控制）。

- **性质：**根因（ref 生命周期与账号作用域不匹配）+ 补丁叠补丁（清理逻辑是逐个 ref 手写枚举，漏一个是必然）。

- **影响：**
  1. **切换妙手账号后，新账号的 site 商品永远不会被自动分配**。用户按负责人口径（2026-11-21「打开这个软件如果有未分配的自动分配」）期待的是「每个账号打开都自动分配」，实际只有**第一个账号**有效。
  2. 更糟的是「第一个账号没有 site 商品 → 立即置 true」这条路径：如果启动时账号 A 没有待分配商品，之后切到账号 B（有 47 件待分配），**永远不会触发**。用户看到的就是「明明有未分配的，软件不动」。
  3. 这个 bug 只在多账号场景出现，而多账号是老师/管理员的核心用法。

- **根因修复方案：**
  1. **不要用单个 boolean ref 当作用域无关的闩锁**。改成按 `accountId` 记录：`const assignedSiteRef = useRef<Set<string>>(new Set())`，判断 `if (assignedSiteRef.current.has(accountId)) return`，成功后 `add(accountId)`。
  2. **不要「无 site 商品就置位」**。无 site 商品时保持未置位，下次列表刷新（`refetchInterval: 30_000` 的轮询）还会再判一次。代价是每 30 秒一次 `items.some(...)`（O(n) 且 n ≤ 20），可忽略。真正要防的是 `assignSiteItems.mutateAsync` 的重复调用，而不是判断本身。
  3. 或者干脆把「是否已分配过」交给后端：`AssignSiteItemsResult` 已声明幂等（`queries.ts:1038` 「幂等：已分好店的商品不在 site 列表里，重复调用只会返回 total=0」），那就**允许重复调用**，只用一个「同一 accountId 不并发」的 in-flight 集合即可。这比猜什么时候该停更稳。

---

## P1 问题

### D-P1-1 `apiErrorMessage` 不吞错误，但调用点把它用成了「吞错误」—— 119 处 onError 与 89 处 `.mutate(` 的比例失衡，靠 hook 级兜底

- **证据 1：** `apiErrorMessage` 本身是对的

`web/src/lib/api.ts:159-166`
```ts
export function apiErrorMessage(error: unknown): string {
  if (isApiError(error)) {
    if (error.message) return error.message;
    return API_ERROR_TEXT[error.code] ?? "操作失败";
  }
  if (error instanceof Error && error.message) return error.message;
  return "操作失败";
}
```
后端 `message` 优先，只有 message 为空才退到 code 文案表。**不吞错误。**

- **证据 2：** 但 `fallbackCode` 的 403 映射把语义抹平了

`web/src/lib/api.ts:251-253`
```ts
    case 403:
      return "READ_ONLY_MODE";
```
后端 403 至少有 **3 种不同语义**（`server/core/errors.py`）：
- `READ_ONLY_MODE`
- `FORBIDDEN`（`api/deps.py:171` 「操作员身份与访问令牌不匹配」、`deps.py` 权限不足）
- `PUBLISH_DISABLED` / `PASSWORD_CHANGE_REQUIRED`

后端在 `main.py:523-533` 的 `AppError` handler 里会带正确的 code，所以**正常情况下前端拿到的是真 code**；`fallbackCode` 只在响应体不是合法信封时才用。但一旦后端某个路由抛出的是 `StarletteHTTPException`（`main.py:534-541`），`http_error_handler` 会把 403 映射成 `FORBIDDEN_ORIGIN`：
```python
# server/main.py:461
                403: errors.FORBIDDEN_ORIGIN}.get(exc.status_code, errors.INTERNAL_ERROR)
```
而 `frontend fallbackCode(403)` 给的是 `READ_ONLY_MODE`。**同一个 403，两条降级路径给出两个不同 code**，都会让用户看到「当前为只读模式，无法执行写操作」，而真实原因可能是权限不足。这是**误导性错误文案**。

- **证据 3：** `API_ERROR_TEXT` 只覆盖后端 37 个 code 中的 11 个

后端 `server/core/errors.py` 定义了 **37 个** code（另有 `PASSWORD_CHANGE_REQUIRED` 以字面量形式直接写在 `server/api/deps.py:231`，未进 errors.py）。前端 `API_ERROR_TEXT`（`api.ts:140-157`）共 **16 个键**，其中 5 个是纯前端 code（`NETWORK_ERROR`、`TIMEOUT`、`ABORTED`、`BAD_RESPONSE`、`BOOT_MISSING`）。

交集只有 11 个：
```
AUTH_REQUIRED CONFLICT_BUSY INTERNAL_ERROR RATE_LIMITED READ_ONLY_MODE
REVISION_CONFLICT TOKEN_INVALID UPSTREAM_ERROR UPSTREAM_RATE_LIMIT
UPSTREAM_TIMEOUT UPSTREAM_UNKNOWN_RESULT
```

**后端定义但前端无文案的 26 个**：
```
ACCOUNT_SECRET_UNREADABLE  ALREADY_CLAIMED      AMBIGUOUS_PRODUCT     BAD_JSON
CARRIER_UNAVAILABLE        CATEGORY_NOT_LEAF    CONFIRM_REQUIRED      DIGEST_MISMATCH
DUPLICATE_NAME             DUPLICATE_WORD       FORBIDDEN             FORBIDDEN_ORIGIN
IMAGE_BUDGET_EXCEEDED      IMAGE_TOO_LARGE      NOT_FOUND             PUBLISH_DISABLED
RESULT_FILE_MISSING        RETRY_UNSAFE         SHOP_NOT_IN_ACCOUNT   SITE_LANGUAGE_MISMATCH
TEMPLATE_INVALID           UNPROCESSABLE        UPDATE_IN_PROGRESS    VALIDATION_ERROR
WORD_CONFLICT              WORD_LIMIT_EXCEEDED
```
外加 `PASSWORD_CHANGE_REQUIRED`（`deps.py:231` 内联）。

这 27 个 code 一旦后端返回空 `message`，用户看到的就是 `"操作失败"` —— **正是任务简报里点名要查的「是否把真实错误吞成『操作失败』」。答案是：函数本身不主动吞，但 27/38 的 code 没有兜底文案，实际效果等同吞掉。**

`types.ts:19-65` 的 `KnownApiErrorCode` 也是手写的，同样不含 `FORBIDDEN`、`PASSWORD_CHANGE_REQUIRED`、`AMBIGUOUS_PRODUCT`、`ACCOUNT_SECRET_UNREADABLE`。但因为 `ApiErrorCode = KnownApiErrorCode | (string & {})`（`types.ts:67`），**这些遗漏不会产生任何编译错误**。

- **证据 4：** `isBlocking` getter 定义了但全仓 0 使用

`web/src/lib/api.ts:111-114`
```ts
  /** 是否为「需要用户处理」的阻断性错误（用于 Toast vs 错误态判断） */
  get isBlocking(): boolean {
    return this.status >= 500 || this.status === 401 || this.status === 403 || this.code === "NETWORK_ERROR";
  }
```
```
=== isBlocking 全仓引用 ===
./lib/api.ts:112:  get isBlocking(): boolean {
```
「阻断性 vs 非阻断」的分流规则**定义在类型上，但从未被任何调用点采用**。119 处 onError 各自即兴决定弹 error 还是 warning。

- **性质：**根因（错误处理无统一策略，只有统一工具函数）
- **影响：**同一种错误（比如 403 权限不足）在不同页面弹出不同颜色的 toast、不同文案。`BatchCategoryDialog.tsx:127-129` 把 `CONFLICT_BUSY` 弹 warning，`BatchPanel.tsx:219-222` 也弹 warning 但加了「自动推进没停」的描述 —— 这种细粒度手工判断散落在 20+ 文件里，**没有任何一处能被集中修改**。
- **根因修复方案：**
  1. 用 `openapi-typescript` 生成 `ApiErrorCode`（`docs/architecture.md:463` **声称已经这么做**，实际上 `types.ts:1-3` 的手写注释和 package.json 里没有生成脚本证明并未如此）。这是文档与实现的直接背离，必须修正：要么加 `pnpm gen:types`，要么改文档。
  2. `API_ERROR_TEXT` 补全全部 37 个后端 code（+ `PASSWORD_CHANGE_REQUIRED`），并加一个测试断言「后端 `errors.py` 的 code 集合 ⊆ 前端 `API_ERROR_TEXT` 的 key 集合」。
  3. 删除 `fallbackCode` 里的 `case 403: return "READ_ONLY_MODE"`，改用 `FORBIDDEN`，与后端 `main.py:461` 对齐。
  4. `isBlocking` 要么用起来（在统一 onError 里判定 toast 类型），要么删掉。

---

### D-P1-2 `onError` 与 `ApiError` 混用导致 toast 重复弹 —— 好在只有 1 处真重复，但 `removeJob` 连确认框都没有

- **证据 1：** 项目自己已经识别了双重 toast 风险

`web/src/main.tsx:45-56`
```ts
    /**
     * 写操作不重试：失败立即反馈，静默重试会造成「点一次提交两次」。
     *
     * 这里**刻意不设全局 onError**。曾经想加一个「兜底 toast」，
     * 但 TanStack Query 的语义是：`mutate(vars, { onError })` 的调用点回调
     * 与 hook 里的 `onError` **都会执行**，不是覆盖关系。而本项目 57 处调用点里
     * 有 54 处自己传了 onError —— 加全局兜底的结果是**每个失败都弹两个 toast**，
     * 比漏提示更烦人。
     */
```
统计核实：`.mutate(` 共 89 处，`onError` 共 119 处（含 hook 内定义与 `mutateAsync` 的 catch）。**判断正确，处置正确。**

- **证据 2：** 真正的漏网之鱼只有 1 处，且是破坏性操作

`web/src/features/warehouse/ClaimJobsPanel.tsx:242`
```tsx
                        removeJob.mutate(job.id);
```
`removeJob` 的定义（`ClaimJobsPanel.tsx:56-71`）自带 `onError`，所以不会重复弹。但**调用点没有任何二次确认**：

对比同文件的重试（`ClaimJobsPanel.tsx:112-124` 有 success/error toast）和 `BatchPanel.tsx:311-326` 的 `confirmDelete`（有 `ConfirmDialog` 包裹）。`removeJob` 直接 `mutate(job.id)` —— 删的是「认领/发布任务的本地台账」。虽然注释（第 55 行）说「只删本地批次台账，妙手侧已发出的商品不受影响」，但**用户丢失的是整批任务的进度记录和失败明细**，而这正是他们判断「哪件没发出去」的唯一依据。

- **证据 3：** `useToggleOperator` 的 `enable` 回退分支是补丁叠补丁

`web/src/lib/queries.ts:2541-2573`
```ts
    mutationFn: async ({ id, enabled }: { id: string; enabled: boolean }) => {
      // 停用：契约里唯一明确的动作，直接打 /disable。
      if (!enabled) {
        return api.post<{ operator: Operator }>(`/operators/${encodeURIComponent(id)}/disable`);
      }
      // 启用：契约没写这条路径。先按对称命名试 /enable；
      // 404/405 说明这版后端只提供 PATCH，退回 { enabled: true }。
      // 两条都不通就把错误抛出去，不做本地乐观翻转（身份是权限问题，不能猜）。
      try {
        return await api.post<{ operator: Operator }>(
          `/operators/${encodeURIComponent(id)}/enable`
        );
      } catch (error) {
        if (isApiError(error) && (error.status === 404 || error.status === 405)) {
          return api.patch<{ operator: Operator }, { enabled: boolean }>(
            `/operators/${encodeURIComponent(id)}`,
            { enabled: true }
          );
        }
        throw error;
      }
    },
```
「先试一个接口，404 再试另一个」—— 这是**在运行时探测契约**。每次启用都白打一个 404 请求，且如果后端将来加了 `/enable` 但语义不同，代码会静默走错分支。注释自己承认「契约没写这条路径」，说明是**后端契约不完整时前端打的补丁**。

- **性质：**D-P1-2 主条是根因（乐观更新缺失 + 破坏性操作无确认）；`useToggleOperator` 是补丁叠补丁
- **影响：**
  - `removeJob` 误点击 = 丢失任务台账，无法恢复。
  - `useToggleOperator` 每次启用操作产生一条 404 日志（后端会记 warning 级），污染排障日志。
- **根因修复方案：**
  1. `removeJob.mutate` 外面套 `ConfirmDialog`，与其他删除路径对齐。
  2. `useToggleOperator` 收敛到 `PATCH /operators/{id} {enabled}` 一条路径，后端补 `/enable` 则是后端的事，前端不该探测。
  3. 全仓 89 处 `.mutate(` 做一次 onError 覆盖审计（当前 89 个调用点 vs 119 处 onError，说明多数由 hook 级兜底，但需逐个确认调用点传入的 `onError` 是否与 hook 级重复）。

---

### D-P1-3 轮询：3 处 `refetchInterval` 都有停止条件，但 `useCollectBoxList` 的 30 秒轮询在采集箱页**始终跑**，且 `AutomationPage` 的执行循环用 `setTimeout(4000)` 无限轮询

- **证据 1：** `useCollectBoxList` 的轮询无业务停止条件

`web/src/lib/queries.ts:976-987`
```ts
    // A cached list is useful for the first paint, but it must not hide items
    // added upstream while the user was away from this page.
    refetchOnMount: "always",
    refetchOnWindowFocus: "always",
    refetchInterval: 30_000,
    refetchIntervalInBackground: false,
    enabled,
```
`refetchInterval: 30_000` 是**常量**，不随「有没有正在跑的任务」变化。作为对照，同文件的 `useSchedules`（`queries.ts:1769-1770`）做对了：
```ts
    refetchInterval: (query) =>
      query.state.data?.items?.some((item) => item.running) ? 5_000 : 30_000,
```
也没有 `refetchIntervalInBackground: false`（这是个小瑕疵，但至少区间是动态的）。

`WarehousePage.tsx:127-129` 的 claimJobs 也做对了：
```ts
    refetchInterval: (state) => state.state.data?.items.some(
      (job) => job.status === "pending" || job.status === "running"
    ) ? 3000 : false,
```
`ClaimJobsPanel.tsx:131` 同理：
```ts
    refetchInterval: (query) => ["pending", "running"].includes(query.state.data?.batch.status ?? "") ? 3000 : false,
```
**4 处轮询里 3 处有停止条件，唯一没停的恰恰是流量最大的采集箱列表**（每次打妙手上游）。

- **证据 2：** `refetchIntervalInBackground: false` 只挡住「窗口失焦」，不挡「路由切走」

TanStack Query 的 `refetchIntervalInBackground` 控制的是**窗口/标签页不可见**时是否继续。路由切走（组件 unmount）会让 observe 停止，所以这一层没问题。但用户停在采集箱页什么都没做时，**每 30 秒一次上游请求**永不停止，`enabled` 恒 `true`（`AutomationPage.tsx:176` `connected`）。

- **证据 3：** `AutomationPage` 的图片等待循环用 `setTimeout` 而非 TanStack 轮询

`AutomationPage.tsx:1633-1640`
```tsx
          if (imageItems.length === queuedIds.size
            && imageItems.every((item) => item.processResult.images?.completed === true)) break;
        } catch { /* 单次查询失败保留待核对状态，下轮再试。 */ }
        if (scope !== executionScopeRef.current) return;
        await new Promise((resolve) => setTimeout(resolve, 4000));
      }
```
上限由 `AutomationPage.tsx:1601` 的 `const deadline = Date.now() + 8 * 60_000;` 控制。**有 deadline，正确。**

但卸载防护只有 `executionScopeRef.current` 一道，`useLayoutEffect` 的 cleanup 里递增它（`AutomationPage.tsx:513-518`）：
```tsx
  useLayoutEffect(() => () => {
    executionScopeRef.current += 1;
    stopRef.current = true;
    ...
  }, []);
```
**这是正确的**：一旦卸载，`scope !== executionScopeRef.current` 立刻为真，下一次 4 秒醒来就 return。但**当前正在 sleep 的那个 Promise 无法取消**，最多多活 4 秒 —— 可接受。

- **证据 4：** `ProgressPanel` 的 50ms 定时器常驻

`ProgressPanel.tsx:168-202`
```tsx
  useEffect(() => {
    const timer = window.setInterval(() => {
      ...
    }, TICK_MS);
    return () => window.clearInterval(timer);
  }, []);
```
`TICK_MS = 50`（第 139 行）。这个 effect 依赖数组是 `[]`，**组件挂载期间每 50 毫秒跑一次**，20 次/秒，**即使 `run.phase === "idle"`**。每次 tick 里三个 `if` 判断都不成立时不会 setState（`changed === false`），所以不触发重渲染 —— 设计上是正确的。但 20 Hz 的 JS 定时器在空闲时也在消耗主线程时间片。相比之下 `useTickingElapsed`（`ProgressPanel.tsx:38-43`）就做对了：
```tsx
  useEffect(() => {
    if (!active || startedAt === null) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active, startedAt]);
```
`!active` 直接 return，1000ms。**同一个文件里两种写法，一个对一个欠妥。**

- **性质：**D-P1-3 轮询本身是补丁叠补丁（4 处各写各的，没有统一策略）；`ProgressPanel` 的 50ms 是根因（用定时器做动画，应该用 CSS transition）
- **影响：**
  - 采集箱页空转 30 秒打一次妙手上游 API，有 QPS 配额风险（`architecture.md:522` 提到同账号 1.5s 间隔才不触发限流）。
  - `ProgressPanel` 的 50ms 定时器在 4 个页面级场景中常驻，笔记本续航受影响。
- **根因修复方案：**
  1. `useCollectBoxList` 的 `refetchInterval` 改成函数式：仅在「有 pending 状态的 item」或「`listQuery.isFetching` 刚结束 5 秒内」时 30 秒，否则 `false`。或者干脆去掉定时轮询，依赖 `refetchOnWindowFocus: "always"`。
  2. `ProgressPanel` 的进度条动画改用 CSS `transition: width 220ms linear`，删除 50ms 定时器和 6 个 ref。这是纯展示逻辑，不需要 JS。

---

### D-P1-4 乐观更新：全仓**零处** `onMutate` 乐观写缓存，唯一例外是 `useSelectAccount` 的 `setQueryData`，且没有 `onError` 回滚

- **证据 1：** `onMutate` 全仓 6 处，全部是「记录 epoch」而非乐观写

`web/src/lib/queries.ts:353, 2369, 2392, 2431, 2457, 2469`
```ts
    onMutate: () => getOperatorSessionEpoch(),
```
这 6 处 `onMutate` **不返回 context、不写缓存、不做任何乐观更新**。用途是记录会话 epoch，在 `onSuccess` 里判断「操作期间有没有换人」。

- **证据 2：** 唯一的 `setQueryData` 在 `onSuccess` 里，不是乐观更新

`queries.ts:354-368`
```ts
    onSuccess: async (result, _accountId, startedEpoch) => {
      if (startedEpoch !== getOperatorSessionEpoch()) return;
      await client.cancelQueries({ queryKey: queryKeys.auth.all });
      if (startedEpoch !== getOperatorSessionEpoch()) return;
      client.setQueryData<AuthStatus>(queryKeys.auth.status(), (previous) => previous ? {
        ...previous,
        connected: result.connected,
        accountId: result.accountId,
        connectionName: result.connectionName,
        appIdHint: result.appIdHint,
        accounts: previous.accounts.map((account) => ({
          ...account,
          isDefault: account.id === result.accountId,
        })),
      } : previous);
```
这是 `onSuccess`（后端已确认），不是 `onMutate`（先本地写）。**因此不存在「乐观更新没有配套 onError 回滚」的缺陷 —— 因为根本没有乐观更新。**

- **证据 3：** 但有一个真正的「乐观态」缺口

`useSaveWorkflowDefaults`（`queries.ts:2124-2140`）：
```ts
    onSuccess: (data) => {
      client.setQueryData(workflowDefaultsQueryKey(operatorId), data);
    },
```
只有 `onSuccess`，**没有 `onError`**。而调用方 `AutomationPage.tsx:344-352` 的 800ms 防抖推送：
```tsx
        {
          onError: (error) => {
            if (settingsScopeRef.current !== saveOperatorId) return;
            toast.error("处理默认设置同步失败", {
              description: `本地设置已保留，服务器处理可能仍会使用旧值。${apiErrorMessage(error)}`,
            });
          },
        }
```
提示文案说得非常准确（「本地设置已保留，服务器处理可能仍会使用旧值」）—— **这恰恰证明了双写状态的存在**：localStorage 已写入，服务端没写入，两者不一致且**没有任何自动重试或状态标记**。用户看到一条 toast，6 秒后消失，此后再无痕迹。

- **性质：**根因（本地 localStorage + 服务端 settings 双写，无单一事实来源）
- **影响：**
  1. 网络抖动一次 = 界面参数与服务端参数永久分叉，直到用户下次改任意一项才会重推。而「重量增量」「包裹尺寸」「尺码图」直接影响入仓时的定价与规格，**分叉会导致算价错误**。
  2. 用户无法从界面判断自己看到的是本地值还是服务端值。
- **根因修复方案：**
  1. **取消 localStorage 作为事实来源**。`loadSettings` 只在服务端未就绪时用于首屏占位，一旦 `workflowQuery.data` 到达就以它为准，本地改动走 mutation 并在成功后 `setQueryData`（现有逻辑的后半段）。
  2. 加 `onError` 回滚：失败时 `setQueryData` 回到上一次成功的服务端值，并在 UI 上标注「未同步」。
  3. 更彻底的做法：把「处理设置」提升为独立表单 + 显式「保存」按钮，去掉 800ms 自动推送。自动推送 + 双写 + 无版本控制 = 必然分叉。

---

### D-P1-5 `types.ts` 手工同步后端，`docs/architecture.md` 声称用 `openapi-typescript` 生成 —— 文档与实现背离

- **证据 1：** 文档声明

`docs/architecture.md:463`
```
3. **契约稳定**：FastAPI 生成 OpenAPI，前端 `types.ts` 由 `openapi-typescript` 生成，字段改名会在编译期报错，而不是运行时白屏。
```

`docs/architecture.md:102`
```
│     │  ├─ types.ts            # 由 OpenAPI 生成（openapi-typescript）
```

- **证据 2：** 实现是手写的

`web/src/lib/types.ts:1-3`
```ts
// api.md §0.1 命名与类型 / §0.4 分页 / §0.5 错误信封
// 与后端契约一一对应：camelCase 字段、id 为字符串、*At 为 Unix 秒（浮点）、
// 金额为字符串十进制（"12.34"）、可空字段始终出现（无值为 null）。
```
「与后端契约一一对应」= **人工对齐**。

`web/package.json` 的 devDependencies **没有 `openapi-typescript`**，scripts 里没有生成命令：
```json
  "devDependencies": {
    "@eslint/js": "^9",
    "@types/react": "^18.3.11",
    ...
    "typescript": "^5.6.3",
    "typescript-eslint": "^8.72.1",
    "vite": "^5.4.8"
  }
```
全仓搜索 `openapi-typescript` 只命中两处文档，**零处代码/配置**。

- **证据 3：** 漂移已经发生，且有明确证据

`types.ts:19-65` 的 `KnownApiErrorCode` 缺 4 个后端已定义的码（`FORBIDDEN`、`PASSWORD_CHANGE_REQUIRED`、`AMBIGUOUS_PRODUCT`、`ACCOUNT_SECRET_UNREADABLE`）。

`types.ts:285-296` 的 `Batch`：
```ts
export interface Batch {
  id: string;
  status: BatchStatus | string;
  accountId: string | null;
  shopId: string;
  detailIds: string[];
  /** 指向 detailIds 里的下一条；失败不推进 */
  currentIndex: number;
  settings: BatchSettings;
  results: BatchResultRow[];
  errors: BatchErrorRow[];
  createdAt: number;
  updatedAt: number;
  finishedAt: number | null;
  /** 还剩几件没处理（待跑 + 失败待重试），后端算好，前端不用自己数 */
  remaining: number;
  /** 创建这个批次的操作员名；老后端 / 未记录时为 null */
  operatorName?: string | null;
}
```
对照后端 `server/services/batch_service.py:91-111` 的 `serialize()`：**字段完全一致** —— 但注意 `operatorName` 在后端是 `operator_name or ""`（**空字符串，不是 null**）：
```python
        # 建这批的人。空串 = 没有操作员上下文（老数据 / 脚本建的），
        # 前端判空串即可，不用区分 null。
        "operatorName": operator_name or "",
```
而前端类型声明 `operatorName?: string | null`（可选 + 可 null）。**后端明确说「空串，不用区分 null」，前端却声明了 null**。前端没有任何地方读这个字段（全仓 0 引用），所以不会立刻出错，但类型在说谎。

- **证据 4：** `Schedule.running` 是后端内存态，前端类型如实标注了

`types.ts:1076-1077`
```ts
  /** 后端内存态：此刻正在执行（不落库，重启即消失）。 */
  running?: boolean;
```
后端 `server/services/schedule_service.py:419` `"running": task_running(row.id)` —— **这个字段是内存字典 `_running_tasks` 的投影**。多 worker 部署时必然不一致，但后端强制单 worker（`architecture.md:475` 「后端强制单 worker」），所以侥幸正确。**这一条是「注释写明了风险」的正面案例**，值得对照。

- **性质：**根因（无代码生成的契约同步机制）
- **影响：**
  1. 后端加字段前端不感知，运行时 `undefined`，表现为「某个数字不显示」或「某个筛选失效」—— 正是 CLAUDE.md 快速诊断里 `draft_verified_revision` 消失那类问题的前端镜像。
  2. 2 637 行 `queries.ts` 里还有 56 个内联 interface 也是手写的（见 P2-1），**漂移面比 `types.ts` 更大**。
  3. `docs/architecture.md` 的错误声明会让后续维护者相信「编译期能抓漂移」，从而跳过运行时校验。
- **根因修复方案：**
  1. 加 `pnpm gen:types`：`openapi-typescript http://127.0.0.1:8812/openapi.json -o src/lib/api-schema.d.ts`，`types.ts` 改为 `api-schema.d.ts` 的别名导出 + 手写业务别名。
  2. 在 `package.json` 的 `check` 脚本前挂 `gen:types && git diff --exit-code src/lib/api-schema.d.ts`，漂移即 CI 失败。
  3. 修正 `docs/architecture.md` 的两处声明，或在实现追上之前标注「计划中」。

---

### D-P1-6 `queryKeys` 里 4 个工厂函数是死定义，制造「key 有统一管理」的假象

- **证据 1：** 全仓引用计数

```
=== 死定义核实 ===
system.health            0
imageJobs.detail         0
titles.preview           0
sync.priceTemplates      0
```

对应定义：
`queryKeys.ts:87` `health: () => ["system", "health"] as const,`
`queryKeys.ts:50-51` `detail: (accountId, id) => ["image-jobs", accountId ?? "current", id] as const,`
`queryKeys.ts:95-98` `titles: { all: ["titles"], preview: (key: string) => ["titles", "preview", key] as const }`
`queryKeys.ts:118-122` `sync: { all: ["sync"], priceTemplates: (params) => ["sync", "price-templates", params] }`

**注意 `titles.preview` 的参数是 `key: string` 但没有任何消费者**，而 `useTitlePreview`（`queries.ts:2164`）是 mutation 不是 query —— 说明 `titles.preview` 是为一个**从未实现的 query 版本**预留的。

- **性质：**补丁叠补丁（先设计全套 key 空间，实现时只做了一半，另一半留着）
- **影响：**审计和 code review 会反复把 `queryKeys.xxx` 当成「已覆盖」的证据。本报告 P0-2 的分析就差点被这个假象误导。
- **根因修复方案：**删除 4 处死定义。留着不用 = 负资产。

---

### D-P1-7 `useWarehouseStore` 的筛选 setter 逐个手写「清空 selected」，8 个 setter 重复 8 遍，且 `resetOperatorSessionUiState` 漏了 `sourceStatus`

- **证据 1：** 8 个 setter 逐个手写 `selected: []`

`web/src/features/stores.ts:125-135`
```ts
  // 换页只改页码：选中集合跨页保留（批量操作对整批生效）
  setPage: (page) => set({ page }),
  // 换筛选条件必须清空选中：否则会对不可见商品执行批量改分类/删除
  setPageSize: (pageSize) => set({ pageSize, page: 1, selected: [] }),
  setSort: (sort) => set({ sort, page: 1, selected: [] }),
  setSearchDraft: (searchDraft) => set({ searchDraft }),
  setQ: (q) => set({ q, page: 1, selected: [] }),
  setClaimed: (claimed) => set({ claimed, page: 1, selected: [] }),
  setStatus: (status) => set({ status, page: 1, selected: [] }),
  setQualityStatus: (qualityStatus) => set({ qualityStatus, page: 1, selected: [] }),
  setSourceStatus: (sourceStatus) => set({ sourceStatus, page: 1, selected: [] }),
  setCategory: (category) => set({ category, page: 1, selected: [] }),
```

同一模式重复 8 次（`setPageSize` / `setSort` / `setQ` / `setClaimed` / `setStatus` / `setQualityStatus` / `setSourceStatus` / `setCategory`）。**每一个新增筛选条件都是一次「记得加 `selected: []`」的人肉检查。**

- **证据 2：** `setSearchDraft` 不清 selected，是对的（草稿不生效）；但 `setQ` 清，两者只差一个字母

- **证据 3：** `resetOperatorSessionUiState` 又手写了一遍完整重置

`stores.ts:204-215`
```ts
  useWarehouseStore.setState({
    page: 1,
    pageSize: 20,
    sort: "savedDesc",
    searchDraft: "",
    q: "",
    claimed: null,
    status: null,
    qualityStatus: null,
    category: { kind: "all" },
    selected: [],
  });
```
**注意：这里没有 `sourceStatus`。** 初始 state（`stores.ts:111-122`）有 `sourceStatus: null`，重置函数里漏了。切换操作员后，上一个用户的「货源缺失」筛选会保留。

- **证据 4：** `useWordsStore` 的重置漏了 `selectedWords` 之外的 `category`？

`stores.ts:216-224`
```ts
  useWordsStore.setState({
    page: 1,
    pageSize: 100,
    category: null,
    searchDraft: "",
    q: "",
    selectedWords: [],
  });
```
这个完整的。但 `usePublishLogsStore`：
```ts
  usePublishLogsStore.setState({ page: 1, pageSize: 20, status: null });
```
也是完整的（该 store 只有 3 个字段）。

**所以：3 个 store 的重置，2 个完整，1 个漏了 `sourceStatus`。**

- **性质：**补丁叠补丁（每次加字段就手抄一遍初始值，漏抄是必然）
- **影响：**
  - 切换操作员后残留 `sourceStatus` 筛选，新用户看到的是被筛选过的列表，且**看不到任何筛选器处于激活态的视觉提示**（`sourceStatus` 参与 `hasActiveWarehouseFilter` 的判断在 `WarehousePage.tsx:186-188`，所以空状态会提示「没有符合条件的商品」，但 Select 的 value 计算（`WarehousePage.tsx:754-759`）会正确显示，所以用户能自己发现）。
  - 影响可控，但属同一发病机制的早期症状。
- **根因修复方案：**
  1. 抽出 `INITIAL_WAREHOUSE_STATE` 常量，`create()` 的初始值和 `reset()` 都引用它。**唯一事实来源。**
  2. 或者：筛选条件集中到一个 `filters` 对象字段，`setFilters(patch)` 统一处理 `page: 1, selected: []`。这样新增筛选只改类型，不改 setter。

---

### D-P1-8 `useAuditLogs` 的 `before` 游标分页没有下一页推进逻辑，`limit` 归一成 `null` 与后端默认值语义不匹配

- **证据 1：** `limit`/`before` 在 key 里归一成 `null`

`web/src/lib/queries.ts:2609-2637`
```ts
export function useAuditLogs(
  query: AuditLogQuery,
  options: { enabled?: boolean } = {}
): UseQueryResult<AuditLogsResponse> {
  return useQuery({
    queryKey: queryKeys.auditLogs.list({
      operatorId: query.operatorId ?? null,
      action: query.action ?? null,
      targetKind: query.targetKind ?? null,
      result: query.result ?? null,
      limit: query.limit ?? null,
      before: query.before ?? null,
    }),
```
`queryKeys.ts:127-139` 里有详细注释说明为什么强制 `?? null`：
```ts
      // limit 允许 null：调用方用 `?? null` 统一把 undefined 归一成 null，
      // 这样缓存键的形状稳定（undefined 与 null 在 JSON 里都不出现，但作为
      // 对象字面量参与序列化时两者不同，混用会导致同一查询算出两个键）。
```
**这个归一化是对的**，且注释解释了原因。

但 `compactQuery`（`queries.ts:119-128`）会把 `null` 丢掉：
```ts
function compactQuery(query: Record<string, unknown>): QueryParams {
  const result: QueryParams = {};
  for (const [key, value] of Object.entries(query)) {
    if (value === null || value === undefined || value === "") continue;
```
所以 `limit: null` 实际不发这个参数，后端用默认值。**key 里有 `limit: null`，请求里没有 `limit`** —— 两处对同一个状态有不同表示。

- **性质：**根因（key 归一化与请求归一化用了两套规则）
- **影响：**
  - 只要后端默认 `limit` 变化，缓存 key 不变但返回内容变了 —— TanStack Query 不会感知。
  - `before` 游标（翻页）在 UI 层是否被推进需要单独核实，但 `AuditLogPanel.tsx` 是唯一的消费者，本次未深入。
- **根因修复方案：**
  1. `compactQuery` 与 key 归一化共用同一个函数：key 用归一化后的值构造，请求也用同一份，**保证 key ↔ request 双射**。
  2. 或者：在 `compactQuery` 里显式保留 `null` 作为「明确不传」的信号，但 key 里用 `undefined`。关键是**一处定义**。

---

### D-P1-9 三层缓存叠加（QueryClient per-operator + localStorage 持久化 + 服务端），失效语义在三个层面各自为政

- **证据 1：** per-identity QueryClient

`web/src/lib/operatorQueryClientRegistry.mjs:19-36`
```js
export function getOperatorQueryClient(identityKey, fallbackClient, persistenceIdentityKey = null) {
  const existing = clientsByOperator.get(identityKey);
  if (existing) return existing;

  const client = clientsByOperator.size === 0
    ? fallbackClient
    : new QueryClient({ defaultOptions: fallbackClient.getDefaultOptions() });
```
每个身份一个 QueryClient。切换身份时旧的 client 被 `cancelQueries()` 但不 `clear()`（`OperatorQueryCacheBoundary.tsx:44-45`）：
```tsx
    resetOperatorSessionUiState();
    void previousClient.client.cancelQueries();
    if (identityKey === null) clearOperatorQueryClientsExcept(client);
```
`clearOperatorQueryClientsExcept` 只在**登出**（`identityKey === null`）时调用。**切换身份不是登出，所以旧身份的内存缓存保留** —— 这是刻意的（快速切回不用重拉），但也意味着**内存里同时存在两个身份的全部数据**。

- **证据 2：** localStorage 持久化

`operatorQueryDiskCache.mjs:5-7`
```js
const STORAGE_PREFIX = "aohuang.query-cache.v2.";
const MAX_ENTRIES = 24;
const MAX_SERIALIZED_LENGTH = 1_000_000;
const MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;
```
每个账号最多 24 条、1 MB、7 天。持久化的 key 只限 `collect-box` 和 `warehouse`（`operatorQueryDiskCache.mjs:26-37`）。

- **证据 3：** 失效时把 `updatedAt` 置 0 作为「已脏」标记

`operatorQueryDiskCache.mjs:158-162`
```js
    entries.push({
      queryKey: query.queryKey,
      data: query.state.data,
      // Keep cached content available immediately, but revalidate it after a
      // mutation invalidates the query.
      updatedAt: query.state.isInvalidated ? 0 : query.state.dataUpdatedAt,
    });
```
`updatedAt: 0` 在写入磁盘时被保留，但读回来时（`operatorQueryDiskCache.mjs:73-77`）：
```js
        const fresh = parsed.entries.filter((entry) =>
          isPlainObject(entry) &&
          isPersistableQueryKey(entry.queryKey) &&
          accountIdForQueryKey(entry.queryKey) === parsed.accountId &&
          Number.isFinite(entry.updatedAt) &&
          entry.updatedAt > 0 &&              // ← updatedAt: 0 的条目被丢弃
          entry.updatedAt <= now &&
          now - entry.updatedAt <= MAX_AGE_MS &&
          "data" in entry,
        );
```
`entry.updatedAt > 0` —— **`updatedAt: 0` 的条目（即「已被 invalidate 的数据」）在下次启动时被当作垃圾清掉**。逻辑是自洽的：脏数据不持久化。

- **证据 4：** 但 hydrate 时只 `setQueryData`，不标 invalidated

`operatorQueryDiskCache.mjs:150-155`
```js
export function hydrateOperatorQueryClient(identityKey, client) {
  for (const entry of readEntries(identityKey)) {
    client.setQueryData(entry.queryKey, entry.data, { updatedAt: entry.updatedAt });
  }
}
```
`setQueryData` 带 `updatedAt` 参数会设置数据时间戳，但**不会设置 `isInvalidated`**。所以从磁盘恢复的数据 `isInvalidated === false`，配合 `useWarehouseList` 的 `refetchOnMount: "always"`（`queries.ts:444`）会立刻重取 —— 侥幸正确。

**但如果某个 hook 没有 `refetchOnMount: "always"`**（例如 `useProductDetail` 有，`useShops` 没有），从磁盘恢复的数据会被当成新鲜数据展示。

- **证据 5：** `useProductDetail` 持久化了，但只有 `warehouse` 前缀的 key 会被持久化

`operatorQueryDiskCache.mjs:26-37` 的 `isPersistableQueryKey` 检查 `queryKey[0] !== "collect-box" && queryKey[0] !== "warehouse"` → false。所以详情（`["warehouse", accountId, id, {includePayload}]`）**会被持久化**。而 `useProductDetail` 的 `staleTime: 5 * 60_000` + `refetchOnMount: "always"` 让它每次重取，磁盘缓存只在「网络请求返回前」用作首屏。

**这三层缓存的设计其实相当严谨**（MAX_SERIALIZED_LENGTH 溢出淘汰、`updatedAt: 0` 脏标记、accountId 双重校验、身份隔离）。问题不在实现，在**复杂度**。

- **性质：**根因（没有单一缓存策略，三层各写一套失效规则）
- **影响：**
  - 排查「数据不对」需要同时考虑：内存 QueryClient、localStorage 条目、后端。`LocalCachePanel` 提供了「清除商品缓存」的手动出口（`clearOperatorProductListCache`），说明**开发者自己也知道用户会需要这个**。
  - `clearOperatorProductListCache`（`operatorQueryClientRegistry.mjs:76-91`）的 filter 只匹配 `typeof queryKey[1] === "object"` —— **不匹配详情页**（`queryKey[1]` 是字符串 accountId）。所以「清除商品缓存」清掉了列表，**详情还在**。而 `operatorQueryDiskCache.mjs` 的注释（第 41-47 行）明确写了「详情原先被排除……放开详情后必须同时识别字符串形态」—— 持久化那边改了，**清理那边没改**。
- **根因修复方案：**
  1. `clearOperatorProductListCache` 的 filter 改为 `queryKey[0] === "collect-box" || queryKey[0] === "warehouse"`，不再判断 `queryKey[1]` 类型。与 `isPersistableQueryKey` 共用同一个 predicate。
  2. 三层缓存是复杂度来源。**如果 localStorage 持久化带来的收益 < 它引起的排查成本，删掉它**。当前 `MAX_ENTRIES=24` / 1MB 的容量决定了它只能缓存少数几页，收益有限。

---

## P2 问题

### D-P2-1 `queries.ts` 是 2 637 行 God File：56 个内联 interface + 103 个 hook + 客户端封装引用

- **证据 1：** 单文件承载 8 个业务域

`web/src/lib/queries.ts` 行数与内容分布：
- 1–129 行：通用工具（`idempotencyKey`、`compactQuery`）
- 130–380 行：认证/账号（15 个 hook）
- 381–840 行：仓库（20 个 hook + 10 个内联 interface）
- 841–935 行：分组 + 类目
- 936–1083 行：采集箱
- 1084–1269 行：自动化运行（10 个 hook）
- 1270–1638 行：图片 + 版本 + 更新
- 1639–1750 行：定时 + 发布
- 1751–1842 行：定时 CRUD
- 1843–1920 行：违规词
- 1921–2079 行：定价模板
- 2080–2141 行：工作流默认值
- 2142–2233 行：标题 + 同步
- 2234–2299 行：载体链接
- 2300–2475 行：批次编排
- 2476–2637 行：操作员 + 审计

**56 个 `export interface` / `export type`**（`grep -cE "^export (interface|type) " lib/queries.ts` = 56），包括 `PricePreviewResponse`（9 行）、`TransformReport`（27 行）、`VersionPayload`（37 行）、`WordsMutationBody`（26 行）这类复杂结构。

**与 `types.ts` 的重复：**
- `PriceTemplateItem`（`queries.ts:1926`）vs `types.ts` 里是否也有？—— `types.ts` 没有，但 `queries.ts` 里 `TemplatesResponse`、`SaveTemplateResponse`、`DeleteTemplateResponse` 三处都引用它，而这三个响应结构**后端只有一个 `templates` 字段**。
- `VersionPayload`（`queries.ts:1307`）vs `SystemVersion`（`types.ts:1386`）—— **两份几乎相同的版本响应类型**。`SystemVersion` 全仓 0 引用（死类型）。
- `WarehouseListParams`（`queries.ts:385`）vs `WarehouseQuery`（`types.ts:615`）—— 见 D-P0-3。
- `SizeChart` 定义在 `features/automation/types.ts:287`，而 `useSizeCharts.ts` 的注释（第 29-31 行）说：
```ts
 * `defaultUrl` 是后端新加的字段（`size_chart_service.list_charts` 里固定带），
 * 类型放在本文件而不是 features/automation/types.ts，是因为那份类型文件
 * 不在这次改动范围内；这里用「继承 + 补字段」表达同一份契约，不影响既有调用方。
```
**「不在这次改动范围内」—— 这是代码里写下的「我知道该改但没改」的自白。** 于是 `SizeChartsListResponse extends SizeChartsResponse` 又多了一层。

- **证据 2：** `useSizeCharts` 的 key 又是第四种拼写

`features/automation/useSizeCharts.ts:23`
```ts
export const sizeChartsKey = ["size-charts"] as const;
```
**没有走 `queryKeys` 工厂。** 注释（`queries.ts:1376-1379`）解释了类似情况的处理：
```ts
 * 更新状态的 queryKey。没有放进 @/lib/queryKeys 是刻意的：那边是全局契约文件，
 * 这次改动范围只到 queries.ts；key 前缀仍按 ["system", ...] 对齐，失效时能命中同一族。
```
「这次改动范围只到 queries.ts」—— 又一个「我知道」的自白。

- **性质：**补丁叠补丁（每次改动都声明「这次不动那个文件」，于是边界越来越碎）
- **影响：**新人先在 `types.ts` 找类型，找不到再去 `queries.ts` 翻 2 637 行，再去 `features/xxx/types.ts` 翻。同一份契约有 3 个候选位置。
- **根因修复方案：**按域拆 8 个文件（`queries/auth.ts`、`queries/warehouse.ts`……），每个文件 ≤ 400 行，类型与 hook 同域。`queries/index.ts` barrel export 保持现有 import 路径不变。

---

### D-P2-2 无虚拟化：仓库支持 1000 件/页，`selectedIds.includes()` 是 O(n²)

- **证据 1：** 页大小上限

`web/src/features/warehouse/WarehousePage.tsx:85`
```tsx
const PAGE_SIZE_OPTIONS = [20, 50, 100, 200, 500, 1000];
```
后端 `server/api/warehouse.py:112`
```python
    pageSize: int = Query(default=20, ge=1, le=1000),
```
**前后端上限都是 1000，用户可以真的拉到 1000 件。**

- **证据 2：** 无任何虚拟化

```
=== 虚拟化库/实现搜索 ===
grep -rn "Virtual|virtual|useWindowVirtualizer|react-window|react-virtual" → 0 命中
```
`@tanstack/react-table` 在 dependencies 里（`web/package.json`），但：
```
=== @tanstack/react-table 使用 ===
0 命中
```
`architecture.md:29` 声称「TanStack Table：采集箱 / 仓库」，**实际零使用**。又一处文档与实现背离。

- **证据 3：** 1000 个卡片 + 1000 个图片 + framer-motion 逐项变体

`WarehousePage.tsx:919-947`
```tsx
                <motion.div
                  initial={reduced ? undefined : "hidden"}
                  animate={reduced ? undefined : "show"}
                  className={cn(
                    "grid gap-4",
                    "grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5 3xl:grid-cols-6",
                    listQuery.isFetching && "opacity-90"
                  )}
                >
                  <AnimatePresence initial={false}>
                    {items.map((product: Product, index: number) => (
                      <motion.div
                        key={product.id}
                        variants={reduced ? undefined : listItem}
                        custom={index}
                        layout={false}
                      >
                        <ProductCard ... />
```
每个 item 包一层 `motion.div` 带 `variants` + `custom={index}`。1000 个 motion 组件，每个都注册到 framer-motion 的动画调度。

`ProductCard.tsx:98-103`
```tsx
          <RemoteImage
            src={product.imageThumb || product.image}
            eager={index < 8}
            zoomOnHover
            className="[&_svg]:size-8"
          />
```
`eager={index < 8}` 是**唯一**的加载控制（前 8 张预加载）。其余 992 张仍会创建 `<img>` DOM 节点，只是 `loading="lazy"`。**1000 个 `<img>` 元素 + IntersectionObserver 回调**。

- **证据 4：** O(n²) 选中判断

`WarehousePage.tsx:940`
```tsx
                          selected={selectedIds.includes(product.id)}
```
`selectedIds` 是 `string[]`（`stores.ts:93` `selected: string[]`）。在 1000 件的渲染中，每次 `.includes()` 是 O(selected.length)。全选 1000 件后，**1000 × 1000 = 1 000 000 次字符串比较，每次渲染**。

同样的模式出现 4 次：
```
272:      if (typeof item.revision === "number" && selectedIds.includes(item.id)) {
279:  const allSelectedOnPage = pageIds.length > 0 && pageIds.every((id) => selectedIds.includes(id));
296:    if (selectedIds.includes(id)) {
940:                          selected={selectedIds.includes(product.id)}
```
279 行在每次渲染都跑（`pageIds.every` × `includes` = O(n²)），272 行在 `useMemo` 里但依赖 `selectedIds` 数组引用，每次选中变化都重算。

- **证据 5：** `toggleWarehouseSelection` 每次都线性查找

`WarehousePage.tsx:294-333`
```tsx
  const toggleWarehouseSelection = useCallback((id: string) => {
    const item = items.find((candidate) => candidate.id === id);
```
然后连续 5 个 `setState` 各做一次对象展开 `{...current}`：
```tsx
      setSourceShopIds((current) => { const next = { ...current }; delete next[id]; return next; }),
      setSelectedClaimed((current) => { ... }),
      setSelectedPublishedShopIds((current) => { ... }),
      setSelectedDetailIds((current) => { ... }),
      setSelectedQualityReviewById((current) => { ... }),
```
**勾选一个商品触发 6 个 setState + 5 次 1000 键对象浅拷贝。** React 18 会自动批处理，但拷贝成本不变。

- **性质：**根因（数据结构选错：`selected` 用数组而非 Set；卡片列表无虚拟化）
- **影响：**1000 件/页时明显卡顿，勾选操作有延迟感。
- **根因修复方案：**
  1. `selected: Set<string>` 或 `Record<string, true>`。`includes` → `has`，O(1)。
  2. 5 个并行 state 合并成一个 `Record<string, SelectionSnapshot>`，勾选时只改一个。
  3. 虚拟化：`@tanstack/react-virtual`（与已有 react-query 同族）包住网格。至少要设一个「超过 200 件时不渲染动画」的降级。

---

### D-P2-3 `RemoteImage` 的 `eager` 判断用 `index < 8`，但 `index` 来自列表顺序而非可见性

- **证据：** `index < 8` 是列表序位不是可见性

`WarehousePage.tsx:938` 把 `index` 传给 `ProductCard`，`ProductCard.tsx:100` 用它决定 `eager`。

如果用户先滚到第 500 件，前 8 件仍在视口外但被 eager 加载（浪费带宽）；而视口内的第 500–508 件是 lazy（延迟）。`index < 8` 是「最先 8 个」的近似，不是「视口内」的准确判断。

- **性质：**补丁（用简单启发式替代 IntersectionObserver）
- **影响：**首屏带宽分配不最优，但影响有限（`imageThumb` 约 30 KB，见 `ProductCard.tsx:95-97` 注释）。
- **根因修复方案：**用 `RemoteImage` 内部已有的 `loading="lazy"` + `fetchpriority="high"` 替代 `eager` prop，让浏览器决定。

---

### D-P2-4 `useScheduleLogs` 的 key 用 `""` 占位，且 `limit` 键与 `query` 里的默认值不一致

- **证据：** key 用 `""` 作空值占位

`web/src/lib/queries.ts:1661-1673`
```ts
export function useScheduleLogs(
  params: { taskId?: string; accountId?: string; limit?: number } = {}
): UseQueryResult<ScheduleLogsResponse> {
  const search = new URLSearchParams();
  if (params.taskId) search.set("taskId", params.taskId);
  if (params.accountId) search.set("accountId", params.accountId);
  search.set("limit", String(params.limit ?? 100));
  return useQuery({
    queryKey: ["schedule-logs", params.taskId ?? "", params.accountId ?? "", params.limit ?? 100],
```
`params.taskId ?? ""` 与「不传 taskId」等价，但空字符串作为 key 段在语义上是「有一个叫空的 taskId」。与 `queryKeys` 里其他地方用 `"current"` / `"none"` 占位的惯例不一致（`queryKeys.ts:29`、`:37`、`:103`）。

- **性质：**根因（缺少统一的「空值占位」约定）
- **影响：**无功能问题，但破坏 key 空间的可读性；且这个 key **没走 `queryKeys` 工厂**（是第 5 种绕过）。
- **根因修复方案：**`""` → `"all"`，并挪进 `queryKeys.schedules.logs()`。

---

### D-P2-5 `useShops` 的 `refresh` 参数进了 key 但**全部 6 个调用点都传 `false`**

- **证据：** `refresh` 进了 key 但从不被置 `true`

`web/src/lib/queries.ts:317-320`
```ts
  const refresh = options.refresh ?? false;
  const enabled = options.enabled ?? true;
  return useQuery({
    queryKey: [...queryKeys.shops.list(accountId), { refresh }],
```
6 个调用点（`UnlockPage.tsx:63`、`AutomationPage.tsx:446`、`PublishDialog.tsx:79`、`SchedulesPage.tsx:105`、`SchedulesPage.tsx:802`、`CreateBatchDialog.tsx:62`）**没有一个传 `refresh`**。所以 key 永远是 `["shops", accountId, {refresh: false}]`。

`attemptMs: 60_000` 的 `timeoutMs`（`queries.ts:324`）也只在 refresh 时才有意义。

- **性质：**补丁（加了能力但没有任何 UI 使用）
- **影响：**`refresh: true` 强制上游重扫店铺的能力无法从界面触达。「店铺列表变了但界面不变」时用户只能重启。
- **根因修复方案：**在账号切换或店铺下拉里加「重新扫描店铺」按钮，接 `useShops(accountId, {refresh: true})`；或删除未使用的分支。

---

### D-P2-6 `useProducts` 的 `placeholderData` 在 `claimAccountId` 相同时保留，但 `useAutomationResults` 用 `previousKey?.[2] === accountId` 判断 —— 两者都是手工解析 key 下标

- **证据：** 三处 `placeholderData` 各自硬编码 key 下标

`web/src/lib/queries.ts:440-443`
```ts
    placeholderData: (previous, previousQuery) => {
      const previousParams = previousQuery?.queryKey[1] as WarehouseListParams | undefined;
      return previousParams?.claimAccountId === (params.claimAccountId ?? null) ? previous : undefined;
    },
```
`queries.ts:458-461`
```ts
    placeholderData: (previous, previousQuery) => {
      const previousKey = previousQuery?.queryKey as readonly unknown[] | undefined;
      return previousKey?.[2] === accountId ? previous : undefined;
    },
```
`queries.ts:983-986`（collect-box）用 `previousQuery?.queryKey[1] as Partial<CollectBoxListParams>`。

**三处各自知道自己的 key 布局，硬编码下标。** 一旦 `queryKeys` 工厂调整层数（比如给 `imageJobs` 加 `"list"` 段），这些地方静默失效。

- **性质：**补丁叠补丁（key 结构变化时靠人肉同步解析逻辑）
- **影响：**改 key 结构 → 三处 placeholder 逻辑静默失效 → 切筛选时出现白屏闪烁（性能问题回归，但不报错）。
- **根因修复方案：**`queryKeys` 工厂同时返回 key 和「解码器」。或者把 `placeholderData` 的判断逻辑下沉到工厂：`queryKeys.warehouse.list` 提供一个 `samePlaceholderScope(a, b)` 伴随函数。

---

### D-P2-7 `AutomationPage` 的 `hydrateRun` 里有日文注释，且注释所述逻辑与代码不符

- **证据：** 同一注释块里日语与「中国語注释」叠加

`web/src/features/automation/AutomationPage.tsx:735-743`
```tsx
        setRun((prev) => ({
          ...prev,
          imageDone: ready, imagePlanned: total, imageFailed: failedJobs,
          // hasDurableImages が true = 画像スレッドが実際に完了した証拠（submitted=0 も含む）。
          // これがないと imagePlanned=0 → imagesDone=false のまま batchPercent が 100% にならない。
          // 中国語注释：hasDurableImages=true はimage线程明确写了 completed 字段，
          // 即使实际提交 0 张也算真的「完成」，区别于还没跑的初始状态。
          ...(hasDurableImages ? { imagingComplete: true } : {}),
        }));
```
**两行日语 + 一行半日半中**。同一个注释块里三种语言混排，且第三行开头「中国語注释」说明作者意识到前两行语言不对但选择了叠加而非替换。

- **性质：**补丁叠补丁（增补注释时没删除旧语言版本）
- **影响：**可读性；且这处涉及 `batchPercent` 计算 —— 这正是 CLAUDE.md 快速诊断里「批次进度卡在 50%」的同一模块。
- **根因修复方案：**统一中文，删掉日语部分。

---

### D-P2-8 `ProgressPanel` 的 `imagePercent` 逻辑与 CLAUDE.md 记录的「卡在 50%」缺陷同源，只是被多层条件掩盖

- **证据 1：** CLAUDE.md 的已知缺陷

`CLAUDE.md:128-129`
```
### 批次进度卡在 50%
→ `imagePercent` 在 `imagePlanned=0` 时返回 0，导致 `(100+0)/2=50`。检查 ProgressPanel.tsx。
```

- **证据 2：** 当前代码用 4 层嵌套三元掩盖了它

`ProgressPanel.tsx:106-114`
```tsx
  const imagePercent = run.phase === "idle"
    ? 0
    : effectivelyDone && settings.imagesEnabled && imagesDone
      ? 100
      : run.imagePlanned > 0
        ? (run.imageDone / run.imagePlanned) * 100
        : !running && run.imagePlanned === 0
          ? 100
          : 0;
```
最后两个分支：`!running && imagePlanned === 0 → 100`，否则 `→ 0`。**「运行时 imagePlanned=0」仍然返回 0**，然后：

`ProgressPanel.tsx:121-132`
```tsx
  const bothEnabled = settings.textEnabled && settings.imagesEnabled;
  const batchPercent = run.phase === "idle"
    ? 0
    : effectivelyDone && imagesDone
      ? 100
      : bothEnabled
        ? (textPercent + imagePercent) / 2
        : settings.imagesEnabled
          ? imagePercent
          : run.total > 0
            ? (run.done / run.total) * 100
            : 0;
```
`bothEnabled && imagesEnabled && imagePlanned === 0 && running` → `(textPercent + 0) / 2`。**如果文字全做完（textPercent=100），批次进度 = 50%。CLAUDE.md 描述的缺陷依然存在。**

第 115-120 行的注释：
```tsx
  // 批次进度 = 文字和图片都完成才到100：
  //   - idle 态 → 0%
  //   - 已完成态且图片也完成 → 直接 100%
  //   - 两者都开启：平均
```
注释没有提及 `imagePlanned === 0` 时的语义。而代码里 `imagesDone` 的计算（第 98-100 行）**已经处理了这种情况**：
```tsx
  const imagesDone = !settings.imagesEnabled
    || (run.imagePlanned > 0 && run.imageDone >= run.imagePlanned)
    || run.imagingComplete === true; // 图片线程明确完成（含提交 0 张的情况）
```
`imagesDone` 有 `imagingComplete` 兜底（当图片线程明确完成、即使 0 张），**但 `imagePercent` 没有用 `imagesDone`**。两者用不同的判据。

- **性质：**补丁叠补丁 —— CLAUDE.md 记录了这个 bug，某次修了 `imagesDone`（加 `imagingComplete`），但 `imagePercent` 的类似分支没同步修。
- **影响：**当商品不需要图片翻译（`imagePlanned=0`）且 `imagingComplete` 尚未被后端置位时，批次进度显示 50%。**这正是 CLAUDE.md 里那条已知缺陷。它没有真正修好。**
- **根因修复方案：**
  1. `imagePercent` 改用 `imagesDone` 作为权威判据：
     ```ts
     const imagePercent = !settings.imagesEnabled ? 0
       : imagesDone ? 100
       : run.imagePlanned > 0 ? (run.imageDone / run.imagePlanned) * 100
       : 0;
     ```
  2. `batchPercent` 在 `!settings.imagesEnabled` 时不应参与平均 —— 现有代码的第 128-132 行已经处理，但 `bothEnabled` 的判断漏了「图片开启但无需翻译」的第三态。
  3. 补一个单测钉死：`{textEnabled:true, imagesEnabled:true, imagePlanned:0, imagingComplete:true, done:total}` → `batchPercent === 100`。

---

## Query Key 全清单

> 「失效时机」列只列显式 `invalidateQueries` 的位置；所有 `queryKeys.X.all` 前缀失效会命中该族全部变体（TanStack Query 的 prefix match）。
> 「是否有遗漏」列标 ⚠️ 的，是本报告已确认的问题。

| queryKey | 用在哪 (文件:行) | 失效时机 | 是否有遗漏 |
|---|---|---|---|
| `["auth","status"]` | `queries.ts:136` | `connect`(338)、`initialize-credentials`(267)、`violateWords`(1916)、`PublishSettingsPanel.tsx:23`；`useSelectAccount` 直接 `setQueryData`(358) | 否。但 `useSelectAccount` 的 `setQueryData` 在 `cancelQueries` 后又 `invalidateQueries(auth.all)`(370)，**自相抵消**（先写后失效 → 立刻重取） |
| `["auth","credentials-status"]` | `queries.ts:224` | 无显式失效 | ⚠️ 密码初始化后不刷新，靠 `staleTime: 60_000` 到期 |
| `["shops", accountId, {refresh}]` | `queries.ts:320` | `connect`(339)、`select-account`(370) | ⚠️ `refresh` 段从未被传 `true`（D-P2-5）；`Register`/`Operator` 变更不失效 |
| `["collect-box", params]` | `queries.ts:959` | `connect`(341)、`select-account`(371)、`assignSiteItems`(1056)、`processCollectItem`(1078)、`WarehousePage.tsx:137` | 否。轮询 30s 恒开（D-P1-3） |
| `["collect-box", accountId, detailId, shopId]` | `queries.ts:1003` | 仅靠 `collectBox.all` 前缀 | 否 |
| `["warehouse", params]` | `queries.ts:420` | 26 处 `warehouse.all` 失效 | ⚠️ `q` 恒 `null`（D-P0-3）；`WarehouseQuery`/`WarehouseListParams` 双定义 |
| `["warehouse","automation-results",accountId,page]` | `queries.ts:453` | `warehouse.all` 前缀（含 `import`/`delete`/`process`） | ⚠️ `pageSize: 24` 硬编码在 `queryFn`(455) 不在 key，改页大小不会产生新 key |
| `["warehouse", accountId, id, {includePayload}]` | `queries.ts:506` | `warehouse.all` 前缀 + **2 处死键**（567、627） | ⚠️ D-P0-1 |
| `["groups"]` | `queries.ts:847` | 10 处 | 否 |
| `["categories", params]` | `queries.ts:913` | `connect`(342)、`select-account`(371) | 否（`staleTime: 5min`） |
| `["image-jobs"]`（族根） | — | `uploadImageJob`(1573)、`translateImages`(1606)、`reconcileImages`(1633) | — |
| `["image-jobs","list",params,{pageSize}]` | `queries.ts:1535` | 同上 | ⚠️ 三层 key；`pageSize` 在第三层（D-P0-2） |
| `["image-jobs", accountId, id]` | **无使用者** | — | ⚠️ 死定义（D-P1-6）；且与 `list` 共用第二层，语义冲突 |
| `["images","usage"]` | `queries.ts:1454` | `verifyPaid`(1509)、`translate`(1608)、`reconcile`(1634) | ⚠️ 硬编码字面量，不走工厂；`ImageServiceCard` 无失效路径 |
| `["images","verify"]` | `queries.ts:1474` | **无失效** | ⚠️ 注释(1470)说「由 UI 在用户点『重新检查』时主动失效」，实际 `ImageServiceCard.tsx:148` 用 `verify.refetch()`。注释与实现不符（refetch 绕过 staleTime，但也不更新其他观察者） |
| `["publish-logs", params, {pageSize}]` | `queries.ts:1707` | `publish`(1688)、`refreshPublishLogs`(1745)、`runSchedule`(1833)、`select-account`(372) | ⚠️ 三层 key；`pageSize` 在第三层 |
| `["schedules", params, {page:1}]` | `queries.ts:1760` | `create`(1782)、`patch`(1808)、`delete`(1819)、`run`(1832)、`select-account`(372) | ⚠️ `{page:1}` 是冗余常量尾巴；`refetchInterval` 缺 `InBackground:false` |
| `["schedule-logs", taskId??"", accountId??"", limit??100]` | `queries.ts:1669` | **无失效** | ⚠️ D-P2-4；`schdeule` 的 create/delete **不失效 logs** → 删了任务历史还在 |
| `["violation-words", params]` | `queries.ts:1850` | `mutateWords`(1915) | 否 |
| `["system","version",{refresh}]` | `queries.ts:1351` | `downloadUpdate`(1424，用 `versionAll` 前缀) | 否。这个族的注释（`queryKeys.ts:71-83`）**是本仓最好的 bug 复盘文档** |
| `["system","version"]`（族根） | `queryKeys.ts:86` | `downloadUpdate`(1424) | 否 |
| `["system","update-status"]` | `queries.ts:1393`（`UPDATE_STATUS_KEY`） | `downloadUpdate`(1425) | ⚠️ 定义在 `queries.ts:1380` 而非 `queryKeys.ts`，注释自认「刻意」（1377-1379） |
| `["system","templates", accountId]` | `queries.ts:1949` | `saveTemplate`(1977)、`selectTemplate`(1999)、`deleteTemplate`(2017)、`select-account`(373)、`WarehousePage.tsx:486`(`removeQueries`) | 否。但 4 处用**字面量** `["system","templates"]` 而非 `queryKeys.system.templates()` |
| `["system","settings", operatorId]` | `queries.ts:2116`（`workflowDefaultsQueryKey`） | `saveWorkflowDefaults` → `setQueryData`(2137) | ⚠️ 无 `onError`（D-P1-4）；`PublishSettingsPanel.tsx:24` 用字面量 `["system","settings"]` 失效全族（会连带失效 workflowDefaults，正确但浪费） |
| `["system","health"]` | **无使用者** | — | ⚠️ 死定义 |
| `["carrier","links"]` | `queries.ts:2255` | `add`(2280)、`remove`(2295) | 否 |
| `["titles","preview",key]` | **无使用者** | — | ⚠️ 死定义（`useTitlePreview` 是 mutation） |
| `["batch", {accountId,limit}]` | `queries.ts:2332` | 6 处 `batch.all` | ⚠️ 消费者 `BatchPanel` 无人引用（D-P0-4） |
| `["batch","detail",accountId,id]` | `queries.ts:2350` | `create`(2371 setQueryData)、`step`(2396)、`control`(2433)、`retry`(2459)、`delete`(2471)、`batch.all` 前缀 | ⚠️ 同上 |
| `["claim-jobs", operatorId??"local", accountId, "list"]` | `WarehousePage.tsx:122`、`ClaimJobsPanel` | `delete`(ClaimJobsPanel:65)、`claimSelected`(WarehousePage:466)、`onJobsCreated`(1278) | 否。`operatorId ?? "local"` 与 `queryKeys.claimJobs.list` 一致 |
| `["claim-jobs", operatorId??"local", accountId, "detail", id]` | `ClaimJobsPanel.tsx:127` | `claimSelected`(WarehousePage:465 setQueryData) | 否。轮询条件正确（`pending`/`running` → 3s，否则 false） |
| `["automation-runs","latest",accountId,shopId]` | `queries.ts:1088` | 8 处 `automationRuns.all` | 否（`staleTime: 0` 强制新鲜） |
| `["automation-runs","history",accountId,shopId,limit]` | `queries.ts:1136` | 同上 | 否 |
| `["sync","price-templates",params]` | **无使用者** | `syncSourceLinks`(2229) 失效 `sync.all` | ⚠️ 死定义；`useSyncPriceTemplates` 是 mutation，`sync.all` 失效无对象 |
| `["operators","list"]` | `queries.ts:2493` | 10 处 `operators.all` | ⚠️ `App.tsx:95` **直接 `api.get("/operators")` 不走 query**，与这里并行两套数据源 |
| `["operator-session", token]` | `features/auth/guard.ts:8` | **无失效** | ⚠️ 不走工厂；`App.tsx:190` 每路由挂载跑一次（`staleTime:0`），与 `useOperators` 重复请求同一个 `/operators` |
| `["audit-logs", params]` | `queries.ts:2614` | `renameOperator`(2536)、`OperatorsPanel.tsx:136` | ⚠️ `limit/before` 的 null 归一化与 `compactQuery` 不一致（D-P1-8） |
| `["size-charts", operatorId??"current"]` | `useSizeCharts.ts:44` | 3 处 `sizeChartsKey` | ⚠️ 完全绕过 `queryKeys` 工厂（D-P2-1） |

**统计：**
- `queryKeys.ts` 定义 22 族 / 30 个工厂函数
- **4 个工厂 0 引用**（死定义）
- **5 处 query 的 key 完全绕过工厂**（`useScheduleLogs`、`useSizeCharts`、`UPDATE_STATUS_KEY`、`["images","usage"]`、`["images","verify"]`、`["operator-session",token]` —— 实为 6 处）
- **11 处 `invalidateQueries` 使用硬编码字面量**（全仓 105 处调用），其中 **2 处是死键**
- **3 处 query 的 key 尾部挂冗余对象**（`publishLogs`、`schedules`、`imageJobs`）

---

## 建议的回归测试

> 以下测试按「能抓到本次审计发现的具体缺陷」设计，不是泛泛的覆盖率提升。

### 1. Key 契约测试（抓 D-P0-1 / D-P0-2 / D-P1-6）

```ts
// web/src/lib/queryKeys.contract.test.mjs
test("每个 queryKey 工厂至少有一个消费者", () => { ... });
// 用 TS AST 扫 web/src/**/*.{ts,tsx}，收集 queryKeys.*.* 的引用；
// 与 queryKeys.ts 导出的工厂集合做差集，差集必须为空。

test("invalidateQueries 的 queryKey 必须能匹配到至少一个 useQuery 的 key", () => { ... });
// 静态分析：收集所有 invalidateQueries({queryKey:[...]}) 的字面量，
// 与所有 useQuery({queryKey: ...}) 的静态可解析 key 做前缀匹配。
// 这条能直接抓到 ["product-detail", id] 和 ["productDetail", id]。
```

### 2. Key ↔ Request 双射测试（抓 D-P1-8 / D-P0-2）

```ts
test("每个 hook 的 queryKey 与 queryFn 的 query 参数一一对应", () => {
  // 用 mock api.get 捕获实际请求参数，与 queryKey 的归一化结果比对：
  // 对 usePublishLogs({page:1, pageSize:20, status:null}) 断言
  //   key    === ["publish-logs", {accountId, page:1, status:undefined, shopId:undefined}, {pageSize:20}]
  //   params === {page:1, pageSize:20, accountId}   // status 被 compactQuery 丢弃
  // 断言：key 里出现的每个筛选字段，要么在 params 里，要么是 null（明确不传）。
});
```

### 3. 详情页三套 key 消失测试（抓 D-P0-1）

```ts
test("patchProduct 之后，useProductDetail 的缓存被标记为失效", async () => {
  const client = new QueryClient();
  const key = queryKeys.warehouse.detail("acc-1", "42", true);
  client.setQueryData(key, { product: { id: "42", revision: 1 } });
  // 执行 usePatchProduct 的 onSuccess
  await patchProductMutation.onSuccess({ product: { id: "42", revision: 2 } }, { id: "42" });
  assert.ok(client.getQueryState(key)?.isInvalidated);
});
// 当前这条会通过（因为 warehouse.all 前缀），但它能防止将来有人"优化"成精确失效时打空。
```

### 4. `assignedSiteRef` 跨账号测试（抓 D-P0-5）

```ts
test("切换到新妙手账号后，自动分配会重新触发一次", async () => {
  // 1. 挂载 AutomationPage，status.accountId = "acc-A"，列表无 site 商品 → 不调用 assign
  // 2. 切到 status.accountId = "acc-B"，列表有 3 件 site 商品
  // 3. 断言 assignSiteItems.mutateAsync 被调用一次，参数 targetShopId = B 的店铺
});
```

### 5. 幽灵功能可达性测试（抓 D-P0-4）

```ts
test("仓库页工具栏能打开所有已渲染的 Dialog", async () => {
  render(<WarehousePage />);
  // 对每个已渲染的 Dialog 的 title，断言能在页面上找到触发按钮
  // 或断言 Dialog 根本不该渲染。
  // 这条会抓到 TransformDialog / PricePreviewDialog / CreateBatchDialog。
});
```

### 6. `batchPercent` 边界测试（抓 D-P2-8）

```ts
test.each([
  // textEnabled, imagesEnabled, imagePlanned, imagingComplete, done, total, 期望
  [true,  true,  0, true,  10, 10, 100],  // 当前会得到 50 —— CLAUDE.md 已知缺陷
  [true,  true,  0, false, 10, 10,  50],  // 图片线程未完成，50 是正确语义
  [true,  true,  9, true,  10, 10, 100],
  [true,  false, 0, false, 10, 10, 100],
  [false, true,  0, true,   0,  0, 100],
  [false, true,  0, false,  0,  0,   0],
])("batchPercent(%s,%s,planned=%s,imagingComplete=%s) === %s", ...);
```

### 7. 错误文案不吞测试（抓 D-P1-1）

```ts
test("apiErrorMessage 始终优先返回后端 message", () => {
  assert.equal(apiErrorMessage(new ApiError({ code: "VALIDATION_ERROR", message: "商品标题超过 255 字" }, 400)),
               "商品标题超过 255 字");
  assert.equal(apiErrorMessage(new ApiError({ code: "INTERNAL_ERROR", message: "" }, 500)),
               "服务内部错误，请查看日志");
});

test("后端 errors.py 的 code 集合 ⊆ 前端 API_ERROR_TEXT 的 key 集合", () => {
  // 解析 server/core/errors.py 的正则 `^([A-Z_]+) = "`，与 API_ERROR_TEXT 比对。
});
```

### 8. 轮询停止条件测试（抓 D-P1-3）

```ts
test("useCollectBoxList 在无进行中任务时不轮询", () => {
  // 用 fake timers，注入 data.items 全为终态
  // 断言 60 秒后 queryFn 只被调用 1 次（挂载时的 refetchOnMount）
});
```

### 9. 缓存清理一致性测试（抓 D-P1-9）

```ts
test("clearOperatorProductListCache 同时清除列表和详情", async () => {
  client.setQueryData(["warehouse", { accountId: "a", page: 1 }], {...});
  client.setQueryData(queryKeys.warehouse.detail("a", "42", true), {...});
  await clearOperatorProductListCache(client);
  assert.equal(client.getQueryData(["warehouse", { accountId: "a", page: 1 }]), undefined);
  assert.equal(client.getQueryData(queryKeys.warehouse.detail("a", "42", true)), undefined);
  // 当前第二条会失败 —— filter 只匹配 queryKey[1] 是 object 的条目。
});
```

### 10. Store 重置完整性测试（抓 D-P1-7）

```ts
test("resetOperatorSessionUiState 把所有 store 恢复成初始 state", () => {
  const initial = {
    warehouse: useWarehouseStore.getState(),
    words: useWordsStore.getState(),
    logs: usePublishLogsStore.getState(),
  };
  // 改动所有字段
  useWarehouseStore.setState({ sourceStatus: "missing", claimed: true, page: 7, selected: ["x"] });
  resetOperatorSessionUiState();
  const after = useWarehouseStore.getState();
  // 断言每个 state 字段都回到 initial（函数字段除外）
  // 当前 sourceStatus 会失败。
});
```

---

## 附：本次审计的方法与边界

**做了什么：**
- 通读 `queries.ts`（2 637 行）、`queryKeys.ts`、`api.ts`、`types.ts`（部分）、`main.tsx`、`App.tsx`、`stores.ts`、6 个 lib/*.mjs
- 通读 `WarehousePage`（1 286 行）、`BatchPanel`（833 行）、`ClaimJobsPanel`、`ProgressPanel`、`ProductDetailPage`（部分）、`ProductDetailDrawer`（部分）、`SourceLinkCard`、`PublishDialog`（部分）、`SchedulesPage`（部分）、`UnlockPage`（部分）、`OperatorsPanel`（部分）、`AutomationPage`（部分，约 800/2 190 行）
- 全仓 grep：`queryKey:`、`invalidateQueries`、`refetchInterval`、`setInterval`、`setTimeout`、`onMutate`、`onError`、`setQueryData`、`as unknown as`、`@ts-ignore`、`any`、`Virtual`、`removeQueries`
- 交叉核对后端：`server/api/warehouse.py`、`server/api/system.py`、`server/api/claim_jobs.py`、`server/services/batch_service.py`、`server/services/schedule_service.py`、`server/core/errors.py`、`server/main.py`、`server/api/deps.py`

**统计结果：**
- `any`：**4 处**，全部集中在 `features/automation/types.ts:154-168` 的 `(merged as any)[key]` 动态赋值
- `@ts-ignore` / `@ts-expect-error`：**0 处**
- `as unknown as`：**3 处**（`SourceLinkCard.tsx:141`、`api.ts:382`、`clipboard.ts:29`）
- 非空断言 `!.`：**6 处**（`AutomationPage.tsx:1080 savedRun!`、`RunHistoryPanel.tsx:133,136 run.issues!`、`ProductPickerDialog.tsx:116 groups!`、`ProductDetailPage.tsx:130 payload!`、`ClaimJobsPanel.tsx:128 expanded!`）
- `.mutate(`：**89 处**；`onError`：**119 处**；`invalidateQueries`：**105 处**（11 处字面量）
- `useQuery` 调用：**32 处**（28 处在 `queries.ts`，4 处在 features：`guard.ts:7`、`useSizeCharts.ts:43`、`ClaimJobsPanel.tsx:126`、`WarehousePage.tsx:121`）
- `useMutation` 调用：**75 处**，全在 `queries.ts`
- `queries.ts` 导出 **103 个 `use*` 函数** + **56 个类型声明**
- `React.memo` / `memo()`：**0 处**

**类型安全结论**：这是**罕见地干净**。4 个 `any` / 0 个 `@ts-ignore` / 3 个 `as unknown as` 在 36k 行里属于极低水平。**类型安全不是本项目的风险点** —— 风险在「类型定义的位置混乱」（D-P0-3 / D-P1-5 / D-P2-1）而非「类型被绕过」。

**没做（明确边界）：**
- 未运行代码、未运行测试、未启动应用。所有结论来自静态阅读。
- `AutomationPage.tsx`（106 KB / 2 190 行）只读了约 35%。这是一份独立审计的工作量，建议单列 D-plus 审计。
- `UnlockPage.tsx`（43 KB）、`SchedulesPage.tsx`（78 KB）、`OperatorsPanel.tsx`（52 KB）、`ProcessSettings.tsx`（34 KB）、`PriceTemplateEditor.tsx`（33 KB）、`ProductDetailDrawer.tsx`（50 KB）、`ProductDetailPage.tsx`（45 KB）均只读了关键片段。
- 未做竞态条件的动态验证（需要 React DevTools / 网络节流）；D-P0-5 和 D-P1-4 的竞态判断基于代码结构而非实测。
- 未审计 `app/`（Electron 主进程）与 `server/`。
