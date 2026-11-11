"use strict";

const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function dom(t) {
  const elements = new Map(), listeners = {};
  const make = () => ({ value: "", disabled: false, hidden: false, children: [], listeners: {}, dataset: {}, attributes: {},
    classList: { toggle() {}, add() {}, remove() {} },
    addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); },
    append(...children) { this.children.push(...children); }, appendChild(child) { this.children.push(child); },
    replaceChildren(...children) { this.children = children; },
    querySelector(selector) { return get(selector); }, querySelectorAll() { return []; },
    getAttribute(name) { return this.attributes[name]; }, setAttribute(name, value) { this.attributes[name] = value; },
  });
  const get = id => { if (!elements.has(id)) elements.set(id, make()); return elements.get(id); };
  const document = { getElementById: get, createElement: make, querySelector: get, querySelectorAll: () => [],
    addEventListener(type, handler) { (listeners[type] ||= []).push(handler); } };
  globalThis.document = document;
  t.after(() => { delete globalThis.document; });
  return { get, make, document, listeners };
}

function classic(filename, expression, document) {
  const source = fs.readFileSync(path.join(__dirname, "../src/tuber_studio/web", filename), "utf8");
  return vm.runInNewContext(`${source}\n;${expression}`, { window: {}, document, confirm: () => true });
}

test("continuation displays zero token and call caps as unlimited without changing recovery count", t => {
  const { get, document } = dom(t);
  const View = classic("continuation.js", "OperationContinuationView", document);
  const view = Object.create(View.prototype);
  Object.assign(view, { root: get("root"), $: get, signature: "", element: () => get("element") });
  view.render({ id: "one", state: "checkpoint", recoveries: 0, max_recoveries: 0,
    budget: { model_calls: 65, input_tokens: 187468, output_tokens: 8363 },
    limits: { max_model_calls: 0, max_input_tokens: 0, max_output_tokens: 16384 }, revisions: [] },
  { channel: "comments", localId: "one" });
  assert.equal(get("[data-continuation-budget]").textContent, "调用 65 / 不限 · 输入 tokens 187468 / 不限 · 输出 tokens 8363 / 16384");
  assert.match(get("[data-continuation-summary]").textContent, /恢复 0 \/ 0 次/);
});

test("terminal generation states show audit outcomes and require backend permission for explicit regeneration", async t => {
  const { get, document } = dom(t);
  const View = classic("continuation.js", "OperationContinuationView", document);
  const view = Object.create(View.prototype);
  Object.assign(view, { root: get("root"), $: get, element: () => get("element") });
  const formatters = require("../src/tuber_studio/web/modules/formatters.js");
  const { label } = await import("../src/tuber_studio/web/modules/workspace-display.js");
  for (const state of ["invalid", "awaiting_review", "deferred"]) {
    assert.notEqual(formatters.humanizeStatus(state).label, state);
    assert.notEqual(label(state), state);
    const value = { id: state, phase: "generation", state, can_resume: false, revisions: [],
      resume_target: { channel: "comments", local_id: "generation:one" } };
    view.render(value, { channel: "comments", localId: 1 });
    assert.equal(get("[data-continuation-resume]").hidden, true);
    assert.doesNotMatch(get("[data-continuation-summary]").textContent, /恢复|累计/);
    assert.doesNotMatch(get("[data-continuation-note]").textContent, /Agent 正在|下一轮继续/);
    view.render({ ...value, can_resume: true }, { channel: "comments", localId: 1 });
    assert.equal(get("[data-continuation-resume]").hidden, false);
    assert.equal(get("[data-continuation-resume]").disabled, false);
    assert.equal(get("[data-continuation-resume]").textContent, "重新生成");
  }
  assert.match(formatters.humanizeTaskReason({ status: "invalid" }), /未形成有效完整候选/);
  assert.match(formatters.humanizeTaskReason({ status: "awaiting_review" }), /等待人工审核/);
});

