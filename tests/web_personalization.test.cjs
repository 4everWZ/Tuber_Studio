"use strict";

const assert = require("node:assert/strict");
const { test } = require("node:test");

test("a slow memory summary survives poll ticks and is cancelled on close or scope change", async t => {
  class Element {
    value = "";
    open = false;
    children = [];
    listeners = {};
    addEventListener(type, listener, options = {}) {
      (this.listeners[type] ??= []).push([listener, options.once]);
    }
    showModal() { this.open = true; }
    close() {
      this.open = false;
      const listeners = this.listeners.close || [];
      this.listeners.close = listeners.filter(([, once]) => !once);
      for (const [listener] of listeners) listener();
    }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    querySelectorAll() { return []; }
    contains() { return false; }
    closest() { return null; }
    reset() {}
    focus() {}
  }

  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  globalThis.document = { getElementById: get, createElement: () => new Element() };
  t.after(() => { delete globalThis.document; });
  const { initPersonalization } = await import("../src/tuber_studio/web/modules/personalization.js");
  const reads = [];
  const deadlines = [];
  t.mock.method(AbortSignal, "timeout", () => {
    const deadline = new AbortController();
    deadlines.push(deadline);
    return deadline.signal;
  });
  let scope = "first";
  const panel = initPersonalization({ page: "personalization" }, (path, { signal } = {}) => {
    if (path.endsWith("/preferences")) return Promise.resolve({ scope, enabled: true });
    assert.equal(path, "/api/daily-memory?limit=100");
    return new Promise((resolve, reject) => reads.push({ signal, resolve, reject }));
  });
  const result = scope => ({ scope, organization: { status: "idle" }, organized: [], highlights: [], summary: [] });
  const flush = () => new Promise(resolve => setImmediate(resolve));
  const load = get("memory-load-state");
  const opener = get("open-memory-summary");
  const dialog = get("memory-summary-dialog");

  opener.onclick({ currentTarget: opener });
  panel.refresh({ background: true });
  panel.refresh({ background: true });
  assert.equal(reads.length, 1);
  assert.equal(load.hidden, false);
  reads[0].resolve(result(scope));
  await flush();
  assert.equal(load.hidden, true);
  assert.equal(get("memory-summary").hidden, false);
  assert.equal(get("memory-summary-list").children[0].textContent, "暂无已保存的长期记忆。");

  panel.refresh({ background: true });
  scope = "second";
  panel.refreshScope();
  assert.equal(reads.length, 3);
  assert.equal(reads[1].signal.aborted, true);
  reads[1].resolve(result("first"));
  await flush();
  assert.equal(load.hidden, false);
  reads[2].resolve(result(scope));
  await flush();
  assert.equal(load.hidden, true);

  panel.refresh({ background: true });
  dialog.close();
  assert.equal(reads[3].signal.aborted, true);

  opener.onclick({ currentTarget: opener });
  deadlines[4].abort();
  assert.equal(reads[4].signal.aborted, true);
  reads[4].reject(new Error("timed out"));
  await flush();
  assert.equal(load.children[0].textContent, "读取记忆摘要超时。");
  const retry = load.children[1].onclick();
  reads[5].resolve(result(scope));
  await retry;
  assert.equal(load.hidden, true);

  dialog.close();
  panel.refresh();
  await flush();
  const statement = get("owner-memory-statement");
  statement.value = "这是尚未确认的原话";
  panel.refreshScope();
  assert.equal(statement.disabled, true);
  await flush();
  assert.equal(statement.value, "这是尚未确认的原话");
  scope = "third";
  panel.refreshScope();
  await flush();
  assert.equal(statement.value, "");
  assert.equal(statement.disabled, false);
});
