// features/automation —— 页面内共用的类型、默认值与选项常量
// 只放「纯数据」：设置项默认值、筛选/语言选项、运行态结构、尺寸表响应结构。
// 服务端类型仍然复用 @/lib/types，不在这里重复定义契约。

/* ------------------------------------------------------------------ *
 * 处理设置
 * ------------------------------------------------------------------ */

/** 商品文字目标语言（与后端翻译链路一致：ms / en / th / vi / zh） */
export type TargetLanguage = "ms" | "en" | "th" | "vi" | "zh";

export const LANGUAGE_OPTIONS: Array<{ value: TargetLanguage; label: string }> = [
  { value: "ms", label: "马来语（ms）" },
  { value: "en", label: "英语（en）" },
  { value: "th", label: "泰语（th）" },
  { value: "vi", label: "越南语（vi）" },
  { value: "zh", label: "简体中文（zh）" },
];

export function languageLabel(code: string): string {
  return LANGUAGE_OPTIONS.find((option) => option.value === code)?.label ?? code;
}

/**
 * 处理设置。老版存 localStorage（persistTaskPreferences），这里保持一致：
 * 关掉应用再打开，包裹尺寸 / 主图数 / 语言 / 模板都还在，不用每次重填。
 * 字段与 POST /collect-box/{detailId}/process 的 ProcessRequest 对齐的部分：
 * language → language、template → template；其余为本地流水线参数。
 */
export interface AutomationSettings {
  /** 商品文字语言（textEnabled=false 时不翻译） */
  language: TargetLanguage;
  /** 文字处理开关 */
  textEnabled: boolean;
  /** 图片处理开关 */
  imagesEnabled: boolean;
  /** 主图数量 1–9 */
  mainImageCount: number;
  /** SKU 图数量 0–9，0 = 不处理 SKU 图 */
  skuImageCount: number;
  /** 详情图数量 0–9，0 = 不处理详情图 */
  detailImageCount: number;
  /** 图片并发数 1–9 */
  imageConcurrency: number;
  /** 重量增加 kg（0–5，步进 0.01） */
  weightExtraKg: number;
  /** 包裹尺寸 cm（0.01–300） */
  lengthCm: number;
  widthCm: number;
  heightCm: number;
  /** 定价模板开关（开 + 选中模板时才随请求下发） */
  priceTemplateEnabled: boolean;
  /** 定价模板名（来自 GET /system/templates） */
  template: string;
  /** 发布到店铺开关（开启后，处理成功的商品自动认领到所选店铺的采集箱，不直接上架） */
  publishEnabled: boolean;
  /** 发布目标店铺（publishEnabled=true 时认领到这家店的采集箱） */
  publishShopId: string;
  /** 选中的尺码图（GET /size-charts 的 id） */
  sizeChartId: string | null;
}

export const DEFAULT_SETTINGS: AutomationSettings = {
  language: "ms",
  textEnabled: true,
  imagesEnabled: true,
  mainImageCount: 5,
  skuImageCount: 5,
  detailImageCount: 5,
  imageConcurrency: 3,
  // 与老版 0.1.4 index.html 的默认 value 完全一致：
  //   weight-increment=0.1、package-length=30、package-width=20、package-height=10
  weightExtraKg: 0.1,
  lengthCm: 30,
  widthCm: 20,
  heightCm: 10,
  priceTemplateEnabled: false,
  template: "",
  publishEnabled: false,
  publishShopId: "",
  sizeChartId: null,
};

/* ------------------------------------------------------------------ *
 * 设置持久化（对照老版 web/app.js persistTaskPreferences）
 * ------------------------------------------------------------------ */

const SETTINGS_STORAGE_KEY = "aohuang.automation.settings.v1";

/** 只允许这些键落盘：将来加字段不会把旧存档里的脏值带进来 */
const PERSISTED_KEYS: Array<keyof AutomationSettings> = [
  "language",
  "textEnabled",
  "imagesEnabled",
  "mainImageCount",
  "skuImageCount",
  "detailImageCount",
  "imageConcurrency",
  "weightExtraKg",
  "lengthCm",
  "widthCm",
  "heightCm",
  "priceTemplateEnabled",
  "template",
  "publishEnabled",
  "publishShopId",
  "sizeChartId",
];

/**
 * 读回上次的设置。任何异常都退回默认值 —— 存档坏了不该让页面起不来。
 * 类型逐项对齐 DEFAULT_SETTINGS 的同名字段，避免存档里的字符串污染数字字段。
 */
