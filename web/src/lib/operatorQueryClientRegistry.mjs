import { QueryClient } from "@tanstack/react-query";

const clientsByOperator = new Map();

/**
 * Keep a short-lived QueryClient per software identity so cached results can
 * be reused when switching directly between accounts without ever sharing a
 * cache. The first client for the current identity is the app's configured
 * root client; other identities inherit the same default options.
 */
export function getOperatorQueryClient(identityKey, fallbackClient) {
  const existing = clientsByOperator.get(identityKey);
  if (existing) return existing;

  const client = clientsByOperator.size === 0
    ? fallbackClient
    : new QueryClient({ defaultOptions: fallbackClient.getDefaultOptions() });
  clientsByOperator.set(identityKey, client);
  return client;
}

/** Clear every inactive identity cache while retaining the current public/login cache. */
export function clearOperatorQueryClientsExcept(retainedClient) {
  for (const client of clientsByOperator.values()) {
    if (client === retainedClient) continue;
    void client.cancelQueries();
    client.clear();
  }

  for (const [identityKey, client] of clientsByOperator) {
    if (client !== retainedClient) clientsByOperator.delete(identityKey);
  }
}

/** Explicit logout clears all cached operator data immediately. */
export function clearOperatorQueryClients() {
  for (const client of clientsByOperator.values()) {
    void client.cancelQueries();
    client.clear();
  }
  clientsByOperator.clear();
}
