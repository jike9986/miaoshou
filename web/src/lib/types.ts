// api.md §0.1 命名与类型 / §0.4 分页 / §0.5 错误信封
// 与后端契约一一对应：camelCase 字段、id 为字符串、*At 为 Unix 秒（浮点）、
// 金额为字符串十进制（"12.34"）、可空字段始终出现（无值为 null）。

/* ================================================================== *
 * 通用信封
 * ================================================================== */

/** 分页信封（api.md §0.4） */
export interface Page<T> {
  items: T[];
  page: number;
  pageSize: number;
  total: number;
  hasMore: boolean;
}

/** 错误码（api.md §0.5 全局错误码 + 各接口特有码 + 客户端归一化码） */
export type KnownApiErrorCode =
  // 全局
  | "VALIDATION_ERROR"
  | "BAD_JSON"
  | "AUTH_REQUIRED"
  | "TOKEN_INVALID"
  | "FORBIDDEN_ORIGIN"
  | "READ_ONLY_MODE"
  | "NOT_FOUND"
  | "CONFLICT_BUSY"
  | "REVISION_CONFLICT"
  | "DUPLICATE_NAME"
  | "UNPROCESSABLE"
  | "RATE_LIMITED"
  | "INTERNAL_ERROR"
  | "UPSTREAM_ERROR"
  | "UPSTREAM_UNKNOWN_RESULT"
  | "UPSTREAM_RATE_LIMIT"
  | "UPSTREAM_TIMEOUT"
  // 采集箱 / 仓库
  | "ALREADY_CLAIMED"
  | "CARRIER_UNAVAILABLE"
  | "CATEGORY_NOT_LEAF"
  // 图片
  | "IMAGE_TOO_LARGE"
  | "IMAGE_BUDGET_EXCEEDED"
  | "RETRY_UNSAFE"
  | "RESULT_FILE_MISSING"
  // 发布
  | "CONFIRM_REQUIRED"
  | "PUBLISH_DISABLED"
  | "SITE_LANGUAGE_MISMATCH"
  | "TEMPLATE_INVALID"
  | "SHOP_NOT_IN_ACCOUNT"
  // 词库
  | "DUPLICATE_WORD"
  | "WORD_CONFLICT"
  | "WORD_LIMIT_EXCEEDED"
  // 系统
  | "DIGEST_MISMATCH"
  | "UPDATE_IN_PROGRESS"
  // 客户端侧归一化
  | "NETWORK_ERROR"
  | "TIMEOUT"
  | "ABORTED"
  | "BAD_RESPONSE"
  | "BOOT_MISSING";

export type ApiErrorCode = KnownApiErrorCode | (string & {});

/** 错误信封（api.md §0.5） */
export interface ApiErrorPayload {
  code: ApiErrorCode;
  message: string;
  details: Record<string, unknown> | null;
  requestId: string | null;
}

export interface ApiErrorEnvelope {
  error: ApiErrorPayload;
}

/* ================================================================== *
 * 认证 / 账号（api.md §1）
 * ================================================================== */

export type AccountStatus = "active" | "disabled" | "invalid";

export interface Account {
  id: string;
  name: string;
  appIdHint: string;
  site: string | null;
  isDefault: boolean;
  status: AccountStatus | string;
  lastVerifiedAt: number | null;
}

/** api.md §1.1 响应 */
export interface AuthConnectResult {
  connected: boolean;
  accountId: string | null;
  connectionName: string | null;
  appIdHint: string | null;
  secretHint: string | null;
  site: string | null;
  shopsCount: number;
  autoScan: boolean;
  lastVerifiedAt: number | null;
  shops: Shop[];
}

export interface AuthConnectRequest {
  accountId?: string | null;
  name?: string | null;
  appId?: string;
  appSecret?: string;
  save?: boolean;
  autoScan?: boolean;
}

export interface ApiLimits {
  mainLimit: number;
  detailLimit: number;
  detailHardLimit: number;
  concurrency: number;
  collectBoxMaxItems: number;
}

/** api.md §1.2 响应 */
export interface AuthStatus {
  connected: boolean;
  accountId: string | null;
  connectionName: string | null;
  appIdHint: string | null;
  readOnly: boolean;
  publishEnabled: boolean;
  accounts: Account[];
  languages: string[];
  serverVersion: string;
  dataDir: string;
  limits: ApiLimits;
}

/* ================================================================== *
 * 店铺（api.md §2）
 * ================================================================== */

export interface Shop {
  shopId: string;
  name: string;
  site: string;
  siteName: string;
  languages: string[];
  status: string;
  syncedAt?: number | null;
}

export interface ShopsResponse {
  accountId: string;
  accountName: string;
  shops: Shop[];
  siteLanguages: Record<string, string[]>;
  siteNames: Record<string, string>;
  degraded: boolean;
}

/* ================================================================== *
 * 采集箱（api.md §3）
 * ================================================================== */

export type CollectBoxStatus = "notPublished" | "published" | "all";
export type EditModel = "shop" | "site";

