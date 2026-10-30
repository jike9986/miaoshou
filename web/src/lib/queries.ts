// architecture §4.6 前端状态分层 —— TanStack Query 封装（api.md §0.4/§0.5）
// 业务页只从这里取 hook：queryKey 统一走 @/lib/queryKeys，错误统一走 ApiError。

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";
import { api, isApiError, type QueryParams } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import type {
  AuditLogQuery,
  AuditLogsResponse,
  AutomationResultsImportResponse,
  AutomationResultsResponse,
  AutomationRunCreateRequest,
  AutomationPublishConfirmation,
  AutomationRunRecord,
  AuthConnectRequest,
  AuthConnectResult,
  AuthStatus,
  Batch,
  BatchCategoryRequest,
  BatchCategoryResponse,
  BatchControlAction,
  BatchControlResponse,
  BatchCreateRequest,
  BatchDeleteResult,
  BatchDetailResponse,
  BatchListResponse,
  BatchRetryResponse,
  BatchStepResponse,
  CategoryTreeResponse,
  ClaimRequest,
  ClaimResult,
  CollectBoxDetail,
  CollectBoxPage,
  CollectBoxStatus,
  Group,
  GroupCreateRequest,
  GroupDeleteResponse,
  GroupsResponse,
  ImageJobsResponse,
  ImageJobUploadResponse,
  ImageTranslateRequest,
  ImageTranslateResponse,
  ImageVerifyPaidResponse,
  ImageVerifyResponse,
  Operator,
  OperatorCreateRequest,
  OperatorPatchRequest,
  OperatorsResponse,
  OperatorTokenResponse,
  SoftwareLoginResponse,
  StudentCreateRequest,
  StudentCreateResponse,
  ProcessCollectRequest,
  ProcessCollectResult,
  ProductDetailResponse,
  ProductPatchRequest,
  ProductQualityReviewRequest,
  ProductPatchResponse,
  ProductSort,
  ProductStatus,
  QualityReviewStatus,
  PublishLogsResponse,
  PublishRequest,
  PublishResponse,
  Schedule,
  ScheduleCreateRequest,
  ScheduleDeleteResponse,
  SchedulePatchRequest,
  ScheduleRunResult,
  SchedulesResponse,
  ShopsResponse,
  UpdateDownloadRequest,
  UpdateDownloadResult,
  ViolationWord,
  ViolationWordsResponse,
  WarehouseResponse,
  WordAction,
  WordCategory,
} from "@/lib/types";

/* ================================================================== *
 * 通用类型
 * ================================================================== */

interface PageParams {
  page: number;
  pageSize: number;
}

/** 生成幂等键（api.md §0.3：仅 publish / images / schedules 支持） */
export function idempotencyKey(prefix: string): string {
  const random =
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID().replace(/-/g, "").slice(0, 18)
      : Math.random().toString(36).slice(2, 14);
  return `${prefix}-${random}`;
}

function compactQuery(query: Record<string, unknown>): QueryParams {
  const result: QueryParams = {};
  for (const [key, value] of Object.entries(query)) {
    if (value === null || value === undefined || value === "") continue;
    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
      result[key] = value;
    }
  }
  return result;
}

/* ================================================================== *
 * §1 认证 / §2 店铺
 * ================================================================== */

export function useAuthStatus(enabled = true): UseQueryResult<AuthStatus> {
  return useQuery({
    queryKey: queryKeys.auth.status(),
    queryFn: () => api.get<AuthStatus>("/auth/status"),
    enabled,
    staleTime: 10_000,
  });
}

export function useSoftwareLogin(): UseMutationResult<
  SoftwareLoginResponse,
  Error,
  { username: string; password: string }
> {
  return useMutation({
    mutationFn: (body) => api.post<SoftwareLoginResponse, typeof body>("/auth/login", body),
  });
}

export function useSoftwareCredentialsStatus(enabled = true): UseQueryResult<{ initialized: boolean }> {
  return useQuery({
    queryKey: [...queryKeys.auth.all, "credentials-status"],
    queryFn: () => api.get<{ initialized: boolean }>("/auth/credentials-status"),
    enabled,
    staleTime: 60_000,
  });
}

export function useChangeSoftwarePassword(): UseMutationResult<
  { ok: true; token: string; mustChangePassword: false },
  Error,
  { currentPassword: string; newPassword: string }
> {
  return useMutation({
    mutationFn: (body) => api.post<
      { ok: true; token: string; mustChangePassword: false },
      typeof body
    >("/auth/change-password", body),
  });
}

export function useInitializeSoftwareCredentials(): UseMutationResult<
  SoftwareLoginResponse,
  Error,
  { username: string; password: string }
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body) => api.post<SoftwareLoginResponse, typeof body>("/auth/initialize-credentials", body),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.operators.all });
      void client.invalidateQueries({ queryKey: queryKeys.auth.all });
    },
  });
}

export function useCreateStudent(): UseMutationResult<StudentCreateResponse, Error, StudentCreateRequest> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body) => api.post<StudentCreateResponse, StudentCreateRequest>("/operators/students", body),
    onSuccess: () => void client.invalidateQueries({ queryKey: queryKeys.operators.all }),
  });
}

export function useResetStudentPassword(): UseMutationResult<
  { ok: true },
  Error,
  { id: string; password: string; username?: string }
> {
  return useMutation({
    mutationFn: ({ id, ...body }) =>
      api.post<{ ok: true }, Omit<typeof body, "id">>(`/operators/${encodeURIComponent(id)}/reset-password`, body),
  });
}

export function useShops(
  accountId?: string | null,
  options: { refresh?: boolean; enabled?: boolean } = {}
): UseQueryResult<ShopsResponse> {
  const refresh = options.refresh ?? false;
  const enabled = options.enabled ?? true;
  return useQuery({
    queryKey: [...queryKeys.shops.list(accountId), { refresh }],
    queryFn: () =>
      api.get<ShopsResponse>("/shops", {
        query: compactQuery({ accountId, refresh: refresh ? true : undefined }),
        timeoutMs: 60_000,
      }),
    enabled,
    staleTime: 60_000,
  });
}

export function useConnectAccount(): UseMutationResult<AuthConnectResult, Error, AuthConnectRequest> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: AuthConnectRequest) =>
      api.post<AuthConnectResult, AuthConnectRequest>("/auth/connect", body, { timeoutMs: 60_000 }),
    onSuccess: () => {
      // 连接会改变账号、店铺、类目缓存与全部业务列表的可见范围
      void client.invalidateQueries({ queryKey: queryKeys.auth.all });
      void client.invalidateQueries({ queryKey: queryKeys.shops.all });
      void client.invalidateQueries({ queryKey: queryKeys.warehouse.all });
      void client.invalidateQueries({ queryKey: queryKeys.collectBox.all });
      void client.invalidateQueries({ queryKey: queryKeys.categories.all });
    },
  });
}

/* ================================================================== *
 * §4 成品仓库
 * ================================================================== */

export interface WarehouseListParams extends PageParams {
  categoryId?: string | null;
  cid?: string | null;
  groupId?: string | null;
  q?: string | null;
  accountId?: string | null;
  status?: ProductStatus | null;
  qualityStatus?: QualityReviewStatus | null;
  claimed?: boolean | null;
  sort?: ProductSort;
}

export function warehouseQueryKey(params: WarehouseListParams): readonly unknown[] {
  return queryKeys.warehouse.list({
    page: params.page,
    pageSize: params.pageSize,
    categoryId: params.categoryId ?? null,
    cid: params.cid ?? null,
    groupId: params.groupId ?? null,
    q: params.q ?? null,
    accountId: params.accountId ?? null,
    status: params.status ?? null,
    qualityStatus: params.qualityStatus ?? null,
    claimed: params.claimed ?? null,
    sort: params.sort,
  });
}

