import type { QualityReviewReason, QualityReviewStatus } from "@/lib/types";

export interface QualityReviewSnapshot {
  status?: QualityReviewStatus;
  reasons: QualityReviewReason[];
}

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

export function selectedQualityReviews(
  selectedIds: string[],
  snapshots: Record<string, QualityReviewSnapshot>,
): Record<string, QualityReviewSnapshot> {
  return Object.fromEntries(selectedIds.map((id) => [
    id,
    snapshots[id] ?? { status: undefined, reasons: [] },
  ]));
}

export function warehouseIdsForClaimedDetails(
  selectedWarehouseIds: string[],
  detailIdsByWarehouseId: Record<string, string>,
  claimedDetailIds: string[],
): string[] {
  const claimed = new Set(claimedDetailIds.map(String));
  return selectedWarehouseIds.filter((id) => claimed.has(detailIdsByWarehouseId[id]));
}
