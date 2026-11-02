import { useLayoutEffect, useRef, type ReactNode } from "react";
import { QueryClientProvider, useQueryClient } from "@tanstack/react-query";
import { resetOperatorSessionUiState } from "@/features/stores";
import { useOperatorIdentity } from "@/lib/operator";
import {
  clearOperatorQueryClientsExcept,
  getOperatorQueryClient,
} from "@/lib/operatorQueryClientRegistry.mjs";

/** Keep account query caches isolated while switching directly between operators. */
export function OperatorQueryCacheBoundary({ children }: { children: ReactNode }) {
  const identity = useOperatorIdentity();
  const rootClient = useQueryClient();
  const identityKey = identity
    ? `${identity.id ?? identity.token}:${identity.role ?? ""}`
    : null;
  const client = getOperatorQueryClient(identityKey, rootClient);
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