export interface CollectBoxItem {
  key: string;
  detailId: string;
  shopId: string;
  shopName: string;
  site: string;
  title: string;
  imageUrl: string | null;
  sourceId: string | null;
  status: string;
  editModel: EditModel | string;
  inWarehouse: boolean;
  hasDraftIn: string[];
}

export interface CollectBoxPage extends Page<CollectBoxItem> {
  scannedAt: number | null;
  degraded: boolean;
}

export interface SkuAttrValue {
  attrValueId: string;
  attrValue: string;
  imgUrl: string | null;
}

export interface SkuProperty {
  attrName: string;
  attrValueList: SkuAttrValue[];
}

export interface Sku {
  skuKey: string;
  price: string;
  priceIncludeVat: string;
  stock: number;
  weight: string;
  isDelete: string;
}

export interface PriceRange {
  min: string;
  max: string;
  currency: string;
}

export interface PackageDimensions {
  length: number;
  width: number;
  height: number;
}

export type DiagnosticSeverity = "error" | "warning" | "info";

export interface Diagnostic {
  severity: DiagnosticSeverity;
  code: string;
  message: string;
}

/** api.md §3.2 响应 */
export interface CollectBoxDetail {
  detailId: string;
  shopId: string;
  mode: EditModel | string;
  ossMd5: string;
  editModel: EditModel | string;
  title: string;
  site: string;
  cid: string | null;
  categoryPath: string | null;
  language: string | null;
  images: string[];
  imageCount: number;
  detailImages: string[];
  skuCount: number;
  skuProperties: SkuProperty[];
  skus: Sku[];
  purchasePrice: PriceRange | null;
  weight: string | null;
  packageDimensions: PackageDimensions | null;
  sizeChartUrl: string | null;
  notes: string | null;
  diagnostics: Diagnostic[];
  readyToProcess: boolean;
  inWarehouse: boolean;
  warehouseId: string | null;
}

/**
 * api.md §3 附加：POST /collect-box/{detailId}/process
 * 与 server/schemas/requests.py 的 ProcessRequest 一一对应（唯一入仓入口）。
 */
export interface ProcessCollectRequest {
  /** 来源店铺（必填） */
  shopId: string;
  accountId?: string | null;
  /** 翻译目标语言，留空则不翻译 */
  language?: string | null;
  /** 入库后落到哪个自定义分组，null = 不分组 */
  groupId?: string | null;
  /** 定价模板名，null = 不重算价格 */
  template?: string | null;
  /** 汇率，字符串或数字；留空则后端用默认值 */
  exchangeRate?: string | number | null;
  /** false = 只预览（后端 rollback），true = 真正写库 */
  save?: boolean;
  /** 是否提交图片翻译（老版 process_images）。不传＝不处理 */
  processImages?: boolean;
  /** 每件提交几张主图去翻译 */
  mainImageCount?: number;
  /** 每件提交几张 SKU 图去翻译，0 = 跳过 */
  skuImageCount?: number;
  /** 每件提交几张详情图去翻译，0 = 跳过 */
  detailImageCount?: number;
}

/** 图片翻译提交结果：submitted 才是真的提交了几张，不是商品有几张图 */
export interface ProcessImagesResult {
  requested: number;
  submitted: number;
  failed: number;
  jobs: Array<{ id?: string; status?: string; source?: string }>;
  batchId?: string;
  reason?: string;
}

/** POST /collect-box/{detailId}/process 响应（server/api/collect_box.py: process） */
export interface ProcessCollectResult {
  saved: boolean;
  product: Product & { draftSaved?: boolean; draftVerified?: boolean };
  images?: ProcessImagesResult | null;
}

export interface AutomationRunItemRecord {
  id: string;
  key: string;
  detailId: string;
  shopId: string;
  title: string;
  processStatus: "queued" | "running" | "succeeded" | "failed" | "needs_review";
  processReason: string;
  productId: string | null;
  processResult: { textDone?: boolean; images?: ProcessImagesResult | null };
  publishStatus: "not_requested" | "queued" | "running" | "succeeded" | "failed" | "needs_review";
  publishReason: string;
  publishResult: Record<string, unknown> & { resumeKind?: "images_pending" | "images_ready" };
  updatedAt: number;
}

export interface AutomationRunRecord {
  id: string;
  ownerOperatorId: string;
  accountId: string;
  shopId: string;
  status: "queued" | "running" | "needs_review" | "partial" | "completed";
  settings: Record<string, unknown>;
  createdAt: number;
  updatedAt: number;
  finishedAt: number | null;
  items: AutomationRunItemRecord[];
}

/** Per-product acknowledgement bound to the exact current warehouse revision. */
export interface AutomationPublishConfirmation {
  confirmedProductId: string;
  confirmedRevision: number;
}

export interface AutomationRunCreateRequest {
  accountId: string;
  shopId: string;
  settings: Record<string, unknown>;
  items: Array<{ key: string; detailId: string; shopId: string; title: string }>;
}

