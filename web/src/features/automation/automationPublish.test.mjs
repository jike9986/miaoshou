import assert from "node:assert/strict";
import test from "node:test";
import { automationImagesPending, pendingAutomationPublishAction } from "./automationPublish.ts";

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
