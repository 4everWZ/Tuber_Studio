async page => {
  const base = await page.evaluate(() => location.origin);
  if (base !== "http://127.0.0.1:18765") throw Error("Use the isolated Studio and private_call_fixture.py");
  await page.addInitScript(() => {
    if (window.testTracks) return;
    window.testTracks = [];
    window.callContexts = [];
    const NativeContext = window.AudioContext;
    window.AudioContext = class extends NativeContext {
      constructor(...args) { super(...args); window.callContexts.push(this); }
      createBufferSource() {
        const source = super.createBufferSource(), schedule = source.start.bind(source);
        source.start = timestamp => { window.assistantStart = timestamp; schedule(timestamp); };
        return source;
      }
    };
    navigator.mediaDevices.getUserMedia = async () => {
      const context = new NativeContext();
      const tone = context.createOscillator();
      const destination = context.createMediaStreamDestination();
      tone.connect(destination);
      tone.start();
      await context.resume();
      window.testTracks.push(...destination.stream.getTracks());
      if (window.holdPermission) await new Promise(resolve => { window.releasePermission = resolve; });
      return destination.stream;
    };
  });
  await page.reload();
  const mutations = [];
  page.on("request", request => {
    if (request.method() !== "GET" && request.url().startsWith(base)) mutations.push(request.url().slice(base.length));
  });
  const before = await (await page.request.get("http://127.0.0.1:18767/observations")).json();
  await page.getByRole("button", {name:"更多对话操作"}).click();
  await page.getByRole("button", {name:"私有语音通话"}).click();
  if (await page.evaluate(() => window.testTracks.length) || mutations.length) throw Error("premature call");
  await page.getByRole("button", {name:"开始通话"}).click();
  await page.getByText(/麦克风已开启/).waitFor();
  const handoff = page.getByRole("button", {name:"将这句话交办"});
  await handoff.waitFor();
  if (await handoff.count() !== 1 || mutations.join() !== "/api/chat/private-call/ticket") throw Error("unconfirmed handoff");
  await page.getByText("整理一份私有提纲", {exact:true}).waitFor();
  const avatar = page.locator("#private-call-avatar");
  await avatar.waitFor();
  const picture = () => avatar.evaluate(canvas => canvas.toDataURL());
  const mouth = value => page.waitForFunction(value =>
    document.querySelector("#private-call-avatar").dataset.mouth === value, value, {timeout: 5000});
  const stimulate = async kind => {
    if (!(await page.request.post("http://127.0.0.1:18767/stimulus/" + kind)).ok()) throw Error("fixture not connected");
  };
  await mouth("closed");
  const closed = await picture();
  await page.evaluate(() => window.callContexts[0].suspend());
  await stimulate("speech");
  await page.waitForFunction(() => Number.isFinite(window.assistantStart));
  await page.waitForTimeout(600);
  if (await picture() !== closed) throw Error("queued PCM opened mouth before playback");
  if ((await (await page.request.get("http://127.0.0.1:18767/observations")).json()).played !== before.played)
    throw Error("queued PCM was acknowledged before playback");
  await page.evaluate(() => window.callContexts[0].resume());
  const position = () => page.evaluate(() => Math.round(1000 *
    (window.callContexts[0].getOutputTimestamp().contextTime - window.assistantStart)));
  await page.waitForFunction(() => window.callContexts[0].getOutputTimestamp().contextTime - window.assistantStart > .1);
  if (await picture() !== closed) throw Error("silent lead-in animated");
  await mouth("open");
  const openedMs = await position();
  if (openedMs < 500 || openedMs > 2500) throw Error("mouth outside the fixture's audible interval: " + openedMs);
  if (await picture() === closed) throw Error("speaking frame did not change");
  await mouth("closed");
  const silentMs = await position();
  if (silentMs < 2500 || silentMs >= 3000) throw Error("silent tail did not close within playback: " + silentMs);
  if (await picture() !== closed) throw Error("silent tail did not restore closed frame");
  await stimulate("speech");
  await mouth("open");
  await stimulate("interrupt");
  await mouth("closed");
  await stimulate("speech");
  await mouth("open");
  const mute = page.locator("#private-call-mute");
  await mute.click();
  await mouth("closed");
  await stimulate("speech");
  await page.waitForTimeout(950);
  if (await picture() !== closed) throw Error("muted voice animated");
  await mute.click();
  await page.emulateMedia({reducedMotion: "reduce"});
  await stimulate("speech");
  await page.waitForTimeout(1400);
  if (await picture() !== closed) throw Error("reduced-motion avatar animated");
  await page.emulateMedia({reducedMotion: "no-preference"});
  await mouth("open");
  const posted = page.waitForResponse(response => response.url() === base + "/api/agent/tasks" && response.request().method() === "POST");
  await handoff.click();
  const task = await (await posted).json();
  if (!task.id || task.goal !== "整理一份私有提纲") throw Error("incorrect handoff");
  await page.getByRole("button", {name:"挂断"}).click();
  await mouth("closed");
  await page.waitForFunction(() => window.testTracks.length === 1 && window.testTracks[0].readyState === "ended" &&
    window.callContexts.length === 1 && window.callContexts[0].state === "closed");
  const after = await (await page.request.get("http://127.0.0.1:18767/observations")).json();
  if (after.input <= before.input || !after.played || after.closed !== before.closed + 1) throw Error(JSON.stringify(after));
  if ((after.played_by_response["voice-2"] || 0) >= 3000 || (after.played_by_response["voice-3"] || 0) >= 3000 ||
      after.played_by_response["voice-4"]) throw Error("interrupted or muted audio was counted as played");
  if (mutations.join() !== "/api/chat/private-call/ticket,/api/agent/tasks") throw Error("Unexpected business writes: " + mutations);
  const retained = await (await page.request.get(base + "/api/agent/tasks/" + task.id)).json();
  if (retained.id !== task.id || retained.status === "cancelled") throw Error("hangup lost the task");
  await page.evaluate(() => { window.holdPermission = true; });
  let missingFallback = false;
  await page.route("**/assets/whale/{sing_open.png,sing_closed.png,idle.gif}", async route => {
    if (route.request().url().endsWith("idle.gif")) missingFallback = true;
    await route.fulfill({status:404, body:"missing fixture asset"});
  });
  await page.getByRole("button", {name:"更多对话操作"}).click();
  await page.getByRole("button", {name:"私有语音通话"}).click();
  await page.getByRole("button", {name:"开始通话"}).click();
  await page.waitForFunction(() => typeof window.releasePermission === "function");
  await page.locator("#private-call-avatar-fallback").getByText("大肥鱼", {exact:true}).waitFor();
  if (!missingFallback || await avatar.isVisible()) throw Error("missing assets must leave a text-only call");
  await page.getByRole("button", {name:"挂断"}).click();
  await page.evaluate(() => window.releasePermission());
  await page.waitForFunction(() => window.testTracks.every(track => track.readyState === "ended") &&
    window.callContexts.every(context => context.state === "closed"));
  if (mutations.join() !== "/api/chat/private-call/ticket,/api/agent/tasks,/api/chat/private-call/ticket")
    throw Error("Late microphone permission triggered a business action");
  return {...after, openedMs, silentMs};
}