export interface ClaimRequest {
  targetShopId: string;
  accountId?: string | null;
  markCollected?: boolean;
}

export interface ClaimResult {
  claimed: boolean;
  detailId: string;
  fromShopId: string;
  targetShopId: string;
  mode: string;
  count: number;
  detailIds: string[];
  failures: Array<{ detailId?: string; reason?: string }>;
  collected: Record<string, { at: number; detailId: string }>;
}

/* ================================================================== *
 * 成品仓库（api.md §4）
 * ================================================================== */

export type ProductStatus = "ready" | "claimed" | "published" | "archived";
export type QualityReviewStatus = "verified" | "needs_review";
export type QualityReviewReason =
  | "missing_weight"
  | "missing_dimensions"
  | "default_dimensions"
  | "unverified"
  | "stale_review";

export interface Product {
  id: string;
  key: string;
  detailId: string;
  shopId: string;
  shopName: string;
  account: string;
  accountId: string;
  title: string;
  language: string | null;
  site: string;
  images: string[];
  image: string | null;
  imageCount: number;
  skuCount: number;
  purchasePriceMin: string;
  purchasePriceMax: string;
  purchaseCurrency: string;
  cid: string | null;
  categoryName: string | null;
  categoryId: string | null;
  group: string | null;
  groupId: string | null;
  savedAt: number;
  claimed: boolean;
  claimedAt: number | null;
  claimedShopId: string | null;
  collected: Record<string, { at: number; detailId: string }>;
  qualityReview?: ProductQualityReview | null;
  qualityReviewStatus?: QualityReviewStatus;
  qualityReviewReasons?: QualityReviewReason[];
  salesLast30: number;
  stock: number;
  status: ProductStatus | string;
  /** 学员成功发布过该商品的妙手店铺 ID。 */
  publishedShopIds?: string[];
  /**
   * 乐观并发版本号（每次写成功 +1）。
   * 老后端不返回这个字段 → 运行时是 undefined，前端按「不支持并发检查」处理，
   * 保存时不带 expectedRevision（老后端会忽略这个字段）。
   */
  revision?: number;
  /** 最后改动人的操作员名；空 / 缺省表示没记录（界面不显示「未知」占位） */
  updatedBy?: string | null;
  /**
   * 最后改动时间。列表接口老版本不回这个字段，详情一定有；
   * 没有时署名行只显示「谁改的」，不硬编一个时间出来。
   */
  updatedAt?: number;
}

export interface ProductQualityReview {
  weightKg: number;
  packageDimensions: {
    packageLength: number;
    packageWidth: number;
    packageHeight: number;
  };
  source: string;
  evidence: string;
  site: string;
  verifiedAt: number;
  verifiedByOperatorId: number;
  verifiedByOperatorName: string;
}

export interface ProductQualityReviewRequest {
  weightKg: number;
  packageLength: number;
  packageWidth: number;
  packageHeight: number;
  source: string;
  evidence: string;
  expectedRevision: number;
}

export interface CategoryFacet {
  name: string;
  cid: string;
  categoryId: string;
  count: number;
}

export interface GroupFacet {
  id: string;
  name: string;
  color: string;
  count: number;
}

export interface AccountFacet {
  accountId: string;
  name: string;
  count: number;
}

export interface WarehouseFacets {
  categories: CategoryFacet[];
  groups: GroupFacet[];
  accounts: AccountFacet[];
}

export interface WarehouseStats {
  totalProducts: number;
  totalClaimed: number;
  totalPublished: number;
  /**
   * 仓库容量上限（后端 `WAREHOUSE_MAX_ITEMS`，当前 2000）。
   *
   * 这几个字段是「提示」而不是「自动清理」：后端没有任何路径会自动删商品
   * （`trim_overflow` 默认拒绝执行），上限只用来在快满时提醒用户自己安排清理。
   * 可选是因为老后端不返回它们——前端不该因为后端版本旧就崩。
   */
  maxProducts?: number;
  /** 还能放多少件；到 0 说明满了，但**仍然能继续入仓**（只是会超过上限） */
  remaining?: number;
  /** 到 80% 提示线，界面据此显示提醒 */
  nearLimit?: boolean;
}

/** api.md §4.1 响应 */
export interface WarehouseResponse extends Page<Product> {
  facets?: WarehouseFacets;
  stats: WarehouseStats;
}

export type AutomationResultsResponse = Page<Product>;

export interface AutomationResultsImportResponse {
  added: number;
  ids: string[];
}

export type ProductSort = "savedDesc" | "savedAsc" | "priceAsc" | "priceDesc" | "titleAsc";

export interface WarehouseQuery {
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
  claimed?: boolean | null;
  sort?: ProductSort;
}

/** api.md §4.2 响应（product 较列表项多 payload / updatedAt） */
export interface ProductDetailProduct extends Product {
  payload: Record<string, unknown> | null;
  updatedAt: number;
}

export interface PublishHistoryEntry {
  logId: string;
  shopId: string;
  shopName: string;
  status: string;
  requestedAt: number;
  platformItemId: string | null;
}

