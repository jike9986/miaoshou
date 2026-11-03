import assert from "node:assert/strict";
import test from "node:test";
import {
  getCollectBoxFallbackNotice,
  shouldShowCollectBoxReadError,
} from "./collectBoxFallbackNotice.mjs";

test("shows a stale-cache notice only when the connected collect box is degraded", () => {
  assert.equal(
    getCollectBoxFallbackNotice({ connected: true, degraded: true }),
    "显示的是上次缓存，刷新未能联系妙手。当前列表只可查看；恢复连接并刷新成功后才能开始处理。"
  );
  assert.equal(getCollectBoxFallbackNotice({ connected: true, degraded: false }), null);
  assert.equal(getCollectBoxFallbackNotice({ connected: false, degraded: true }), null);
});

test("keeps a stale-list notice after a failed refresh when previous items remain", () => {
  assert.equal(
    getCollectBoxFallbackNotice({ connected: true, degraded: false, refreshFailedWithItems: true }),
    "刷新失败，以下为上次成功加载的列表。当前列表只可查看；刷新成功后才能开始处理。"
  );
  assert.equal(
    getCollectBoxFallbackNotice({ connected: true, degraded: false, refreshFailedWithItems: false }),
    null
  );
});

test("shows the read error only when no previous collect-box items can remain visible", () => {
  assert.equal(
    shouldShowCollectBoxReadError({ connected: true, loading: false, error: new Error("offline"), itemCount: 0 }),
    true
  );
  assert.equal(
    shouldShowCollectBoxReadError({ connected: true, loading: false, error: new Error("offline"), itemCount: 8 }),
    false
  );
  assert.equal(
    shouldShowCollectBoxReadError({ connected: true, loading: true, error: new Error("offline"), itemCount: 0 }),
    false
  );
});
