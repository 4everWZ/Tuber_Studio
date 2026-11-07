"use strict";

const assert = require("node:assert/strict");
const { test } = require("node:test");

function setupDOM(t) {
  const elements = new Map();
  function element(tag = "div") {
    return {
      tagName: tag.toUpperCase(),
      value: "",
      children: [],
      dataset: {},
      classList: { toggle() {} },
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
  globalThis.TuberFormatters = require("../src/tuber_studio/web/modules/formatters.js");
  globalThis.OperationContinuationView = class {
    constructor(root) { this.root = root; }
    render() {}
  };
  t.after(() => {
    delete globalThis.document;
    delete globalThis.matchMedia;
    delete globalThis.TuberFormatters;
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
    "/api/comments/workbench?view=history&limit=10&offset=0",
  ]);
  assert.equal(byId("queue-view").value, "history");
  assert.equal(byId("queue-list").children[0].href, "#comments/history/browsing/auto-reply");
  assert.equal(byId("queue-detail").children[0].textContent, "平台已受理");

  await queue.open("review");
  assert.deepEqual(calls.splice(0), ["/api/comments/workbench?view=review&limit=10&offset=0"]);
  assert.equal(byId("queue-list").children.length, 0);

  await queue.open("history", "browsing/auto-reply");
  assert.deepEqual(calls, [
    "/api/comments/workbench?view=history&limit=10&offset=0",
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

test("comments use ten compact records per page without loading browsing history", async t => {
  setupDOM(t);
  const { initCommentQueue } = await import("../src/tuber_studio/web/modules/comment-queue.js");
  const calls = [];
  const queue = initCommentQueue(async path => { calls.push(path); return { items: [], total: 0, offset: 0 }; }, () => {}, () => null);
  await queue.open("review");
  assert.deepEqual(calls, ["/api/comments/workbench?view=review&limit=10&offset=0"]);
});

test("daily operational activity shows video replies with date and page navigation", async t => {
  const byId = setupDOM(t);
  const { initOperationsActivity } = await import("../src/tuber_studio/web/modules/operations-activity.js");
  const calls = [];
  const refresh = initOperationsActivity(async path => {
    calls.push(path);
    const query = new URLSearchParams(path.split("?")[1]);
    return { day: query.get("day") || "2026-11-07", page: Number(query.get("page")), total: 11, pages: 2,
      counts: { videos: 11, watched: 8, fully_read: 2, partial: 6, replied: 1, replies: 1, failed: 0 },
      items: [{ id: "video/BVexample", bvid: "BVexample", title: "真实视频标题", read_state: "viewed", fully_read: false, status: "published",
        reason: "这段饭锅的镜头让我想提醒饭要趁热吃。", replies: [{ id: "reply", kind: "reply_public", recipient_name: "小明", status: "published", text: "白饭记得趁热吃。" }] }],
    };
  });
  refresh.setKind("videos");
  await refresh();
  assert.match(calls[0], /page=1&page_size=10/);
  const row = byId("discovery-activity-list").children[0];
  assert.equal(row.children[0].children[0].textContent, "真实视频标题");
  assert.equal(row.children[0].children[1].textContent, "已看过");
  const reply = row.children[2].children[1];
  assert.equal(reply.children[0].textContent, "回复 小明 · 已发送");
  assert.equal(reply.children[1].textContent, "白饭记得趁热吃。");
  assert.match(byId("discovery-history-counts").textContent, /视频 11/);
  await byId("discovery-history-next").onclick();
  assert.match(calls[1], /page=2/);
  byId("discovery-history-day").value = "2026-11-06";
  await byId("discovery-history-day").listeners.change();
  assert.match(calls[2], /page=1.*day=2026-11-06/);
});

test("unified activity distinguishes approved drafts, received messages and successful sends", async t => {
  const byId = setupDOM(t);
  const { initOperationsActivity } = await import("../src/tuber_studio/web/modules/operations-activity.js");
  const calls = [];
  const refresh = initOperationsActivity(async path => {
    calls.push(path);
    const items = [
      { id: "v", kind: "video", title: "看过的视频", bvid: "BVx", read_state: "viewed", has_main_reply: true, main_replies: 0 },
      { id: "c", kind: "comment", title: "回复 小蓝", text: "等待审核的正文", status: "pending_review" },
      { id: "m", kind: "message", direction: "incoming", title: "收到 小明 的私信", text: "你好", status: "received" },
      { id: "d", kind: "dynamic", title: "动态", text: "已发送动态正文", status: "published" },
    ];
    const filtered = path.includes("kind=messages") ? items.filter(item => item.kind === "message") : items;
    return { day: "2026-11-07", page: 1, total: filtered.length, pages: 1, counts: { total: 4, sent: 1, pending: 2 }, items: filtered };
  });
  await refresh();
  assert.match(calls[0], /\/api\/automation\/activity\?.*kind=all/);
  const rows = byId("discovery-activity-list").children;
  assert.equal(rows[0].children[1].children[0].textContent, "已有主评");
  assert.equal(rows[1].children[0].children[1].textContent, "待审核");
  assert.equal(rows[2].children[0].children[1].textContent, "收到来信");
  assert.equal(rows[3].children[0].children[1].textContent, "已发送");
  refresh.setKind("messages");
  await refresh();
  assert.match(calls[1], /kind=messages/);
  assert.match(byId("discovery-history-counts").textContent, /当日总计 4/);
  assert.match(byId("discovery-history-page").textContent, /共 1 条记录/);
});

test("clearing read alerts clears local and remote counts and stays clear after refresh", async t => {
  const byId = setupDOM(t);
  const { initIssueLog } = await import("../src/tuber_studio/web/modules/discovery-panel.js");
  let cleared = false;
  const issues = initIssueLog(async (path, options) => {
    if (options) { assert.equal(path, "/api/automation/issues/clear"); assert.equal(options.method, "POST"); cleared = true; return { records: { issues: [], sent: [] } }; }
    return { issues: cleared ? [] : [{ error: "网络中断", status: "retry_wait", updated_at: 100 }], sent: [] };
  }, () => "issues");
  issues.record("本地测试告警", "#automation");
  await issues.refresh();
  assert.match(byId("workspace-issues").textContent, /2/);
  await byId("clear-workspace-issues").onclick({ currentTarget: byId("clear-workspace-issues") });
  assert.equal(byId("workspace-issues").textContent, "告警与错误");
  await issues.refresh();
  assert.equal(byId("workspace-issues").textContent, "告警与错误");
});

test("node connection and model load state are separate from stopped livestream state", async () => {
  const { inferenceDisplay } = await import("../src/tuber_studio/web/modules/inference-status.js");
  const settings = { inference_broker: { deployment: "remote" } };
  const status = { state: "stopped", inference: { state: "active", active_provider: "pet", providers: { pet: { running: true, is_sleeping: false } } } };
  assert.match(inferenceDisplay(status, settings).text, /Jetson 已连接.*模型已加载/);
  assert.doesNotMatch(inferenceDisplay(status, settings).text, /已停止/);
  status.inference.state = "idle";
  status.inference.providers.pet.running = false;
  assert.match(inferenceDisplay(status, settings).text, /已连接.*模型未加载/);
  status.inference.error = "无法连接";
  assert.match(inferenceDisplay(status, settings).text, /连接异常/);
});

test("new generation shows only this task and selects its first actual draft", async t => {
  const byId = setupDOM(t);
  const { initCommentQueue } = await import("../src/tuber_studio/web/modules/comment-queue.js");
  let ready = false;
  const old = { id: "old", channel: "comments", status: "pending_review", source: "manual", text: "旧草稿" };
  const fresh = { id: "fresh", channel: "comments", status: "pending_review", source: "manual", text: "本次生成的新回复" };
  const calls = [];
  const queue = initCommentQueue(async path => {
    calls.push(path);
    if (path.includes("?view=")) {
      const scoped = path.includes("task_id=new-task");
      const items = scoped ? ready ? [fresh] : [] : [old];
      return { items, offset: 0, total: items.length, ...(scoped ? { task: { id: "new-task", status: ready ? "completed" : "running", total: 1, done: ready ? 1 : 0 } } : {}) };
    }
    return path.endsWith("fresh") ? fresh : old;
  }, () => {}, () => null);
  await queue.open("review");
  await queue.trackGeneration("new-task", [123]);
  assert.match(calls.at(-1), /task_id=new-task&comment_ids=123/);
  assert.equal(byId("queue-list").children.length, 0);
  ready = true;
  await queue.refresh();
  assert.equal(byId("queue-list").children[0].dataset.queueKey, "comments/fresh");
  assert.equal(byId("queue-detail").children.find(child => child.tagName === "LABEL").children[0].value, "本次生成的新回复");
});

test("JSON import is a single write and opens the imported scope without mandatory preview", async t => {
  const byId = setupDOM(t);
  const { initMaintenanceModule } = await import("../src/tuber_studio/web/modules/maintenance.js");
  byId("import-video").value = "import-one-step";
  byId("import-comments").value = JSON.stringify([{ comment_id: "1", content: "白饭呢？" }]);
  const pending = [], calls = [], scopes = [];
  initMaintenanceModule({}, {
    api: async (path, options) => { calls.push([path, options]); return { imported: 1, video_id: "import-one-step" }; },
    busy: (_, action) => { const work = action(); pending.push(work); return work; }, notice() {}, onCommentsImported: value => scopes.push(value),
  });
  byId("import-comments-form").listeners.submit({ preventDefault() {}, submitter: byId("submit-import") });
  await Promise.all(pending);
  assert.deepEqual(calls.map(([path]) => path), ["/api/comments/import"]);
  assert.deepEqual(scopes, ["import-one-step"]);
});

test("reading comments sends the video and page count and selects the returned video scope", async t => {
  const byId = setupDOM(t);
  const { initCommentsModule } = await import("../src/tuber_studio/web/modules/comments.js");
  byId("comment-video").value = "https://www.bilibili.com/video/BVexample";
  byId("comment-pages").value = "3";
  const calls = [], pending = [], scopes = [];
  initCommentsModule({}, {
    api: async (path, options) => { calls.push([path, options.body]); return { video_id: "BVexample", count: 3 }; },
    busy: (_, action) => { const work = action(); pending.push(work); return work; },
    onScopeChanged: value => scopes.push(value),
  });
  byId("fetch-comments-form").listeners.submit({ preventDefault() {}, submitter: byId("submit-fetch") });
  await Promise.all(pending);
  assert.deepEqual(calls, [["/api/comments/fetch", { video: "https://www.bilibili.com/video/BVexample", max_pages: 3 }]]);
  assert.deepEqual(scopes, ["BVexample"]);
});

test("task reasons preserve specific model explanations and separate scheduler state", () => {
  const { humanizeTaskReason } = require("../src/tuber_studio/web/modules/formatters.js");
  assert.equal(humanizeTaskReason({ status: "selected", decisions: [{ explanation: "片尾的饭锅镜头让我想补一句饭要趁热吃。", reason: "specific_detail" }] }),
    "片尾的饭锅镜头让我想补一句饭要趁热吃。");
  const waiting = humanizeTaskReason({ status: "checkpoint", retry_kind: "round_budget", reason: "已经读完前两段字幕，准备继续看第三段。" });
  assert.match(waiting, /已经读完前两段字幕/);
  assert.match(waiting, /程序进度：/);
  assert.match(humanizeTaskReason({ status: "preparing" }), /正在读取字幕与画面/);
  assert.match(humanizeTaskReason({ status: "viewed_without_comment" }), /历史记录未保存具体原因/);
  assert.match(humanizeTaskReason({ reason: "daily_watch_limit" }), /^程序进度：今日观看次数已用完/);
  assert.match(humanizeTaskReason({ reason: "daily_reply_limit" }), /^程序进度：该视频今日回复数已用完/);
  const { humanizeStatus } = require("../src/tuber_studio/web/modules/formatters.js");
  for (const status of ["paused", "deferred_daily", "comments_checked"]) assert.notEqual(humanizeStatus(status).label, status);
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

test("one parent start action uses saved permissions without enabling additional channels", async t => {
  const byId = setupDOM(t);
  const { initOperationsModule, updatePermissionCapsule } = await import("../src/tuber_studio/web/modules/operations.js");
  const calls = [];
  const status = { policy: { enabled: false }, settings: { discovery_enabled: false, reply_enabled: true }, permissions: { video_enabled: false, replies: "review", private_messages: "off", dynamics: "off" } };
  const api = async (path, options) => {
    if (options) { calls.push([path, options.body]); assert.equal(path, "/api/automation/enable"); status.policy.enabled = options.body.enabled; }
    return status;
  };
  initOperationsModule(api, null);
  await updatePermissionCapsule(api, null);
  await byId("op-toggle-auto").onclick();
  assert.equal(byId("op-toggle-auto").textContent, "停止自主运营");
  assert.match(byId("op-permissions-summary").textContent, /评论（生成草稿）/);
  assert.doesNotMatch(byId("op-permissions-summary").textContent, /刷视频|私信|动态/);
  assert.equal(status.settings.discovery_enabled, false);
  await byId("op-toggle-auto").onclick();
  assert.deepEqual(calls, [["/api/automation/enable", { enabled: true }], ["/api/automation/enable", { enabled: false }]]);
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

test("sending readiness reports actual blockers without inventing an approval gate", async t => {
  const byId = setupDOM(t);
  const { renderPermissionStatus } = await import("../src/tuber_studio/web/modules/permission-matrix.js");
  renderPermissionStatus({ settings: { top_level_enabled: true, top_level_auto_publish: true }, automatic_sending_ready: false,
    blockers: ["模型不可用：连接失败"] }, { configured: { sessdata: true, csrf: true }, state: "logged_in", profile: { uid: "test" } });
  assert.match(byId("automatic-sending-state").textContent, /模型不可用：连接失败/);
  assert.doesNotMatch(byId("automatic-sending-state").textContent, /语义|验收|需审核/);
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
