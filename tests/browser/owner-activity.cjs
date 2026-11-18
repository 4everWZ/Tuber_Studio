const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require(require.resolve("playwright", {
  paths: [__dirname, path.join(path.dirname(process.execPath), "node_modules/@playwright/cli")],
}));

const origin = process.argv[2];
assert.equal(new URL(origin).hostname, "127.0.0.1");
const initial = "先核对动态草稿 #1 的原评论出处，等我决定是否发布。";
const revised = "只发布动态草稿 #1，保留原评论出处，完成后给我回执。";
const outgoing = "今晚开场分享海底厨房的来信。\n\n原评论：https://www.bilibili.com/video/BV1d8pU6xEZX#reply42";
const artifactEdit = "开场备忘：已核对本次公开动态的原评论。";
const blocked = [];

async function owner(browser) {
  const context = await browser.newContext({
    serviceWorkers: "block", viewport: { width: 1280, height: 800 }, reducedMotion: "reduce",
  });
  await context.route("**/*", route => {
    if (new URL(route.request().url()).origin === origin) return route.continue();
    blocked.push(route.request().method());
    return route.abort();
  });
  const page = await context.newPage();
  page.setDefaultTimeout(20000);
  assert.equal((await context.cookies()).length, 0);
  await page.goto(origin);
  await page.locator("#token").fill(process.env.TUBER_ADMIN_TOKEN);
  await page.locator("#login-form button").click();
  await page.getByText("素材札记 09", { exact: true }).waitFor();
  const session = (await context.cookies()).find(cookie => cookie.name === "tuber_admin");
  assert.ok(session?.httpOnly);
  return { page, context, session: session.value };
}

function anchor(page) {
  return page.locator("#operator-chat-timeline").evaluate(el => {
    const top = el.getBoundingClientRect().top;
    const turn = [...el.querySelectorAll(".operator-chat-turn")].find(
      item => item.getBoundingClientRect().bottom > top + 1,
    );
    return {
      message: turn.querySelector(".operator-chat-user").textContent,
      id: turn.dataset.requestId, offset: turn.getBoundingClientRect().top - top,
      scroll: el.scrollTop, remaining: el.scrollHeight - el.scrollTop - el.clientHeight,
    };
  });
}

async function observation(page) {
  const response = await page.request.get(origin + "/__fixture__/observations");
  assert.equal(response.status(), 200);
  return response.json();
}

