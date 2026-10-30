export const API_BASE_STORAGE_KEY = "aohuang.api.base";

function isIpv4Loopback(hostname: string): boolean {
  const parts = hostname.split(".");
  if (parts.length !== 4 || parts.some((part) => !/^\d{1,3}$/.test(part))) return false;
  const octets = parts.map(Number);
  return octets.every((part) => part >= 0 && part <= 255) && octets[0] === 127;
}

export function normalizeApiBase(value: string): string {
  const input = value.trim();
  if (!input) return "";
  if (input.includes("\\")) {
    throw new Error("服务端地址路径不能包含反斜杠");
  }

  let url: URL;
  try {
    url = new URL(input);
  } catch {
    throw new Error("请输入完整的服务端地址，例如 https://api.example.com");
  }

  const hostname = url.hostname.toLowerCase();
  const loopback = hostname === "localhost" || hostname === "[::1]" || isIpv4Loopback(hostname);
  if (url.username || url.password) throw new Error("服务端地址不能包含用户名或密码");
  if (url.search || url.hash) {
    throw new Error("服务端地址不能包含查询参数或片段");
  }
  const path = url.pathname === "/" ? "" : url.pathname.replace(/\/+$/, "");
  const segments = path.split("/").slice(1);
  if (
    segments.some(
      (segment) => !/^[A-Za-z0-9._~-]+$/.test(segment) || segment === "." || segment === "..",
    )
  ) {
    throw new Error("服务端地址路径只能包含字母、数字、点、下划线、波浪号和连字符");
  }
  if (url.protocol !== "https:" && !(url.protocol === "http:" && loopback)) {
    throw new Error("远程服务必须使用 HTTPS；HTTP 仅允许本机回环地址");
  }

  return `${url.origin}${path}`;
}

export function getSavedApiBase(): string {
  if (typeof window === "undefined") return "";
  try {
    return window.localStorage.getItem(API_BASE_STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

export function saveApiBase(value: string): string {
  const normalized = normalizeApiBase(value);
  if (typeof window === "undefined") throw new Error("当前环境无法保存服务端地址");
  try {
    if (normalized) window.localStorage.setItem(API_BASE_STORAGE_KEY, normalized);
    else window.localStorage.removeItem(API_BASE_STORAGE_KEY);
  } catch {
    throw new Error("无法保存服务端地址，请检查应用存储权限");
  }
  return normalized;
}
