"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");

test("private contact labels preserve actual direction, waiting state and free-text reasons", async () => {
  const { contactLabel, contactReason, contactStatus } = await import("../src/tuber_studio/web/modules/message-labels.js");
  assert.equal(contactLabel({ direction: "incoming" }), "收到私信");
  assert.equal(contactLabel({ contact_type: "outreach", source_kind: "comment" }), "主动私信");
  assert.equal(contactLabel({ contact_type: "reply", source_kind: "message" }), "回复来信");
  assert.equal(contactLabel({ direction: "outgoing" }), "私信草稿");
  assert.equal(contactStatus({ status: "not_initiated" }), "未发起私信");
  assert.equal(contactStatus({ status: "waiting", outreach_status: "awaiting_completion" }), "等待约定完成");
  assert.equal(contactStatus({ status: "waiting", contact_state: "awaiting_response" }), "等待对方回应");
  assert.equal(contactReason("long_followup"), "楼内话题后续交流");
  assert.equal(contactReason("user_requested"), "对方请求私下交流");
  assert.equal(contactReason("对方明确邀请讨论白饭"), "对方明确邀请讨论白饭");
});

test("stopped parent displays saved automatic private-message preferences separately", async () => {
  const { MessagesPanel } = await import("../src/tuber_studio/web/messages.js");
  const panel = Object.create(MessagesPanel.prototype);
  const policy = { automation_override: false, auto_poll: false, auto_send: false,
    base: { auto_poll: true, auto_send: true } };
  assert.equal(panel.policyStatus(policy), '自主运营已停止；已配置自动读取 · 自动回复');
  assert.equal(panel.policyStatus({ ...policy, emergency: true }), '紧急停止；已配置自动读取 · 自动回复');
  assert.equal(panel.policyStatus({ ...policy, automation_override: true, auto_poll: true, auto_send: true }), '自动读取 · 自动回复');
  assert.equal(panel.policyStatus({ automation_override: false, base: { auto_poll: false, auto_send: false } }), '自主运营已停止；已配置手动读取 · 审核后发送');
});

test("private message provenance follows the current draft and clears on deselect", async () => {
  const { MessagesPanel } = await import("../src/tuber_studio/web/messages.js");
  const nodes = new Map();
  const make = (tag, text) => ({ tag, textContent: text || "", dataset: {}, children: [],
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; },
    querySelector(tag) { return this.children.find(child => child.tag === tag); },
  });
  nodes.set('[data-source]', { after(node) { nodes.set('[data-trigger]', node); } });
  const panel = Object.create(MessagesPanel.prototype);
  panel.$ = selector => nodes.get(selector);
  panel.element = make;
  const comment = { type: "outreach", purpose: "履行楼内约定", trigger_source: {
    kind: "comment", bvid: "BV13S4i6fE82", root: "323202459488", comment_id: "323231188736" },
    outreach_check: { assessment: { reason: "对方有明确邀请", invitation_quote: "来私信说" } } };
  panel.renderTrigger(comment);
  const box = nodes.get('[data-trigger]'), body = box.querySelector('div');
  assert.equal(box.hidden, false);
  assert.equal(body.children[0].textContent, "主动私信 · 履行楼内约定");
  assert.equal(body.children[1].href, "https://www.bilibili.com/video/BV13S4i6fE82#reply323231188736");
  assert.match(body.children.at(-1).textContent, /来私信说/);
  panel.renderTrigger({ type: "reply", purpose: "回答新来信", trigger_source: {
    kind: "message", ids: [7, 8], message_id: "actual-platform-message" } });
  assert.equal(body.children.length, 2);
  assert.match(body.children[0].textContent, /^回复来信/);
  assert.match(body.children[1].textContent, /7、8.*actual-platform-message/);
  assert.ok(body.children.every(child => !child.href));
  panel.renderTrigger(null);
  assert.equal(box.hidden, true);
  assert.deepEqual(body.children, []);
  assert.equal(panel.label("waiting"), "等待约定或对方回应");
  assert.equal(panel.label("not_initiated"), "未发起私信");
});
