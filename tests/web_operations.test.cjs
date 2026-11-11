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
      attributes: {},
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
      setAttribute(name, value) { this.attributes[name] = value; },
      getAttribute(name) { return this.attributes[name]; },
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
    if (path.includes("?view=")) {
      const items = path.includes("view=history") === (row.status === "published") ? [row] : [];
      return { items, offset: 0, total: items.length };
    }
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
  assert.equal(detail.children[0].textContent, "记录详情");
  assert.match(detail.children[1].textContent, /本条已处理/);
  assert.equal(byId("queue-list").children.length, 0);
  await queue.refresh();
  assert.equal(detail.querySelectorAll("button").length, 0);
  await queue.open("history", "comments/draft");
  assert.equal(detail.children[0].textContent, "平台已受理");
});

test("a selected comment completed in the background clears its detail while dirty local text remains recoverable", async t => {
  const byId = setupDOM(t);
  const { initCommentQueue } = await import("../src/tuber_studio/web/modules/comment-queue.js");
  for (const dirty of [false, true]) {
    let status = "pending_review", cleared = 0;
    const row = () => ({ id: "background", channel: "comments", source: "manual", status, text: "已保存原文", updated_at: status });
    const queue = initCommentQueue(async url => url.includes("?view=") ?
      { items: status === "pending_review" ? [row()] : [], total: status === "pending_review" ? 1 : 0, offset: 0 } : row(),
    () => {}, () => null, { onSelectionCleared: () => { cleared++; } });
    await queue.open("review", "comments/background");
    const detail = byId("queue-detail");
    if (dirty) {
      const editor = detail.children.find(item => item.tagName === "LABEL").children[0];
      editor.value = "尚未提交的本地修改";
      editor.listeners.input();
    }
    status = "published";
    await queue.refresh();
    if (dirty) {
      const field = detail.children.find(item => item.tagName === "LABEL");
      assert.equal(field.textContent, "本地未保存修改");
      assert.equal(field.children[0].value, "尚未提交的本地修改");
      assert.equal(field.children[0].readOnly, true);
      assert.equal(queue.hasUnsaved(), true);
      assert.equal(cleared, 0);
    } else {
      assert.equal(detail.querySelectorAll("button").length, 0);
      assert.match(detail.children[1].textContent, /本条已处理/);
      assert.equal(cleared, 1);
    }
    queue.leave();
  }
});

test("comments use ten compact records per page without loading browsing history", async t => {
  setupDOM(t);
  const { initCommentQueue } = await import("../src/tuber_studio/web/modules/comment-queue.js");
  const calls = [];
  const queue = initCommentQueue(async path => { calls.push(path); return { items: [], total: 0, offset: 0 }; }, () => {}, () => null);
  await queue.open("review");
  assert.deepEqual(calls, ["/api/comments/workbench?view=review&limit=10&offset=0"]);
});

function sourceDependencies(api) {
  const node = (tag, text) => Object.assign(document.createElement(tag), { textContent: text });
  return {
    api, node, list: value => value, label: value => value, badge: text => node("span", text),
    cell(row, text) { const td = node("td"); td.append(typeof text === "object" ? text : node("span", text)); row.append(td); return td; },
    empty(row) { row.replaceChildren(); }, busy: (_, action) => action(), notice() {},
    commentContinuation: { render() {} }, setBadge() {}, describe() {},
  };
}

const sourceComment = (id, status) => ({ id, comment_id: String(id), video_id: "import-ui-test", content: `评论 ${id}`,
  ...(status ? { draft: { id: `draft-${id}`, status, text: "已有回复" } } : {}) });