export function loadSettings(): AutomationSettings {
  try {
    const raw = window.localStorage.getItem(SETTINGS_STORAGE_KEY);
    if (!raw) return DEFAULT_SETTINGS;
    const saved = JSON.parse(raw) as Record<string, unknown>;
    const merged: AutomationSettings = { ...DEFAULT_SETTINGS };

    for (const key of PERSISTED_KEYS) {
      if (!(key in saved)) continue;
      const fallback = DEFAULT_SETTINGS[key];
      const value = saved[key];
      if (typeof fallback === "number") {
        const parsed = Number(value);
        if (Number.isFinite(parsed)) {
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          (merged as any)[key] = parsed;
        }
      } else if (typeof fallback === "boolean") {
        if (typeof value === "boolean") {
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          (merged as any)[key] = value;
        }
      } else if (fallback === null) {
        if (value === null || typeof value === "string") {
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          (merged as any)[key] = value;
        }
      } else if (typeof fallback === "string" && typeof value === "string") {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        (merged as any)[key] = value;
      }
    }
    return merged;
  } catch {
    return DEFAULT_SETTINGS;
  }
}

/** 落盘。写不进去（隐私模式 / 磁盘满）只警告，不影响当前这次处理。 */
export function saveSettings(settings: AutomationSettings): void {
  try {
    const body: Record<string, unknown> = {};
    for (const key of PERSISTED_KEYS) body[key] = settings[key];
    window.localStorage.setItem(SETTINGS_STORAGE_KEY, JSON.stringify(body));
  } catch {
    // 存档失败不阻断操作：设置丢了是小事，处理跑不起来才是大事
  }
}

/* ------------------------------------------------------------------ *
 * 选品列表筛选
 * ------------------------------------------------------------------ */

export type ListFilter = "all" | "selected" | "pending" | "running" | "done" | "failed" | "needs_review";

export const LIST_FILTER_OPTIONS: Array<{ value: ListFilter; label: string }> = [
  { value: "all", label: "全部" },
  { value: "selected", label: "已勾选" },
  { value: "pending", label: "待处理" },
  { value: "running", label: "处理中" },
  { value: "done", label: "已完成" },
  { value: "failed", label: "失败" },
  { value: "needs_review", label: "待核对" },
];

/** 本地处理态（后端没有批次任务表，只能记在当前页面会话里） */
export type ItemStage = "queued" | "running" | "done" | "failed" | "needs_review";

/**
 * 一件商品实际完成了哪几步。
 *
 * 老版 productStatus 会显示「文字完成 · 图片待处理」这类混合状态：
 * 一件商品可能文字译好了、图片全失败，只显示一个「已完成」会盖掉这件事，
 * 用户要等上架后看到中文图才发现。
 */
export interface ItemProgress {
  textDone?: boolean;
  imagesDone?: boolean;
  imageFailures?: number;
}

/* ------------------------------------------------------------------ *
 * 运行态（右侧进度栏的数据源）
 * ------------------------------------------------------------------ */

export type RunPhase = "idle" | "running" | "stopped" | "done";

export interface RunState {
  phase: RunPhase;
  /** 本批次总件数（开始那一刻的快照） */
  total: number;
  /** 已处理件数（含失败） */
  done: number;
  /** 其中失败件数 */
  failed: number;
  startedAt: number | null;
  endedAt: number | null;
  /** 已提交翻译的图片张数（累加每次响应的 images.submitted；
      注意不是商品自带图片数——那样勾不勾「图片」都会涨） */
  imageDone: number;
  /** 文字处理完成的件数（老版 productStatus 的 textCompleted 汇总） */
  textDone?: number;
  /** 计划处理图片张数（选中件数 × 主图数量，开关关闭时为 0） */
  imagePlanned: number;
  /** 当前阶段文案 */
  stage: string;
  failedKeys: string[];
  /** 逐条失败明细（老版 failures-section 的等价物）。
      只有 failedKeys 时只能看到「失败 N 件」，看不到为什么失败；
      这里留住每件的错误原因，批次跑完仍可回看。 */
  failures: RunFailure[];
  /** 商品执行情况来自持久化运行记录，需要人工核对的项目数 */
  needsReview?: number;
}

export interface RunFailure {
  /** 采集箱里的仓库键 detailId:shopId */
  key: string;
  title: string;
  detailId: string;
  /** 上游/校验给出的失败原因，原样保留，不翻译不改写 */
  reason: string;
}

export const IDLE_RUN: RunState = {
  phase: "idle",
  total: 0,
  done: 0,
  failed: 0,
  startedAt: null,
  endedAt: null,
  imageDone: 0,
  imagePlanned: 0,
  stage: "等待开始",
  failedKeys: [],
  failures: [],
};

/* ------------------------------------------------------------------ *
 * 尺寸表（api.md §13 GET /size-charts）
 * ------------------------------------------------------------------ */

export interface SizeChart {
  id: string;
  name: string;
  site: string;
  url: string;
  contentJson: unknown;
  createdAt: number;
  updatedAt: number;
}

export interface SizeChartsResponse {
  items: SizeChart[];
  /** 老版字段名，服务端原样带回 */
  charts: SizeChart[];
  total: number;
  sites: string[];
}
