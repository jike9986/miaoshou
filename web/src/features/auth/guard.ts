// features/auth/guard —— 「未连接时自动跳 /unlock」的判定（api.md §1.2）
//
// 规则：
// • 首次进入应用（会话内第一次落在业务页）且 GET /auth/status 返回 connected=false → 跳 /unlock；
// • 只跳一次：之后用户仍然可以从侧边栏回到仓库/词库等本地可用的页面（仓库是本地数据，
//   断连时也应该能看、能改分类、能删除）；
// • /unlock 上点「稍后连接」后置 guest=true（存 sessionStorage，刷新不丢），本次会话不再自动跳；
// • 需要妙手连接的页面（采集箱 / 发布 / 定时发布 / 图片翻译）另用 useConnectionState().blocked
//   就地显示引导，不强行改地址。

import { useEffect, useRef } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { create } from "zustand";
import { useAuthStatus } from "@/lib/queries";

const GUEST_KEY = "aohuang.guest";

function readGuest(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.sessionStorage.getItem(GUEST_KEY) === "1";
  } catch {
    return false;
  }
}

interface AuthGuardState {
  /** 已确认连不上且用户选择继续使用本地功能 */
  guest: boolean;
  markGuest: () => void;
  resetGuest: () => void;
}

export const useAuthGuardStore = create<AuthGuardState>((set) => ({
  guest: readGuest(),
  markGuest: () => {
    try {
      window.sessionStorage.setItem(GUEST_KEY, "1");
    } catch {
      /* 隐私模式下忽略 */
    }
    set({ guest: true });
  },
  resetGuest: () => {
    try {
      window.sessionStorage.removeItem(GUEST_KEY);
    } catch {
      /* 忽略 */
    }
    set({ guest: false });
  },
}));

/**
 * 挂一次即可（在 App 内）：会话内首次进入业务页且未连接 → 跳 /unlock 并记住来源页。
 * 只跳一次，避免把用户从本地可用的仓库里反复踢走。
 */
export function useAutoUnlock(): void {
  const { data, isSuccess } = useAuthStatus();
  const guest = useAuthGuardStore((state) => state.guest);
  const navigate = useNavigate();
  const location = useLocation();
  const redirectedRef = useRef(false);

  useEffect(() => {
    if (redirectedRef.current) return;
    if (!isSuccess || !data) return;
    if (data.connected || guest) {
      redirectedRef.current = true;
      return;
    }
    if (location.pathname === "/unlock") {
      redirectedRef.current = true;
      return;
    }
    redirectedRef.current = true;
    navigate("/unlock", { replace: true, state: { from: location.pathname } });
  }, [data, isSuccess, guest, location.pathname, navigate]);
}

/** 页面级使用：返回 { connected, guest, blocked }，blocked 时页面显示引导而不是列表 */
export function useConnectionState(): {
  connected: boolean;
  guest: boolean;
  loading: boolean;
  blocked: boolean;
} {
  const { data, isLoading } = useAuthStatus();
  const guest = useAuthGuardStore((state) => state.guest);
  const connected = data?.connected ?? false;
  return {
    connected,
    guest,
    loading: isLoading,
    blocked: !isLoading && !connected && !guest,
  };
}
