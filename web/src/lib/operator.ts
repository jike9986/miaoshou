// 操作员身份（多用户协作）—— 本地存储 + 请求头注入源
//
// 为什么单独一个模块：
//   1. `api.ts` 需要在拼请求头时同步读到 token，不能等 React 渲染；
//   2. 界面（设置页 / 冲突弹窗 / 列表署名）需要订阅「当前身份变了」，
//      否则换了身份之后页面上还挂着上一个人的名字。
//
// 与妙手 token 的区别（api.md §0.2）：
//   · 妙手 token 由 Electron preload 注入内存，永不落盘，只代表「这台机器能连后端」；
//   · 操作员 token 代表「这台机器上现在是谁在操作」，必须持久化，
//     否则每次重启都要重新贴一遍对方发来的那串字符。
//
// 兼容性红线：**没设置操作员时什么都不注入**。
// 后端在收不到 X-Operator 时会把操作归到内置的「本机」操作员，
// 老用户升级后不选身份也照常能用，不会被强制弹窗拦住。

import { useSyncExternalStore } from "react";

/* ------------------------------------------------------------------ *
 * 存储键
 * ------------------------------------------------------------------ */

/** 操作员 token（一次性发放，后端只存哈希） */
export const OPERATOR_TOKEN_KEY = "aohuang.operator.token";
/** 操作员 id（审计日志筛选、认人用） */
export const OPERATOR_ID_KEY = "aohuang.operator.id";
/** 操作员名字（纯展示：标题栏/设置页/署名） */
export const OPERATOR_NAME_KEY = "aohuang.operator.name";
export const OPERATOR_ROLE_KEY = "aohuang.operator.role";
export const OPERATOR_CHANGE_PASSWORD_KEY = "aohuang.operator.must-change-password";

/** 后端在没有 X-Operator 时使用的内置操作员名（与 server 侧常量对齐） */
export const LOCAL_OPERATOR_NAME = "本机";

export interface OperatorIdentity {
  /** token 明文，只存在于本机浏览器存储里 */
  token: string;
  id: string | null;
  name: string;
  role?: "admin" | "teacher" | "member";
  mustChangePassword?: boolean;
}

/* ------------------------------------------------------------------ *
 * 读写（localStorage 可能被禁用：全部 try/catch 兜住，读失败当作未设置）
 * ------------------------------------------------------------------ */

function readItem(key: string): string | null {
  if (typeof window === "undefined") return null;
  try {
    const value = window.localStorage.getItem(key);
    return value && value.trim() !== "" ? value : null;
  } catch {
    return null;
  }
}

function writeItem(key: string, value: string | null): void {
  if (typeof window === "undefined") return;
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    /* 隐私模式 / 配额满：内存态仍然生效，只是重启后要重贴一次 */
  }
}

/* ------------------------------------------------------------------ *
 * 外部 store：让 React 与 api.ts 共用同一份真值
 * ------------------------------------------------------------------ */

let cached: OperatorIdentity | null | undefined;
const listeners = new Set<() => void>();

function readSnapshot(): OperatorIdentity | null {
  const token = readItem(OPERATOR_TOKEN_KEY);
  if (!token) return null;
  return {
    token,
    id: readItem(OPERATOR_ID_KEY),
    name: readItem(OPERATOR_NAME_KEY) ?? LOCAL_OPERATOR_NAME,
    role: (readItem(OPERATOR_ROLE_KEY) as OperatorIdentity["role"]) ?? undefined,
    mustChangePassword: readItem(OPERATOR_CHANGE_PASSWORD_KEY) === "1",
  };
}

function getSnapshot(): OperatorIdentity | null {
  if (cached === undefined) cached = readSnapshot();
  return cached;
}

function emit(): void {
  cached = readSnapshot();
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  // 别的窗口/标签页改了身份也要跟上
  const onStorage = (event: StorageEvent): void => {
    if (
      event.key === null ||
      event.key === OPERATOR_TOKEN_KEY ||
      event.key === OPERATOR_ID_KEY ||
      event.key === OPERATOR_NAME_KEY
      || event.key === OPERATOR_ROLE_KEY
      || event.key === OPERATOR_CHANGE_PASSWORD_KEY
    ) {
      emit();
    }
  };
  if (typeof window !== "undefined") window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(listener);
    if (typeof window !== "undefined") window.removeEventListener("storage", onStorage);
  };
}

/* ------------------------------------------------------------------ *
 * 对外 API
 * ------------------------------------------------------------------ */

/** 同步读 token —— `api.ts` 拼请求头时用；未设置返回 null（此时不注入 X-Operator） */
export function operatorToken(): string | null {
  return getSnapshot()?.token ?? null;
}

/** 当前身份（未设置返回 null，界面上按「本机」处理） */
export function currentOperator(): OperatorIdentity | null {
  return getSnapshot();
}

/** 当前身份显示名：未设置时回落「本机」，与后端行为一致 */
export function operatorDisplayName(): string {
  return getSnapshot()?.name ?? LOCAL_OPERATOR_NAME;
}

/** 写入身份（登录 / 切换 / 重置 token 后调用） */
export function setOperator(identity: {
  token: string;
  id?: string | null;
  name: string;
  role?: OperatorIdentity["role"];
  mustChangePassword?: boolean;
}): void {
  writeItem(OPERATOR_TOKEN_KEY, identity.token);
  writeItem(OPERATOR_ID_KEY, identity.id ?? null);
  writeItem(OPERATOR_NAME_KEY, identity.name);
  writeItem(OPERATOR_ROLE_KEY, identity.role ?? null);
  writeItem(OPERATOR_CHANGE_PASSWORD_KEY, identity.mustChangePassword ? "1" : null);
  emit();
}

/** 清空身份（「以本机身份继续」） */
export function clearOperator(): void {
  writeItem(OPERATOR_TOKEN_KEY, null);
  writeItem(OPERATOR_ID_KEY, null);
  writeItem(OPERATOR_NAME_KEY, null);
  writeItem(OPERATOR_ROLE_KEY, null);
  writeItem(OPERATOR_CHANGE_PASSWORD_KEY, null);
  emit();
}

/** React 侧订阅：身份变化会触发重渲染 */
export function useOperatorIdentity(): OperatorIdentity | null {
  return useSyncExternalStore(subscribe, getSnapshot, () => null);
}
