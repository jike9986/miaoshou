export function partitionMemberSelection(
  selectedIds: string[],
  claimedById: Record<string, boolean>,
  publishedShopIdsById: Record<string, string[]> = {},
  targetShopId?: string | null,
): { claimable: string[]; publishable: string[] } {
  const claimable: string[] = [];
  const publishable: string[] = [];
  for (const id of selectedIds) {
    const claimed = claimedById[id];
    if (claimed === false) claimable.push(id);
    if (claimed === true && !publishedShopIdsById[id]?.includes(String(targetShopId ?? ""))) {
      publishable.push(id);
    }
  }
  return { claimable, publishable };
}
