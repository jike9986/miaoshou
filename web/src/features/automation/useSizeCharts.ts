// features/automation —— 尺寸表数据（api.md §13）
//
// @/lib/queries 里没有尺寸表 hook，这里用 @/lib/api 的 client 直接调：
//   GET    /size-charts              列表（items / charts / total / sites / defaultUrl）
//   DELETE /size-charts/{id}         按 id 忘书签，不删图片
//   DELETE /size-charts              按地址删（body `{url}` 或 query `?url=`，新增能力）
//   POST   /size-charts              登记一个已有图床地址
//   POST   /size-charts/upload       传本地文件 → 图床 → 登记（老版等价接口）
//                                    body 是 application/json 时走「只登记地址」分支
//
// 上传语义与老版 POST /api/upload-size-chart 一致：后端收原始字节、按魔数判
// 图片类型、传到图床（SFTP 到 aohuang888.com），再记成书签。
//
// 后端另外两处与老版对齐、前端此前没接的能力：
//   · 列表响应里的 `defaultUrl`：一条本地尺码图都没有时也能用的兜底地址；
//   · DELETE /size-charts 认 url：老版 delete-size-chart 的入参只有 url。

import { useMutation, useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { api, apiErrorMessage, isApiError } from "@/lib/api";
import type { SizeChart, SizeChartsResponse } from "@/features/automation/types";

export const sizeChartsKey = ["size-charts"] as const;

/**
 * 列表响应。
 *
 * `defaultUrl` 是后端新加的字段（`size_chart_service.list_charts` 里固定带），
 * 类型放在本文件而不是 features/automation/types.ts，是因为那份类型文件
 * 不在这次改动范围内；这里用「继承 + 补字段」表达同一份契约，不影响既有调用方。
 */
export interface SizeChartsListResponse extends SizeChartsResponse {
  /** 项目默认尺码图地址；后端没配 / 配错时是空串，不是 null */
  defaultUrl: string;
}

export function useSizeCharts(enabled = true): UseQueryResult<SizeChartsListResponse> {
  return useQuery({
    queryKey: sizeChartsKey,
    queryFn: () => api.get<SizeChartsListResponse>("/size-charts"),
    enabled,
    staleTime: 30_000,
  });
}

/* ------------------------------------------------------------------ *
 * 写接口的响应形状
 *
 * 后端所有写接口都回 `{chart, items}` 信封（create / update / remember_url
 * 都走 create 或 update 的返回），而前端早前按「裸 chart」声明类型，
 * 结果报警时读到的是 undefined。这里统一压成单条，调用方不用记两种形状。
 * ------------------------------------------------------------------ */

interface SizeChartEnvelope {
  chart?: SizeChart;
  items?: SizeChart[];
  replaced?: boolean;
}

function unwrapChart(payload: SizeChart | SizeChartEnvelope): SizeChart {
  if (payload && typeof payload === "object" && "chart" in payload && payload.chart) {
    return payload.chart;
  }
  // 老接口 / 未来可能出现的裸返回：原样当 chart 用
  return payload as SizeChart;
}

/* ------------------------------------------------------------------ *
 * 默认图判定
 * ------------------------------------------------------------------ */

/** 兜底地址（后端没配时是空串） */
export function pickDefaultUrl(data: SizeChartsListResponse | null | undefined): string {
  const url = data?.defaultUrl;
  return typeof url === "string" ? url.trim() : "";
}

/** 后端认「项目默认图」的口径就是 url 相等（`delete_by_url` 里逐字比较），前端必须同源 */
export function isDefaultSizeChart(
  chart: Pick<SizeChart, "url"> | null | undefined,
  defaultUrl: string
): boolean {
  if (!chart || !defaultUrl) return false;
  return chart.url.trim() === defaultUrl.trim();
}

export const PROJECT_DEFAULT_CHART_NAME = "项目默认尺码图";

/**
 * 把兜底地址包装成一条「看起来像尺码图」的记录，只给预览用。
 *
 * id 留空串是刻意的：settings.sizeChartId 只认后端真实 id，
 * 拿一条编造的 id 去选中，后端 resolve 会落到别的图上（或报不存在）。
 */
export function buildDefaultSizeChart(defaultUrl: string): SizeChart | null {
  const url = defaultUrl.trim();
  if (!url) return null;
  return {
    id: "",
    name: PROJECT_DEFAULT_CHART_NAME,
    site: "",
    url,
    contentJson: null,
    createdAt: 0,
    updatedAt: 0,
  };
}

/* ------------------------------------------------------------------ *
 * 删除
 * ------------------------------------------------------------------ */

export interface DeleteSizeChartResult {
  deleted: boolean;
  id: string;
  name: string;
  url: string;
  imageKept: boolean;
}

export function useDeleteSizeChart() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (chartId: string) =>
      api.del<DeleteSizeChartResult>(`/size-charts/${encodeURIComponent(chartId)}`),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: sizeChartsKey });
    },
  });
}

