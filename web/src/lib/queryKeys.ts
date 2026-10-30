// architecture §4.6 前端状态分层 —— queryKey 规范
// 业务页只用这里的工厂函数拼 key，避免各页面写死字符串导致失效不准。

import type { WarehouseQuery } from "@/lib/types";

/** 服务端状态 key（TanStack Query） */
export const queryKeys = {
  auth: {
    all: ["auth"] as const,
    status: () => ["auth", "status"] as const,
  },
  shops: {
    all: ["shops"] as const,
    list: (accountId?: string | null) => ["shops", accountId ?? "current"] as const,
  },
  collectBox: {
    all: ["collect-box"] as const,
    // accountId 参与 key：切换妙手账号后不能复用上一个账号的采集箱缓存
    list: (params: {
      accountId?: string | null;
      page: number;
      pageSize: number;
      status?: string;
      keyword?: string;
      shopId?: string | null;
      site?: string | null;
      sort?: string;
    }) => ["collect-box", params] as const,
    detail: (detailId: string, shopId: string) => ["collect-box", detailId, shopId] as const,
  },
  warehouse: {
    all: ["warehouse"] as const,
    list: (query: WarehouseQuery) => ["warehouse", query] as const,
    automationResults: (page: number) => ["warehouse", "automation-results", page] as const,
    detail: (id: string, includePayload = false) => ["warehouse", id, { includePayload }] as const,
  },
  groups: {
    all: ["groups"] as const,
  },
  categories: {
    all: ["categories"] as const,
    tree: (params: { site?: string | null; depth?: number; leafOnly?: boolean; rootCid?: string | null }) =>
      ["categories", params] as const,
  },
  imageJobs: {
    all: ["image-jobs"] as const,
    detail: (id: string) => ["image-jobs", id] as const,
    list: (params: { batchId?: string; productId?: string; status?: string; page?: number }) =>
      ["image-jobs", "list", params] as const,
  },
  publishLogs: {
    all: ["publish-logs"] as const,
    list: (params: { page?: number; status?: string; shopId?: string; scheduleId?: string }) =>
      ["publish-logs", params] as const,
  },
  schedules: {
    all: ["schedules"] as const,
    list: (params: { accountId?: string | null; enabled?: boolean | null } = {}) =>
      ["schedules", params] as const,
  },
  violationWords: {
    all: ["violation-words"] as const,
    list: (params: { action?: string; q?: string; page?: number; pageSize?: number }) =>
      ["violation-words", params] as const,
  },
  system: {
    /**
     * 版本查询键。
     *
     * 这里有个历史坑，改之前先读：`useVersion` 曾经硬编码
     * `["system-version", refresh]`，而这里定义的是 `["system", "version", ...]`
     * ——两套键并存。后果是 `useCheckUpdate` 里的
     * `invalidateQueries({ queryKey: ["system-version"] })` 只失效了硬编码那套，
     * 任何用 `queryKeys.system.version()` 的地方**不会**被刷新，
     * 界面上表现为「点了检查更新，版本号没变」。
     *
     * 现在统一走这个函数。前缀 `["system", "version"]` 也能被
     * `invalidateQueries({ queryKey: ["system", "version"] })` 整族命中。
     */
    version: (refresh = true) => ["system", "version", { refresh }] as const,
    /** 整族前缀：失效版本相关的所有变体（不同 refresh 组合）时用它 */
    versionAll: ["system", "version"] as const,
    health: () => ["system", "health"] as const,
  },
  carrier: {
    all: ["carrier"] as const,
    links: () => ["carrier", "links"] as const,
  },
  titles: {
    all: ["titles"] as const,
    preview: (key: string) => ["titles", "preview", key] as const,
  },
  batch: {
    all: ["batch"] as const,
    list: (params: { accountId?: string | null; limit?: number } = {}) =>
      ["batch", "list", params] as const,
    detail: (id: string) => ["batch", "detail", id] as const,
  },
  automationRuns: {
    all: ["automation-runs"] as const,
    latest: (accountId: string | null, shopId: string | null) =>
      ["automation-runs", "latest", accountId, shopId] as const,
  },
  sync: {
    all: ["sync"] as const,
    priceTemplates: (params: { site?: string; name?: string; pageNo?: number }) =>
      ["sync", "price-templates", params] as const,
  },
  operators: {
    all: ["operators"] as const,
    list: () => ["operators", "list"] as const,
  },
  auditLogs: {
    all: ["audit-logs"] as const,
    list: (params: {
      operatorId?: string | null;
      action?: string | null;
      targetKind?: string | null;
      result?: string | null;
      // limit 允许 null：调用方用 `?? null` 统一把 undefined 归一成 null，
      // 这样缓存键的形状稳定（undefined 与 null 在 JSON 里都不出现，但作为
      // 对象字面量参与序列化时两者不同，混用会导致同一查询算出两个键）。
      limit?: number | null;
      before?: number | null;
    }) => ["audit-logs", params] as const,
  },
} as const;
