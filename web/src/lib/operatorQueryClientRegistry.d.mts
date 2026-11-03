import type { QueryClient } from "@tanstack/react-query";

export function getOperatorQueryClient(
  identityKey: string | null,
  fallbackClient: QueryClient,
  persistenceIdentityKey?: string | null,
): QueryClient;
export function clearOperatorQueryClientsExcept(retainedClient: QueryClient): void;
export function clearOperatorQueryClients(): void;
export function clearOperatorProductListCache(client: QueryClient): Promise<void>;