export function useWarehouseList(params: WarehouseListParams): UseQueryResult<WarehouseResponse> {
  return useQuery({
    queryKey: warehouseQueryKey(params),
    queryFn: () =>
      api.get<WarehouseResponse>("/warehouse", {
        query: compactQuery({
          page: params.page,
          pageSize: params.pageSize,
          categoryId: params.categoryId,
          cid: params.cid,
          groupId: params.groupId,
          q: params.q,
          accountId: params.accountId,
          status: params.status,
          qualityStatus: params.qualityStatus,
          claimed: params.claimed,
          sort: params.sort,
        }),
      }),
    // 保持上一页数据，翻页时列表不闪成骨架
    placeholderData: (previous) => previous,
  });
}

export function useAutomationResults(page: number, enabled = true): UseQueryResult<AutomationResultsResponse> {
  return useQuery({
    queryKey: queryKeys.warehouse.automationResults(page),
    queryFn: () => api.get<AutomationResultsResponse>("/warehouse/automation-results", {
      query: { page, pageSize: 24 },
    }),
    enabled,
    placeholderData: (previous) => previous,
  });
}

export function useImportAutomationResults(): UseMutationResult<
  AutomationResultsImportResponse,
  Error,
  string[]
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (ids) => api.post<AutomationResultsImportResponse, { ids: string[] }>(
      "/warehouse/automation-results/import", { ids }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.warehouse.all });
    },
  });
}

export function useProductDetail(
  id: string | null,
  includePayload = false
): UseQueryResult<ProductDetailResponse> {
  return useQuery({
    queryKey: queryKeys.warehouse.detail(id ?? "none", includePayload),
    queryFn: () =>
      api.get<ProductDetailResponse>(`/warehouse/${encodeURIComponent(id ?? "")}`, {
        query: includePayload ? { includePayload: true } : undefined,
      }),
    enabled: Boolean(id),
  });
}

export function usePatchProduct(): UseMutationResult<
  ProductPatchResponse,
  Error,
  { id: string; patch: ProductPatchRequest }
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }) =>
      api.patch<ProductPatchResponse, ProductPatchRequest>(
        `/warehouse/${encodeURIComponent(id)}`,
        patch
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.warehouse.all });
      void client.invalidateQueries({ queryKey: queryKeys.groups.all });
    },
  });
}

export function useReviewProductQuality(): UseMutationResult<
  { id: string; revision: number },
  Error,
  { id: string; review: ProductQualityReviewRequest }
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, review }) =>
      api.patch<{ id: string; revision: number }, ProductQualityReviewRequest>(
        `/warehouse/${encodeURIComponent(id)}/quality-review`, review
      ),
    onSuccess: (_result, variables) => {
      void client.invalidateQueries({ queryKey: queryKeys.warehouse.all });
      void client.invalidateQueries({
        queryKey: queryKeys.warehouse.detail(variables.id, true),
      });
    },
  });
}

export interface DeleteProductResult {
  deleted: boolean;
  id: string;
  filesRemoved: number;
}

export function useDeleteProduct(): UseMutationResult<DeleteProductResult, Error, string> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      api.del<DeleteProductResult>(`/warehouse/${encodeURIComponent(id)}`),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.warehouse.all });
      void client.invalidateQueries({ queryKey: queryKeys.groups.all });
    },
  });
}

export function useBatchCategory(): UseMutationResult<
  BatchCategoryResponse,
  Error,
  BatchCategoryRequest
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: BatchCategoryRequest) =>
      api.post<BatchCategoryResponse, BatchCategoryRequest>("/warehouse/batch-category", body),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.warehouse.all });
      void client.invalidateQueries({ queryKey: queryKeys.groups.all });
    },
  });
}

export interface BatchDeleteResponse {
  deleted: string[];
  requested: number;
  deletedCount: number;
  filesRemoved: number;
  failures: Array<{ id: string; code?: string; reason: string }>;
}

/* ------------------------------------------------------------------ *
 * 价格试算（POST /warehouse/price-preview）
 *
 * 等价老版 /api/price-preview。只算价：不发布、不写库、不调上游。
 * 前端此前完全没有这个 hook，所以界面上「价格预览」等于不存在。
 * ------------------------------------------------------------------ */

export interface PricePreviewItemRef {
  /** 仓库主键 id */
  warehouseId?: string;
  /** 备选：warehouseKey */
  key?: string;
  /** 备选：采集箱 detailId + shopId */
  detailId?: string;
  shopId?: string;
}

export interface PricePreviewBody {
  items: PricePreviewItemRef[];
  /** 模板名；留空则用当前选中模板 */
  template?: string | null;
  /** 手工指定汇率；留空则按商品里记录的汇率 */
  exchangeRate?: string | number | null;
}

export interface PriceRange {
  min: string | null;
  max: string | null;
  currency: string;
}

export interface PricePreviewEntry {
  warehouseId: string;
  warehouseKey: string | null;
  title: string;
  detailId: string | null;
  shopId: string | null;
  purchasePrice: PriceRange;
  priceBefore: PriceRange;
  priceAfter: PriceRange;
  currency: string;
  templateName: string | null;
  templateApplied: boolean;
  rate: string | number | null;
  rateSource: string;
  weightKg: string | number | null;
  skuCount: number | null;
  changedCount: number | null;
  calculation: PriceCalculation;
}

/** 单件试算的费用拆分（display_price 的输出）。 */
export interface PriceCalculation {
  price?: number;
  currency?: string;
  rate?: number;
  rateSource?: string;
  buyInPrice?: number;
  lines?: Record<string, number>;
  /** 模板里没填的费率项；非空表示这个价是估算的 */
  missingRateFields?: string[];
  isEstimate?: boolean;
  freightAssumption?: string;
}

export interface PricePreviewWarning {
  index: number;
  code: string;
  message: string;
  title?: string;
}

export interface PricePreviewResponse {
  template: { name: string | null; site: string | null; sellingCurrency: string };
  exchangeRate: string | number | null;
  items: PricePreviewEntry[];
  total: number;
  repriced: number;
  skipped: number;
  warnings: PricePreviewWarning[];
}

/** 试算不改数据，所以不 invalidate 任何查询。 */
export function usePricePreview(): UseMutationResult<
  PricePreviewResponse,
  Error,
  PricePreviewBody
> {
  return useMutation({
    mutationFn: (body: PricePreviewBody) =>
      api.post<PricePreviewResponse, PricePreviewBody>("/warehouse/price-preview", body),
  });
}

/** 批量变换：改价 / 删图 / 清标题。默认只预览（dryRun），确认后才写库。 */
export interface TransformBody {
  ids: string[];
  title?: string;
  removeTitleWords?: string[];
  useWordLibrary?: boolean;
  priceMultiplier?: string | number;
  targetPrice?: string | number;
  targetPriceIncludeVat?: string | number;
  removeImageIndexes?: number[];
  removeImageUrls?: string[];
  dryRun?: boolean;
  /**
   * 逐件版本约束（id → revision）。带上了后端就会在任何一件被别人改过时整体回滚并回 409；
   * 只在真写库（dryRun=false）时带 —— 预览不写数据，没必要因为别人改了就让预览失败。
   */
  expectedRevisions?: Record<string, number>;
}

export interface TransformReport {
  title: { before: string; after: string } | null;
  prices: {
    changedCount: number;
    samples: Array<{
      skuKey: string;
      priceBefore: number;
      priceAfter: number;
      priceIncludeVatBefore: number | null;
      priceIncludeVatAfter: number | null;
    }>;
  } | null;
  images: {
    removedCount: number;
    removedUrls: string[];
    remainingCount: number;
  } | null;
  titleCheck?: {
    before: string;
    after: string;
    removedWords: string[];
    reviewWords: string[];
    needsReview: boolean;
  };
  warnings: string[];
  changed: boolean;
}

export interface TransformItem {
  index: number;
  id: number;
  warehouseId: string;
  warehouseKey: string;
  title: string;
  report: TransformReport;
  applied: boolean;
}

export interface TransformResponse {
  dryRun: boolean;
  items: TransformItem[];
  total: number;
  changedCount: number;
  warnings: Array<{ index: number; id: string; code: string; message: string }>;
}

