const {chromium} = require(require.resolve("playwright", {paths: [process.cwd(),
  require("node:path").join(require("node:path").dirname(process.execPath), "node_modules/@playwright/cli")]}));
const {randomUUID} = require("node:crypto");

(async () => {
  const origin = "http://127.0.0.1:" + Number(process.argv[2]);
  const browser = await chromium.launch({executablePath: process.env.CHROMIUM_EXECUTABLE,
    args: ["--autoplay-policy=no-user-gesture-required", "--disable-background-networking", "--no-proxy-server"]});
  try {
    const page = await browser.newPage();
    await page.route("**/*", route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
    await page.addInitScript(() => {
      window.mediaEvents = [];
      window.Audio = class extends Audio {
        constructor(...args) {
          super(...args);
          for (const kind of ["playing", "ended"]) this.addEventListener(kind, event => {
            window.mediaEvents.push({kind, trusted: event.isTrusted, seconds: this.currentTime,
              duration: this.duration, audible: !this.muted && this.volume > 0});
          });
        }
      };
    });
    async function api(method, path, data) {
      const reply = await page.request.fetch(origin + path, {method, data, failOnStatusCode: true});
      return reply.json();
    }
    async function until(fn) {
      for (let i = 0; i < 200; i++) {
        const hit = await fn();
        if (hit) return hit;
        await page.waitForTimeout(70);
      }
      throw Error("Expected handoff transition timed out");
    }
    await page.goto((await api("GET", "/api/v1/system/settings")).overlay_url);
    await api("POST", "/api/v1/live/start");
    const tasks = [];
    for (let i = 0; i < 2; i++) {
      const id = randomUUID(), path = "/api/v1/agent/tasks/" + id;
      tasks.push(id);
      await api("POST", "/api/v1/agent/tasks", {request_id: id, goal: "把一段公开问候交给本场节目"});
      const waiting = await until(async () => {
        const t = await api("GET", path);
        return t.status === "awaiting_approval" && t;
      });
      await api("PATCH", path, {action: "approve", revision: waiting.revision, step_id: waiting.steps.at(-1).id});
      await page.waitForFunction(n => window.mediaEvents.filter(e => e.kind === "playing").length >= n, i+1);
      const playing = await api("GET", path);
      if (playing.steps.at(-1).result || playing.status === "completed") throw Error("Premature delivery");
      if (i) await api("POST", "/api/v1/live/skip");
      await until(async () => (await api("GET", path)).steps.at(-1).result);
    }
    const media = await page.evaluate(() => window.mediaEvents);
    if (media.map(e => e.kind).join() !== "playing,ended,playing" || !media.every(e => e.trusted && e.audible) ||
        media[1].seconds < .1 || Math.abs(media[1].duration - media[1].seconds) > .05) throw Error("Audio did not finish");
    console.log(JSON.stringify({tasks, media}));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