test("source selection excludes completed records and never regenerates existing drafts", async t => {
  const byId = setupDOM(t);
  const { initCommentsModule } = await import("../src/tuber_studio/web/modules/comments.js");
  const records = [sourceComment(1, "rejected"), sourceComment(2, "published"), sourceComment(3, "copied"),
    sourceComment(4, "approved"), sourceComment(5, "pending_review"), sourceComment(6)];
  const model = { comments: [], categorySaving: new Set(), selectedComments: new Set([1, 2, 3, 4, 5, 6]) };
  const module = initCommentsModule(model, sourceDependencies(async url => url.startsWith("/api/comments?") ?
    url.includes("status=rejected") ? records.slice(0, 1) : records : []));
  await module.refreshComments();
  assert.deepEqual(model.comments.map(row => row.id), [4, 5, 6]);
  assert.deepEqual([...model.selectedComments], [6]);
  assert.equal(byId("selected-comment-count").textContent, "已选 1 条");
  assert.equal(byId("comments").children[0].children[0].children[0].disabled, true);
  byId("select-comments").checked = false;
  byId("select-comments").listeners.change();
  assert.equal(model.selectedComments.size, 0);
  byId("filter-status").value = "rejected";
  await module.refreshComments();
  assert.deepEqual(model.comments.map(row => row.id), [1]);
  assert.equal(byId("comments").children[0].children[0].children[0].disabled, true);
  assert.equal(byId("select-comments").checked, false);
});

test("source pagination clears the previous page's detail and context while retaining batch checks", async t => {
  const byId = setupDOM(t);
  const { initCommentsModule } = await import("../src/tuber_studio/web/modules/comments.js");
  const model = { comments: Array.from({ length: 12 }, (_, i) => sourceComment(i + 1)), commentId: 1,
    selectedComments: new Set([1]), categorySaving: new Set() };
  let selection = "old";
  initCommentsModule(model, { ...sourceDependencies(), onCommentSelected: value => { selection = value; } });
  byId("thread-context-state").textContent = "旧楼上下文";
  byId("source-next").onclick();
  assert.equal(model.commentSourcePage, 1);
  assert.equal(model.commentId, null);
  assert.equal(selection, null);
  assert.deepEqual([...model.selectedComments], [1]);
  assert.equal(byId("thread-context-state").textContent, "");
  assert.match(byId("draft-source").textContent, /选择一条评论/);
});

test("accepted generation clears its selected sources while a failed request preserves them", async t => {
  const byId = setupDOM(t);
  const { initCommentsModule } = await import("../src/tuber_studio/web/modules/comments.js");
  const model = { comments: [sourceComment(1)], categorySaving: new Set(), selectedComments: new Set([1]) };
  let fail = true, tracked;
  initCommentsModule(model, { ...sourceDependencies(async (url, options) => {
    assert.equal(url, "/api/comments/drafts");
    assert.deepEqual(options.body, { comment_ids: [1] });
    if (fail) throw Error("连接失败");
    return { task_id: "new-task" };
  }), onDraftsRequested: (id, ids) => { tracked = [id, ids]; } });
  await assert.rejects(byId("generate-drafts").listeners.click(), /连接失败/);
  assert.deepEqual([...model.selectedComments], [1]);
  fail = false;
  await byId("generate-drafts").listeners.click();
  assert.deepEqual(tracked, ["new-task", [1]]);
  assert.equal(model.selectedComments.size, 0);
  assert.equal(byId("selected-comment-count").textContent, "已选 0 条");
  assert.equal(byId("select-comments").checked, false);
  assert.equal(byId("generate-drafts").disabled, true);
});

