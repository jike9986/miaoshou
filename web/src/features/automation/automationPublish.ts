import type { AutomationRunItemRecord } from "@/lib/types";

/** Return the safe action for an already processed item awaiting publish. */
export function pendingAutomationPublishAction(
  item: AutomationRunItemRecord,
): "start" | "resume" | "retry" | null {
  if (item.processStatus !== "succeeded") return null;
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
