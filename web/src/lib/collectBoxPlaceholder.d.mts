export interface CollectBoxPlaceholderParams {
  accountId?: string | null;
  page: number;
  pageSize: number;
  status: string;
  keyword?: string;
  shopId?: string | null;
  sort?: string;
}

export function canUseCollectBoxPlaceholder(
  previous: Partial<CollectBoxPlaceholderParams> | undefined,
  current: CollectBoxPlaceholderParams,
): boolean;