export function useTransform(): UseMutationResult<TransformResponse, Error, TransformBody> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: TransformBody) =>
      api.post<TransformResponse, TransformBody>("/warehouse/transform", body),
    onSuccess: (result) => {
      // 只在真写库时刷新列表；预览不改数据，不必重拉
      if (!result.dryRun) {
        void client.invalidateQueries({ queryKey: queryKeys.warehouse.all });
      }
    },
  });
}

export function useBatchDelete(): UseMutationResult<BatchDeleteResponse, Error, string[]> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (ids: string[]) =>
      api.post<BatchDeleteResponse, { ids: string[] }>("/warehouse/batch-delete", { ids }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.warehouse.all });
      void client.invalidateQueries({ queryKey: queryKeys.groups.all });
    },
  });
}

/* ================================================================== *
 * §5 自定义分组
 * ================================================================== */

export function useGroups(enabled = true): UseQueryResult<GroupsResponse> {
  return useQuery({
    queryKey: queryKeys.groups.all,
    queryFn: () => api.get<GroupsResponse>("/groups"),
    enabled,
    staleTime: 60_000,
  });
}

export function useCreateGroup(): UseMutationResult<{ group: Group }, Error, GroupCreateRequest> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: GroupCreateRequest) =>
      api.post<{ group: Group }, GroupCreateRequest>("/groups", body),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.groups.all });
      void client.invalidateQueries({ queryKey: queryKeys.warehouse.all });
    },
  });
}

export function useDeleteGroup(): UseMutationResult<GroupDeleteResponse, Error, string> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.del<GroupDeleteResponse>(`/groups/${encodeURIComponent(id)}`),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.groups.all });
      void client.invalidateQueries({ queryKey: queryKeys.warehouse.all });
    },
  });
}

/* ================================================================== *
 * §6 类目
 * ================================================================== */

export interface CategoryTreeParams {
  site?: string | null;
  depth?: number;
  leafOnly?: boolean;
  rootCid?: string | null;
}

export function useCategoryTree(
  params: CategoryTreeParams,
  enabled = true
): UseQueryResult<CategoryTreeResponse> {
  return useQuery({
    queryKey: queryKeys.categories.tree({
      site: params.site ?? null,
      depth: params.depth,
      leafOnly: params.leafOnly,
      rootCid: params.rootCid ?? null,
    }),
    queryFn: () =>
      api.get<CategoryTreeResponse>("/categories", {
        query: compactQuery({
          site: params.site,
          depth: params.depth,
          leafOnly: params.leafOnly,
          rootCid: params.rootCid,
        }),
        timeoutMs: 60_000,
      }),
    enabled,
    staleTime: 5 * 60_000,
  });
}

/* ================================================================== *
 * §3 采集箱
 * ================================================================== */

export interface CollectBoxListParams extends PageParams {
  status: CollectBoxStatus;
  keyword?: string;
  shopId?: string | null;
  sort?: "default" | "savedDesc";
}

export function useCollectBoxList(
  params: CollectBoxListParams,
  enabled = true
): UseQueryResult<CollectBoxPage> {
  return useQuery({
    queryKey: queryKeys.collectBox.list({
      page: params.page,
      pageSize: params.pageSize,
      status: params.status,
      keyword: params.keyword ?? "",
      shopId: params.shopId ?? null,
    }),
    queryFn: () =>
      api.get<CollectBoxPage>("/collect-box", {
        query: compactQuery({
          page: params.page,
          pageSize: params.pageSize,
          status: params.status,
          keyword: params.keyword,
          shopId: params.shopId,
          sort: params.sort,
        }),
        timeoutMs: 60_000,
      }),
    enabled,
    placeholderData: (previous) => previous,
  });
}

export function useCollectBoxDetail(
  detailId: string | null,
  shopId: string | null
): UseQueryResult<CollectBoxDetail> {
  return useQuery({
    queryKey: queryKeys.collectBox.detail(detailId ?? "none", shopId ?? "none"),
    queryFn: () =>
      api.get<CollectBoxDetail>(`/collect-box/${encodeURIComponent(detailId ?? "")}`, {
        query: { shopId: shopId ?? "" },
        timeoutMs: 60_000,
      }),
    enabled: Boolean(detailId && shopId),
  });
}

export interface ClaimVariables {
  detailId: string;
  shopId: string;
  request: ClaimRequest;
}

export interface ProcessCollectVariables {
  detailId: string;
  request: ProcessCollectRequest;
}

/**
 * 处理入仓：POST /collect-box/{detailId}/process（拉详情→翻译→定价→违规词→入库）。
 * 这是采集箱商品进入成品仓库的唯一入口；save=false 时后端只预览并回滚。
 * 批量场景请由调用方串行 await，避免后端并发写库。
 */
export function useProcessCollectItem(): UseMutationResult<
  ProcessCollectResult,
  Error,
  ProcessCollectVariables
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ detailId, request }: ProcessCollectVariables) =>
      api.post<ProcessCollectResult, ProcessCollectRequest>(
        `/collect-box/${encodeURIComponent(detailId)}/process`,
        request,
        { timeoutMs: 120_000 }
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.collectBox.all });
      void client.invalidateQueries({ queryKey: queryKeys.warehouse.all });
      void client.invalidateQueries({ queryKey: queryKeys.groups.all });
    },
  });
}

export function useLatestAutomationRun(accountId: string | null, shopId: string | null,
                                       enabled = true): UseQueryResult<{ run: AutomationRunRecord | null }> {
  return useQuery({
    queryKey: queryKeys.automationRuns.latest(accountId, shopId),
    queryFn: () => api.get<{ run: AutomationRunRecord | null }>("/automation-runs/latest", {
      query: { accountId, shopId },
    }),
    enabled: Boolean(accountId) && enabled,
    staleTime: 0,
  });
}

function invalidateAutomationRuns(client: ReturnType<typeof useQueryClient>): void {
  void client.invalidateQueries({ queryKey: queryKeys.automationRuns.all });
}

export function useCreateAutomationRun(): UseMutationResult<
  { run: AutomationRunRecord }, Error, AutomationRunCreateRequest
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body) => api.post<{ run: AutomationRunRecord }, AutomationRunCreateRequest>(
      "/automation-runs", body),
    onSuccess: () => invalidateAutomationRuns(client),
  });
}

export function useRecoverAutomationRun(): UseMutationResult<
  { run: AutomationRunRecord }, Error, string
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id) => api.post<{ run: AutomationRunRecord }>(
      `/automation-runs/${encodeURIComponent(id)}/recover`),
    onSuccess: () => invalidateAutomationRuns(client),
  });
}

export function useStartAutomationRunItem(): UseMutationResult<
  { run: AutomationRunRecord }, Error, { runId: string; itemId: string }
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ runId, itemId }) => api.post<{ run: AutomationRunRecord }>(
      `/automation-runs/${encodeURIComponent(runId)}/items/${encodeURIComponent(itemId)}/start`),
    onSuccess: () => invalidateAutomationRuns(client),
  });
}

export type AutomationItemOutcome = "succeeded" | "failed" | "needs_review";

export function useAutomationRunProcessResult(): UseMutationResult<
  { run: AutomationRunRecord }, Error,
  { runId: string; itemId: string; status: AutomationItemOutcome; reason?: string;
    productId?: string | null; result?: Record<string, unknown> }
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ runId, itemId, ...body }) => api.post<{ run: AutomationRunRecord }>(
      `/automation-runs/${encodeURIComponent(runId)}/items/${encodeURIComponent(itemId)}/process-result`, body),
    onSuccess: () => invalidateAutomationRuns(client),
  });
}

export function useStartAutomationPublish(): UseMutationResult<
  { run: AutomationRunRecord }, Error,
  { runId: string; itemId: string } & Partial<AutomationPublishConfirmation>
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ runId, itemId, ...body }) => api.post<{ run: AutomationRunRecord }, typeof body>(
      `/automation-runs/${encodeURIComponent(runId)}/items/${encodeURIComponent(itemId)}/publish-start`, body),
    onSuccess: () => invalidateAutomationRuns(client),
  });
}

