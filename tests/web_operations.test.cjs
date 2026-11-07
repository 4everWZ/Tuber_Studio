"use strict";

const assert = require("node:assert/strict");
const { test } = require("node:test");

function setupDOM(t) {
  const elements = new Map();
  function element(tag = "div") {
    return {
      tagName: tag.toUpperCase(),
      children: [],
      dataset: {},
      listeners: {},
      style: {},
      append(...children) { this.children.push(...children); },
      insertBefore(child, before) {
        const previous = this.children.indexOf(child);
        if (previous >= 0) this.children.splice(previous, 1);
        const index = this.children.indexOf(before);
        this.children.splice(index < 0 ? this.children.length : index, 0, child);
      },
      replaceChildren(...children) { this.children = children; },
      setAttribute() {},
      addEventListener(type, listener) { this.listeners[type] = listener; },
      querySelector() { return null; },
      querySelectorAll(selector) {
        const descendants = this.children.filter(child => child && typeof child === "object");
        return descendants.flatMap(child => [
          ...(selector === "button" && child.tagName === "BUTTON" ? [child] : []),
          ...(child.querySelectorAll?.(selector) || []),
        ]);
      },
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

test("publishing saves the current draft and a failed attempt remains retryable", async t => {
  const byId = setupDOM(t);
  const { initCommentQueue } = await import("../src/tuber_studio/web/modules/comment-queue.js");
  let row = { id: "draft", channel: "comments", source: "interaction", status: "pending_review", text: "原稿", can_publish: true };
  let fail = true;
  const writes = [];
  const api = async (path, options) => {
    if (options) {
      writes.push([path, options.body]);
      if (path.endsWith("/review")) row = { ...row, status: "approved", text: options.body.text };
      if (path.endsWith("/publish")) {
        if (fail) { fail = false; throw new Error("临时网络错误"); }
        row = { ...row, status: "published" };
      }
      return row;
    }
    if (path.includes("?view=")) return { items: [row], offset: 0, total: 1 };
    return row;
  };
  const queue = initCommentQueue(api, () => {}, () => null);
  await queue.open("review", "comments/draft");
  const detail = byId("queue-detail");
  const editor = detail.children.find(child => child.tagName === "LABEL").children[0];
  editor.value = "编辑后的内容";
  editor.listeners.input();
  assert.deepEqual(detail.querySelectorAll("button").map(button => button.textContent), ["保存", "拒绝", "发布", "复制"]);
  await detail.querySelectorAll("button").find(button => button.textContent === "发布").onclick();
  assert.equal(byId("queue-feedback").textContent, "发布失败：临时网络错误");
  assert.deepEqual(writes, [
    ["/api/comments/drafts/draft/review", { action: "approve", text: "编辑后的内容" }],
    ["/api/comments/drafts/draft/publish", undefined],
  ]);
  const retry = detail.querySelectorAll("button").find(button => button.textContent === "发布");
  assert.equal(retry.disabled, false);
  await retry.onclick();
  assert.equal(row.status, "published");
  assert.equal(writes.filter(([path]) => path.endsWith("/review")).length, 1);
  assert.equal(detail.children[0].textContent, "平台已受理");
});

test("browsing activity stays visible beside drafts without another view switch", async t => {
  const byId = setupDOM(t);
  const { initCommentQueue } = await import("../src/tuber_studio/web/modules/comment-queue.js");
  let activityLoads = 0;
  const queue = initCommentQueue(async () => ({ items: [], total: 0, offset: 0 }), () => {}, () => null,
    async () => { activityLoads++; });
  await queue.open("review");
  assert.equal(byId("queue-records").hidden, false);
  assert.equal(byId("queue-activity").hidden, false);
  assert.equal(byId("queue-view").value, "review");
  await queue.open("activity");
  assert.equal(byId("queue-view").value, "review");
  assert.equal(activityLoads, 2);
});

test("activity shows the recipient, reply and outcome while scheduling stays collapsed", async t => {
  const byId = setupDOM(t);
  const { initDiscoveryActivity } = await import("../src/tuber_studio/web/modules/discovery-panel.js");
  const refresh = initDiscoveryActivity(async () => ({
    scope: ["account", "whale", 3],
    activity: [
      { id: "task", kind: "task", title: "取材中的视频", status: "retry_wait", updated_at: 100, reason: "round_budget" },
      { id: "reply", kind: "reply_public", title: "视频标题", recipient_name: "小明", status: "published", excerpt: "白饭记得趁热吃。", updated_at: 100 },
    ],
  }));
  await refresh();
  const [empty, replies, scheduling] = byId("discovery-activity-list").children;
  assert.equal(empty.hidden, true);
  assert.equal(replies.children.length, 1);
  const [badge, title, , excerpt] = replies.children[0].children[0].children;
  assert.equal(title.textContent, "回复 小明");
  assert.equal(excerpt.textContent, "白饭记得趁热吃。");
  assert.equal(badge.textContent, "已发送");
  assert.ok(!scheduling.open);
  assert.equal(scheduling.children[0].textContent, "调度详情（1）");
  assert.equal(scheduling.children[1].children.length, 1);
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

  document.querySelector = query => query.includes('name="matrix_dm"') ? { value: "0" } : null;
  await savePermissionMatrix(api, null);
  const [shared, matrix] = calls.filter(([path]) => path === "/api/automation/settings").map(([, { body }]) => body);
  for (const quota of ["max_replies_per_video", "max_per_hour", "max_per_day"]) assert.equal(shared[quota], 0);
  for (const field of Object.keys(shared)) assert.ok(!Object.hasOwn(matrix, field), field);
  const messageSave = calls.find(([path]) => path === "/api/messages/policy");
  assert.equal(messageSave[1].body.enabled, false);
  assert.equal(messageSave[1].body.auto_send, false);
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

test("matrix enables private messages when selecting automatic handoff without enabling polling", async t => {
  const byId = setupDOM(t);
  const { savePermissionMatrix } = await import("../src/tuber_studio/web/modules/operations.js");
  document.querySelector = query => query.includes('name="matrix_dm"') ? { value: "2" } : null;
  const messages = { enabled: false, auto_poll: false, allow_comment_handoff: true, auto_send: false, max_per_day: 12 };
  let saved;
  const api = async (path, options) => {
    if (path === "/api/messages/policy") saved = options.body;
    return { settings: {}, base: { messages } };
  };
  await savePermissionMatrix(api, null);
  assert.deepEqual(saved, { ...messages, enabled: true, auto_send: true });
  assert.match(byId("matrix-save-feedback").textContent, /已保存/);
});
