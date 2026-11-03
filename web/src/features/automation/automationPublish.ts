import type { AutomationRunItemRecord, ProcessImagesResult, PublishResponse } from "@/lib/types";

export function automationImagesPending(images: ProcessImagesResult | null | undefined): boolean {
  return images?.queued === true || (images?.completed !== true && (images?.submitted ?? 0) > 0);
}

/** An accepted request still needs a platform receipt; an empty response is unknown. */
export function automationPublishOutcome(response: Pick<PublishResponse, "published" | "failures">): {
  accepted: boolean;
  status: "failed" | "needs_review";
  reason: string;
} {
  const accepted = response.published.length > 0;
  const failureReason = response.failures.map((failure) => failure.reason).filter(Boolean).join("；");
  return {
    accepted,
    status: accepted || response.failures.length === 0 ? "needs_review" : "failed",
    reason: accepted
      ? "妙手已受理发布请求，最终状态以发布记录回执为准。"
      : failureReason || "妙手未返回可核实的发布结果，请先刷新发布回执，勿重复提交。",
  };
}

/** Keep unfinished/uncertain items visible after reopening, without making them retryable. */
export function retainAutomationSelection(item: AutomationRunItemRecord): boolean {
  return item.processStatus !== "succeeded"
    || ["failed", "needs_review", "running"].includes(item.publishStatus)
    || pendingAutomationPublishAction(item) !== null;
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
