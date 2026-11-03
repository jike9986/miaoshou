import type { QueryClient } from "@tanstack/react-query";

export function hydrateOperatorQueryClient(identityKey: string, client: QueryClient): void;
export function persistOperatorQueryClient(identityKey: string | null, client: QueryClient): () => void;
export function clearOperatorQueryDiskCache(identityKey: string): boolean;
export function clearAllOperatorQueryDiskCaches(): void;
