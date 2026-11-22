async page => {
  const engine = "http://127.0.0.1:18867";
  const obs = async () => (await page.request.get(engine + "/observations")).json();
  const until = async (predicate, name) => {
    for (let i = 0; i < 100; i++) {
      const state = await obs();
      if (predicate(state)) return state;
      await page.waitForTimeout(100);
    }
    throw Error(name + " " + JSON.stringify(await obs()));
  };
  const speech = async () => {
    const reply = await page.request.post(engine + "/stimulus/speech");
    if (!reply.ok()) throw Error("engine disconnected");
    return "voice-" + (await reply.json()).sequence;
  };
  await page.addInitScript(() => {
    window.whepDiagnostics = [];
    window.addEventListener("studio-whep-diagnostic", ({detail}) => window.whepDiagnostics.push(detail));
    window.WebSocket = class extends WebSocket {
      interruptCommands = [];
      constructor(...args) {
        super(...args);
        if (new URL(this.url).pathname === "/v1/realtime") window.testRealtimeSocket = this;
      }
      send(message) {
        if (this === window.testRealtimeSocket) {
          const {type} = JSON.parse(message);
          if (type === "response.cancel" || type === "output_audio_buffer.clear") this.interruptCommands.push(type);
        }
        super.send(message);
      }
    };
    const Peer = window.RTCPeerConnection;
    window.RTCPeerConnection = class extends Peer {
      constructor(...args) { super(...args); window.lastWhepPeer = this; }
    };
    navigator.mediaDevices.getUserMedia = async () => {
      const ctx = new AudioContext(), tone = ctx.createOscillator(), out = ctx.createMediaStreamDestination();
      tone.connect(out); tone.start(); await ctx.resume(); return out.stream;
    };
  });
  await page.reload();
  const exchanges = [], sockets = [];
  page.on("websocket", ws => sockets.push(ws.url()));
  page.on("response", res => {
    if (res.url().endsWith("/whep") && res.request().method() === "POST") exchanges.push(res.status());
  });
  const results = [];
  async function realtimeError(code, expected, action = "call_end") {
    const diagnostic = await page.evaluate(value => {
      const error = {type: "invalid_request_error", code: value, message: "private fixture message", param: "/private-fixture"};
      testRealtimeSocket.dispatchEvent(new MessageEvent("message", {data: JSON.stringify({type: "error", error})}));
      return whepDiagnostics.at(-1);
    }, code);
    if (diagnostic.action !== action || diagnostic.cause !== "realtime_error_event" ||
        diagnostic.code !== expected || JSON.stringify(diagnostic).includes("private"))
      throw Error("Realtime error diagnostics");
  }
  const smoothing = page.getByRole("checkbox", {name: "减少画面卡顿"});
  const expectTargets = target => page.waitForFunction(value => {
    const receivers = window.lastWhepPeer.getReceivers();
    return receivers.length === 2 && receivers.every(r => r.jitterBufferTarget === value);
  }, target);
  for (const [index, profile] of ["original", "q"].entries()) {
    if (index === 1) await page.evaluate(() => { delete RTCRtpReceiver.prototype.jitterBufferTarget; });
    await page.getByRole("button", {name: "私有语音通话"}).click();
    await page.getByRole("combobox", {name: "通话形象", exact: true}).selectOption(profile);
    await page.getByRole("button", {name: "开始通话"}).click();
    await page.getByText(/麦克风已开启/).waitFor();
    const video = page.getByLabel("实时通话形象");
    await video.waitFor();
    await page.waitForFunction(() => {
      const v = document.querySelector("#private-call-dialog video");
      return v.currentTime > .15 && v.videoWidth === 640 && v.videoHeight === 720 &&
        v.srcObject.getAudioTracks().length === 1 && v.getVideoPlaybackQuality().totalVideoFrames > 2;
    });
    if (index === 0) {
      await smoothing.waitFor();
      if (await smoothing.isChecked()) throw Error("smoothing must start off");
      await expectTargets(null);
      await smoothing.check();
      await expectTargets(240);
    } else if (await smoothing.isVisible()) throw Error("unsupported smoothing control");
    const id = await speech();
    const heard = await until(s => s.played_by_response[id] > 200 && s.avatars[index]?.ack.played_ms > 0, "playback ack");
    const row = heard.avatars[index];
    const expected = process.env.STUDIO_TEST_RIG_DIR ? ["regular", "q"][index] : "portrait_" + (index ? "b" : "a").repeat(32);
    if (row.avatar !== expected || !row.source || row.peers !== 1)
      throw Error("source or profile binding");
    await page.getByRole("button", {name: "播放静音"}).click();
    const muted = await speech();
    await page.waitForTimeout(600);
    if ((await obs()).played_by_response[muted]) throw Error("muted response acknowledged");
    await page.getByRole("button", {name: "恢复播放"}).click();
    const before = exchanges.length;
    await page.getByRole("button", {name: "打断回答"}).click();
    await until(s => s.avatars[index].generation > 0 && exchanges.length > before &&
      s.avatars[index].ack.generation === s.avatars[index].generation, "new generation and peer");
    const commands = await page.evaluate(() => testRealtimeSocket.interruptCommands);
    if (commands.length !== 1 || commands[0] !== "output_audio_buffer.clear") throw Error("duplicate interrupt command");
    if (index === 0) {
      if (!await smoothing.isChecked()) throw Error("interrupt lost smoothing preference");
      await expectTargets(240);
      await smoothing.uncheck();
      await expectTargets(null);
    }
    const resumed = await speech();
    await until(s => s.played_by_response[resumed] > 0, "ack after interrupt");
    results.push({profile, played_ms: heard.played_by_response[id], timeline_ms: row.ack.played_ms});
    await page.getByRole("button", {name: "挂断"}).click();
    await until(s => s.avatars[index].state === "closed" && !s.avatars[index].peers, "hangup cleanup");
  }
  if (exchanges.some(status => status !== 201) || !sockets.some(url => url.includes("/events?ticket=")) ||
      sockets.some(url => url.includes("private-call-fixture"))) throw Error("browser transport credentials or WHEP");
  const subscriptions = exchanges.length;
  const ends = await page.evaluate(() => whepDiagnostics.filter(row => row.action === "call_end"));
  if (ends.length !== 2 || ends.some(row => row.cause !== "hangup_button" || row.peer !== "connected"))
    throw Error("missing pre-close hangup state");
  await page.route("**/api/v1/agent/private-call/avatars/*/whep", route => route.fulfill({status: 503}));
  await page.getByRole("button", {name: "私有语音通话"}).click();
  await page.getByRole("combobox", {name: "通话形象", exact: true}).selectOption("original");
  await page.getByRole("button", {name: "开始通话"}).click();
  await page.getByText("实时形象不可用，继续语音通话").waitFor();
  const fallback = await page.evaluate(() => whepDiagnostics.filter(row => row.action === "voice_fallback"));
  if (fallback.length !== 1 || fallback[0].cause !== "whep_http_error" || fallback[0].code !== 503)
    throw Error("missing WHEP HTTP fallback cause");
  await page.getByText(/麦克风已开启/).waitFor();
  const continued = await speech();
  await until(s => s.played_by_response[continued] > 0 && s.avatars[2].state === "closed", "native fallback");
  await realtimeError("private-provider-detail", "other");
  await page.locator("#private-call-end").click();
  await page.unroute("**/api/v1/agent/private-call/avatars/*/whep");
  const studio = await page.evaluate(() => location.origin);
  const request = async (method, path, data) => (await page.request.fetch(studio + path,
    {method, data, failOnStatusCode: true})).json();
  const old = "开场时我喜欢讲海底厨房。", updated = "开场时我喜欢讲蓝色灯塔。";
  const pref = await request("GET", "/api/v1/persona/daily-memory/preferences");
  await request("PUT", "/api/v1/persona/daily-memory/preferences", {enabled:true, scope: pref.scope});
  const note = {source:"settings", text:old};
  const {stamp} = await request("POST", "/api/v1/agent/memory/propose", note);
  const {id: memoryId} = await request("POST", "/api/v1/agent/memory/confirm", {...note, stamp});
  const hit = await request("POST", "/api/v1/agent/memory/get", {id:memoryId});
  const prompts = [], controls = [];
  page.on("websocket", ws => {
    if (!ws.url().includes("/v1/realtime?")) return;
    ws.on("framesent", ({payload}) => {
      const e = JSON.parse(payload);
      if (e.type === "session.update") prompts.push(e.session.instructions);
      if (["response.cancel","output_audio_buffer.clear","session.close"].includes(e.type)) controls.push(e.type);
    });
  });
  await page.getByRole("button", {name: "私有语音通话"}).click();
  await page.getByRole("combobox", {name: "通话形象", exact: true}).selectOption("q");
  await page.locator("#private-call-topic").fill(hit.source_key);
  await page.getByRole("button", {name: "开始通话"}).click();
  await page.getByLabel("实时通话形象").waitFor();
  await page.getByText(/麦克风已开启/).waitFor();
  await until(s => s.avatars[3]?.ack.played_ms > 0, "guarded subscription");
  const privateReply = await speech();
  const first = await until(s => s.played_by_response[privateReply] > 0, "memory-aware playback");
  if (!prompts[0]?.includes(old)) throw Error("preference absent");
  await page.evaluate(() => { window.oldTracks = document.querySelector("#private-call-dialog video").srcObject.getTracks(); });
  await request("PATCH", "/api/v1/persona/daily-memory/organized/" + memoryId + "?expected_revision=" + hit.revision, {note: updated});
  const reopened = await until(s => s.avatars[3].state === "closed" && !s.avatars[3].peers &&
    s.avatars[4]?.ack.played_ms > 0, "reopened after revision");
  const revisionChecks = {newSource: first.avatars[3].source !== reopened.avatars[4].source,
    newNote: prompts[1]?.includes(updated), oldNoteRemoved: !prompts[1]?.includes(old),
    clear: controls.includes("output_audio_buffer.clear"), cancel: controls.includes("response.cancel"),
    tracksEnded: await page.evaluate(() => window.oldTracks.every(t=>t.readyState === "ended"))};
  if (!Object.values(revisionChecks).every(Boolean)) throw Error("correction: " + JSON.stringify(revisionChecks));
  const next = await speech();
  await until(s => s.played_by_response[next] > 0, "revised preference playback");
  await request("DELETE", "/api/v1/persona/daily-memory/organized/" + memoryId + "?expected_revision=" + (hit.revision + 1));
  await until(s => s.avatars[4].state === "closed" && !s.avatars[4].peers &&
    s.avatars[5]?.ack.played_ms > 0, "reopened after removal");
  if (prompts[2]?.includes(updated) || prompts[2]?.includes(old)) throw Error("removed memory survived");
  const clean = await speech();
  await until(s => s.played_by_response[clean] > 0, "playback after removal");
  await realtimeError("response_not_active", "response_not_active", "response_error");
  const afterError = await speech();
  await until(s => s.played_by_response[afterError] > 0 && s.avatars[5].peers === 1, "playback after inactive response");
  await realtimeError("session_closed", "session_closed");
  await until(s => s.avatars[5].state === "closed" && !s.avatars[5].peers, "terminal session error");
  await page.locator("#private-call-end").click();
  return {results, subscriptions, fallback: true, memoryCorrectionAndRemoval: true};
}