export function useResumeAutomationPublish(): UseMutationResult<
  { run: AutomationRunRecord }, Error,
  { runId: string; itemId: string } & Partial<AutomationPublishConfirmation>
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ runId, itemId, ...body }) => api.post<{ run: AutomationRunRecord }, typeof body>(
      `/automation-runs/${encodeURIComponent(runId)}/items/${encodeURIComponent(itemId)}/resume-publish`, body),
    onSuccess: () => invalidateAutomationRuns(client),
  });
}

export function useRetryAutomationPublish(): UseMutationResult<
  { run: AutomationRunRecord }, Error, { runId: string; itemId: string }
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ runId, itemId }) => api.post<{ run: AutomationRunRecord }>(
      `/automation-runs/${encodeURIComponent(runId)}/items/${encodeURIComponent(itemId)}/retry-publish`),
    onSuccess: () => invalidateAutomationRuns(client),
  });
}

export function useAutomationPublishResult(): UseMutationResult<
  { run: AutomationRunRecord }, Error,
  { runId: string; itemId: string; status: AutomationItemOutcome; reason?: string;
    result?: Record<string, unknown> }
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ runId, itemId, ...body }) => api.post<{ run: AutomationRunRecord }>(
      `/automation-runs/${encodeURIComponent(runId)}/items/${encodeURIComponent(itemId)}/publish-result`, body),
    onSuccess: () => invalidateAutomationRuns(client),
  });
}

export interface BatchClaimResponse {
  claimed: number;
  requested: number;
  targetShopId: string;
  results: ClaimResult[];
  failures: Array<{ key: string; code?: string; reason: string }>;
}

/* ================================================================== *
 * §7 图片翻译
 * ================================================================== */

/** 主图文字识别（本地 OCR）：判断图上有没有中文，选中处理方式前先看结果 */
export interface ImageAnalyzeItem {
  index: number;
  url: string;
  action: "keep" | "translate" | "review";
  reason: string;
  hasChinese: boolean | null;
  chineseText: string;
  text: string;
  error: boolean;
}

export interface ImageAnalyzeResponse {
  items: ImageAnalyzeItem[];
  summary: {
    total: number;
    keep: number;
    translate: number;
    review: number;
    withChinese: number;
    errors: number;
  };
}

/* ------------------------------------------------------------------ *
 * 版本与更新（GET /system/version、POST /system/update-check、
 *            GET+POST /system/update）
 *
 * /system/version 与 /system/update-check 只回答「有没有新版本」；
 * 真正下载安装包、判断本机能不能装，走 /system/update 这一对。
 * 两条链路是不同的接口，别合并：前者不落盘，后者会写 data_dir/updates。
 * ------------------------------------------------------------------ */

export interface VersionPayload {
  name: string;
  nameEn: string;
  current: string;
  latest: string;
  updateAvailable: boolean;
  ok: boolean;
  channel: string;
  manifestUrl: string;
  platform: string;
  checkedAt: number;
  error?: string;
  url?: string | null;
  sha256?: string | null;
  sizeBytes?: number | null;
  notes?: string;
  publishedAt?: string;
  /**
   * 安装相关字段来自 GET /system/update（/system/version 不带）。
   * 「有没有新版本」和「能不能在这儿装」是两件事：服务端在只读模式下
   * 会直接拒绝 POST，所以界面必须在按钮上先把原因说清楚，别让用户点了才吃 403。
   */
  /** 上次下载留在 data_dir/updates 的安装包，有值就能直接装，不必重下几十兆 */
  pendingFile?: string | null;
  /** GET 恒 false；POST 下载成功后才 true */
  downloaded?: boolean;
  /** 当前平台能否自动（静默）安装 */
  canInstall?: boolean;
  /** 只读模式：下载会被 403 拦掉 */
  readOnly?: boolean;
  /** 上游给了 sha256 且一致才是 true */
  sha256Verified?: boolean;
  /** 安装包落盘路径（POST 返回） */
  path?: string;
  /** 实际写入字节数（POST 返回） */
  size?: number;
}

/** 取版本；refresh=true 时真的去拉更新清单。 */

export function useVersion(refresh = false, enabled = true): UseQueryResult<VersionPayload> {
  return useQuery({
    // 必须走 queryKeys 而不是硬编码 ["system-version", refresh]：
    // 硬编码会与 useCheckUpdate 里的失效键错位，点「检查更新」后版本号不刷新。
    queryKey: queryKeys.system.version(refresh),
    queryFn: () =>
      api.get<VersionPayload>("/system/version", {
        query: refresh ? { refresh: "true" } : undefined,
        timeoutMs: 20_000,
      }),
    enabled,
    staleTime: 0,
    retry: false,
  });
}

/** 手动检查更新：POST /system/update-check（强制 refresh）。 */
export function useCheckUpdate(): UseMutationResult<VersionPayload, Error, void> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<VersionPayload>("/system/update-check", { refresh: true }),
    onSuccess: (data) => {
      // 写回缓存必须用与读取**完全相同**的键，否则写进去也没人读。
      // 原来这里写的是 `["system-version", true]`，而界面上显示的版本号读的是
      // `useVersion(false)` → 键是 `["system-version", false]`，两个键对不上，
      // 表现就是「点了检查更新，版本号纹丝不动」，而后端其实已经返回了新版本。
      //
      // 两件事都做：先把结果写进**当前显示用的那个键**（false），
      // 再整族失效一次，把其它 refresh 组合的变体一起刷新。
      client.setQueryData(queryKeys.system.version(false), data);
      void client.invalidateQueries({ queryKey: queryKeys.system.versionAll });
    },
  });
}

/**
 * 更新状态的 queryKey。没有放进 @/lib/queryKeys 是刻意的：那边是全局契约文件，
 * 这次改动范围只到 queries.ts；key 前缀仍按 ["system", ...] 对齐，失效时能命中同一族。
 */
const UPDATE_STATUS_KEY = ["system", "update-status"] as const;

/**
 * 更新状态：GET /system/update（只查询，不下载）。
 *
 * 与 /system/version 的区别：这个接口额外回答「本机能不能装」（canInstall）、
 * 「是不是只读」（readOnly）、「上次是不是已经下好了」（pendingFile）。
 * 缺了这些，界面只能给一个永远可点的按钮，点下去才知道是 403 还是平台不支持。
 *
 * staleTime 给长一点：它内部会去拉一次更新清单，每次开页都打没意义。
 */
export function useUpdateStatus(options: { enabled?: boolean } = {}): UseQueryResult<VersionPayload> {
  return useQuery({
    queryKey: UPDATE_STATUS_KEY,
    queryFn: () => api.get<VersionPayload>("/system/update", { timeoutMs: 30_000 }),
    enabled: options.enabled ?? true,
    staleTime: 5 * 60_000,
    // 本地服务，失败就是失败（只读 / 无清单），重试只会拖慢按钮出状态
    retry: false,
  });
}

/**
 * 下载安装包：POST /system/update。
 *
 * `install: true` 时后端下载完直接拉起安装器，会退出当前程序 —— 界面必须用
 * 单独一次点击来确认这件事，不能和「下载」合并成一个按钮。
 * 超时给到 5 分钟：安装包几十兆，默认 30 秒必然半路掐断。
 */
export function useDownloadUpdate(): UseMutationResult<
  UpdateDownloadResult,
  Error,
  UpdateDownloadRequest
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: UpdateDownloadRequest) =>
      api.post<UpdateDownloadResult, UpdateDownloadRequest>("/system/update", body ?? {}, {
        timeoutMs: 300_000,
      }),
    onSuccess: () => {
      // 下载完 latest / pendingFile / downloaded 全变了，两个版本查询都要重取。
      // 用 versionAll 前缀整族失效：不同 refresh 组合是不同的键，
      // 只失效某一个变体会漏掉另一处正在显示的版本号。
      void client.invalidateQueries({ queryKey: queryKeys.system.versionAll });
      void client.invalidateQueries({ queryKey: UPDATE_STATUS_KEY });
    },
  });
}

