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
      querySelector() { return null; },
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

test("round settings preserve unlimited quotas and matrix saves leave them intact", async t => {
  const byId = setupDOM(t);
  const { initOperationsModule, savePermissionMatrix, updatePermissionCapsule } = await import("../src/tuber_studio/web/modules/operations.js");
  const calls = [];
  const status = {
    settings: { workers: 2, max_replies_per_video: 1, max_per_hour: 3, max_per_day: 10, interval_seconds: 600, visual_enabled: true },
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
  byId("op-max-replies").value = "0";
  byId("op-max-per-hour").value = "0";
  byId("matrix-max-per-day").value = "0";
  await byId("op-round-settings").onsubmit({ preventDefault() {} });
  assert.equal(status.settings.workers, 6);
  assert.equal(byId("op-workers").value, 6);
  assert.equal(byId("op-preview-round").disabled, false);
  for (const id of ["op-max-replies", "op-max-per-hour", "matrix-max-per-day"]) assert.equal(byId(id).value, 0);

  await savePermissionMatrix(api, null);
  const [shared, matrix] = calls.filter(([path]) => path === "/api/automation/settings").map(([, { body }]) => body);
  for (const quota of ["max_replies_per_video", "max_per_hour", "max_per_day"]) assert.equal(shared[quota], 0);
  for (const field of Object.keys(shared)) assert.ok(!Object.hasOwn(matrix, field), field);
  assert.ok(!calls.some(([path]) => path === "/api/messages/policy"));
});

test("both discovery buttons share state while full-channel operations are enabled", async t => {
  const byId = setupDOM(t);
  const { initDiscoveryPanel } = await import("../src/tuber_studio/web/modules/discovery-panel.js");
  const { renderOperationsQuickBar } = await import("../src/tuber_studio/web/modules/operations.js");
  const video = { id: "video", config: { discovery_id: "current" } };
  const inbox = { id: "inbox", config: { interaction_only: true } };
  const data = {
    enabled: false, state: "off", overlay_enabled: true,
    source: { mode: "recommend", keywords: [], watch_videos: [] },
  };
  const calls = [];
  const discovery = initDiscoveryPanel(async (path, options) => {
    assert.equal(path, "/api/automation/discovery");
    if (options) {
      calls.push(options);
      data.enabled = options.body.enabled;
      data.state = data.enabled ? "running" : "off";
    }
    return data;
  });
  await discovery.refresh();
  const home = byId("discovery-toggle");
  const settings = byId("discovery-toggle-settings");
  assert.equal(home.disabled, false);
  assert.equal(settings.disabled, false);
  await settings.onclick({ currentTarget: settings });
  assert.equal(home.textContent, "停止自动刷视频");
  assert.equal(settings.textContent, home.textContent);
  assert.equal(byId("discovery-settings-state").textContent, byId("discovery-state").textContent);

  const status = { policy: { enabled: false }, discovery: { enabled: true }, active_rounds: [video, inbox] };
  renderOperationsQuickBar(status);
  assert.equal(byId("op-toggle-auto").disabled, false);
  assert.equal(byId("op-stop-round").disabled, false);
  assert.equal(byId("op-preview-round").disabled, true);
  assert.match(byId("op-active-rounds").textContent, /刷视频.*互动收件箱/);
  renderOperationsQuickBar({ ...status, active_rounds: [inbox] });
  assert.equal(byId("op-stop-round").disabled, false);
  assert.equal(byId("op-preview-round").disabled, false);

  await home.onclick({ currentTarget: home });
  assert.equal(settings.textContent, "开始自动刷视频");
  assert.deepEqual(calls, [
    { method: "PUT", body: { enabled: true } },
    { method: "PUT", body: { enabled: false } },
  ]);
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