test("record refresh reads record collections while parent status stays with the workspace coordinator", async t => {
  const { document } = dom(t);
  const Panel = classic("automation.js", "AutomationPanel", document);
  const panel = Object.create(Panel.prototype), calls = [];
  Object.assign(panel, { root: { closest: () => null }, roundId: "one", tasks: [], rounds: [], intents: [], inbox: [],
    api: async url => { calls.push(url); return url.includes("intents") ? [{kind:"reply_public"},{kind:"video_like"}] : []; },
    workbench: { updateData(data) { assert.equal(data.intents.length, 1); } } });
  await panel.refresh();
  assert.equal(calls.length, 4);
  assert.ok(!calls.includes("/api/automation"));
  assert.ok(calls.includes("/api/automation/tasks?round_id=one"));
});

test("interactive controls bind once and preserve zero thresholds and unsaved rules", async t => {
  const { get, document, listeners } = dom(t);
  const UI = classic("interactive-games.js", "window.InteractiveGamesUI", document);
  const writes = [];
  const rules = { song_request: { mode: "free", min_coin: 0, gift_names: [] },
    draw_and_guess: { enabled: true, trigger_mode: "gift_only", min_coin: 0, round_duration_sec: 30, gift_names: [] } };
  const ui = new UI({ api: async (url, options) => {
    if (options) { writes.push([url, options.body]); return options.body; }
    if (url === "/api/interactive-rules") return rules;
    return { active: false };
  } });
  ui.bindEvents();
  assert.equal(listeners.click.length, 1);
  await ui.fetchRules();
  assert.equal(get("rule-dg-coin").value, 0);
  get("rule-dg-coin").value = "9";
  get("interactive-rules-form").listeners.input[0]();
  await ui.fetchRules();
  assert.equal(get("rule-dg-coin").value, "9");
  get("rule-dg-coin").value = "0";
  await ui.saveRulesFromForm();
  assert.equal(writes[0][0], "/api/interactive-rules");
  assert.equal(writes[0][1].draw_and_guess.min_coin, 0);
  assert.equal(ui.rulesDirty, false);
  await ui.startDrawGuess();
  assert.equal(writes[1][1].duration_sec, 30);
  let selected;
  ui.switchGame = async id => { selected = id; };
  const button = { id: "", disabled: false, dataset: { gameSwitch: "pvz" }, closest: query => query === "[data-game-switch]" ? button : null };
  await listeners.click[0]({ target: button });
  assert.equal(selected, "pvz");
});

test("daily memory extraction reloads results before releasing its busy state", async t => {
  const { get, document } = dom(t);
  const UI = classic("daily-memory.js", "DailyMemoryUI", document);
  const calls = [];
  const ui = new UI({ container: get("memory"), notify() {}, api: async (url, options) => {
    calls.push(url);
    return options ? { total: 1 } : { highlights: [{ id: 1, headline: "新趣事" }], active_anecdotes: [] };
  } });
  let rendered = 0;
  ui.render = () => { rendered++; };
  await ui.extract();
  assert.deepEqual(calls, ["/api/daily-memory/extract", "/api/daily-memory"]);
  assert.equal(ui.data[0].headline, "新趣事");
  assert.equal(rendered, 1);
  assert.equal(ui.busy, false);
});

test("saving text model settings leaves independent vision configuration untouched", async t => {
  const { get } = dom(t);
  const { readProviderPayload } = await import("../src/tuber_studio/web/modules/providers.js");
  const values = { "llm-kind": "llama_cpp", "llm-base-url": "http://node:9012/llm", "llm-model": "Dafeiyu-DoRA",
    "llm-context-tokens": "131072", "llm-native-tokens": "262144", "llm-operations-tokens": "8192" };
  for (const [id, value] of Object.entries(values)) get(id).value = value;
  const payload = readProviderPayload();
  assert.equal(payload.llm.model, "Dafeiyu-DoRA");
  assert.equal(Object.hasOwn(payload, "vision"), false);
});