/** 图片额度：今日已用 / 日上限 / 预算余额 / 下一张要等多久。 */
export interface ImageUsage {
  usedToday: number;
  usedThisHour: number;
  usedThisMinute: number;
  budgets: { minute: number; hour: number; day: number };
  nextSlotSeconds: number;
  budget: {
    limitMilliyuan: number;
    reservedMilliyuan: number;
    remainingMilliyuan: number;
    unitMilliyuan: number;
  };
}

/**
 * 查询图片翻译额度。
 *
 * 老版顶栏一直挂着「图片翻译（本机 Key）· 今日 3/416」的徽标，
 * 用户随时知道还能译几张。新版这块信息只存在于后端，界面完全看不到，
 * 额度用光时只能靠提交失败才发现。
 */
export function useImageUsage(options: { enabled?: boolean } = {}) {
  return useQuery({
    queryKey: ["images", "usage"] as const,
    queryFn: () => api.get<ImageUsage>("/images/usage"),
    // 额度变化慢，30 秒够用；提交翻译后会主动失效重取
    staleTime: 30_000,
    enabled: options.enabled ?? true,
  });
}

/**
 * 图片翻译密钥自检：GET /images/verify（只查配置，不发起付费请求）。
 *
 * 老版有 verify_image_api.py 能跑一次付费单图验证密钥；新版这条入口整个丢了，
 * 用户配错 image-key.txt 只能等批量提交时在某个 job.message 里看到 503。
 * 这个 hook 让界面在用户动手之前就能回答「密钥配没配、从哪个文件读的」。
 *
 * 只读接口、不花钱，但也没必要频繁打：密钥是文件/环境变量，改动后要重启或
 * 重配才生效，所以 staleTime 给长一点，由 UI 在用户点「重新检查」时主动失效。
 */
export function useImageVerify(options: { enabled?: boolean } = {}): UseQueryResult<ImageVerifyResponse> {
  return useQuery({
    queryKey: ["images", "verify"] as const,
    queryFn: () => api.get<ImageVerifyResponse>("/images/verify"),
    staleTime: 5 * 60_000,
    enabled: options.enabled ?? true,
  });
}

/**
 * 付费冒烟：POST /images/verify，真发一张 64×64 的图，验端到端链路。
 *
 * 这条会花钱，所以 `confirm: true` **写死在 mutationFn 里**，不接受调用方传参：
 * 界面上唯一的前置条件是 ConfirmDialog（用户在弹窗里点过「开始测试」才会走到
 * 这里），如果 confirm 还能从外面传，就多了一条「忘了带 confirm → 400」的路。
 *
 * 注意返回语义：失败也是 200（body.ok=false + 后端 message 原文），
 * 只有 400（没带 confirm）/ 503（没配密钥）/ 只读模式才抛 HTTP 错误。
 *
 * 超时给到 60 秒：这是一次真实的上游出图请求，默认 30 秒会半路掐断，
 * 而掐断时上游可能已经受理——那笔钱就白花了。
 */
export function useImageVerifyPaid(): UseMutationResult<
  ImageVerifyPaidResponse,
  Error,
  void
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () =>
      api.post<ImageVerifyPaidResponse, { confirm: true }>(
        "/images/verify",
        { confirm: true },
        { timeoutMs: 60_000 }
      ),
    onSuccess: () => {
      // 冒烟会占掉一次额度（成功失败都占），徽标上的数字要跟着动
      void client.invalidateQueries({ queryKey: ["images", "usage"] });
    },
  });
}

/**
 * 图片任务列表（GET /images/jobs）。
 *
 * 与商品详情里的 imageJobs 不同：那边是精简版（没有 publicUrl / uploadError），
 * 这里返回完整 job，才有「公网链接 / 传到图床」两列可显示。
 */
export function useImageJobs(
  params: { productId?: string | null; batchId?: string; status?: string; page?: number; pageSize?: number } = {},
  options: { enabled?: boolean } = {}
): UseQueryResult<ImageJobsResponse> {
  const search: QueryParams = {
    productId: params.productId ?? undefined,
    batchId: params.batchId,
    status: params.status,
    page: params.page ?? 1,
    pageSize: params.pageSize ?? 50,
  };
  return useQuery({
    queryKey: [...queryKeys.imageJobs.list({
      productId: params.productId ?? undefined,
      batchId: params.batchId,
      status: params.status,
      page: params.page ?? 1,
    }), { pageSize: params.pageSize ?? 50 }],
    queryFn: () => api.get<ImageJobsResponse>("/images/jobs", { query: search }),
    enabled: options.enabled ?? true,
    staleTime: 10_000,
  });
}

/**
 * 把已翻译结果补传到图床（POST /images/jobs/{id}/upload），拿 publicUrl。
 *
 * 后端三处自动接线（translate / refresh / reconcile）已经会在结果就绪时传一次，
 * 这个 mutation 是给「当时传失败了」和「历史任务没有公网地址」留的手动重试口。
 * 幂等：已有 public_url 直接 200 回现有值，不会重复走 SFTP/HTTP。
 *
 * 错误语义（界面按这个分支）：404 任务不存在 / 409 任务还没 ready / 502 上传失败。
 */
export function useUploadImageJob(): UseMutationResult<
  ImageJobUploadResponse,
  Error,
  string
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (jobId: string) =>
      api.post<ImageJobUploadResponse, object>(
        `/images/jobs/${encodeURIComponent(jobId)}/upload`,
        {},
        // 图床可能是 SFTP，几十兆的图要传一会儿
        { timeoutMs: 120_000 }
      ),
    onSuccess: () => {
      // 行内的「公网链接」靠重新取列表出现，不在本地拼假地址
      void client.invalidateQueries({ queryKey: queryKeys.imageJobs.all });
    },
  });
}

export function useAnalyzeImages(): UseMutationResult<
  ImageAnalyzeResponse,
  Error,
  { images: string[]; retry?: boolean }
> {
  return useMutation({
    mutationFn: (body: { images: string[]; retry?: boolean }) =>
      api.post<ImageAnalyzeResponse, { images: string[]; retry?: boolean }>(
        "/images/analyze",
        body,
        { timeoutMs: 180_000 }
      ),
  });
}

export function useTranslateImages(): UseMutationResult<
  ImageTranslateResponse,
  Error,
  ImageTranslateRequest
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: ImageTranslateRequest) =>
      api.post<ImageTranslateResponse, ImageTranslateRequest>("/images/translate", body, {
        timeoutMs: 120_000,
        idempotencyKey: body.batchId ?? idempotencyKey("imgb"),
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.imageJobs.all });
      // 提交成功就消耗了额度，徽标上的数字要跟着动
      void client.invalidateQueries({ queryKey: ["images", "usage"] });
    },
  });
}

/**
 * 对账：进程重启后重新查上游任务状态，只更新状态，绝不重复提交。
 *
 * 老版有「查询已提交任务」按钮（index.html#reconcile-image-tasks）。
 * 没有它，卡在「已提交」的任务永远等不到结果——重启前提交的任务没人认领，
 * 界面上就一直是「处理中」，用户只能干等。
 */
export function useReconcileImages(): UseMutationResult<
  { checked: number; ready: number; pending: number; failed: number },
  Error,
  void
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () =>
      api.post<{ checked: number; ready: number; pending: number; failed: number }, object>(
        "/images/reconcile",
        {}
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.imageJobs.all });
      void client.invalidateQueries({ queryKey: ["images", "usage"] });
    },
  });
}

export interface ScheduleLogItem {
  id: number;
  taskId: string;
  taskName: string;
  accountId: string | null;
  status: string;
  reason: string;
  requested: number;
  selected: number;
  published: number;
  failed: number;
  at: number;
}

