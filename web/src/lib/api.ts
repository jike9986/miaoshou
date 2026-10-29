// api.md §0 通用约定 · §0.2 认证 · §0.3 请求追踪与幂等 · §0.4 分页 · §0.5 错误信封
// 统一 fetch 封装：Bearer 鉴权、超时、错误归一化（暴露 error.code）、幂等键、请求 id。

import type { ApiErrorCode, ApiErrorPayload } from "@/lib/types";
import { operatorToken } from "@/lib/operator";
import { getSavedApiBase } from "@/lib/serverAddress";

/* ------------------------------------------------------------------ *
 * 运行时配置
 * ------------------------------------------------------------------ */

const DEFAULT_API_BASE = "";
const FALLBACK_API_BASE = "http://127.0.0.1:8812";
const API_PREFIX = "/api/v1";
export const DEFAULT_TIMEOUT_MS = 30_000;

/**
 * 后端 base（不含 /api/v1）。
 * 优先级：用户保存的地址 → window.__DSH_BOOT__.apiBase（Electron 注入的实探端口）→ ?apiBase= 查询参数 →
 * window.__AOHUANG_API_BASE__（旧注入点）→ VITE_API_BASE（.env.development）→
 * dev 走 Vite 代理（空串，相对路径）→ 生产兜底本机 8812。
 */
export function resolveApiBase(): string {
  if (typeof window === "undefined") return DEFAULT_API_BASE;
  const savedBase = getSavedApiBase();
  if (savedBase) return savedBase;
  const boot = window.__DSH_BOOT__;
  if (boot?.apiBase) return boot.apiBase.replace(/\/+$/, "");
  // Electron 生产模式用 file:// 加载页面，没有 Vite 代理，地址通过查询参数传入。
  const fromQuery = new URLSearchParams(window.location.search).get("apiBase");
  if (fromQuery) return fromQuery.replace(/\/+$/, "");
  if (window.__AOHUANG_API_BASE__) return window.__AOHUANG_API_BASE__.replace(/\/+$/, "");
  const envBase = import.meta.env.VITE_API_BASE;
  if (envBase) return envBase.replace(/\/+$/, "");
  return import.meta.env.DEV ? DEFAULT_API_BASE : FALLBACK_API_BASE;
}

function isDefaultApiTarget(): boolean {
  if (typeof window === "undefined") return true;
  if (getSavedApiBase()) return false;
  if (window.__AOHUANG_API_BASE__) return false;
  if (import.meta.env.VITE_API_BASE) return false;
  const bootBase = window.__DSH_BOOT__?.apiBase?.replace(/\/+$/, "");
  const queryBase = new URLSearchParams(window.location.search).get("apiBase")?.replace(/\/+$/, "");
  if (!bootBase && !queryBase) return true;
  if (bootBase && (!queryBase || queryBase === bootBase)) {
    try {
      const host = new URL(bootBase).hostname;
      return host === "127.0.0.1" || host === "localhost" || host === "[::1]";
    } catch {
      return false;
    }
  }
  return false;
}

function requestToken(path?: string): string | null {
  if (operatorToken()) return operatorToken();
  if (path === "/auth/initialize-credentials" && isDefaultApiTarget()) {
    return resolveToken();
  }
  return null;
}

/** token 来源：preload 注入的内存值（api.md §0.2，不落 localStorage） */
export function resolveToken(): string | null {
  if (typeof window === "undefined") return null;
  const boot = window.__DSH_BOOT__;
  if (boot?.token) return boot.token;
  if (window.__AOHUANG_TOKEN__) return window.__AOHUANG_TOKEN__;
  return null;
}

/** 客户端版本，用于服务端 system/version 比对 */
export function resolveClientVersion(): string {
  if (typeof window === "undefined") return "0.0.0";
  return window.__DSH_BOOT__?.version ?? "0.0.0";
}

/* ------------------------------------------------------------------ *
 * 错误对象
 * ------------------------------------------------------------------ */

/**
 * 归一化后的接口错误。分支判断只看 code（api.md §0.5）。
 *
 * try { await api.get("/warehouse") }
 * catch (e) { if (isApiError(e) && e.code === "READ_ONLY_MODE") ... }
 */
export class ApiError extends Error {
  readonly code: ApiErrorCode;
  readonly status: number;
  readonly details: Record<string, unknown> | null;
  readonly requestId: string | null;

  constructor(payload: ApiErrorPayload, status = 0) {
    super(payload.message);
    this.name = "ApiError";
    this.code = payload.code;
    this.status = status;
    this.details = payload.details;
    this.requestId = payload.requestId;
  }

