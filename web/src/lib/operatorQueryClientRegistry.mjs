import { QueryClient } from "@tanstack/react-query";
import {
  clearOperatorQueryDiskCache,
  hydrateOperatorQueryClient,
  persistOperatorQueryClient,
} from "./operatorQueryDiskCache.mjs";

const clientsByOperator = new Map();
const persistenceByClient = new Map();

/**
 * Keep a short-lived QueryClient per software identity so cached results can
 * be reused when switching directly between accounts without ever sharing a
 * cache. The first client for the current identity is the app's configured
 * root client; other identities inherit the same default options.
 */
export function getOperatorQueryClient(identityKey, fallbackClient, persistenceIdentityKey = null) {
  const existing = clientsByOperator.get(identityKey);
  if (existing) return existing;

  const client = clientsByOperator.size === 0
    ? fallbackClient
    : new QueryClient({ defaultOptions: fallbackClient.getDefaultOptions() });
  if (persistenceIdentityKey) {
    hydrateOperatorQueryClient(persistenceIdentityKey, client);
    persistenceByClient.set(client, {
      identityKey: persistenceIdentityKey,
      unsubscribe: persistOperatorQueryClient(persistenceIdentityKey, client),
    });
  }
  clientsByOperator.set(identityKey, client);
  return client;
}

/** Clear every inactive identity cache while retaining the current public/login cache. */
export function clearOperatorQueryClientsExcept(retainedClient) {
  for (const client of clientsByOperator.values()) {
    if (client === retainedClient) continue;
    void client.cancelQueries();
    client.clear();
    persistenceByClient.get(client)?.unsubscribe();
    persistenceByClient.delete(client);
  }

  for (const [identityKey, client] of clientsByOperator) {
    if (client !== retainedClient) clientsByOperator.delete(identityKey);
  }
}

/** Logout clears in-memory results; stable-id disk caches remain account-scoped for next login. */
export function clearOperatorQueryClients() {
  for (const client of clientsByOperator.values()) {
    void client.cancelQueries();
    client.clear();
    persistenceByClient.get(client)?.unsubscribe();
    persistenceByClient.delete(client);
  }
  clientsByOperator.clear();
}

/** Clear only this user's product list previews; login and business data stay intact. */
export async function clearOperatorProductListCache(client) {
  const filter = {
    predicate: ({ queryKey }) =>
      (queryKey[0] === "collect-box" || queryKey[0] === "warehouse") &&
      typeof queryKey[1] === "object" && queryKey[1] !== null && !Array.isArray(queryKey[1]),
  };
  await client.cancelQueries(filter);
  client.removeQueries(filter);
  const identityKey = persistenceByClient.get(client)?.identityKey;
  if (identityKey && !clearOperatorQueryDiskCache(identityKey)) {
    throw new Error("本机存储暂不可用，请重启软件后再清除缓存。");
  }
}