export interface ScheduleLogsResponse {
  items: ScheduleLogItem[];
  total: number;
  limit: number;
}

/** 定时任务执行历史。老版存在 `publish-schedule-log.json` 里，新版此前只有最近一次。 */
export function useScheduleLogs(
  params: { taskId?: string; limit?: number } = {}
): UseQueryResult<ScheduleLogsResponse> {
  const search = new URLSearchParams();
  if (params.taskId) search.set("taskId", params.taskId);
  search.set("limit", String(params.limit ?? 100));
  return useQuery({
    queryKey: ["schedule-logs", params.taskId ?? "", params.limit ?? 100],
    queryFn: () => api.get<ScheduleLogsResponse>(`/schedules/logs?${search.toString()}`),
    staleTime: 10_000,
  });
}

/* ================================================================== *
 * §8 发布
 * ================================================================== */

export function usePublish(): UseMutationResult<PublishResponse, Error, PublishRequest> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: PublishRequest) =>
      api.post<PublishResponse, PublishRequest>("/publish", body, {
        timeoutMs: 180_000,
        idempotencyKey: idempotencyKey("pub"),
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.publishLogs.all });
      void client.invalidateQueries({ queryKey: queryKeys.warehouse.all });
    },
  });
}

export interface PublishLogParams {
  page: number;
  pageSize: number;
  /** processing / success / fail / cancel（api.md §8.2） */
  status?: string | null;
  shopId?: string | null;
}

export function usePublishLogs(params: PublishLogParams): UseQueryResult<PublishLogsResponse> {
  return useQuery({
    queryKey: [
      ...queryKeys.publishLogs.list({
        page: params.page,
        status: params.status ?? undefined,
        shopId: params.shopId ?? undefined,
      }),
      { pageSize: params.pageSize },
    ],
    queryFn: () =>
      api.get<PublishLogsResponse>("/publish/logs", {
        query: compactQuery({
          page: params.page,
          pageSize: params.pageSize,
          status: params.status,
          shopId: params.shopId,
          refresh: true,
        }),
      }),
    placeholderData: (previous) => previous,
  });
}

export interface RefreshLogsResponse {
  updated: number;
  degraded: boolean;
  accountId: string;
}

export function useRefreshPublishLogs(): UseMutationResult<RefreshLogsResponse, Error, void> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<RefreshLogsResponse>("/publish/logs/refresh", undefined, {
      timeoutMs: 60_000,
    }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.publishLogs.all });
      void client.invalidateQueries({ queryKey: queryKeys.automationRuns.all });
    },
  });
}

/* ================================================================== *
 * §9 定时发布
 * ================================================================== */

export function useSchedules(
  params: { accountId?: string | null; enabled?: boolean | null } = {},
  options: { enabled?: boolean } = {}
): UseQueryResult<SchedulesResponse> {
  return useQuery({
    queryKey: [...queryKeys.schedules.list(params), { page: 1 }],
    enabled: options.enabled ?? true,
    queryFn: () =>
      api.get<SchedulesResponse>("/schedules", {
        query: compactQuery({ accountId: params.accountId, enabled: params.enabled }),
      }),
  });
}

export function useCreateSchedule(): UseMutationResult<{ task: Schedule }, Error, ScheduleCreateRequest> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: ScheduleCreateRequest) =>
      api.post<{ task: Schedule }, ScheduleCreateRequest>("/schedules", body, {
        idempotencyKey: idempotencyKey("sch"),
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.schedules.all });
    },
  });
}

/**
 * 局部更新体：api.md §9.3 允许改 shopId / template / accountId，
 * 而 types 里的 SchedulePatchRequest 把这三项排除在外（那是给「纯调度参数」用的），
 * 这里显式放开，避免编辑抽屉改店铺后静默丢失。
 */
export type SchedulePatchBody = SchedulePatchRequest & {
  accountId?: string;
  shopId?: string;
  template?: string;
};

export function usePatchSchedule(): UseMutationResult<
  { task: Schedule },
  Error,
  { id: string; patch: SchedulePatchBody }
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }) =>
      api.patch<{ task: Schedule }, SchedulePatchBody>(`/schedules/${encodeURIComponent(id)}`, patch),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.schedules.all });
    },
  });
}

export function useDeleteSchedule(): UseMutationResult<ScheduleDeleteResponse, Error, string> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      api.del<ScheduleDeleteResponse>(`/schedules/${encodeURIComponent(id)}`),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.schedules.all });
    },
  });
}

export function useRunSchedule(): UseMutationResult<ScheduleRunResult, Error, string> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      api.post<ScheduleRunResult>(`/schedules/${encodeURIComponent(id)}/run`, undefined, {
        timeoutMs: 150_000,
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.schedules.all });
      void client.invalidateQueries({ queryKey: queryKeys.publishLogs.all });
      void client.invalidateQueries({ queryKey: queryKeys.warehouse.all });
    },
  });
}

/* ================================================================== *
 * §10 违规词库
 * ================================================================== */

export interface WordListParams extends PageParams {
  action?: WordCategory | null;
  q?: string;
}

export function useViolationWords(params: WordListParams): UseQueryResult<ViolationWordsResponse> {
  return useQuery({
    queryKey: queryKeys.violationWords.list({
      action: params.action ?? undefined,
      q: params.q || undefined,
      page: params.page,
      pageSize: params.pageSize,
    }),
    queryFn: () =>
      api.get<ViolationWordsResponse>("/violation-words", {
        query: compactQuery({
          action: params.action,
          q: params.q,
          page: params.page,
          pageSize: params.pageSize,
        }),
      }),
    placeholderData: (previous) => previous,
  });
}

export interface WordsMutationBody {
  action: WordAction;
  word?: string;
  /** 既可传数组，也可传纯文本：按换行/逗号/顿号/分号/制表符切分，`#` 起为注释。
   *  运营一般是直接从表格里粘一整块，要求他们先改成 JSON 数组不现实。 */
  words?: string[] | string | {
    format: "aohuang.violation-words";
    version: 1;
    items: Array<{
      word: string;
      action: "remove" | "review";
      source: string;
      siteScope?: string | null;
      sourceUrl?: string | null;
      ruleVersion?: string | null;
      enabled: boolean;
      createdAt: number;
    }>;
  };
  category?: WordCategory;
  siteScope?: string | null;
  sourceUrl?: string | null;
  ruleVersion?: string | null;
  mode?: "append" | "replace";
  confirm?: true;
}

export interface WordsMutationResult {
  items: ViolationWord[];
  added: number;
  removed: number;
  /** 被跳过的**词**本身（重复、超长、放不下），不只是个数 */
  skipped: string[];
  skippedCount: number;
  truncated?: boolean;
  processed?: number;
  limit?: number;
  counts: { remove: number; review: number; limit: number; remaining: number };
}

export function useMutateWords(): UseMutationResult<WordsMutationResult, Error, WordsMutationBody> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: WordsMutationBody) =>
      api.post<WordsMutationResult, WordsMutationBody>("/violation-words", body),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.violationWords.all });
      void client.invalidateQueries({ queryKey: queryKeys.auth.all });
    },
  });
}


/* ================================================================== *
 * §11 定价模板（发布与定时任务选择模板）
 * ================================================================== */

export interface PriceTemplateItem {
  name: string;
  invalid?: boolean;
  error?: string;
  template?: Record<string, unknown>;
}

export interface TemplatesResponse {
  templates: PriceTemplateItem[];
  total: number;
  selected: Record<string, string>;
}

export function useTemplates(enabled = true): UseQueryResult<TemplatesResponse> {
  return useQuery({
    queryKey: ["system", "templates"],
    queryFn: () => api.get<TemplatesResponse>("/system/templates"),
    enabled,
    staleTime: 60_000,
  });
}

export interface SaveTemplateResponse {
  template: Record<string, unknown>;
  templates: PriceTemplateItem[];
}