test("saving retains a reviewable draft and terminal actions clear selection and detail", async t => {
  const byId = setupDOM(t);
  const { initCommentQueue } = await import("../src/tuber_studio/web/modules/comment-queue.js");
  const { initCommentsModule } = await import("../src/tuber_studio/web/modules/comments.js");
  const navigatorBefore = Object.getOwnPropertyDescriptor(globalThis, "navigator");
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { clipboard: { async writeText() {} } } });
  t.after(() => navigatorBefore ? Object.defineProperty(globalThis, "navigator", navigatorBefore) : delete globalThis.navigator);
  const model = { comments: [sourceComment(1, "pending_review"), sourceComment(2, "approved")], categorySaving: new Set(), selectedComments: new Set([1, 2]), commentId: 1 };
  const sources = initCommentsModule(model, sourceDependencies());
  let row = { id: "draft-1", comment_id: 1, channel: "comments", source: "manual", status: "pending_review", text: "待审核回复" };
  const next = { id: "draft-2", comment_id: 2, channel: "comments", source: "manual", status: "approved", text: "下一条" };
  const queue = initCommentQueue(async (url, options) => {
    if (options) {
      if (url.endsWith("/copy")) row = { ...row, status: "copied" };
      else row = { ...row, status: options.body.action === "reject" ? "rejected" : "approved" };
      return row;
    }
    if (url.includes("?view=")) {
      const items = [row, next].filter(item => ["pending_review", "approved"].includes(item.status));
      return { items, offset: 0, total: items.length };
    }
    return url.endsWith("draft-2") ? next : row;
  }, () => {}, () => null, { onDraftUpdated: sources.draftUpdated });
  await queue.open("review", "comments/draft-1");
  let detail = byId("queue-detail");
  await detail.querySelectorAll("button").find(button => button.textContent === "保存").onclick();
  assert.equal(model.comments[0].draft.status, "approved");
  assert.equal(detail.children[0].textContent, "已审核 · 待提交");
  await detail.querySelectorAll("button").find(button => button.textContent === "复制").onclick();
  assert.equal(model.comments.length, 1);
  assert.equal(model.commentId, null);
  assert.equal(model.selectedComments.size, 0);
  assert.equal(detail.querySelectorAll("button").length, 0);
  await queue.refresh();
  assert.equal(detail.querySelectorAll("button").length, 0);
  assert.equal(byId("queue-list").children.length, 1);
  await queue.open("review", "comments/draft-2");
  row = next;
  await detail.querySelectorAll("button").find(button => button.textContent === "拒绝").onclick();
  assert.equal(model.comments.length, 0);
  assert.equal(detail.querySelectorAll("button").length, 0);
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

test("activity distinguishes an uninitiated outreach from a reply and translates its progress", async t => {
  const byId = setupDOM(t);
  const { initOperationsActivity } = await import("../src/tuber_studio/web/modules/operations-activity.js");
  const refresh = initOperationsActivity(async () => ({ day: "2026-11-07", page: 1, total: 2, pages: 1, items: [
    { id: "outreach/1", kind: "message", contact_type: "outreach", recipient_name: "小蓝", status: "not_initiated", reason: "long_followup" },
    { id: "outreach/2", kind: "message", contact_type: "outreach", status: "waiting", outreach_status: "awaiting_completion" },
  ] }));
  await refresh();
  const rows = byId("discovery-activity-list").children;
  assert.equal(rows[0].children[0].children[0].textContent, "主动私信 · 小蓝");
  assert.equal(rows[0].children[0].children[1].textContent, "未发起私信");
  assert.equal(rows[0].children[1].textContent, "楼内话题后续交流");
  assert.equal(rows[1].children[0].children[1].textContent, "等待约定完成");
});

test("activity updates preserve each stable record's expanded details", async t => {
  const byId = setupDOM(t);
  const { initOperationsActivity } = await import("../src/tuber_studio/web/modules/operations-activity.js");
  let updated = 1;
  const refresh = initOperationsActivity(async () => ({ day: "2026-11-07", page: 1, total: 1, pages: 1,
    items: [{ id: "video/BVstable", kind: "video", bvid: "BVstable", updated_at: updated++ }] }));
  await refresh();
  const details = () => byId("discovery-activity-list").children[0].children.find(item => item.tagName === "DETAILS");
  details().open = true;
  await refresh();
  assert.equal(details().open, true);
  details().open = false;
  await refresh();
  assert.equal(details().open, false);
});

