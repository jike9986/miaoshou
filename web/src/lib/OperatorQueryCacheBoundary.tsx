import { useLayoutEffect, useRef, type ReactNode } from "react";
import { QueryClientProvider, useQueryClient } from "@tanstack/react-query";
import { resetOperatorSessionUiState } from "@/features/stores";
import { resolveApiBase } from "@/lib/api";
import { useOperatorIdentity } from "@/lib/operator";
import { normalizeApiBase } from "@/lib/serverAddress";
import {
  clearOperatorQueryClientsExcept,
  getOperatorQueryClient,
} from "@/lib/operatorQueryClientRegistry.mjs";

function getCacheApiBase() {
  const apiBase = resolveApiBase();
  try {
    return normalizeApiBase(apiBase);
  } catch {
    // Preserve isolation even when a legacy server address is invalid; the API
    // layer will surface the address error without crashing the app boundary.
    return apiBase.trim().replace(/\/+$/, "");
  }
}

/** Keep account query caches isolated while switching directly between operators. */
export function OperatorQueryCacheBoundary({ children }: { children: ReactNode }) {
  const identity = useOperatorIdentity();
  const rootClient = useQueryClient();
  const apiBase = getCacheApiBase();
  const identityKey = identity
    ? `${identity.id ?? identity.token}:${identity.role ?? ""}:${apiBase}`
    : null;
  // Persist product summaries only for stable software account ids. Legacy
  // token-only identities stay memory-scoped and never become disk keys.
  const persistenceIdentityKey = identity?.id
    ? `${identity.id}:${identity.role ?? ""}:${apiBase}`
    : null;
  const client = getOperatorQueryClient(identityKey, rootClient, persistenceIdentityKey);
  const previous = useRef({ identityKey, client });

  useLayoutEffect(() => {
    const previousClient = previous.current;
    if (previousClient.identityKey === identityKey) return;
    previous.current = { identityKey, client };
    resetOperatorSessionUiState();
    void previousClient.client.cancelQueries();
    if (identityKey === null) clearOperatorQueryClientsExcept(client);
  }, [client, identityKey]);

  return (
    <QueryClientProvider client={client} key={identityKey ?? "anonymous"}>
      {children}
    </QueryClientProvider>
  );
}
