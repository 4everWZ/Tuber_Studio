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
  const note = {source:"settings", text:"仅在写视频提纲时，每个标题都要以问号结尾。"};
  const preview = await page.request.post(base + "/api/agent/memory.propose", {data:note, failOnStatusCode:true});
  await page.request.post(base + "/api/agent/memory/confirm", {
    data:{...note, stamp:(await preview.json()).stamp}, failOnStatusCode:true});
  const prompts = [];
  page.on("websocket", socket => {
    if (new URL(socket.url()).pathname !== "/v1/realtime") return;
    socket.on("framesent", ({payload}) => {
      const event = JSON.parse(payload);
      if (event.type === "session.update") prompts.push(event.session.instructions);
    });
  });
  await page.getByRole("button", {name:"更多对话操作"}).click();
  await page.getByRole("button", {name:"私有语音通话"}).click();
  if (await page.evaluate(() => window.testTracks.length) || mutations.length) throw Error("premature call");
  await page.getByRole("button", {name:"开始通话"}).click();
  await page.getByText(/麦克风已开启/).waitFor();
  if (prompts.length !== 1 || prompts[0].includes(note.text)) throw Error("unrelated writing rule entered the call");
  const handoff = page.getByRole("button", {name:"将这句话交办"});
  await page.getByText("查看通话转写", {exact:true}).waitFor();
  if (await handoff.count() || await page.locator("#private-call-review").evaluate(panel => panel.open))
    throw Error("transcript opened without owner action");
  await page.getByText("查看通话转写", {exact:true}).click();
  await page.getByLabel("选择要处理的转写").selectOption({label:"整理一份私有提纲"});
  await handoff.waitFor();
  if (await handoff.count() !== 1 || mutations.join() !== "/api/chat/private-call/ticket") throw Error("unconfirmed handoff");
  await page.locator("#private-call-utterances blockquote").filter({hasText:"整理一份私有提纲"}).waitFor();
  const prompted = page.waitForEvent("dialog");
  const remember = page.getByRole("button", {name:"确认并记住原话"});
  const click = remember.click();
  const confirmation = await prompted;
  if (!confirmation.message().includes("整理一份私有提纲")) throw Error("wrong transcript in preview");
  await confirmation.dismiss();
  await click;
  const pet = page.locator("#private-call-dialog #chat-pet");
  await pet.waitFor();
  if (await page.locator("#private-call-dialog video, #private-call-dialog canvas").count())
    throw Error("voice-only call created a portrait or mouth renderer");
  const stimulate = async kind => {
    if (!(await page.request.post("http://127.0.0.1:18767/stimulus/" + kind)).ok()) throw Error("fixture not connected");
  };
  await page.evaluate(() => window.callContexts[0].suspend());
  await stimulate("speech");
  await page.waitForFunction(() => Number.isFinite(window.assistantStart));
  await page.waitForTimeout(600);
  if ((await (await page.request.get("http://127.0.0.1:18767/observations")).json()).played !== before.played)
    throw Error("queued PCM was acknowledged before playback");
  await page.evaluate(() => window.callContexts[0].resume());
  const heard = async id => {
    for (let i = 0; i < 60; i++) {
      const response = await page.request.get("http://127.0.0.1:18767/observations");
      if ((await response.json()).played_by_response[id] > 0) return;
      await page.waitForTimeout(100);
    }
    throw Error("no audible samples acknowledged for " + id);
  };
  await heard("voice-1");
  const changeDisplay = async data => {
    const reply = await page.request.put(base + "/api/settings/performance", {data});
    if (!reply.ok()) throw Error("display update failed");
  };
  await changeDisplay({pets_enabled:false, avatar_enabled:true});
  await pet.waitFor({state:"hidden"});
  if (!await page.evaluate(() => window.testTracks[0].readyState === "live")) throw Error("display change stopped microphone");
  await stimulate("speech");
  await heard("voice-2");
  await stimulate("interrupt");
  await stimulate("speech");
  await heard("voice-3");
  const mute = page.locator("#private-call-mute");
  await mute.click();
  await stimulate("speech");
  await page.waitForTimeout(500);
  await mute.click();
  const posted = page.waitForResponse(response => response.url() === base + "/api/agent/tasks" && response.request().method() === "POST");
  await handoff.click();
  const task = await (await posted).json();
  if (!task.id || task.goal !== "整理一份私有提纲") throw Error("incorrect handoff");
  await page.getByRole("button", {name:"挂断"}).click();
  await changeDisplay({pets_enabled:true});
  await page.locator(".chat-agent-identity #chat-pet").waitFor();
  await page.waitForFunction(() => window.testTracks.length === 1 && window.testTracks[0].readyState === "ended" &&
    window.callContexts.length === 1 && window.callContexts[0].state === "closed");
  const after = await (await page.request.get("http://127.0.0.1:18767/observations")).json();
  if (after.input <= before.input || !after.played || after.closed !== before.closed + 1) throw Error(JSON.stringify(after));
  if ((after.played_by_response["voice-2"] || 0) >= 3000 || (after.played_by_response["voice-3"] || 0) >= 3000 ||
      after.played_by_response["voice-4"]) throw Error("interrupted or muted audio was counted as played");
  const expected = ["/api/chat/private-call/ticket", "/api/agent/memory.propose", "/api/agent/tasks"];
  if (mutations.join() !== expected.join()) throw Error("Unexpected business writes: " + mutations);
  const retained = await (await page.request.get(base + "/api/agent/tasks/" + task.id)).json();
  if (retained.id !== task.id || retained.status === "cancelled") throw Error("hangup lost the task");
  await page.evaluate(() => { window.holdPermission = true; });
  await page.getByRole("button", {name:"更多对话操作"}).click();
  await page.getByRole("button", {name:"私有语音通话"}).click();
  await page.getByRole("button", {name:"开始通话"}).click();
  await page.waitForFunction(() => typeof window.releasePermission === "function");
  await pet.waitFor();
  await page.getByRole("button", {name:"挂断"}).click();
  await page.evaluate(() => window.releasePermission());
  await page.waitForFunction(() => window.testTracks.every(track => track.readyState === "ended") &&
    window.callContexts.every(context => context.state === "closed"));
  if (mutations.join() !== [...expected, "/api/chat/private-call/ticket"].join())
    throw Error("Late microphone permission triggered a business action");
  return after;
}
