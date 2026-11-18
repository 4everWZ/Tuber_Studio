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
  for (const [index, profile] of ["original", "q"].entries()) {
    await page.getByRole("button", {name: "更多对话操作"}).click();
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
    const id = await speech();
    const heard = await until(s => s.played_by_response[id] > 200 && s.avatars[index]?.ack.played_ms > 0, "playback ack");
    const row = heard.avatars[index];
    if (row.avatar !== "portrait_" + (index ? "b" : "a").repeat(32) || !row.source || row.peers !== 1)
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
    const resumed = await speech();
    await until(s => s.played_by_response[resumed] > 0, "ack after interrupt");
    results.push({profile, played_ms: heard.played_by_response[id], timeline_ms: row.ack.played_ms});
    await page.getByRole("button", {name: "挂断"}).click();
    await until(s => s.avatars[index].state === "closed" && !s.avatars[index].peers, "hangup cleanup");
  }
  if (exchanges.some(status => status !== 201) || !sockets.some(url => url.includes("/events?ticket=")) ||
      sockets.some(url => url.includes("private-call-fixture"))) throw Error("browser transport credentials or WHEP");
  const subscriptions = exchanges.length;
  await page.route("**/api/chat/private-call/avatars/*/whep", route => route.fulfill({status: 503}));
  await page.getByRole("button", {name: "更多对话操作"}).click();
  await page.getByRole("button", {name: "私有语音通话"}).click();
  await page.getByRole("combobox", {name: "通话形象", exact: true}).selectOption("original");
  await page.getByRole("button", {name: "开始通话"}).click();
  await page.getByText("实时形象不可用，继续语音通话").waitFor();
  await page.getByText(/麦克风已开启/).waitFor();
  const continued = await speech();
  await until(s => s.played_by_response[continued] > 0 && s.avatars[2].state === "closed", "native fallback");
  await page.getByRole("button", {name: "挂断"}).click();
  await page.route("**/api/chat/private-call/ticket", async route => {
    const reply = await route.fetch();
    await route.fulfill({json: {...await reply.json(), memory_use: {ids: [1], stamp: "fixture"}, memory_citations: []}});
  });
  await page.route("**/api/agent/memory/validate", route => route.fulfill({json: {ok: true}}));
  await page.getByRole("button", {name: "更多对话操作"}).click();
  await page.getByRole("button", {name: "私有语音通话"}).click();
  await page.getByRole("combobox", {name: "通话形象", exact: true}).selectOption("q");
  await page.getByRole("button", {name: "开始通话"}).click();
  await page.getByText(/使用逐段核验的语音播放/).waitFor();
  await page.getByText(/麦克风已开启/).waitFor();
  const privateReply = await speech();
  await until(s => s.played_by_response[privateReply] > 0 && s.avatars.length === 3, "memory-aware playback");
  await page.getByRole("button", {name: "挂断"}).click();
  return {results, subscriptions, fallback: true, memoryUsesNativeAudio: true};
}