/** POST /system/templates —— 保存（新建或覆盖同名）定价模板。 */
export function useSaveTemplate(): UseMutationResult<
  SaveTemplateResponse,
  Error,
  Record<string, unknown>
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (template) =>
      api.post<SaveTemplateResponse, { template: Record<string, unknown> }>(
        "/system/templates",
        { template }
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["system", "templates"] });
    },
  });
}

/**
 * 记住某个账号当前用的模板（老版 /api/price-select）。
 *
 * 不调的话 `/system/templates` 的 `selected` 永远是空的，
 * 发布和定时新建都拿不到该账号的默认模板，每次都得手动选。
 */
export function useSelectTemplate(): UseMutationResult<
  { account: string; name: string; selected: Record<string, string> },
  Error,
  { name: string; account?: string }
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: { name: string; account?: string }) =>
      api.post<{ account: string; name: string; selected: Record<string, string> },
        { name: string; account?: string }>("/system/templates/select", body),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["system", "templates"] });
    },
  });
}

export interface DeleteTemplateResponse {
  deleted: boolean;
  name: string;
  templates: PriceTemplateItem[];
}

/** DELETE /system/templates/{name} —— 删除定价模板。 */
export function useDeleteTemplate(): UseMutationResult<DeleteTemplateResponse, Error, string> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (name) =>
      api.del<DeleteTemplateResponse>(`/system/templates/${encodeURIComponent(name)}`),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["system", "templates"] });
    },
  });
}

/* ================================================================== *
 * 工作流默认值（重量增量 / 包裹尺寸 / 尺码图地址）
 *
 * 后端 workflow.defaults 是入仓时真正读取的那份配置
 * （save_from_collect_box → _workflow_setting）。前端处理设置面板
 * 改了这三个值就同步写到这里，保证「界面上调的参数，处理时真的生效」。
 * ================================================================== */

export interface WorkflowDefaults {
  weightIncrement?: number;
  packageDimensions?: {
    packageLength: number;
    packageWidth: number;
    packageHeight: number;
  };
  sizeChartUrl?: string;
}

export interface SystemSettingsResponse {
  workflowDefaults?: WorkflowDefaults;
  readOnly?: boolean;
  publishEnabled?: boolean;
}

export function useWorkflowDefaults(enabled = true): UseQueryResult<SystemSettingsResponse> {
  return useQuery({
    queryKey: ["system", "settings"],
    queryFn: () => api.get<SystemSettingsResponse>("/system/settings"),
    enabled,
    staleTime: 60_000,
  });
}

/** POST /system/settings 的 workflowDefaults 分支：只覆盖传入的键。 */
export function useSaveWorkflowDefaults(): UseMutationResult<
  SystemSettingsResponse,
  Error,
  WorkflowDefaults
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (patch) =>
      api.post<SystemSettingsResponse, { workflowDefaults: WorkflowDefaults }>(
        "/system/settings",
        { workflowDefaults: patch }
      ),
    onSuccess: (data) => {
      client.setQueryData(["system", "settings"], data);
    },
  });
}

/* ================================================================== *
 * §16 标题清理（api.md §16）
 * ================================================================== */

export interface TitlePreviewItem {
  index: number;
  original: string;
  cleaned: string;
  removedWords: string[];
  reviewWords: string[];
  /** 标题里能刷新的过时年份个数（大于 0 才值得开 refreshYears） */
  staleYears?: number;
  changed: boolean;
}

export interface TitlePreviewResponse {
  items: TitlePreviewItem[];
  total: number;
  changedCount: number;
}

/** 标题清理预览：不写库、不调上游，只按词库算一遍 */
export function useTitlePreview(): UseMutationResult<
  TitlePreviewResponse,
  Error,
  { titles?: string[]; warehouseIds?: string[]; limit?: number; refreshYears?: boolean }
> {
  return useMutation({
    mutationFn: (body: { titles?: string[]; warehouseIds?: string[]; limit?: number; refreshYears?: boolean }) =>
      api.post<TitlePreviewResponse, typeof body>("/titles/preview", body),
  });
}

/* ================================================================== *
 * §17 同步（api.md §17）
 * ================================================================== */

export interface PriceTemplateRemote {
  name?: string;
  site?: string;
  currency?: string;
  [key: string]: unknown;
}

export interface PriceTemplateSyncResponse {
  items: PriceTemplateRemote[];
  total: number;
  site: string;
  pageNo: number;
  pageSize: number;
}

/** 从妙手拉定价模板列表（只读，不落库） */
export function useSyncPriceTemplates(): UseMutationResult<
  PriceTemplateSyncResponse,
  Error,
  { site?: string; name?: string; pageNo?: number; pageSize?: number; accountId?: string }
> {
  return useMutation({
    mutationFn: (body: {
      site?: string;
      name?: string;
      pageNo?: number;
      pageSize?: number;
      accountId?: string;
    }) => api.post<PriceTemplateSyncResponse, typeof body>("/sync/price-templates", body),
  });
}

export interface SourceLinkSyncResponse {
  scanned: number;
  added: number;
  total: number;
  usable: number;
}

/** 从已有成品回收 1688 货源链接进池子 */
export function useSyncSourceLinks(): UseMutationResult<
  SourceLinkSyncResponse,
  Error,
  { warehouseIds?: string[]; all?: boolean; limit?: number }
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: { warehouseIds?: string[]; all?: boolean; limit?: number }) =>
      api.post<SourceLinkSyncResponse, typeof body>("/sync/source-links", body),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.sync.all });
    },
  });
}

/* ================================================================== *
 * §12 载体链接池（api.md §12）
 * 跨店发布（mode=carrier）需要 1688 货源链接，池子空的时候无法重建载体。
 * ================================================================== */

export interface CarrierLink {
  url: string;
  addedAt?: number;
  failed?: number;
  ok?: number;
}

export interface CarrierLinksResponse {
  total: number;
  usable: number;
  items: CarrierLink[];
  limit: number;
}

export function useCarrierLinks(enabled = true): UseQueryResult<CarrierLinksResponse> {
  return useQuery({
    queryKey: queryKeys.carrier.links(),
    queryFn: () => api.get<CarrierLinksResponse>("/carrier/links"),
    enabled,
    staleTime: 15_000,
  });
}

export interface CarrierAddResponse {
  added: number;
  skipped: number;
  invalid: string[];
  requested: number;
  total: number;
}

export function useAddCarrierLinks(): UseMutationResult<
  CarrierAddResponse,
  Error,
  { urls: string[] }
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: { urls: string[] }) =>
      api.post<CarrierAddResponse, typeof body>("/carrier/links", body),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.carrier.all });
    },
  });
}

export function useRemoveCarrierLinks(): UseMutationResult<
  { removed: number; total: number },
  Error,
  { urls: string[] }
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: { urls: string[] }) =>
      api.del<{ removed: number; total: number }, typeof body>("/carrier/links", body),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.carrier.all });
    },
  });
}

/* ================================================================== *
 * 批次编排（POST /batch 系列）
 *
 * 后端是「单步推进」语义：没有常驻线程，`/step` 每调一次跑一件。
 * 所以这里的 step / control / retry 都是普通 mutation，没有后台任务，
 * 也不做轮询——自动推进的循环归界面管（BatchPanel 的 useEffect）。
 * ================================================================== */

export interface BatchListParams {
  accountId?: string | null;
  /** 后端夹在 1..200，默认 50 */
  limit?: number;
}

/**
 * 只修补「已经查过」的批次详情缓存。
 *
 * control / retry 的返回只有 batch、没有 items；直接 setQueryData 一个
 * items 为空的对象，会让展开区先闪一屏「没有逐件记录」再被真正的数据替换。
 * 没缓存就什么都不做，等用户展开时正常拉一次。
 */
function patchBatchDetail(client: ReturnType<typeof useQueryClient>, batch: Batch): void {
  client.setQueryData<BatchDetailResponse>(queryKeys.batch.detail(batch.id), (previous) =>
    previous ? { batch, items: previous.items } : previous
  );
}

