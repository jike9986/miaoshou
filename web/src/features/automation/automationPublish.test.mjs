import assert from "node:assert/strict";
import test from "node:test";
import {
  automationImagesPending,
  automationPublishOutcome,
  pendingAutomationPublishAction,
  retainAutomationSelection,
} from "./automationPublish.ts";

function item(processStatus, publishStatus, resumeKind) {
  return { processStatus, publishStatus, publishResult: { resumeKind } };
}

test("routes a queued publish through start", () => {
  assert.equal(pendingAutomationPublishAction(item("succeeded", "queued")), "start");
});

test("routes completed-image review through resume", () => {
  assert.equal(pendingAutomationPublishAction(item("succeeded", "needs_review", "images_pending")), "resume");
});

test("only retries failed publishing when a final platform receipt is attached", () => {
  assert.equal(pendingAutomationPublishAction(item("succeeded", "running")), null);
  assert.equal(pendingAutomationPublishAction(item("succeeded", "failed")), null);
  assert.equal(pendingAutomationPublishAction(item("succeeded", "succeeded")), null);
  assert.equal(pendingAutomationPublishAction({
    ...item("succeeded", "failed"),
    publishResult: { publishLogId: "17", platformStatus: "fail" },
  }), "retry");
  assert.equal(pendingAutomationPublishAction({
    ...item("succeeded", "failed"),
    publishResult: { publishLogId: "17", platformStatus: "processing" },
  }), null);
});

test("does not publish before product processing succeeds", () => {
  assert.equal(pendingAutomationPublishAction(item("queued", "queued")), null);
  assert.equal(pendingAutomationPublishAction(item("failed", "queued")), null);
});

test("waits through OCR before the first image translation job exists", () => {
  const images = { queued: true, completed: false, submitted: 0, failed: 0 };
  assert.equal(automationImagesPending(images), true);
  assert.equal(pendingAutomationPublishAction({
    ...item("succeeded", "queued"), processResult: { images },
  }), null);
});

test("waits for draft writeback even after image jobs are ready", () => {
  assert.equal(automationImagesPending({ queued: true, submitted: 2, completed: false }), true);
  assert.equal(automationImagesPending({ queued: false, submitted: 2, completed: true }), false);
});

test("allows verified completion when no images need translation", () => {
  const images = { queued: false, completed: true, submitted: 0, failed: 0 };
  assert.equal(automationImagesPending(images), false);
  assert.equal(pendingAutomationPublishAction({
    ...item("succeeded", "needs_review", "images_pending"), processResult: { images },
  }), "resume");
});

test("accepted publishing waits for receipt instead of claiming success", () => {
  assert.deepEqual(automationPublishOutcome({ published: ["17"], failures: [] }), {
    accepted: true,
    status: "needs_review",
    reason: "妙手已受理发布请求，最终状态以发布记录回执为准。",
  });
});

test("explicit publish failures remain visible without enabling a blind retry", () => {
  const outcome = automationPublishOutcome({ published: [], failures: [{ reason: "价格未通过" }] });
  assert.equal(outcome.accepted, false);
  assert.equal(outcome.status, "failed");
  assert.equal(outcome.reason, "价格未通过");
  const failed = item("succeeded", outcome.status);
  assert.equal(retainAutomationSelection(failed), true);
  assert.equal(pendingAutomationPublishAction(failed), null);
});

test("empty or ambiguous publishing retains selection and requires receipt review", () => {
  const outcome = automationPublishOutcome({ published: [], failures: [] });
  assert.equal(outcome.accepted, false);
  assert.equal(outcome.status, "needs_review");
  assert.match(outcome.reason, /勿重复提交/);
  const unknown = item("succeeded", outcome.status);
  assert.equal(retainAutomationSelection(unknown), true);
  assert.equal(pendingAutomationPublishAction(unknown), null);
  assert.equal(retainAutomationSelection(item("succeeded", "running")), true);
});

test("recovery keeps failures and pending work, but clears confirmed completion", () => {
  assert.equal(retainAutomationSelection(item("queued", "queued")), true);
  assert.equal(retainAutomationSelection(item("failed", "not_requested")), true);
  assert.equal(retainAutomationSelection(item("needs_review", "not_requested")), true);
  assert.equal(retainAutomationSelection(item("succeeded", "queued")), true);
  assert.equal(retainAutomationSelection(item("succeeded", "succeeded")), false);
  assert.equal(retainAutomationSelection(item("succeeded", "not_requested")), false);
});

test("a confirmed failure can be safely retried without repeating product processing", () => {
  const failed = {
    ...item("succeeded", "failed"),
    publishResult: { publishLogId: "17", platformStatus: "fail" },
  };
  assert.equal(retainAutomationSelection(failed), true);
  assert.equal(pendingAutomationPublishAction(failed), "retry");
});