export interface ProductImageJobBrief {
  id: string;
  source: ImageSource | string;
  status: ImageJobStatus | string;
  reservedMilliyuan: number;
  createdAt: number;
}

export interface ProductDetailResponse {
  product: ProductDetailProduct;
  publishHistory: PublishHistoryEntry[];
  imageJobs: ProductImageJobBrief[];
}

export interface ProductPatchRequest {
  categoryId?: string | null;
  cid?: string;
  name?: string;
  groupId?: string | null;
  title?: string;
  /**
   * 打开编辑器时看到的版本号。后端比对不一致就回 409 REVISION_CONFLICT，
   * 而不是把别人的改动盖掉。老后端忽略该字段，因此可以一直带。
   */
  expectedRevision?: number;
}

export interface ProductPatchResponse {
  product: Pick<
    Product,
    "id" | "key" | "title" | "cid" | "categoryId" | "categoryName" | "groupId" | "group"
  > & { updatedAt: number; revision?: number; updatedBy?: string | null };
}

export interface ProductDeleteResponse {
  deleted: boolean;
  id: string;
  filesRemoved: number;
  stats: Partial<WarehouseStats>;
}

export interface BatchCategoryRequest {
  ids: string[];
  categoryId?: string | null;
  cid?: string;
  name?: string;
  groupId?: string | null;
  /**
   * 批量改分类时对「每件商品各自当前版本」的期望值。
   * 后端逐件比对，任何一件对不上就整体回滚并回 409 REVISION_CONFLICT；
   * 老后端忽略该字段。
   */
  expectedRevisions?: Record<string, number>;
}

export interface BatchCategoryResponse {
  updated: number;
  requested: number;
  failed: Array<{ id: string; reason: string }>;
  products: Array<
    Pick<Product, "id" | "cid" | "categoryId" | "categoryName" | "groupId" | "group">
  >;
}

/* ================================================================== *
 * 自定义分组（api.md §5）
 * ================================================================== */

export type GroupColor = "slate" | "rose" | "amber" | "emerald" | "sky" | "violet";

export interface Group {
  id: string;
  name: string;
  color: GroupColor | string;
  sort: number;
  count: number;
  createdAt: number;
  updatedAt: number;
}

export interface GroupsResponse {
  groups: Group[];
  total: number;
}

export interface GroupCreateRequest {
  name: string;
  color?: GroupColor;
  sort?: number;
}

export interface GroupDeleteResponse {
  deleted: boolean;
  id: string;
  productsUnassigned: number;
}

/* ================================================================== *
 * 类目（api.md §6）
 * ================================================================== */

export interface CategoryNode {
  cid: string;
  parentCid: string | null;
  name: string;
  nameZh: string;
  path: string;
  depth: number;
  isLeaf: boolean;
  children: CategoryNode[];
}

export interface CategoryStats {
  total: number;
  leaf: number;
  cached: boolean;
}

export interface CategoryTreeResponse {
  site: string;
  rootCid: string | null;
  syncedAt: number;
  stale: boolean;
  tree: CategoryNode[];
  stats: CategoryStats;
}

/* ================================================================== *
 * 图片翻译（api.md §7）
 * ================================================================== */

export type ImageSource = "main" | "detail" | "sku";
export type ImageSize = "1:1" | "16:9" | "9:16";
export type ImageJobStatus = "queued" | "submitted" | "ready" | "unknown" | "failed";

export interface ImageTranslateItem {
  productId?: string | null;
  detailId?: string | null;
  source: ImageSource;
  url?: string;
  imageBase64?: string | null;
  size?: ImageSize | null;
  /** 人工核对结论：keep 跳过不译；不传按识别结果 */
  action?: "keep" | "translate" | null;
}

export interface ImageTranslateRequest {
  batchId?: string | null;
  size?: ImageSize;
  retryOcr?: boolean;
  items: ImageTranslateItem[];
}

export interface ImageBudget {
  limitMilliyuan: number;
  reservedMilliyuan: number;
  remainingMilliyuan: number;
  unitMilliyuan: number;
}

export interface ImageJobBrief {
  id: string;
  productId: string | null;
  source: ImageSource | string;
  status: ImageJobStatus | string;
  filename: string | null;
  publicUrl?: string | null;
  uploadedAt?: number | null;
  uploadError?: string;
  taskId?: string | null;
  reservedMilliyuan: number;
  reused: boolean;
}

export interface ImageTranslateResponse {
  batchId: string;
  accepted: number;
  reused: number;
  jobs: ImageJobBrief[];
  budget: ImageBudget;
  jobsUrl: string;
}

export interface ImageJob {
  id: string;
  batchId: string | null;
  productId: string | null;
  detailId: string | null;
  source: ImageSource | string;
  sourceUrl: string | null;
  status: ImageJobStatus | string;
  model: string | null;
  size: ImageSize | string | null;
  reservedMilliyuan: number;
  taskId: string | null;
  upstreamStatus: string | null;
  filename: string | null;
  previewUrl: string | null;
  publicUrl?: string | null;
  uploadedAt?: number | null;
  uploadError?: string;
  message: string | null;
  retryOf: string | null;
  attempt: number;
  createdAt: number;
  updatedAt: number;
  readyAt: number | null;
}

