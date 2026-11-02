import type { QueryClient } from "@tanstack/react-query";

export function getOperatorQueryClient(
  identityKey: string | null,
  fallbackClient: QueryClient,
): QueryClient;
export function clearOperatorQueryClientsExcept(retainedClient: QueryClient): void;
export function clearOperatorQueryClients(): void;
