export function partitionMemberSelection(
  selectedIds: string[],
  claimedById: Record<string, boolean>,
): { claimable: string[]; publishable: string[] } {
  const claimable: string[] = [];
  const publishable: string[] = [];
  for (const id of selectedIds) {
    const claimed = claimedById[id];
    if (claimed === false) claimable.push(id);
    if (claimed === true) publishable.push(id);
  }
  return { claimable, publishable };
}