export interface ImageJobProgress {
  polledAt: number;
  elapsedSeconds: number;
  estimateSeconds: number;
}

export interface ImageJobResponse {
  job: ImageJob;
  progress: ImageJobProgress;
}

/**
 * GET /images/verify 的返回（密钥配置自检，不发起付费请求）。
 *
 * 老版有 verify_image_api.py 可以跑一次付费单图验证密钥，新版这条入口整个丢了，
 * 用户配错 image-key.txt 只能等批量翻译提交才发现。这个接口就是补回来的入口：
 * configured=false 时 message 里带着「文件放哪、叫什么名字」的具体说明。
 */
export interface ImageVerifyResponse {
  /** 是否已读到密钥（不代表密钥有效——有效性要走 POST 付费冒烟） */
  configured: boolean;
  /** direct=官方端点直连；proxy=走 AOHUANG_IMAGE_PROXY；未配置时为空串 */
  mode: "direct" | "proxy" | "";
  /** 只含前后各 4 位，如 "sk-a...4f9c"；绝不返回完整密钥 */
  keyHint: string;
  /** 人话来源，如「数据目录 image-key.txt」「老版遗留文件 codesonline-key.txt」 */
  source: string;
  /** 命中的文件绝对路径（环境变量来源时为空） */
  path: string;
  /** 实际会打的端点，便于核对代理地址有没有配错 */
  endpoint: string;
  /** 未配置时是可行动的处理说明；已配置时是「要不要做付费验证」的提示 */
  message: string;
}

/**
 * POST /images/verify 的返回（付费冒烟：真发一张 64×64 的图）。
 *
 * 注意：**失败也是 200**。后端把上游的失败翻进 body（ok=false + message 原文），
 * 只有「没带 confirm」（400）、「没配密钥」（503）、只读模式才会走 HTTP 错误。
 * 所以界面必须两种都看：先看 HTTP 错误，再看 ok 字段，否则一次失败会显示成成功。
 */
export interface ImageVerifyPaidResponse {
  /** 上游是否受理了这张测试图 */
  ok: boolean;
  /** 上游任务状态：submitted / ready / failed / unknown */
  status: string;
  /** 人话结论；失败时是上游错误原文（后端已抹掉密钥） */
  message: string;
  /** 这次冒烟占用的预算（毫元），成功失败都会占 */
  reservedMilliyuan: number;
  /** 与 GET 同源的密钥指纹（前后各 4 位） */
  keyHint: string;
  /** 实际打到的端点 */
  endpoint: string;
  /** 后端审计日志里的请求号，排查「谁在扣费」时用它对齐 */
  requestId: string;
  /** 上游任务 id（异步受理时才有） */
  taskId?: string;
}

/** GET /images/jobs 的返回（任务列表，带 publicUrl / uploadError） */
export interface ImageJobsResponse {
  items: ImageJob[];
  page: number;
  pageSize: number;
  total: number;
  hasMore: boolean;
  budget: ImageBudget;
}

/** POST /images/jobs/{id}/upload 的返回：传成功后 job.publicUrl 就有值了 */
export interface ImageJobUploadResponse {
  job: ImageJob;
}

/* ================================================================== *
 * 发布（api.md §8）
 * ================================================================== */

export type PublishItem =
  | { warehouseId: string; detailId?: undefined; shopId?: undefined }
  | { detailId: string; shopId: string; warehouseId?: undefined };

export interface PublishRequest {
  items: PublishItem[];
  shopId: string;
  accountId?: string | null;
  template?: string | null;
  exchangeRate?: string | null;
  language?: string | null;
  allowMismatch?: boolean;
  confirmUnverifiedSpecifications?: boolean;
  confirm: true;
  claimOnly?: boolean;
  titles?: Record<string, string>;
}

/**
 * 发布时按模板重算售价的明细（server/services/publish_service.py: _repricing 的 reports 项）。
 * 字段名与后端返回逐字对应：warehouseId / detailId / priceBefore / priceAfter /
 * currency / weightKg / changed / skuCount。后端在取不到样本价时会回 null。
 */
export interface RepricedEntry {
  warehouseId: string | null;
  detailId: string | null;
  priceBefore: string | null;
  priceAfter: string | null;
  currency: string;
  weightKg: string | null;
  /** 被改价的 SKU 数 */
  changed: number | null;
  /** 该商品的 SKU 总数 */
  skuCount: number | null;
}

export type PublishLogStatus = "processing" | "success" | "fail" | "cancel";

export interface PublishRecord {
  logId: string;
  detailId: string | null;
  shopId: string;
  shopName: string;
  status: PublishLogStatus | string;
  reason: string;
  requestedAt: number;
  platformItemId?: string | null;
}

