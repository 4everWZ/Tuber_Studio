"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");

test("sidebar contains four workspaces and keeps existing features in local navigation", () => {
  const html = fs.readFileSync(path.join(__dirname, "../src/tuber_studio/web/index.html"), "utf8");
  const sidebar = html.match(/<nav class="explorer-nav"[\s\S]*?<\/nav>/)[0];
  assert.deepEqual([...sidebar.matchAll(/data-parent-page="([^"]+)"/g)].map(match => match[1]), ["operations", "live", "characters", "settings"]);
  assert.equal([...sidebar.matchAll(/<a /g)].length, 4);
  for (const route of ["live-scene", "live-music", "live-interaction", "characters", "voices", "assets", "settings", "accounts", "overview", "issues"]) {
    assert.ok(html.includes(`data-workspace-tab="${route}"`), `existing ${route} is reachable within its workspace`);
  }
  assert.match(html, /href="#issues" id="workspace-issues"/);
  for (const page of ["live", "live-scene", "live-music", "live-interaction", "characters", "voices", "assets", "settings", "accounts", "overview"]) {
    const content = fs.readFileSync(path.join(__dirname, `../src/tuber_studio/web/pages/${page}.html`), "utf8");
    assert.ok(!content.includes('<div class="win-property-tabs">'), `${page} must not duplicate the shared workspace tabs`);
  }
});

test("deep links select their parent workspace and only its local tabs", async t => {
  const elements = new Map();
  const make = dataset => ({ dataset, hidden: false, attributes: {}, classList: { toggle() {}, remove() {} },
    getAttribute(key) { return this.attributes[key]; }, setAttribute(key, value) { this.attributes[key] = value; },
    removeAttribute(key) { delete this.attributes[key]; }, hasAttribute() { return false; }, querySelector() { return null; } });
  const get = id => { if (!elements.has(id)) elements.set(id, make({})); return elements.get(id); };
  const groups = ["operations", "live", "characters", "settings"];
  const parents = groups.map(parentPage => make({ parentPage }));
  const navs = groups.map(workspaceNavigation => make({ workspaceNavigation }));
  const routes = ["automation", "comments", "automation-records", "live", "live-scene", "live-music", "live-interaction", "characters", "voices", "assets", "settings", "accounts", "overview", "issues"];
  const sections = routes.map(section => make({ section }));
  const tabs = routes.map(workspaceTab => make({ workspaceTab }));
  const operations = ["overview", "comments"].map(operationsTab => make({ operationsTab }));
  const main = make({});
  globalThis.document = { getElementById: get, addEventListener() {}, querySelector: () => main,
    querySelectorAll: selector => ({ "[data-section]": sections, "[data-page]": parents,
      "[data-workspace-navigation]": navs, "[data-workspace-tab]": tabs, "[data-operations-tab]": operations })[selector] || [] };
  globalThis.window = { addEventListener() {}, scrollY: 0, scrollTo() {} };
  globalThis.location = { hash: "#comments" };
  globalThis.matchMedia = () => ({ addEventListener() {} });
  t.after(() => { for (const name of ["document", "window", "location", "matchMedia"]) delete globalThis[name]; });
  const { initWorkspaceNavigation } = await import("../src/tuber_studio/web/modules/workspace-navigation.js");
  const model = { page: "automation", authenticated: true }, loaded = [];
  const change = initWorkspaceNavigation(model, async args => loaded.push(args));
  for (const [hash, page, group] of [
    ["#comments/review/comments/draft", "comments", "operations"],
    ["#automation/round/round/task", "automation-records", "operations"],
    ["#live-interaction", "live-interaction", "live"], ["#voices", "voices", "characters"],
    ["#accounts", "accounts", "settings"], ["#issues", "issues", "settings"],
  ]) {
    location.hash = hash; await change();
    assert.equal(model.page, page);
    assert.deepEqual(parents.filter(link => link.attributes["aria-current"]).map(link => link.dataset.parentPage), [group]);
    assert.deepEqual(navs.filter(nav => !nav.hidden).map(nav => nav.dataset.workspaceNavigation), [group]);
    assert.deepEqual(sections.filter(section => !section.hidden).map(section => section.dataset.section), [page]);
  }
  assert.equal(loaded[0].taskId, "draft");
  assert.equal(loaded[1].routeId, "round");
  assert.equal(get("workspace-menu").getAttribute("aria-expanded"), "false");
});