export interface DeleteSizeChartByUrlResult {
  deleted: boolean;
  /** 同一条地址登记在多个站点时后端整条清掉，此时 id 为 null */
  id: string | null;
  name: string;
  site: string;
  url: string;
  /** 实际删掉的记录条数 */
  count: number;
  imageKept: boolean;
  items?: SizeChart[];
}

/**
 * DELETE /size-charts —— 按地址删除（老版 delete-size-chart 的形态）。
 *
 * 为什么要留这条：老接口回的记录只有 url 没有 id，而且后端「项目默认尺码图
 * 不能删除」的 400 只在按地址删时触发——按 id 删默认图反而会成功。
 * 所以界面上的默认图保护必须落在 UI 层（按钮 disabled），这条 hook 是兜底路径。
 */
export function useDeleteSizeChartByUrl() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (url: string) =>
      api.del<DeleteSizeChartByUrlResult, { url: string }>("/size-charts", { url }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: sizeChartsKey });
    },
  });
}

/* ------------------------------------------------------------------ *
 * 新建 / 上传
 * ------------------------------------------------------------------ */

export interface UploadSizeChartFileBody {
  file: File;
  name: string;
  site?: string | null;
}

/** 「用链接添加」：只给地址，不传文件（后端 JSON 分支，201） */
export interface UploadSizeChartUrlBody {
  url: string;
  name?: string;
  site?: string | null;
  category?: string | null;
}

/**
 * 上传体：带 file 走二进制分支，带 url 走 JSON 分支。
 *
 * 用同一个 hook 是因为后端把两条分支放在同一个 POST /size-charts/upload 上
 * （按 content-type 分流），调用方不必先判断自己手里是文件还是地址。
 */
export type UploadSizeChartBody = UploadSizeChartFileBody | UploadSizeChartUrlBody;

/** 老版上限：8 MB、PNG/JPEG/WebP。前端先挡一道，省得白传。 */
export const SIZE_CHART_MAX_BYTES = 8 * 1024 * 1024;
export const SIZE_CHART_ACCEPT = ["image/png", "image/jpeg", "image/webp"];

/**
 * POST /size-charts/upload —— 把本地图片传到图床并登记，或只登记一个地址。
 *
 * 文件分支用 api.upload 发原始字节（后端按魔数判类型，不收 multipart），
 * name / site 走 query，因为 body 整个是图片本身。
 * 地址分支走 api.post（自动带 application/json），字段放 body 里。
 */
export function useUploadSizeChart() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (body: UploadSizeChartBody): Promise<SizeChart> => {
      if ("file" in body) {
        const query = new URLSearchParams({ name: body.name });
        if (body.site) query.set("site", body.site);
        const payload = await api.upload<SizeChart | SizeChartEnvelope>(
          `/size-charts/upload?${query.toString()}`,
          body.file,
          { fileName: body.file.name }
        );
        return unwrapChart(payload);
      }

      const payload = await api.post<SizeChart | SizeChartEnvelope, Record<string, string>>(
        "/size-charts/upload",
        {
          url: body.url,
          ...(body.name ? { name: body.name } : {}),
          ...(body.site ? { site: body.site } : {}),
          ...(body.category ? { category: body.category } : {}),
        }
      );
      return unwrapChart(payload);
    },
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: sizeChartsKey });
    },
  });
}

/* ------------------------------------------------------------------ *
 * 错误文案
 * ------------------------------------------------------------------ */

/**
 * 尺码图相关操作的错误文案。
 *
 * 后端 422 的 message 本身就是给用户看的（「文件类型与声明不一致：声明
 * image/png，实际 WebP」），直接原样显示；吞成「上传失败」会让用户按错误的
 * 方向反复重试。这里只兜住「后端没给 message」的情况。
 */
export function sizeChartErrorMessage(error: unknown): string {
  if (isApiError(error) && (error.status === 422 || error.code === "UNPROCESSABLE")) {
    return error.message || "文件校验未通过，请确认图片类型后重试";
  }
  return apiErrorMessage(error);
}

/** 从列表响应里取条目（服务端 items / charts 内容相同，取兼容） */
export function pickCharts(data: SizeChartsResponse | undefined): SizeChart[] {
  if (!data) return [];
  if (Array.isArray(data.items) && data.items.length > 0) return data.items;
  return Array.isArray(data.charts) ? data.charts : [];
}