async function run(browser) {
  const a = await owner(browser);
  await a.page.locator("#operator-chat-shortcuts-toggle").click();
  await a.page.getByRole("button", { name: "交办任务", exact: true }).click();
  await a.page.locator("#operator-chat-input").fill(initial);
  await a.page.locator("#operator-chat-send").click();
  const turn = a.page.locator(".operator-chat-turn").filter({ hasText: initial });
  await turn.getByRole("button", { name: "查看任务" }).click();
  const taskId = await turn.getAttribute("data-request-id");
  const confirmation = { name: "确认公开发布这条动态", exact: true };
  const question = "已核对原评论出处。需要我提交发布吗？";
  await a.page.locator("#chat-activity").getByText(question, { exact: true }).waitFor();
  assert.equal(await a.page.getByRole("button", confirmation).count(), 0);
  await a.page.close();
  await a.context.close();

  const b = await owner(browser);
  assert.notEqual(b.session, a.session);
  const page = b.page;
  const panel = page.locator("#chat-activity");
  const restored = page.locator(`.operator-chat-turn[data-request-id="${taskId}"]`);
  assert.equal(await restored.locator(".operator-chat-user").innerText(), initial);
  await page.getByText("素材札记 09", { exact: true }).scrollIntoViewIfNeeded();
  const before = await anchor(page);
  assert.ok(before.scroll > 100 && before.remaining > 100);
  await page.locator("#chat-activity-toggle").click();
  await panel.locator(".chat-task-row").filter({ hasText: initial }).click();
  await panel.getByText(question, { exact: true }).waitFor();
  assert.equal(await panel.getByRole("button", confirmation).count(), 0);
  await panel.getByRole("button", { name: "补充要求" }).click();
  await panel.getByLabel("新的任务要求").fill(revised);
  async function patch(action, click, suffix = "") {
    const [response] = await Promise.all([
      page.waitForResponse(r => r.request().method() === "PATCH" &&
        new URL(r.url()).pathname === "/api/agent/tasks/" + taskId + suffix),
      click(),
    ]);
    assert.equal(response.status(), 200);
    if (action) assert.equal(response.request().postDataJSON().action, action);
    return response.json();
  }
  const steered = await patch("steer", () => panel.getByRole("button", { name: "继续任务" }).click());
  assert.equal(steered.revision, 2);
  assert.equal(steered.goal, revised);
  const confirm = panel.getByRole("button", confirmation);
  await confirm.waitFor();
  const pre = await observation(page);
  assert.equal(pre.requests.length, 0);
  assert.equal(pre.attempts, 0);
  assert.deepEqual(pre.posts.map(row => row.status), ["pending_review", "pending_review"]);
  const target = `B站 UID 100 的公开动态 · 草稿 #1 · ${pre.character} v${pre.version} · 含配图`;
  const preview = panel.locator("p").filter({ has: page.locator("img.dynamic-comment-card") });
  assert.equal(await preview.evaluate(el => el.firstChild.textContent), target + " · " + outgoing);
  await preview.locator("img").evaluate(img => img.decode());
  assert.equal(await preview.getByText("查看原始来源").getAttribute("href"), "https://www.bilibili.com/video/BV1d8pU6xEZX");
  const approved = await patch("approve", () => confirm.click());
  const grants = approved.steps.filter(step => step.approved);
  assert.equal(grants.length, 1);
  assert.equal(grants[0].revision, 2);
  assert.equal(grants[0].approval.post_id, 1);
  const edit = panel.getByRole("button", { name: "编辑成果" });
  await edit.click();
  const body = panel.getByLabel("私有成果正文");
  const original = await body.inputValue();
  assert.ok(original.includes("平台已确认该动态发布"));
  await body.fill(original + "\n\n" + artifactEdit);
  const updated = await patch(null, () => panel.getByRole("button", { name: "保存", exact: true }).click(), "/artifact");
  assert.equal(updated.artifact.edition, 2);
  await panel.getByText(artifactEdit, { exact: true }).waitFor();
  await page.locator("#chat-activity-close").click();
  await panel.waitFor({ state: "hidden" });
  assert.equal(await page.evaluate(() => document.activeElement.id), "chat-activity-toggle");
  const after = await anchor(page);
  assert.equal(after.id, before.id);
  assert.ok(Math.abs(after.offset - before.offset) <= 1 && Math.abs(after.scroll - before.scroll) <= 1);

  await page.reload();
  await restored.getByRole("button", { name: "查看任务" }).click();
  await panel.getByText(artifactEdit, { exact: true }).waitFor();
  await panel.getByText("任务 r2 · 成果 v2", { exact: true }).waitFor();
  const final = await observation(page);
  assert.deepEqual(final.requests.map(r => r.path), ["/x/dynamic/feed/draw/upload_bfs", "/x/dynamic/feed/create/dyn"]);
  assert.ok(final.requests.every(r => r.host === "platform.example.test" && r.uid === "100"));
  assert.equal(final.requests[1].text, outgoing);
  assert.equal(final.attempts, 1);
  assert.deepEqual(final.posts.map(row => row.status), ["published", "pending_review"]);
  assert.equal(JSON.parse(final.posts[0].receipt).dynamic_id, "99001");
  assert.ok(!final.posts[1].receipt);
  assert.deepEqual(final.denied_connections, []);
  assert.deepEqual(blocked, []);
  console.log(JSON.stringify({ chromium: browser.version(), taskId, revision: steered.revision,
    edition: updated.artifact.edition, target, before, after, platform: final.requests,
    drafts: final.posts, ledgerAttempts: final.attempts, externalConnections: final.denied_connections.length,
    externalBrowserRequests: blocked.length }, null, 2));
}

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_EXECUTABLE, args: ["--disable-background-networking",
    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1", "--no-proxy-server"] });
  try {
    await run(browser);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
