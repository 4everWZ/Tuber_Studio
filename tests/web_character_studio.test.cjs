"use strict";

const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");

test("OC tabs switch panels and visual edits preserve the rest of the saved persona", async t => {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      id, value: "", hidden: false, dataset: {}, listeners: {}, attributes: {},
      classList: { toggle() {} },
      addEventListener(type, handler) { this.listeners[type] = handler; },
      setAttribute(name, value) { this.attributes[name] = value; },
    });
    return elements.get(id);
  };
  const names = ["persona", "style", "assets", "json"];
  const tabs = names.map(name => Object.assign(element(`oc-tab-${name}`), { dataset: { tab: `tab-oc-${name}` } }));
  const panels = names.map(name => element(`tab-oc-${name}`));
  globalThis.document = {
    getElementById: element,
    querySelectorAll: selector => selector === ".oc-tab-btn" ? tabs : panels,
  };
  t.after(() => { delete globalThis.document; });
  const studio = await import("../src/tuber_studio/web/modules/character-studio.js");
  const character = JSON.parse(fs.readFileSync(path.join(__dirname, "../config/characters.json"), "utf8"))[0];
  const model = { character };
  element("character-editor").value = JSON.stringify(character);
  studio.syncCharacterJsonToVisual(character);
  studio.initCharacterStudioUI(model, {});

  for (const tab of tabs) {
    tab.listeners.click();
    assert.deepEqual(panels.filter(panel => !panel.hidden).map(panel => panel.id), [tab.dataset.tab]);
    assert.equal(tab.attributes["aria-selected"], "true");
  }
  element("oc-background").value = "新背景";
  element("oc-background").listeners.input();
  const saved = JSON.parse(element("character-editor").value);
  assert.equal(saved.core.background, "新背景");
  assert.deepEqual(saved.core.traits, character.core.traits);
  assert.deepEqual(saved.style, character.style);
  assert.deepEqual(saved.core.variants, character.core.variants);
});
