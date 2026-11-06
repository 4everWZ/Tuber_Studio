"use strict";

const assert = require("node:assert/strict");
const { test } = require("node:test");

function setupDOM(t) {
  const elements = new Map();
  function element() {
    return {
      children: [],
      dataset: {},
      style: {},
      append(...children) { this.children.push(...children); },
      replaceChildren(...children) { this.children = children; },
      setAttribute() {},
      addEventListener() {},
      querySelectorAll() { return []; },
      contains() { return false; },
    };
  }
  function byId(id) {
    if (!elements.has(id)) elements.set(id, { ...element(), parentElement: element() });
    return elements.get(id);
  }
  globalThis.document = { getElementById: byId, createElement: element, querySelector: () => null };
  globalThis.matchMedia = () => ({ matches: false });
  globalThis.OperationContinuationView = class {
    constructor(root) { this.root = root; }
    render() {}
  };
  t.after(() => {
    delete globalThis.document;
    delete globalThis.matchMedia;
    delete globalThis.OperationContinuationView;
  });
  return byId;
}

test("a published browsing comment opens in the shared history, including legacy links", async t => {
  const byId = setupDOM(t);
  const { initCommentQueue } = await import("../src/tuber_studio/web/modules/comment-queue.js");
  const reply = { id: "auto-reply", channel: "browsing", source: "discovery", status: "published", text: "实测成功" };
  const calls = [];
  const api = async path => {
    calls.push(path);
    if (path.includes("view=history")) return { items: [reply], offset: 0, total: 1 };
    if (path.includes("view=review")) return { items: [], offset: 0, total: 0 };
    assert.equal(path, "/api/comments/workbench/browsing/auto-reply");
    return reply;
  };
  const queue = initCommentQueue(api, () => {}, () => null);

  await queue.open(null, "browsing/auto-reply");
  assert.deepEqual(calls.splice(0), [
    "/api/comments/workbench/browsing/auto-reply",
    "/api/comments/workbench?view=history&limit=30&offset=0",
  ]);
  assert.equal(byId("queue-view").value, "history");
  assert.equal(byId("queue-list").children[0].href, "#comments/history/browsing/auto-reply");
  assert.equal(byId("queue-detail").children[0].textContent, "平台已受理");

  await queue.open("review");
  assert.deepEqual(calls.splice(0), ["/api/comments/workbench?view=review&limit=30&offset=0"]);
  assert.equal(byId("queue-list").children.length, 0);

  await queue.open("history", "browsing/auto-reply");
  assert.deepEqual(calls, [
    "/api/comments/workbench?view=history&limit=30&offset=0",
    "/api/comments/workbench/browsing/auto-reply",
  ]);
});

test("the round form saves shared workers and matrix saves do not overwrite them", async t => {
  const byId = setupDOM(t);
  const { initOperationsModule, savePermissionMatrix, updatePermissionCapsule } = await import("../src/tuber_studio/web/modules/operations.js");
  const calls = [];
  const status = {
    settings: { workers: 2, max_replies_per_video: 1, interval_seconds: 600, visual_enabled: true },
    base: { messages: { enabled: false, allow_comment_handoff: true, auto_send: false } },
  };
  const api = async (path, options) => {
    calls.push([path, options]);
    if (path === "/api/automation/settings") Object.assign(status.settings, options.body);
    return status;
  };
  initOperationsModule(api, null);
  await updatePermissionCapsule(api, null);
  assert.equal(byId("op-workers").value, 2);
  assert.equal(byId("op-preview-round").disabled, false);
  status.active_round = { id: "current" };
  await updatePermissionCapsule(api, null);
  assert.equal(byId("op-preview-round").disabled, true);
  status.active_round = null;
  byId("op-workers").value = 6;
  await byId("op-round-settings").onsubmit({ preventDefault() {} });
  assert.equal(status.settings.workers, 6);
  assert.equal(byId("op-workers").value, 6);
  assert.equal(byId("op-preview-round").disabled, false);

  await savePermissionMatrix(api, null);
  const [shared, matrix] = calls.filter(([path]) => path === "/api/automation/settings").map(([, { body }]) => body);
  for (const field of Object.keys(shared)) assert.ok(!Object.hasOwn(matrix, field), field);
  assert.ok(!calls.some(([path]) => path === "/api/messages/policy"));
});

test("matrix uses the stored message policy and displays partial failure", async t => {
  const byId = setupDOM(t);
  const { savePermissionMatrix } = await import("../src/tuber_studio/web/modules/operations.js");
  document.querySelector = query => query.includes('name="matrix_dm"') ? { value: "2" } : null;
  const messages = { enabled: true, allow_comment_handoff: true, auto_send: false, max_per_day: 12 };
  const api = async (path, options) => {
    if (path === "/api/messages/policy") {
      assert.deepEqual(options.body, { ...messages, auto_send: true });
      throw new Error("写入失败");
    }
    return { settings: {}, base: { messages } };
  };
  await savePermissionMatrix(api, null);
  assert.equal(byId("matrix-save-feedback").textContent, "运营设置已保存，私信设置保存失败：写入失败");
});