test("main and standalone live controls share session, feature and mode-transition rules", async () => {
  const { liveActionDisabled, inferenceModeChanging } = await import("../src/tuber_studio/web/modules/live-controls.js");
  assert.equal(liveActionDisabled("start", { state: "stopped" }, { enabled: false }), true);
  assert.equal(liveActionDisabled("stop", { state: "live" }, { enabled: false }), false);
  assert.equal(liveActionDisabled("start", { state: "stopped" }, { enabled: true }), false);
  assert.equal(liveActionDisabled("pause", { state: "stopped" }, { enabled: true }), true);
  assert.equal(liveActionDisabled("resume", { state: "paused" }, { enabled: true }), false);
  assert.equal(inferenceModeChanging({ mode: "text", operation: { mode: "text" } }), false);
  assert.equal(inferenceModeChanging({ mode: "text", operation: { mode: "live" } }), true);
});

test("node pairing preserves newer credential edits made while its request is pending", async t => {
  const { get, document } = dom(t);
  const Panel = classic("inference-node.js", "InferenceNodePanel", document);
  let complete;
  const panel = new Panel(get("node"), async () => new Promise(resolve => { complete = resolve; }), async () => {});
  get("[data-url]").value = "http://old-node:9012";
  get("[data-token]").value = "first-value";
  get("[data-pair]").onsubmit({ preventDefault() {} });
  get("[data-token]").value = "second-value";
  get("[data-token]").oninput();
  complete({ paired: true, base_url: "http://old-node:9012", capabilities: {} });
  for (let i = 0; panel.busy && i < 10; i++) await new Promise(resolve => setImmediate(resolve));
  assert.equal(panel.busy, false);
  assert.equal(get("[data-token]").value, "second-value");
  assert.equal(panel.dirty, true);
});

test("an older character request cannot replace the last selected character", async t => {
  const { get, make } = dom(t);
  const { selectCharacter } = await import("../src/tuber_studio/web/modules/character-studio.js");
  const model = { characters: [] };
  let finishOld;
  const dependencies = { list: value => value, node: (_, text) => Object.assign(make(), { textContent: text }),
    api: async url => url.includes("first") ? new Promise(resolve => { finishOld = resolve; }) : [{ id: "second", name: "第二角色", version: 1 }] };
  const first = selectCharacter("first", null, model, dependencies);
  await selectCharacter("second", null, model, dependencies);
  finishOld([{ id: "first", name: "第一角色", version: 1 }]);
  await first;
  assert.equal(model.characterId, "second");
  assert.equal(get("editor-title").textContent, "第二角色 · v1");
});

test("canceling a character version switch retains both selection and unsaved edits", async t => {
  const { get } = dom(t);
  const { initCharacterStudioUI } = await import("../src/tuber_studio/web/modules/character-studio.js");
  const current = { id: "whale", version: 2 };
  const model = { character: current, characterDirty: true, versions: [{ id: "whale", version: 1 }, current] };
  globalThis.confirm = () => false;
  t.after(() => { delete globalThis.confirm; });
  initCharacterStudioUI(model, {});
  get("character-version").value = "1";
  get("oc-background").value = "还未保存的背景";
  get("character-editor").value = "未保存的 JSON";
  get("character-version").listeners.change[0]();
  assert.equal(get("character-version").value, "2");
  assert.equal(get("oc-background").value, "还未保存的背景");
  assert.equal(get("character-editor").value, "未保存的 JSON");
  assert.equal(model.character, current);
  assert.equal(model.characterDirty, true);
});

