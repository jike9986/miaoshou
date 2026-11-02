/** Keep previous publish-log rows only while the selected MiaoShou account stays the same. */
export function publishLogsPlaceholderData(previous, previousQuery, accountId) {
  const previousParams = previousQuery?.queryKey?.[1];
  return previousParams?.accountId === accountId ? previous : undefined;
}
