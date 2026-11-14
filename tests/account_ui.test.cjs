"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {test} = require("node:test");

const context = vm.createContext({Intl, Date, console});
vm.runInContext(fs.readFileSync(path.join(__dirname, "../src/tuber_studio/web/account.js"), "utf8") +
  "\nglobalThis.AccountUI = BilibiliAccountUI;", context);

function account() {
  const ui = Object.create(context.AccountUI.prototype), nodes = new Map();
  ui.h = selector => {
    if (!nodes.has(selector)) nodes.set(selector, {textContent: "", hidden: false, src: "", dataset: {},
      setAttribute() {}, removeAttribute() {}, classList: {toggle() {}}});
    return nodes.get(selector);
  };
  const badge = {querySelector: ui.h, dataset: {}, setAttribute() {}};
  Object.assign(ui, {settings: null, badges: [badge], active: true, pending: false, loading: false, sequence: 0,
    requestError: "", sensitiveValues: [], avatarKey: null, notify() {},
    value: {state: "logged_in", checked_at: 1700000000, configured: {sessdata: true, csrf: true},
      profile: {uid: "123", name: "大肥鱼", coins: 3.5, fans: 42, avatar_url: "https://i0.hdslb.com/bfs/x"}}});
  return ui;
}

test("management API read failure retains last profile, avatar, and verified timestamp", async () => {
  const ui = account();
  ui.render();
  const avatar = ui.h("[data-avatar]").src;
  ui.api = async () => { throw new Error("network unavailable"); };
  await ui.load();
  assert.equal(ui.h("[data-name]").textContent, "大肥鱼");
  assert.equal(ui.h("[data-uid]").textContent, "UID 123");
  assert.equal(ui.h("[data-coins]").textContent, "硬币 3.5");
  assert.equal(ui.h("[data-fans]").textContent, "粉丝 42");
  assert.equal(ui.h("[data-avatar]").src, avatar);
  assert.equal(ui.h("[data-profile-note]").hidden, false);
  assert.match(ui.h("[data-profile-note]").textContent, /上次资料.*2023/);
  assert.equal(ui.h("[data-status]").textContent, "状态读取失败");
  assert.equal(ui.h(".account-badge-name").textContent, "大肥鱼");
  assert.equal(ui.h(".account-state").textContent, "状态读取失败");
});

test("explicit refresh transport failure marks a stale profile instead of showing logged in", async () => {
  const ui = account();
  ui.api = async () => { throw new Error("refresh interrupted"); };
  await ui.refreshProfile();
  assert.equal(ui.h("[data-status]").textContent, "状态读取失败");
  assert.equal(ui.h("[data-uid]").textContent, "UID 123");
  assert.equal(ui.h("[data-profile-note]").hidden, false);
});

test("an explicit account clear removes the old identity", () => {
  const ui = account();
  ui.render();
  ui.onChange = () => {};
  ui.accept({state: "not_configured", configured: {sessdata: false, csrf: false}, profile: null});
  assert.equal(ui.h("[data-uid]").textContent, "UID —");
  assert.equal(ui.h("[data-profile-note]").hidden, true);
  assert.equal(ui.h(".account-badge-name").textContent, "B站账号");
  assert.equal(ui.h(".account-state").textContent, "未配置");
  assert.equal(ui.h("img").hidden, true);
});
