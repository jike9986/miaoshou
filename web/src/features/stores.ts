// architecture §4.6 前端状态分层 —— 界面状态（Zustand）
// 只放「不进服务端」的状态：筛选、分页、选中集合、抽屉/弹窗开关。
// 服务端状态一律走 TanStack Query（@/lib/queries），两边不重复。

import { create } from "zustand";
import type {
  ProductSort,
  ProductStatus,
  QualityReviewStatus,
  ScheduleFrequency,
  WordCategory,
} from "@/lib/types";

/* ================================================================== *
 * 会话级：当前店铺（顶部店铺下拉，跨页面共享）
 * ================================================================== */

export interface SessionState {
  /** 顶部下拉选中的店铺 id；null 表示未选 */
  currentShopId: string | null;
  setCurrentShopId: (shopId: string | null) => void;
}

export const useSessionStore = create<SessionState>((set) => ({
  currentShopId: null,
  setCurrentShopId: (shopId) => set({ currentShopId: shopId }),
}));

/* ================================================================== *
 * 成品仓库
 * ================================================================== */

export type WarehouseCategoryFilter =
  | { kind: "all" }
  | { kind: "cid"; cid: string; name: string }
  | { kind: "categoryId"; categoryId: string; name: string }
  | { kind: "group"; groupId: string; name: string };

export interface WarehouseState {
  page: number;
  pageSize: number;
  sort: ProductSort;
  /** 输入框里的原始值（300ms 后才进查询） */
  searchDraft: string;
  /** 已生效的搜索词 */
  q: string;
  claimed: boolean | null;
  status: ProductStatus | null;
  qualityStatus: QualityReviewStatus | null;
  category: WarehouseCategoryFilter;
  /** 选中商品 id（跨页保留） */
  selected: string[];

  setPage: (page: number) => void;
  setPageSize: (pageSize: number) => void;
  setSort: (sort: ProductSort) => void;
  setSearchDraft: (value: string) => void;
  setQ: (value: string) => void;
  setClaimed: (claimed: boolean | null) => void;
  setStatus: (status: ProductStatus | null) => void;
  setQualityStatus: (status: QualityReviewStatus | null) => void;
  setCategory: (category: WarehouseCategoryFilter) => void;
  toggleSelected: (id: string) => void;
  selectMany: (ids: string[]) => void;
  deselectMany: (ids: string[]) => void;
  clearSelected: () => void;
}

export const useWarehouseStore = create<WarehouseState>((set) => ({
  page: 1,
  pageSize: 24,
  sort: "savedDesc",
  searchDraft: "",
  q: "",
  claimed: null,
  status: null,
  qualityStatus: null,
  category: { kind: "all" },
  selected: [],

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
  setCategory: (category) => set({ category, page: 1, selected: [] }),
  toggleSelected: (id) =>
    set((state) => ({
      selected: state.selected.includes(id)
        ? state.selected.filter((item) => item !== id)
        : [...state.selected, id],
    })),
  selectMany: (ids) =>
    set((state) => {
      const merged = new Set([...state.selected, ...ids]);
      return { selected: Array.from(merged) };
    }),
  deselectMany: (ids) =>
    set((state) => {
      const removed = new Set(ids);
      return { selected: state.selected.filter((id) => !removed.has(id)) };
    }),
  clearSelected: () => set({ selected: [] }),
}));

/* ================================================================== *
 * 违规词库
 * ================================================================== */

export interface WordsState {
  page: number;
  /** 上限 500（api.md §10.1） */
  pageSize: number;
  category: WordCategory | null;
  searchDraft: string;
  q: string;
  selectedWords: string[];

  setPage: (page: number) => void;
  setPageSize: (pageSize: number) => void;
  setCategory: (category: WordCategory | null) => void;
  setSearchDraft: (value: string) => void;
  setQ: (value: string) => void;
  toggleWord: (word: string) => void;
  setWords: (words: string[]) => void;
  clearWords: () => void;
}

export const useWordsStore = create<WordsState>((set) => ({
  page: 1,
  pageSize: 100,
  category: null,
  searchDraft: "",
  q: "",
  selectedWords: [],

  setPage: (page) => set({ page }),
  setPageSize: (pageSize) => set({ pageSize, page: 1 }),
  setCategory: (category) => set({ category, page: 1, selectedWords: [] }),
  setSearchDraft: (searchDraft) => set({ searchDraft }),
  setQ: (q) => set({ q, page: 1 }),
  toggleWord: (word) =>
    set((state) => ({
      selectedWords: state.selectedWords.includes(word)
        ? state.selectedWords.filter((item) => item !== word)
        : [...state.selectedWords, word],
    })),
  setWords: (words) => set({ selectedWords: words }),
  clearWords: () => set({ selectedWords: [] }),
}));

/* ================================================================== *
 * 发布记录
 * ================================================================== */

export interface PublishLogsState {
  page: number;
  pageSize: number;
  status: string | null;

  setPage: (page: number) => void;
  setPageSize: (pageSize: number) => void;
  setStatus: (status: string | null) => void;
}

export const usePublishLogsStore = create<PublishLogsState>((set) => ({
  page: 1,
  pageSize: 20,
  status: null,
  setPage: (page) => set({ page }),
  setPageSize: (pageSize) => set({ pageSize, page: 1 }),
  setStatus: (status) => set({ status, page: 1 }),
}));

/* ================================================================== *
 * 定时发布：表单默认值（新建时用到）
 * ================================================================== */

export interface ScheduleFormDraft {
  name: string;
  frequency: ScheduleFrequency;
  time: string;
  days: number[];
  dayOfMonth: number[];
  startDate: string;
  endDate: string;
  perRun: number;
  claimOnly: boolean;
}

export const EMPTY_SCHEDULE_FORM: ScheduleFormDraft = {
  name: "",
  frequency: "daily",
  time: "09:30",
  days: [],
  dayOfMonth: [],
  startDate: "",
  endDate: "",
  perRun: 10,
  claimOnly: false,
};