export interface PublishResponse {
  publishRequested: boolean;
  mode: "direct" | "carrier" | string;
  batchId: string;
  targetShopId: string;
  published: string[];
  claimed: string[];
  failures: Array<{ detailId?: string; reason?: string }>;
  warning: string | null;
  repriced: RepricedEntry[];
  records: { total: number; items: PublishRecord[] };
}

export interface PublishLog {
  logId: string;
  detailId: string | null;
  warehouseId: string | null;
  warehouseKey: string | null;
  shopId: string;
  shopName: string;
  title: string;
  status: PublishLogStatus | string;
  reason: string;
  mode: string | null;
  batchId: string | null;
  scheduleId: string | null;
  accountId: string | null;
  platformItemId: string | null;
  itemEditUrl: string | null;
  attempts: number;
  requestedAt: number;
  finishedAt: number | null;
  /** 发起这次发布的操作员名；老后端 / 未记录时为 null */
  operatorName?: string | null;
}

export interface PublishLogsResponse extends Page<PublishLog> {
  benchOnly: boolean;
  platformTotal: number | null;
  degraded: boolean;
}

/* ================================================================== *
 * 定时发布（api.md §9）
 * ================================================================== */

export type ScheduleFrequency = "daily" | "weekly" | "monthly" | "range";
export type ScheduleRunStatus = "ok" | "partial" | "failed" | "empty";

export interface ScheduleLastResult {
  status: ScheduleRunStatus | string;
  count: number;
  failed: number;
  at: number;
}

export interface Schedule {
  id: string;
  name: string;
  enabled: boolean;
  accountId: string;
  account: string;
  shopId: string;
  shopName: string;
  template: string;
  frequency: ScheduleFrequency | string;
  days: number[];
  dayOfMonth: number[];
  startDate: string;
  endDate: string;
  time: string;
  perRun: number;
  claimOnly: boolean;
  lastRunDate: string;
  attemptDate: string;
  attempts: number;
  nextDueAt: number | null;
  nextDueText: string;
  createdAt: number;
  updatedAt: number;
  lastResult?: ScheduleLastResult | null;
}

export interface SchedulesResponse {
  items: Schedule[];
  total: number;
  serverTime: number;
}

export type ScheduleCreateRequest = Omit<
  Schedule,
  | "id"
  | "account"
  | "shopName"
  | "lastRunDate"
  | "attemptDate"
  | "attempts"
  | "nextDueAt"
  | "nextDueText"
  | "createdAt"
  | "updatedAt"
  | "lastResult"
> & { name?: string | null; days?: number[]; dayOfMonth?: number[]; startDate?: string; endDate?: string };

export type SchedulePatchRequest = Partial<
  Omit<ScheduleCreateRequest, "accountId" | "shopId" | "template">
>;

export interface ScheduleDeleteResponse {
  deleted: boolean;
  id: string;
  logsKept: number;
}

export interface ScheduleRunResultItem {
  detailId: string | null;
  status: string;
  title?: string;
  reason?: string;
}

export interface ScheduleRunResult {
  taskId: string;
  status: ScheduleRunStatus | string;
  requested: number;
  count: number;
  failed: number;
  reason: string;
  results: ScheduleRunResultItem[];
  at: number;
  runId?: string;
}

/* ================================================================== *
 * 违规词库（api.md §10）
 * ================================================================== */

export type WordCategory = "remove";
export type WordAction = "add" | "remove" | "import" | "remove-many" | "clear";
export type WordSource = "seed" | "manual";

export interface ViolationWord {
  id: string;
  word: string;
  normalized: string;
  action: WordCategory | string;
  source: WordSource | string;
  siteScope?: string | null;
  sourceUrl?: string | null;
  ruleVersion?: string | null;
  enabled: boolean;
  createdAt: number;
}

export interface WordCounts {
  remove: number;
  review: number;
  limit: number;
  remaining: number;
}

export interface ViolationWordsResponse extends Page<ViolationWord> {
  counts: WordCounts;
}

export interface WordsMutationRequest {
  action: WordAction;
  word?: string;
  /** 数组或纯文本都接受，纯文本按换行/逗号/顿号/分号/制表符切分 */
  words?: string[] | string;
  category?: WordCategory;
  siteScope?: string | null;
  sourceUrl?: string | null;
  ruleVersion?: string | null;
  mode?: "append" | "replace";
  confirm?: true;
}

export interface WordsMutationResponse {
  items: ViolationWord[];
  added: number;
  removed: number;
  /** 被跳过的**词**本身（重复、超长、放不下），不只是个数 */
  skipped: string[];
  skippedCount: number;
  /** 超过单次 500 条上限时截断，剩余部分需要再提交一次 */
  truncated?: boolean;
  processed?: number;
  limit?: number;
  counts: WordCounts;
}

/* ================================================================== *
 * 批次编排（POST /batch 系列）
 *
 * 后端没有常驻线程：`POST /batch/{id}/step` 只推进一件，调用方（界面上的
 * 「推进一步」/ 自动推进开关）自己循环。字段名全部按
 * services/batch_service.py 的 serialize() / serialize_item() 抄，
 * 不是按老版 automation-batch.json 的形状猜的。
 * ================================================================== */

