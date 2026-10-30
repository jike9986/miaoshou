import assert from "node:assert/strict";
import test from "node:test";
import { pendingAutomationPublishAction } from "./automationPublish.ts";

function item(processStatus, publishStatus, resumeKind) {
  return { processStatus, publishStatus, publishResult: { resumeKind } };
}

test("routes a queued publish through start", () => {
  assert.equal(pendingAutomationPublishAction(item("succeeded", "queued")), "start");
});

test("routes completed-image review through resume", () => {
  assert.equal(pendingAutomationPublishAction(item("succeeded", "needs_review", "images_pending")), "resume");
});

test("does not retry running, failed, or completed publishing", () => {
  assert.equal(pendingAutomationPublishAction(item("succeeded", "running")), null);
  assert.equal(pendingAutomationPublishAction(item("succeeded", "failed")), null);
  assert.equal(pendingAutomationPublishAction(item("succeeded", "succeeded")), null);
});

test("does not publish before product processing succeeds", () => {
  assert.equal(pendingAutomationPublishAction(item("queued", "queued")), null);
  assert.equal(pendingAutomationPublishAction(item("failed", "queued")), null);
});
