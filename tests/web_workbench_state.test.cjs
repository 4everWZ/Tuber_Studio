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