/** 批次状态机：pending → running → done，可 paused / stopped / failed */
export type BatchStatus =
  | "pending"
  | "running"
  | "paused"
  | "stopped"
  | "done"
  | "failed";

/** 子项状态：running 表示「正在提交」，崩在这个状态上即结果未知 */
export type BatchItemStatus = "pending" | "running" | "ok" | "failed" | "skipped";

/** batch.results 里的结果快照（人工核对看的就是这份） */
export interface BatchResultRow {
  detailId: string;
  status: BatchItemStatus | string;
  message: string;
  attempt: number;
  at: number;
}

/** batch.errors 里的一条（后端只保留最近 50 条） */
export interface BatchErrorRow {
  detailId: string;
  message: string;
  attempt: number;
  at: number;
  /** true = 上一次提交结果未知，重跑前必须先去妙手核对 */
  unknownResult: boolean;
}

/** 逐件处理参数，键名与 /collect-box/{id}/process 的 ProcessRequest 对齐 */
export interface BatchSettings {
  language?: string;
  template?: string;
  groupId?: string | null;
  exchangeRate?: string | number | null;
  /** false = 只预览不入仓 */
  save?: boolean;
  processImages?: boolean;
  mainImageCount?: number;
}

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

export interface BatchItem {
  id: string;
  batchId: string;
  detailId: string;
  status: BatchItemStatus | string;
  /** 失败原因 / 跳过原因，直接给人看 */
  message: string;
  /** 详情快照 sha256，幂等闸的判据 */
  payloadHash: string;
  attempt: number;
  createdAt: number;
  updatedAt: number;
}

/** GET /batch —— {items, total}（limit 由后端夹到 1..200） */
export interface BatchListResponse {
  items: Batch[];
  total: number;
}

/** GET /batch/{id}、POST /batch —— {batch, items} */
export interface BatchDetailResponse {
  batch: Batch;
  items: BatchItem[];
}

export interface BatchCreateRequest {
  detailIds: string[];
  shopId: string;
  accountId?: string | null;
  /** 逐件参数；老版散在 body 顶层的 template / language 等，新版统一进 settings */
  settings?: BatchSettings;
}

/** POST /batch/{id}/step —— 一件的结果 */
export interface BatchStepResponse {
  batch: Batch;
  item: BatchItem | null;
  /** true = 这一件推进成功；false 可能是暂停中，也可能是这件失败了 */
  advanced: boolean;
  /** 后端给的人话原因，前端直接展示，不自己编 */
  message: string;
  product?: Record<string, unknown>;
  images?: Record<string, unknown>;
}

/** POST /batch/{id}/control —— 暂停 / 继续 / 停止 */
export type BatchControlAction = "pause" | "resume" | "stop";

export interface BatchControlResponse {
  batch: Batch;
  message: string;
}

/** POST /batch/{id}/retry-failed */
export interface BatchRetryResponse {
  batch: Batch;
  retried: number;
  message: string;
}

/** DELETE /batch/{id} */
export interface BatchDeleteResult {
  deleted: boolean;
  id: string;
  items: number;
}

/* ================================================================== *
 * 系统（api.md §11）
 * ================================================================== */

export interface SystemVersion {
  name: string;
  nameEn?: string;
  current: string;
  latest: string;
  updateAvailable: boolean;
  ok: boolean;
  error?: string;
  url?: string;
  sha256?: string;
  sizeBytes?: number;
  notes?: string;
  publishedAt?: string;
  channel?: string;
  manifestUrl: string;
  platform?: string;
  checkedAt: number;
  /**
   * 以下字段只有 GET /system/update 会回（/system/version 没有）：
   * 光知道「有新版本」不够，界面还得知道能不能装、是不是只读，
   * 否则用户点了下载才被 403 或 UNSUPPORTED_PLATFORM 拦住。
   */
  /** 上次下载留下的安装包，可直接安装；空串 / null 表示没有 */
  pendingFile?: string | null;
  /** GET 恒为 false（只查询不落盘），POST 下载成功后才为 true */
  downloaded?: boolean;
  /** 当前平台能否自动（静默）安装 */
  canInstall?: boolean;
  /** 只读模式：服务端会拒绝下载，按钮要提前禁掉 */
  readOnly?: boolean;
  /** 上游给了 sha256 且对上才算校验通过；false = 这份包来源未被证明 */
  sha256Verified?: boolean;
  /** 安装包落盘路径（POST 返回） */
  path?: string;
  /** 实际写入字节数（POST 返回；与清单里的 sizeBytes 不是一回事） */
  size?: number;
}

export interface SystemUpdateRequest {
  url: string;
  sha256: string;
  restart?: boolean;
}

/** POST /system/update 请求体：url / sha256 都可以不传，不传就用清单里的 */
export interface UpdateDownloadRequest {
  url?: string;
  sha256?: string;
  /** true = 下完立刻静默安装（会退出当前程序） */
  install?: boolean;
}