test("video main comment summaries distinguish actual receipts, drafts and model choices", async () => {
  const { mainReplySummary } = await import("../src/tuber_studio/web/modules/operations-activity.js");
  assert.equal(mainReplySummary({ main_replies: 0 }), "主评尚未决定");
  assert.equal(mainReplySummary({ read_state: "comments_only" }), "只逛评论，未生成主评");
  assert.equal(mainReplySummary({ main_decision: { action: "skip" } }), "本次不发主评");
  assert.equal(mainReplySummary({ main_decision: { action: "reply_public" } }), "已决定主评，等待生成");
  assert.equal(mainReplySummary({ replies: [{ kind: "top_level", status: "pending_review" }] }), "主评待审核");
  assert.equal(mainReplySummary({ has_main_reply: true }), "已有主评");
});

test("filtering browser tasks clears a now unrelated right-hand inspector", t => {
  const byId = setupDOM(t);
  const Workbench = require("../src/tuber_studio/web/modules/operations-workbench.js");
  const workbench = Object.create(Workbench.prototype);
  Object.assign(workbench, { activeTab: "tasks", tasks: [{ id: "old", status: "completed" }, { id: "next", status: "checkpoint" }],
    taskFilter: "", selectedTaskId: "old", taskPaginator: { currentPage: 3 }, container: { querySelector: query => byId(query) } });
  let rendered = 0;
  workbench.renderMasterList = () => {};
  workbench.renderDetail = () => { rendered++; };
  workbench.renderMasterSubbar();
  const select = byId("#op-master-subbar").children[0];
  select.value = "checkpoint";
  select.onchange();
  assert.equal(workbench.selectedTaskId, null);
  assert.equal(workbench.taskFilter, "checkpoint");
  assert.equal(workbench.taskPaginator.currentPage, 1);
  assert.equal(rendered, 1);
});

test("dynamic text edits survive refresh, save the submitted text and clear only after rejection", async t => {
  const byId = setupDOM(t);
  const { initDynamicsModule } = await import("../src/tuber_studio/web/modules/dynamics.js");
  const post = { id: 1, content: "原动态", status: "pending_review" };
  const model = { dynamicPosts: [post], dynamicPostId: 1, dynamicMemeCache: new Map(), dynamicMemePending: new Set(), dynamicMemorySaving: new Set() };
  const writes = [];
  const module = initDynamicsModule(model, { ...sourceDependencies(async (url, options) => {
    if (options) {
      writes.push(options.body);
      post.content = options.body.text;
      post.status = options.body.action === "reject" ? "rejected" : "approved";
      return post;
    }
    if (url === "/api/dynamics/policy") return { credentials_configured: true };
    if (url === "/api/dynamics/posts") return [{ ...post }];
    if (url === "/api/dynamics/memory-summary") return { entries: [] };
    return [];
  }), publicSettings: value => value, safeMessage: String, display: String, tokenCount: String });
  byId("dynamic-post-text").value = "修改后的动态";
  byId("dynamic-post-text").listeners.input();
  await module.refreshDynamics();
  assert.equal(byId("dynamic-post-text").value, "修改后的动态");
  await byId("approve-dynamic").listeners.click();
  assert.deepEqual(writes[0], { action: "approve", text: "修改后的动态" });
  assert.equal(model.dynamicPostId, 1);
  assert.equal(model.dynamicPostDirty, false);
  await byId("reject-dynamic").listeners.click();
  assert.equal(model.dynamicPostId, null);
  assert.equal(byId("dynamic-post-text").value, "");
  assert.equal(byId("dynamic-post-receipt").textContent, "");
  assert.equal(byId("dynamic-posts").children.length, 0);
});

