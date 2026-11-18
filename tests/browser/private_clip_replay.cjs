const assert = require("node:assert/strict");
const {readFile} = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require(require.resolve("playwright", {
  paths: [__dirname, path.join(path.dirname(process.execPath), "node_modules/@playwright/cli")],
}));

const [origin, task, other, reference] = process.argv.slice(2);
const base = "/api/agent/tasks/";
assert.equal(new URL(origin).hostname, "127.0.0.1");

function anchor(page) {
  return page.locator("#operator-chat-timeline").evaluate(el => {
    const top = el.getBoundingClientRect().top;
    const turn = [...el.querySelectorAll(".operator-chat-turn")].find(t => t.getBoundingClientRect().bottom > top + 1);
    return [turn.dataset.requestId, turn.getBoundingClientRect().top - top, el.scrollTop];
  });
}

async function run(browser) {
  const context = await browser.newContext({viewport: {width: 1280, height: 800}, serviceWorkers: "block"});
  const external = [];
  await context.route("**/*", route => {
    const u = new URL(route.request().url());
    if (u.origin === origin) return route.continue();
    external.push(u.href);
    if (u.origin === "https://www.bilibili.com" && route.request().isNavigationRequest())
      return route.fulfill({contentType: "text/plain", body: "Offline source navigation"});
    return route.abort();
  });
  const page = await context.newPage();
  page.setDefaultTimeout(12000);
  await page.goto(origin);
  await page.locator("#token").fill(process.env.TUBER_ADMIN_TOKEN);
  await page.locator("#login-form button").click();
  const entry = page.locator(`.operator-chat-turn[data-request-id="${task}"]`).getByRole("button", {name: "查看任务"});
  await entry.waitFor();
  await entry.evaluate(el => el.scrollIntoView({block: "center"}));
  const before = await anchor(page);
  assert.ok(before[2] > 0);
  const detail = await (await page.request.get(origin + base + task)).json();
  assert.notEqual(detail.attention, null);
  await entry.click();
  const panel = page.locator("#chat-activity");
  const steps = detail.steps.filter(s => s.tool === "inspect_video" && s.result.clip);
  assert.equal(steps.length, 2);
  for (const step of steps) {
    const clip = step.result.clip;
    const preview = panel.locator("details").filter({has: page.locator("video")}).filter({
      has: page.getByText(`查看 P2 · ${clip.start}–${clip.end} 秒画面`, {exact: true}),
    });
    const summary = preview.locator("summary").first();
    await summary.click();
    const video = preview.locator("video");
    const url = origin + base + task + "/clips/" + step.id;
    assert.equal(await video.evaluate(v => v.src), url);
    const media = await video.evaluate(async v => {
      await v.play();
      return new Promise(resolve => {
        function observe(t, metadata) {
          if (metadata.mediaTime > .24) resolve({t: metadata.mediaTime, frames: metadata.presentedFrames,
                                                 width: v.videoWidth, duration: v.duration});
          else v.requestVideoFrameCallback(observe);
        }
        v.requestVideoFrameCallback(observe);
      });
    });
    assert.ok(media.frames > 1 && media.width > 0 && Math.abs(media.duration - 3) < .15);
    await summary.click();
    await video.evaluate(v => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))));
    assert.ok(await video.evaluate(v => v.paused && !v.ended), "collapsing the clip must pause it");
    const position = await video.evaluate(v => v.currentTime);
    if (step === steps[0]) {
      // The first detail read clears attention. Its next poll changes the
      // task JSON, but must leave the existing media element mounted.
      const player = await video.elementHandle();
      await page.waitForResponse(r => r.url() === origin + base + task && r.request().method() === "GET");
      await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))));
      assert.ok(await player.evaluate(v => v.isConnected));
      await player.dispose();
    }
    await summary.click();
    assert.equal(await video.evaluate(v => v.currentTime), position);
    const source = preview.getByRole("link", {name: "跳到原片"});
    assert.ok((await preview.innerText()).includes(clip.bvid) && (await preview.innerText()).includes(clip.cid));
    const expected = `https://www.bilibili.com/video/${clip.bvid}/?p=${clip.part}&t=${clip.start}`;
    const [popup] = await Promise.all([context.waitForEvent("page"), source.click()]);
    await popup.waitForURL(expected);
    await popup.close();
    assert.equal(await video.evaluate(v => v.currentTime), position);
    const seek = await video.evaluate(async v => {
      function event(name) {
        return new Promise((resolve, reject) => {
          const timer = setTimeout(() => reject(new Error(
            `Missing ${name}: t=${v.currentTime} paused=${v.paused} ready=${v.readyState} duration=${v.duration} connected=${v.isConnected}`
          )), 6500);
          v.addEventListener(name, () => { clearTimeout(timer); resolve(); }, {once: true});
        });
      }
      const sought = event("seeked");
      v.currentTime = 1.6;
      await sought;
      const at = v.currentTime;
      const ended = event("ended");
      await v.play();
      await ended;
      return {at, end: v.currentTime, ended: v.ended};
    });
    assert.ok(Math.abs(seek.at - 1.6) < .05 && seek.ended && Math.abs(seek.end - media.duration) < .05);
    const range = await page.request.get(url, {headers: {Range: "bytes=0-31"}});
    assert.equal(range.status(), 206);
    assert.equal((await range.body()).length, 32);
    assert.match(range.headers()["cache-control"], /no-store/);
    const served = await page.request.get(url);
    assert.equal(served.headers()["content-type"], "video/mp4");
    assert.ok((await served.body()).equals(await readFile(path.join(reference, `decoded-${clip.start}.mp4`))));
    const guest = await browser.newContext();
    assert.equal((await guest.request.get(url)).status(), 401);
    await guest.close();
    assert.equal((await page.request.get(url.replace(task, other))).status(), 404);
    console.log(JSON.stringify({start: clip.start, ...media, ...seek}));
    await summary.click();
  }
  await page.locator("#chat-activity-close").click();
  const after = await anchor(page);
  assert.equal(after[0], before[0]);
  assert.ok(Math.abs(after[1] - before[1]) <= 1 && Math.abs(after[2] - before[2]) <= 1);
  assert.ok(await entry.evaluate(el => el === document.activeElement), "return focus to the task card");
  await entry.click();
  const previews = panel.locator("video");
  await previews.first().waitFor({state: "attached"});
  const role = await page.request.post(origin + "/api/characters", {data: {name: "隔离对照角色"}});
  assert.equal(role.status(), 200);
  const id = (await role.json()).id;
  assert.equal((await page.request.post(origin + "/api/characters/" + id + "/activate")).status(), 200);
  assert.equal((await page.request.get(origin + base + task)).status(), 404);
  await previews.first().waitFor({state: "detached"});
  for (const step of steps) {
    const res = await page.request.get(origin + base + task + "/clips/" + step.id, {headers: {Range: "bytes=0-31"}});
    assert.equal(res.status(), 404);
    assert.doesNotMatch(res.headers()["content-type"], /video/);
  }
  assert.deepEqual(external, steps.map(s => `https://www.bilibili.com/video/${s.result.clip.bvid}/?p=2&t=${s.result.clip.start}`));
  console.log(JSON.stringify({browser: browser.version(), readingOffsetBefore: before[1], readingOffsetAfter: after[1]}));
}

(async () => {
  const browser = await chromium.launch({executablePath: process.env.CHROMIUM_EXECUTABLE,
    args: ["--disable-background-networking", "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1", "--no-proxy-server"]});
  try { await run(browser); } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
