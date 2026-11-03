import type { AutomationRunItemRecord, ProcessImagesResult } from "@/lib/types";

export function automationImagesPending(images: ProcessImagesResult | null | undefined): boolean {
  return images?.queued === true || (images?.completed !== true && (images?.submitted ?? 0) > 0);
}

/** Return the safe action for an already processed item awaiting publish. */
export function pendingAutomationPublishAction(
  item: AutomationRunItemRecord,
): "start" | "resume" | "retry" | null {
  if (item.processStatus !== "succeeded") return null;
  if (automationImagesPending(item.processResult?.images)) return null;
  if (item.publishStatus === "queued") return "start";
  if (item.publishStatus === "needs_review" && item.publishResult.resumeKind === "images_pending") {
    return "resume";
  }
  if (item.publishStatus === "failed"
      && typeof item.publishResult.publishLogId === "string"
      && ["fail", "cancel"].includes(String(item.publishResult.platformStatus))) {
    return "retry";
  }
  return null;
}
