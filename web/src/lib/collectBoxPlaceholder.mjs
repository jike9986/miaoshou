/** Only carry a collect-box result forward when the entire request scope matches. */
export function canUseCollectBoxPlaceholder(previous, current) {
  if (!previous || !current) return false;
  return (previous.accountId ?? null) === (current.accountId ?? null)
    && previous.page === current.page
    && previous.pageSize === current.pageSize
    && previous.status === current.status
    && (previous.keyword ?? "") === (current.keyword ?? "")
    && (previous.shopId ?? null) === (current.shopId ?? null)
    && (previous.sort ?? "default") === (current.sort ?? "default");
}