  /** 上游透传的原始错误（api.md §0.5 上游错误透传规则） */
  get upstream(): Record<string, unknown> | null {
    const up = this.details?.["upstream"];
    return up !== null && typeof up === "object" ? (up as Record<string, unknown>) : null;
  }

  /** 是否为「需要用户处理」的阻断性错误（用于 Toast vs 错误态判断） */
  get isBlocking(): boolean {
    return this.status >= 500 || this.status === 401 || this.status === 403 || this.code === "NETWORK_ERROR";
  }

  toJSON(): ApiErrorPayload {
    return {
      code: this.code,
      message: this.message,
      details: this.details,
      requestId: this.requestId,
    };
  }
}

export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError;
}

/** 从未知错误里取 code（给 React Query onError 用） */
export function errorCode(error: unknown): ApiErrorCode | null {
  return isApiError(error) ? error.code : null;
}

/* ------------------------------------------------------------------ *
 * 错误码 → 中文文案（与后端 message 对齐，作为兜底）
 * 后端总会给 message，这里只处理网络层/未知状态。
 * ------------------------------------------------------------------ */

export const API_ERROR_TEXT: Partial<Record<ApiErrorCode, string>> = {
  NETWORK_ERROR: "无法连接本地服务，请确认后台已启动",
  TIMEOUT: "请求超时，请稍后重试",
  ABORTED: "请求已取消",
  BAD_RESPONSE: "服务响应格式异常",
  BOOT_MISSING: "缺少启动参数，请重启应用",
  AUTH_REQUIRED: "缺少访问令牌，请重启应用",
  TOKEN_INVALID: "访问令牌无效，请重启应用",
  READ_ONLY_MODE: "当前为只读模式，无法执行写操作",
  RATE_LIMITED: "操作过于频繁，请稍后再试",
  CONFLICT_BUSY: "当前有任务正在执行，请稍后重试",
  REVISION_CONFLICT: "这件商品在你编辑期间已被改动",
  INTERNAL_ERROR: "服务内部错误，请查看日志",
  UPSTREAM_ERROR: "上游服务返回错误",
  UPSTREAM_UNKNOWN_RESULT: "上游结果未确认，可能已产生费用，请人工核对",
  UPSTREAM_RATE_LIMIT: "上游限流，请稍后重试",
  UPSTREAM_TIMEOUT: "上游服务超时",
};

export function apiErrorMessage(error: unknown): string {
  if (isApiError(error)) {
    if (error.message) return error.message;
    return API_ERROR_TEXT[error.code] ?? "操作失败";
  }
  if (error instanceof Error && error.message) return error.message;
  return "操作失败";
}

/* ------------------------------------------------------------------ *
 * 请求
 * ------------------------------------------------------------------ */

export type QueryValue = string | number | boolean | null | undefined;
export type QueryParams = Record<string, QueryValue>;

export interface RequestOptions {
  /** 查询参数（null/undefined 自动丢弃；布尔转 "true"/"false"） */
  query?: QueryParams;
  headers?: Record<string, string>;
  /** 超时毫秒，默认 30s（发布/图片提交可传更长） */
  timeoutMs?: number;
  /** 外部取消信号（组件卸载、轮询停止） */
  signal?: AbortSignal;
  /** 幂等键（仅 POST /publish、/images/translate、/schedules 支持） */
  idempotencyKey?: string;
  /** 不抛错，返回 { data, error }（表单场景） */
  noThrow?: boolean;
}

export interface ApiResult<T> {
  data: T | null;
  error: ApiError | null;
}

function buildUrl(path: string, query?: QueryParams): string {
  const base = resolveApiBase();
  const normalized = path.startsWith("/") ? path : `/${path}`;
  const url = `${base}${API_PREFIX}${normalized}`;
  if (!query) return url;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === null || value === undefined) continue;
    params.append(key, String(value));
  }
  const qs = params.toString();
  return qs ? `${url}?${qs}` : url;
}