/**
 * POST /system/update 的返回。
 *
 * 字段多给 `?`：共享模式下服务端只回 `accepted:false / downloaded:false`，
 * 没有 path / size，界面按「没下成」处理即可，不该被类型骗成一定有值。
 */
export interface UpdateDownloadResult {
  ok?: boolean;
  accepted?: boolean;
  version?: string;
  url?: string;
  /** 落盘路径（`file` 是后端同值老字段，两个都留着防其中一边改名） */
  path?: string;
  file?: string;
  /** 实际写入字节数 */
  size?: number;
  /** 本地算出来的摘要，未必等于清单里的 */
  sha256?: string;
  /** 只有上游给了 sha256 并且校验通过才是 true */
  sha256Verified?: boolean;
  downloaded?: boolean;
  canInstall?: boolean;
  /** install: true 时后端拉起安装器的结果（平台相关，前端只透传不解释） */
  installed?: unknown;
  willRestart?: boolean;
  /** 未下载的原因（如 shared-mode） */
  reason?: string;
}

export interface SystemUpdateResponse {
  ok: boolean;
  accepted: boolean;
  reason?: string;
  version?: string;
  file?: string;
  sha256?: string;
  sizeBytes?: number;
  willRestart?: boolean;
  startedAt?: number;
}

export interface HealthResponse {
  ok: boolean;
  name: string;
  version: string;
  db: "ok" | "migrating" | "error";
  uptimeSeconds: number;
}

/* ================================================================== *
 * 操作员与审计日志（多用户协作）
 *
 * 契约要点：
 *   · token 只在 POST /operators 与 POST /operators/{id}/rotate 的响应里出现一次，
 *     后端只存哈希，之后再也拿不回来 —— 界面必须当场把明文摊开让人复制。
 *   · 请求带 X-Operator（或 Authorization: Bearer）认人；都不带则归到内置「本机」。
 *   · 本机密钥（secret.key）解析出来的是内置的「本机」操作员，不是某个真人。
 * ================================================================== */

export interface Operator {
  id: string;
  name: string;
  username?: string | null;
  hasLoginCredentials?: boolean;
  /** 管理员能改名 / 停用 / 重置别人的 token */
  admin: boolean;
  /** 三级角色：admin / teacher / member */
  role?: "admin" | "teacher" | "member";
  /** 停用后该 token 立即失效，但历史记录保留 */
  enabled: boolean;
  /** 最后一次带该 token 发起请求的时间（Unix 秒）；从未用过为 null */
  lastActiveAt: number | null;
  createdAt: number;
  /** 内置「本机」操作员：不可改名、不可停用、不可重置 */
  builtin?: boolean;
}

export interface SoftwareLoginResponse {
  operator: Pick<Operator, "id" | "name" | "role">;
  token: string;
  mustChangePassword: boolean;
}

export interface StudentCreateRequest {
  name: string;
  username: string;
  password: string;
}

export interface StudentCreateResponse {
  operator: Operator & { username?: string };
}

export interface OperatorsResponse {
  operators: Operator[];
  total: number;
  /** 当前请求所用的身份（无 X-Operator 时为内置「本机」） */
  current?: Operator | null;
}

export interface OperatorCreateRequest {
  name: string;
  admin?: boolean;
  role?: "admin" | "teacher" | "member";
}

/** POST /operators、POST /operators/{id}/rotate 的响应 —— token 只在这里出现一次 */
export interface OperatorTokenResponse {
  operator: Operator;
  token: string;
}

export interface OperatorPatchRequest {
  name?: string;
  role?: "admin" | "teacher" | "member";
}

/** GET /audit-logs 的查询参数 */
export interface AuditLogQuery {
  operatorId?: string | null;
  /** 动作标识，如 warehouse.patch、publish.submit */
  action?: string | null;
  targetKind?: string | null;
  /** 结果：success / failed / blocked */
  result?: string | null;
  limit?: number;
  /** 游标：返回该时间点之前的记录（Unix 秒） */
  before?: number | null;
}

export type AuditLogResult = "success" | "failed" | "blocked" | string;

export interface AuditLog {
  id: string;
  operatorId: string | null;
  operatorName: string;
  /** 动作标识（英文点号命名，界面按 ACTION_TEXT 翻译） */
  action: string;
  /** 对象类型：product / batch / publish / schedule / operator … */
  targetKind: string | null;
  targetId: string | null;
  /** 给人看的一句话：对象是什么 */
  targetLabel: string | null;
  result: AuditLogResult;
  /** 失败/拦截的原因；成功时为空串 */
  message: string | null;
  /** 结构化附加信息（版本号、数量等） */
  details: Record<string, unknown> | null;
  createdAt: number;
}

/** GET /audit-logs 的响应：游标分页（不是 page/pageSize） */
export interface AuditLogsResponse {
  items: AuditLog[];
  total: number;
  /** 下一页游标；为 null 表示到底了 */
  nextBefore: number | null;
}
