type PreviousPublishLogsQuery = { queryKey?: readonly unknown[] } | null | undefined;

export function publishLogsPlaceholderData<T>(
  previous: T | undefined,
  previousQuery: PreviousPublishLogsQuery,
  accountId: string | null,
): T | undefined;