/** 请求 id（api.md §0.3，服务端会回传） */
function createRequestId(): string {
  const random =
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID().replace(/-/g, "").slice(0, 20)
      : Math.random().toString(36).slice(2, 12);
  return `req_${random}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** 从响应体里提取错误信封（api.md §0.5） */
function parseErrorEnvelope(body: unknown, status: number, requestId: string): ApiErrorPayload {
  if (isRecord(body) && isRecord(body["error"])) {
    const raw = body["error"] as Record<string, unknown>;
    const code = typeof raw["code"] === "string" && raw["code"] ? raw["code"] : fallbackCode(status);
    const message = typeof raw["message"] === "string" && raw["message"] ? raw["message"] : null;
    return {
      code,
      message: message ?? API_ERROR_TEXT[code] ?? `请求失败（HTTP ${status}）`,
      details: isRecord(raw["details"]) ? raw["details"] : null,
      requestId: typeof raw["requestId"] === "string" ? raw["requestId"] : requestId,
    };
  }
  const code = fallbackCode(status);
  return {
    code,
    message: API_ERROR_TEXT[code] ?? `请求失败（HTTP ${status}）`,
    details: null,
    requestId,
  };
}

function fallbackCode(status: number): ApiErrorCode {
  switch (status) {
    case 400:
      return "VALIDATION_ERROR";
    case 401:
      return "AUTH_REQUIRED";
    case 403:
      return "READ_ONLY_MODE";
    case 404:
      return "NOT_FOUND";
    case 409:
      return "CONFLICT_BUSY";
    case 422:
      return "UNPROCESSABLE";
    case 429:
      return "RATE_LIMITED";
    case 502:
      return "UPSTREAM_ERROR";
    case 503:
      return "UPSTREAM_RATE_LIMIT";
    case 504:
      return "UPSTREAM_TIMEOUT";
    default:
      return status >= 500 ? "INTERNAL_ERROR" : "BAD_RESPONSE";
  }
}

/** 核心请求函数：所有 api.* 都走它 */
async function request<T, B = unknown>(
  method: "GET" | "POST" | "PATCH" | "DELETE" | "PUT",
  path: string,
  body?: B,
  options: RequestOptions = {}
): Promise<T> {
  const { query, headers = {}, timeoutMs = DEFAULT_TIMEOUT_MS, signal, idempotencyKey } = options;

  const isSoftwareLogin = method === "POST" && path === "/auth/login";
  const token = isSoftwareLogin ? null : requestToken(path);
  if (!isSoftwareLogin && !token) {
    throw new ApiError({
      code: "BOOT_MISSING",
      message: API_ERROR_TEXT.BOOT_MISSING ?? "缺少启动参数",
      details: null,
      requestId: null,
    });
  }

  const controller = new AbortController();
  let timedOut = false;
  const timer = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);

  const onExternalAbort = (): void => controller.abort();
  signal?.addEventListener("abort", onExternalAbort);

  const requestId = createRequestId();
  const finalHeaders: Record<string, string> = {
    Accept: "application/json",
    "X-Client-Version": resolveClientVersion(),
    "X-Request-Id": requestId,
    ...headers,
  };
  if (token) finalHeaders.Authorization = `Bearer ${token}`;
  if (body !== undefined) finalHeaders["Content-Type"] = "application/json; charset=utf-8";
  if (idempotencyKey) finalHeaders["Idempotency-Key"] = idempotencyKey;

  let response: Response;
  try {
    response = await fetch(buildUrl(path, query), {
      method,
      headers: finalHeaders,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
      credentials: "omit",
      cache: "no-store",
    });
  } catch (error) {
    const aborted = error instanceof DOMException && error.name === "AbortError";
    if (aborted && timedOut) {
      throw new ApiError(
        {
          code: "TIMEOUT",
          message: API_ERROR_TEXT.TIMEOUT ?? "请求超时",
          details: { timeoutMs, path },
          requestId,
        },
        0
      );
    }
    if (aborted) {
      throw new ApiError(
        {
          code: "ABORTED",
          message: API_ERROR_TEXT.ABORTED ?? "请求已取消",
          details: { path },
          requestId,
        },
        0
      );
    }
    throw new ApiError(
      {
        code: "NETWORK_ERROR",
        message: API_ERROR_TEXT.NETWORK_ERROR ?? "网络错误",
        details: { path, reason: error instanceof Error ? error.message : String(error) },
        requestId,
      },
      0
    );
  } finally {
    window.clearTimeout(timer);
    signal?.removeEventListener("abort", onExternalAbort);
  }

  // 204 / 空响应
  if (response.status === 204) return undefined as T;

  const text = await response.text();
  let parsed: unknown = null;
  if (text) {
    try {
      parsed = JSON.parse(text) as unknown;
    } catch {
      if (response.ok) {
        // 二进制/纯文本成功响应（极少场景）
        return text as unknown as T;
      }
      // 非 JSON 的错误体（代理 502 页面、FastAPI 未捕获异常）：按状态码归一化
      const code = fallbackCode(response.status);
      throw new ApiError(
        {
          code,
          message: API_ERROR_TEXT[code] ?? `请求失败（HTTP ${response.status}）`,
          details: { status: response.status, path, bodyPreview: text.slice(0, 200) },
          requestId: response.headers.get("X-Request-Id") ?? requestId,
        },
        response.status
      );
    }
  }

  if (!response.ok) {
    const payload = parseErrorEnvelope(parsed, response.status, requestId);
    throw new ApiError(
      { ...payload, requestId: payload.requestId ?? response.headers.get("X-Request-Id") },
      response.status
    );
  }

  return parsed as T;
}

/** 不抛错版本：返回 { data, error }；表单/批量操作需要就地显示错误时用 */
async function requestSafe<T, B = unknown>(
  method: "GET" | "POST" | "PATCH" | "DELETE" | "PUT",
  path: string,
  body?: B,
  options: RequestOptions = {}
): Promise<ApiResult<T>> {
  try {
    const data = await request<T, B>(method, path, body, options);
    return { data, error: null };
  } catch (error) {
    if (isApiError(error)) return { data: null, error };
    return {
      data: null,
      error: new ApiError({
        code: "INTERNAL_ERROR",
        message: apiErrorMessage(error),
        details: null,
        requestId: null,
      }),
    };
  }
}

/* ------------------------------------------------------------------ *
 * 导出：统一入口
 * ------------------------------------------------------------------ */

export const api = {
  get: <T>(path: string, options?: RequestOptions): Promise<T> =>
    request<T>("GET", path, undefined, options),

  post: <T, B = unknown>(path: string, body?: B, options?: RequestOptions): Promise<T> =>
    request<T, B>("POST", path, body, options),

  patch: <T, B = unknown>(path: string, body?: B, options?: RequestOptions): Promise<T> =>
    request<T, B>("PATCH", path, body, options),

  put: <T, B = unknown>(path: string, body?: B, options?: RequestOptions): Promise<T> =>
    request<T, B>("PUT", path, body, options),

  del: <T, B = unknown>(path: string, body?: B, options?: RequestOptions): Promise<T> =>
    request<T, B>("DELETE", path, body, options),

  /**
   * 上传原始字节（不是 JSON）。
   *
   * 给「上传尺码图」用：后端 POST /size-charts/upload 直接读 request.body()，
   * 按魔数判图片类型，不接收 multipart。所以这里把 File 原样当 body 发出去，
   * 只带 X-File-Name 供后端取扩展名参考。
   *
   * 走同一套 token / 错误解析，不另开一份 fetch 逻辑。
   */
  upload: <T>(
    path: string,
    file: Blob,
    options: RequestOptions & { fileName?: string } = {}
  ): Promise<T> => {
    const { fileName, headers = {}, timeoutMs = 120_000, signal } = options;
    const token = requestToken();
    if (!token) {
      throw new ApiError({
        code: "BOOT_MISSING",
        message: API_ERROR_TEXT.BOOT_MISSING ?? "缺少启动参数",
        details: null,
        requestId: null,
      });
    }
    const requestId = createRequestId();
    const finalHeaders: Record<string, string> = {
      Accept: "application/json",
      Authorization: `Bearer ${token}`,
      "X-Client-Version": resolveClientVersion(),
      "X-Request-Id": requestId,
      "Content-Type": file.type || "application/octet-stream",
      ...(fileName ? { "X-File-Name": encodeURIComponent(fileName) } : {}),
      ...headers,
    };
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeoutMs);
    signal?.addEventListener("abort", () => controller.abort());
    return fetch(buildUrl(path), {
      method: "POST",
      headers: finalHeaders,
      body: file,
      signal: controller.signal,
      credentials: "omit",
      cache: "no-store",
    })
      .then(async (response) => {
        window.clearTimeout(timer);
        const text = await response.text();
        let payload: unknown = null;
        try {
          payload = text ? JSON.parse(text) : null;
        } catch {
          payload = null;
        }
        if (!response.ok) {
          throw new ApiError(
            parseErrorEnvelope(payload, response.status, response.headers.get("X-Request-Id") ?? requestId)
          );
        }
        return payload as T;
      })
      .finally(() => window.clearTimeout(timer));
  },

  /** 不抛错变体，返回 { data, error } */
  safe: {
    get: <T>(path: string, options?: RequestOptions): Promise<ApiResult<T>> =>
      requestSafe<T>("GET", path, undefined, options),
    post: <T, B = unknown>(path: string, body?: B, options?: RequestOptions): Promise<ApiResult<T>> =>
      requestSafe<T, B>("POST", path, body, options),
    patch: <T, B = unknown>(path: string, body?: B, options?: RequestOptions): Promise<ApiResult<T>> =>
      requestSafe<T, B>("PATCH", path, body, options),
    del: <T, B = unknown>(path: string, body?: B, options?: RequestOptions): Promise<ApiResult<T>> =>
      requestSafe<T, B>("DELETE", path, body, options),
  },
};

export default api;
