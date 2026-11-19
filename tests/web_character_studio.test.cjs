"use strict";

const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");

function dom(t) {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      id, value: "", hidden: false, dataset: {}, listeners: {}, attributes: {}, children: new Map(),
      classList: { toggle() {} },
      addEventListener(type, handler) { this.listeners[type] = handler; },
      setAttribute(name, value) { this.attributes[name] = String(value); },
      getAttribute(name) { return this.attributes[name] ?? null; },
      removeAttribute(name) { delete this.attributes[name]; },
      get src() { return this.getAttribute("src") || ""; }, set src(value) { this.setAttribute("src", value); },
      get href() { return this.getAttribute("href") || ""; }, set href(value) { this.setAttribute("href", value); },
      querySelector(selector) { return this.children.get(selector) ?? null; },
    });
    return elements.get(id);
  };
  element("oc-owner-avatar").children.set("img", element("oc-owner-image"));
  const names = ["persona", "style", "assets", "json"];
  const tabs = names.map(name => Object.assign(element(`oc-tab-${name}`), { dataset: { tab: `tab-oc-${name}` } }));
  const panels = names.map(name => element(`tab-oc-${name}`));
  globalThis.document = {
    getElementById: element,
    querySelectorAll: selector => selector === ".oc-tab-btn" ? tabs : panels,
  };
  t.after(() => { delete globalThis.document; });
  return { element, tabs, panels };
}

test("OC tabs switch panels and visual edits preserve the rest of the saved persona", async t => {
  const { element, tabs, panels } = dom(t);
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
  element("oc-name").value = "新名字";
  element("oc-name").listeners.input();
  assert.equal(JSON.parse(element("character-editor").value).name, "新名字");
  assert.equal(element("activate-character").textContent, "保存并激活");
});


test("owner lookup binds the verified profile and clearing removes its links and avatar", async t => {
  const { element } = dom(t);
  const studio = await import("../src/tuber_studio/web/modules/character-studio.js");
  const character = { id: "whale", name: "大肥鱼", core: {}, style: {} };
  const model = { character, characterSelection: 1 };
  const owner = { uid: "123", name: "号主昵称", avatar_url: "https://i0.hdslb.com/bfs/face/example.jpg" };
  const input = element("oc-bilibili-owner"), profile = element("oc-owner-profile");
  const avatar = element("oc-owner-avatar"), image = avatar.querySelector("img");
  element("character-editor").value = JSON.stringify(character);
  studio.syncCharacterJsonToVisual(character);
  studio.initCharacterStudioUI(model, { api: async url => {
    assert.equal(url, "/api/characters/whale/bilibili-owner?account=https%3A%2F%2Fspace.bilibili.com%2F123");
    return owner;
  } });
  assert.equal(profile.hidden, true);
  assert.equal(avatar.hidden, true);
  input.value = "https://space.bilibili.com/123";
  input.listeners.input();
  await element("oc-owner-lookup").listeners.click();
  assert.deepEqual(JSON.parse(element("character-editor").value).bilibili_owner, owner);
  assert.equal(input.value, owner.uid);
  assert.equal(profile.textContent, "号主昵称 · UID 123");
  assert.equal(profile.href, "https://space.bilibili.com/123");
  assert.equal(profile.hidden, false);
  assert.equal(avatar.hidden, false);
  assert.equal(image.src, owner.avatar_url);
  assert.equal(model.characterDirty, true);

  input.setAttribute("aria-invalid", "true");
  input.value = "";
  input.listeners.input();
  assert.equal(JSON.parse(element("character-editor").value).bilibili_owner, null);
  assert.equal(profile.hidden, true);
  assert.equal(profile.getAttribute("href"), null);
  assert.equal(avatar.hidden, true);
  assert.equal(image.getAttribute("src"), null);
  assert.equal(input.getAttribute("aria-invalid"), null);
  assert.equal(element("oc-owner-status").textContent, "");
});

test("editing and clearing a resident field preserves the complete legacy card", async t => {
  const { element } = dom(t);
  const studio = await import("../src/tuber_studio/web/modules/character-studio.js");
  const card = {
    id: "old", name: "自定义", core: {identity: "旧身份".repeat(120), background: "完整的旧世界观"},
    style: {instructions: ["说话轻柔", "遇到难题时先听对方说完"]}, lore: [{text: "旧设定"}],
  };
  const model = {character: card};
  studio.setCharacterEditor(card, model);
  studio.initCharacterStudioUI(model, {});
  const self = element("oc-self-description"), style = element("oc-speaking-style");
  assert.equal(self.value, "");
  assert.equal(style.value, "");
  assert.equal(element("oc-identity").value, card.core.identity);
  assert.equal(element("oc-instructions").value, card.style.instructions.join("\n"));
  self.value = "我是图书管理员。";
  self.listeners.input();
  style.value = "说话简洁。";
  style.listeners.input();
  const updated = {...card, self_description: self.value, speaking_style: style.value};
  assert.deepEqual(JSON.parse(element("character-editor").value), updated);
  element("character-editor").value = JSON.stringify({...updated, speaking_style: "温和"});
  element("character-editor").listeners.input();
  assert.equal(style.value, "温和");
  self.value = "";
  self.listeners.input();
  assert.deepEqual(JSON.parse(element("character-editor").value), {...card, speaking_style: "温和"});
});
