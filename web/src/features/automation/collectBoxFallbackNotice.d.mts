export function getCollectBoxFallbackNotice(input: {
  connected: boolean;
  degraded: boolean;
  refreshFailedWithItems?: boolean;
}): string | null;

export function shouldShowCollectBoxReadError(input: {
  connected: boolean;
  loading: boolean;
  error: unknown;
  itemCount: number;
}): boolean;
