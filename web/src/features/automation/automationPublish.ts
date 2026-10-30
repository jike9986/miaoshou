import type { AutomationRunItemRecord } from "@/lib/types";

/** Return the safe action for an already processed item awaiting publish. */
export function pendingAutomationPublishAction(
  item: AutomationRunItemRecord,
): "start" | "resume" | null {
  if (item.processStatus !== "succeeded") return null;
  if (item.publishStatus === "queued") return "start";
  if (item.publishStatus === "needs_review" && item.publishResult.resumeKind === "images_pending") {
    return "resume";
  }
  return null;
}