test("dynamic background refresh reconciles external terminal states and preserves local edits", async t => {
  const byId = setupDOM(t);
  const { initDynamicsModule } = await import("../src/tuber_studio/web/modules/dynamics.js");
  for (const dirty of [false, true]) {
    const serverPost = { id: 1, content: "后台已保存正文", status: "published" };
    const model = { dynamicPosts: [{ ...serverPost, status: "approved" }], dynamicPostId: 1, dynamicPostDirty: dirty,
      dynamicListsLoaded: true, dynamicDecisions: [], dynamicMemories: [], dynamicMemorySummary: { entries: [] },
      dynamicMemeCache: new Map(), dynamicMemePending: new Set(), dynamicMemorySaving: new Set() };
    byId("dynamic-post-text").value = dirty ? "本地尚未提交动态" : "后台已保存正文";
    const calls = [];
    const module = initDynamicsModule(model, { ...sourceDependencies(async url => {
      calls.push(url);
      if (url === "/api/dynamics/policy") return { credentials_configured: true };
      if (url === "/api/dynamics/posts") return [{ ...serverPost }];
      if (url === "/api/dynamics/memory-summary") return { entries: [] };
      return [];
    }), publicSettings: value => value, safeMessage: String, display: String, tokenCount: String });
    await module.refreshDynamics({ background: true });
    if (dirty) {
      assert.equal(model.dynamicPostId, 1);
      assert.equal(byId("dynamic-post-text").value, "本地尚未提交动态");
      assert.equal(byId("dynamic-post-text").readOnly, true);
      assert.equal(byId("dynamic-post-text").disabled, false);
    } else {
      assert.equal(model.dynamicPostId, null);
      assert.equal(byId("dynamic-post-text").value, "");
    }
    const before = calls.length;
    await module.refreshDynamics({ background: true });
    assert.equal(calls.length, before, "current-page background calls are throttled");
  }
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

test("a cold node remains connected while the selected model sleeps", async () => {
  const { inferenceDisplay } = await import("../src/tuber_studio/web/modules/inference-status.js");
  const state = inferenceDisplay({ inference: { state: "cold", error: null, active_provider: null,
    selected_provider: "pet", providers: { pet: { running: false, is_sleeping: true } } } },
  { inference_broker: { deployment: "remote" } });
  assert.equal(state.connected, true);
  assert.equal(state.loaded, false);
  assert.equal(state.text, "Jetson 已连接 · 模型未加载");
});

test("a loaded independent model keeps its own connection status", async () => {
  const { inferenceDisplay } = await import("../src/tuber_studio/web/modules/inference-status.js");
  const status = { providers: { llm: "llama_cpp", llm_route: "direct" },
    inference: { state: "error", error: "语音节点暂不可达" },
    context: { verified: true, is_sleeping: false }, persona: { configured_model: "selected-9B" } };
  assert.equal(inferenceDisplay(status).text, "独立推理 已连接 · 模型已加载 · selected-9B");
  status.context.profile_error = "连接中断";
  assert.equal(inferenceDisplay(status).text, "独立推理 连接异常");
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
  await queue.open("verify");
  const verify = calls.findLast(path => path.includes("?view=verify"));
  assert.ok(verify);
  assert.ok(!verify.includes("task_id="), "a normal status route must not keep an earlier generation task scope");
  assert.equal(byId("queue-show-all").hidden, true);
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
  const status = { policy: { enabled: false }, state: "off", settings: { discovery_enabled: false, reply_enabled: true }, permissions: { video_enabled: false, replies: "review", private_messages: "off", dynamics: "off" } };
  const api = async (path, options) => {
    if (options) { calls.push([path, options.body]); assert.equal(path, "/api/automation/enable"); status.policy.enabled = options.body.enabled; status.state = options.body.enabled ? "running" : "off"; }
    return status;
  };
  initOperationsModule(api, null);
  await updatePermissionCapsule(api, null);
  await byId("op-toggle-auto").onclick();
  assert.equal(byId("op-toggle-auto").textContent, "停止自主运营");
  const capabilities = Object.fromEntries(byId("op-permissions-summary").children.map(item => [item.dataset.capability, item]));
  assert.equal(capabilities.comments.dataset.state, "yellow");
  for (const key of ["videos", "message_reply", "dynamics"]) assert.equal(capabilities[key].dataset.state, "gray");
  assert.equal(status.settings.discovery_enabled, false);
  await byId("op-toggle-auto").onclick();
  assert.deepEqual(calls, [["/api/automation/enable", { enabled: true }], ["/api/automation/enable", { enabled: false }]]);
});

test("capability lamps separate saved permissions from actual runtime and private polling", async () => {
  const { operationCapabilities } = await import("../src/tuber_studio/web/modules/operation-capabilities.js");
  const permissions = { video_enabled: true, top_level: "auto", replies: "review", private_poll: false,
    private_messages: "auto", dynamics: "review", video_like: "off", video_fav: "off" };
  const off = operationCapabilities({ policy: { enabled: false }, state: "off", permissions });
  assert.equal(off.length, 8);
  assert.ok(off.every(item => item.color === "gray"));
  assert.equal(off.find(item => item.id === "videos").state, "待启动");
  assert.equal(off.find(item => item.id === "message_poll").state, "关闭");
  const active = Object.fromEntries(operationCapabilities({ policy: { enabled: true }, state: "running", permissions })
    .map(item => [item.id, item]));
  for (const key of ["videos", "top_level", "message_reply"]) assert.equal(active[key].color, "green");
  for (const key of ["comments", "dynamics"]) assert.equal(active[key].color, "yellow");
  for (const key of ["message_poll", "like", "favorite"]) assert.equal(active[key].color, "gray");
});

test("capability blockers use reported parent state and do not turn historical errors into channel failures", async t => {
  const byId = setupDOM(t);
  const { operationCapabilities, renderOperationCapabilities } = await import("../src/tuber_studio/web/modules/operation-capabilities.js");
  const status = { policy: { enabled: true }, state: "blocked", permissions: { video_enabled: true, private_poll: false, private_messages: "review" },
    blockers: ["账号身份核验失败：本地测试原因"] };
  const rows = operationCapabilities(status);
  assert.equal(rows.find(item => item.id === "videos").color, "red");
  assert.equal(rows.find(item => item.id === "message_poll").color, "gray");
  assert.equal(rows.find(item => item.id === "message_reply").detail, status.blockers[0]);
  renderOperationCapabilities(status);
  assert.match(byId("op-capability-blocker").textContent, /账号身份核验失败/);
  assert.equal(byId("op-permissions-summary").children[0].attributes.role, "listitem");
  assert.match(byId("op-permissions-summary").children[0].attributes["aria-label"], /视频，受阻/);
  assert.ok(operationCapabilities({ ...status, state: "running", blockers: [], last_error: "上轮旧错误" }).every(item => item.color !== "red"));
});

test("stopping parent state is consistent in the main label and capability strip", async t => {
  const byId = setupDOM(t);
  const { renderOperationsQuickBar } = await import("../src/tuber_studio/web/modules/operations.js");
  renderOperationsQuickBar({ policy: { enabled: true }, state: "stopping", permissions: { video_enabled: true } });
  assert.equal(byId("op-home-state").textContent, "正在停止自主运营");
  assert.equal(byId("discovery-top-state").textContent, "正在停止自主运营");
  assert.match(byId("op-permissions-summary").children[0].textContent, /停止中/);
});

test("parent start prevents duplicate submissions and releases its button after the saved state arrives", async t => {
  const byId = setupDOM(t);
  const { initOperationsModule, updatePermissionCapsule } = await import("../src/tuber_studio/web/modules/operations.js");
  const status = { policy: { enabled: false }, settings: {}, permissions: {} };
  let finish, writes = 0;
  const api = async (url, options) => {
    if (options) {
      writes++;
      await new Promise(resolve => { finish = resolve; });
      status.policy.enabled = options.body.enabled;
    }
    return status;
  };
  initOperationsModule(api, null);
  await updatePermissionCapsule(api, null);
  const button = byId("op-toggle-auto");
  const first = button.onclick(), duplicate = button.onclick();
  assert.equal(writes, 1);
  assert.equal(button.disabled, true);
  finish();
  await Promise.all([first, duplicate]);
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, "停止自主运营");
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