export function useBatches(
  params: BatchListParams = {},
  options: { enabled?: boolean } = {}
): UseQueryResult<BatchListResponse> {
  return useQuery({
    queryKey: queryKeys.batch.list({ accountId: params.accountId ?? null, limit: params.limit }),
    queryFn: () =>
      api.get<BatchListResponse>("/batch", {
        query: compactQuery({ accountId: params.accountId, limit: params.limit }),
      }),
    enabled: options.enabled ?? true,
    // 批次状态由 step / control 主动推进，窗口聚焦时重拉没有意义
    refetchOnWindowFocus: false,
  });
}

export function useBatch(
  id: string | null,
  options: { enabled?: boolean } = {}
): UseQueryResult<BatchDetailResponse> {
  return useQuery({
    queryKey: queryKeys.batch.detail(id ?? "none"),
    queryFn: () => api.get<BatchDetailResponse>(`/batch/${encodeURIComponent(id ?? "")}`),
    enabled: Boolean(id) && (options.enabled ?? true),
    refetchOnWindowFocus: false,
  });
}

/** 建批次。409 CONFLICT_BUSY = 同账号同店铺已有未完成批次。 */
export function useCreateBatch(): UseMutationResult<
  BatchDetailResponse,
  Error,
  BatchCreateRequest
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: BatchCreateRequest) =>
      api.post<BatchDetailResponse, BatchCreateRequest>("/batch", body),
    onSuccess: (result) => {
      client.setQueryData(queryKeys.batch.detail(result.batch.id), result);
      void client.invalidateQueries({ queryKey: queryKeys.batch.all });
    },
  });
}

/**
 * 推进一件。单件失败不会 reject：后端把 item 标 failed、批次标 failed 后正常返回，
 * 只有「暂停/停止/已完成」和 RETRY_UNSAFE 的 409 会抛出来。
 *
 * 成功时把返回的 batch 直接写进详情缓存（进度立刻动），再让列表失效重取；
 * 详情也失效，这样展开的 items 会跟着刷新。
 */
export function useBatchStep(): UseMutationResult<BatchStepResponse, Error, string> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      api.post<BatchStepResponse>(`/batch/${encodeURIComponent(id)}/step`, undefined, {
        // 单件要拉详情、翻译、定价、入仓，30 秒默认值不够
        timeoutMs: 180_000,
      }),
    onSuccess: (result) => {
      // 只修补「已经展开过」的详情缓存：没查过就别造一个 items 为空的假缓存，
      // 否则展开的瞬间会先闪一屏「没有逐件记录」再被真正的数据替换。
      client.setQueryData<BatchDetailResponse>(
        queryKeys.batch.detail(result.batch.id),
        (previous) => {
          if (!previous) return previous;
          return {
            batch: result.batch,
            items: previous.items.map((item) =>
              result.item && item.id === result.item.id ? result.item : item
            ),
          };
        }
      );
      void client.invalidateQueries({ queryKey: queryKeys.batch.all });
      void client.invalidateQueries({ queryKey: queryKeys.warehouse.all });
    },
  });
}

export interface BatchControlVariables {
  id: string;
  action: BatchControlAction;
}

export function useBatchControl(): UseMutationResult<
  BatchControlResponse,
  Error,
  BatchControlVariables
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, action }: BatchControlVariables) =>
      api.post<BatchControlResponse, { action: BatchControlAction }>(
        `/batch/${encodeURIComponent(id)}/control`,
        { action }
      ),
    onSuccess: (result) => {
      patchBatchDetail(client, result.batch);
      void client.invalidateQueries({ queryKey: queryKeys.batch.all });
    },
  });
}

export interface BatchRetryVariables {
  id: string;
  /** true = 操作者已在妙手核对过、确认上次没保存，允许重跑结果未知的件 */
  allowUnknown?: boolean;
}

export function useBatchRetryFailed(): UseMutationResult<
  BatchRetryResponse,
  Error,
  BatchRetryVariables
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, allowUnknown }: BatchRetryVariables) =>
      api.post<BatchRetryResponse, { allowUnknown: boolean }>(
        `/batch/${encodeURIComponent(id)}/retry-failed`,
        { allowUnknown: Boolean(allowUnknown) }
      ),
    onSuccess: (result) => {
      patchBatchDetail(client, result.batch);
      void client.invalidateQueries({ queryKey: queryKeys.batch.all });
    },
  });
}

export function useDeleteBatch(): UseMutationResult<BatchDeleteResult, Error, string> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.del<BatchDeleteResult>(`/batch/${encodeURIComponent(id)}`),
    onSuccess: (_result, id) => {
      client.removeQueries({ queryKey: queryKeys.batch.detail(id) });
      void client.invalidateQueries({ queryKey: queryKeys.batch.all });
    },
  });
}

/* ================================================================== *
 * 操作员与审计日志（多用户协作）
 *
 * 后端契约：
 *   GET    /operators                  → { operators, total, current }
 *   POST   /operators                  → { operator, token }   token 只出现这一次
 *   PATCH  /operators/{id}             → { operator }
 *   POST   /operators/{id}/disable     → { operator }
 *   POST   /operators/{id}/rotate      → { operator, token }   token 只出现这一次
 *   GET    /audit-logs?operatorId=&action=&targetKind=&result=&limit=&before=
 *
 * token 明文后端只存哈希，所以任何「再看一眼」的需求都只能靠重置 ——
 * 因此这两个返回 token 的接口在界面上必须走一次性展示框，不能直接 toast 完就算。
 * ================================================================== */

export function useOperators(options: { enabled?: boolean } = {}): UseQueryResult<OperatorsResponse> {
  return useQuery({
    queryKey: queryKeys.operators.list(),
    queryFn: () => api.get<OperatorsResponse>("/operators"),
    enabled: options.enabled ?? true,
    // 名单变化不频繁，但「最后活跃时间」需要相对新鲜
    staleTime: 15_000,
  });
}

export function useCreateOperator(): UseMutationResult<
  OperatorTokenResponse,
  Error,
  OperatorCreateRequest
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: OperatorCreateRequest) =>
      api.post<OperatorTokenResponse, OperatorCreateRequest>("/operators", body),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.operators.all });
    },
  });
}

export interface RenameOperatorVariables {
  id: string;
  name: string;
}

export function useRenameOperator(): UseMutationResult<
  { operator: Operator },
  Error,
  RenameOperatorVariables
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, name }: RenameOperatorVariables) =>
      api.patch<{ operator: Operator }, OperatorPatchRequest>(
        `/operators/${encodeURIComponent(id)}`,
        { name }
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.operators.all });
      // 审计日志里存的是当时的名字快照，但列表展示的是现名，一起刷
      void client.invalidateQueries({ queryKey: queryKeys.auditLogs.all });
    },
  });
}

export function useToggleOperator(): UseMutationResult<
  { operator: Operator },
  Error,
  { id: string; enabled: boolean }
> {
  const client = useQueryClient();
  return useMutation({
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
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.operators.all });
    },
  });
}

export function useSetRole(): UseMutationResult<
  { operator: Operator },
  Error,
  { id: string; role: "admin" | "teacher" | "member" }
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, role }: { id: string; role: "admin" | "teacher" | "member" }) =>
      api.patch<{ operator: Operator }, { role: string }>(
        `/operators/${encodeURIComponent(id)}`,
        { role }
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.operators.all });
    },
  });
}

export function useRotateOperator(): UseMutationResult<
  OperatorTokenResponse,
  Error,
  string
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      api.post<OperatorTokenResponse>(`/operators/${encodeURIComponent(id)}/rotate`),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.operators.all });
    },
  });
}

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
    queryFn: () =>
      api.get<AuditLogsResponse>("/audit-logs", {
        query: compactQuery({
          operatorId: query.operatorId,
          action: query.action,
          targetKind: query.targetKind,
          result: query.result,
          limit: query.limit,
          before: query.before,
        }),
      }),
    enabled: options.enabled ?? true,
    // 翻页时保留上一页，避免整表闪成骨架
    placeholderData: (previous) => previous,
  });
}