test("retention settings save only days and preserve null for never clean", async t => {
  const { get } = dom(t);
  const { initMaintenanceModule } = await import("../src/tuber_studio/web/modules/maintenance.js");
  const model = { retention: { days: 30 }, retentionDirty: true };
  const writes = [];
  initMaintenanceModule(model, {
    api: async (url, options) => {
      assert.equal(url, "/api/retention");
      if (options) writes.push(options.body);
      return { days: null, enabled: false };
    },
    notice() {}, describe() {}, dateTime() {},
  });
  get("retention-never").checked = true;
  get("retention-form").listeners.submit[0]({ preventDefault() {} });
  for (let i = 0; model.retentionPending && i < 10; i++) await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(writes, [{ days: null }]);
  assert.equal(model.retentionDirty, false);
  assert.equal(model.retentionPending, null);
});

test("private draft switches preserve edits by account and draft, while completed actions clear only their target", async t => {
  const { get } = dom(t);
  const { MessagesPanel } = await import("../src/tuber_studio/web/messages.js");
  const panel = Object.create(MessagesPanel.prototype);
  Object.assign(panel, { value: { account_id: "900" }, selected: 1, dirty: true, edits: new Map(), $: get,
    render() {}, closeImagePicker() {} });
  get("[data-text]").value = "第一条未保存私信";
  panel.selectDraft(2);
  get("[data-text]").value = "第二条未保存私信";
  panel.dirty = true;
  panel.selectDraft(1);
  assert.equal(get("[data-text]").value, "第一条未保存私信");
  assert.equal(panel.dirty, true);
  panel.applyDraftResult({ status: "published" }, 2);
  assert.equal(panel.selected, 1);
  assert.equal(panel.dirty, true);
  panel.applyDraftResult({ status: "rejected" }, 1);
  assert.equal(panel.selected, null);
  assert.equal(panel.dirty, false);
  assert.equal(panel.edits.size, 0);
});

test("an earlier private conversation response cannot overwrite a newer request for the same user", async t => {
  const { get } = dom(t);
  const { MessagesPanel } = await import("../src/tuber_studio/web/messages.js");
  const finish = [];
  const panel = Object.create(MessagesPanel.prototype);
  Object.assign(panel, { value: { account_id: "900" }, user: "bilibili|901", conversationRequest: 0, $: get,
    api: () => new Promise(resolve => finish.push(resolve)) });
  const old = panel.conversation(), latest = panel.conversation();
  const response = content => ({ account_id: "900", relationship: {}, drafts: [], messages: [{ id: 1, content, sent_at: 0 }] });
  finish[1](response("新响应"));
  await latest;
  finish[0](response("迟到旧响应"));
  await old;
  assert.equal(get("[data-inbox]").children[0].children[1].textContent, "新响应");
});

test("an inbox link selects only the exact message in the current account and character", async t => {
  const { get, make } = dom(t);
  const { MessagesPanel } = await import("../src/tuber_studio/web/messages.js");
  const finish = [], paths = [];
  const panel = Object.create(MessagesPanel.prototype);
  let focused;
  Object.assign(panel, { value: { account_id: "900", character_id: "whale", users: [] }, user: null,
    inboxId: "7", conversationRequest: 0, root: { closest: () => null }, $: get, render() {},
    element(tag, text) { return { ...make(), textContent: text, focus() { focused = this; }, scrollIntoView() {} }; },
    api: path => { paths.push(path); return new Promise(resolve => finish.push(resolve)); } });
  const earlier = panel.conversation();
  panel.inboxId = "8";
  const latest = panel.conversation();
  const response = (id, uid, character = "whale") => ({ account_id: "900", character_id: character,
    relationship: {}, drafts: [], messages: [{ id, platform: "bilibili", platform_user_id: uid, content: `来信 ${id}`, sent_at: 0 }] });
  finish[1](response(8, "902"));
  await latest;
  finish[0](response(7, "901"));
  await earlier;
  assert.deepEqual(paths, ["/api/messages/inbox/7/conversation", "/api/messages/inbox/8/conversation"]);
  assert.equal(panel.user, "bilibili|902");
  assert.equal(focused.dataset.messageId, "8");
  assert.equal(focused.getAttribute("aria-current"), "true");
  assert.equal(get("[data-inbox]").children[0].children[1].textContent, "来信 8");
  panel.value = { account_id: "900", character_id: "other-oc", users: [] };
  const switched = panel.conversation();
  finish[2](response(8, "902"));
  await switched;
  assert.equal(panel.user, null);
  assert.equal(get("[data-inbox]").children.length, 0);
  assert.match(get("[data-conversation-state]").textContent, /来信无法定位/);
});

