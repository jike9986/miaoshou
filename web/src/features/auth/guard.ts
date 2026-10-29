import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { OperatorsResponse } from "@/lib/types";
import { useAuthStatus } from "@/lib/queries";

export function useOperatorSession(token: string | null) {
  return useQuery({
    queryKey: ["operator-session", token],
    queryFn: () => api.get<OperatorsResponse>("/operators"),
    enabled: Boolean(token),
    retry: false,
    staleTime: 0,
  });
}

/** Page-level upstream connection state; software identity is enforced by the route guard. */
export function useConnectionState(): {
  connected: boolean;
  guest: false;
  loading: boolean;
  blocked: boolean;
} {
  const { data, isLoading } = useAuthStatus();
  const connected = data?.connected ?? false;
  return {
    connected,
    guest: false,
    loading: isLoading,
    blocked: !isLoading && !connected,
  };
}