test("private background refresh clears externally completed selection but preserves unsaved local text", async t => {
  const { get } = dom(t);
  const { MessagesPanel } = await import("../src/tuber_studio/web/messages.js");
  for (const status of ["published", "not_initiated"]) for (const dirty of [false, true]) {
    const panel = Object.create(MessagesPanel.prototype);
    const value = { account_id: "900", policy: { enabled: true, credentials_configured: true }, drafts: [{ id: 1, status, text: "服务器记录正文" }] };
    let calls = 0;
    Object.assign(panel, { value: { ...value, drafts: [{ ...value.drafts[0], status: "approved" }] }, selected: 1, dirty,
      draftView: "pending", refreshRequest: 0, edits: new Map(), $: get, memeCache: new Map(), continuation: { render() {} },
      root: { closest: () => null, querySelectorAll: () => [] },
      api: async () => { calls++; return value; }, render() { this.renderEditor(); }, async conversation() {}, closeImagePicker() {} });
    get("[data-text]").value = dirty ? "本地未保存私信" : "服务器记录正文";
    await panel.refresh({ background: true });
    assert.equal(panel.selected, dirty ? 1 : null);
    assert.equal(get("[data-text]").value, dirty ? "本地未保存私信" : "");
    if (dirty) {
      assert.equal(get("[data-text]").readOnly, true);
      assert.equal(get("[data-text]").disabled, false);
    }
    await panel.refresh({ background: true });
    assert.equal(calls, 1);
  }
});

test("private waiting and generation states show progress without review or send actions", async t => {
  const { get, make } = dom(t);
  const { MessagesPanel } = await import("../src/tuber_studio/web/messages.js");
  const panel = Object.create(MessagesPanel.prototype), review = [make(), make(), make()];
  Object.assign(panel, { selected: 1, dirty: false, draftView: "pending", $: get, memeCache: new Map(),
    continuation: { render() {} }, root: { querySelectorAll: query => query === '[data-review]' ? review : [] } });
  const cases = [
    ["waiting", "awaiting_content", "等待符合联系目的的正文"],
    ["waiting", "awaiting_completion", "等待约定完成"],
    ["waiting", "awaiting_response", "等待对方回应"],
    ["waiting", "awaiting_invitation", "等待新的邀请或约定"],
    ["waiting", "check_pending", "首发联系核对中"],
    ["waiting", "reply_received", "已收到来信，转入普通回复"],
    ["queued", "awaiting_completion", "等待生成"],
    ["generating", "awaiting_response", "生成中"],
  ];
  for (const [status, contact_state, expected] of cases) {
    const row = { id: 1, status, contact_state, purpose: "完成楼内约定", type: "outreach" };
    panel.value = { policy: { enabled: true, credentials_configured: true }, drafts: [row] };
    panel.renderEditor();
    assert.ok(get('[data-draft-status]').textContent.startsWith(expected));
    assert.equal(get('[data-text]').hidden, true);
    assert.equal(get('[data-send]').hidden, true);
    assert.equal(get('[data-send]').disabled, true);
    assert.ok(review.every(button => button.hidden && button.disabled));
    const canContinue=status === 'waiting' && contact_state === 'awaiting_completion';
    assert.equal(get('[data-retry]').hidden, !canContinue);
    if (canContinue) assert.equal(get('[data-retry]').textContent, '继续完成约定');
  }
  assert.equal(panel.draftState({ status: "not_initiated" }).history, true);
  assert.equal(panel.draftState({ status: "waiting", contact_state: "awaiting_completion" }).history, false);
});
