async page => {
  const base = await page.evaluate(() => location.origin);
  if (base !== "http://127.0.0.1:18765") throw Error("Use the isolated Studio and private_call_fixture.py");
  await page.addInitScript(() => {
    if (window.testTracks) return;
    window.testTracks = [];
    window.callContexts = [];
    const NativeContext = window.AudioContext;
    window.AudioContext = class extends NativeContext { constructor(...args) { super(...args); window.callContexts.push(this); } };
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
  await page.waitForTimeout(2600);
  const handoff = page.getByRole("button", {name:"将这句话交办"});
  await handoff.waitFor();
  if (await handoff.count() !== 1 || mutations.join() !== "/api/chat/private-call/ticket") throw Error("unconfirmed handoff");
  await page.getByText("整理一份私有提纲", {exact:true}).waitFor();
  const posted = page.waitForResponse(response => response.url() === base + "/api/agent/tasks" && response.request().method() === "POST");
  await handoff.click();
  const task = await (await posted).json();
  if (!task.id || task.goal !== "整理一份私有提纲") throw Error("incorrect handoff");
  await page.getByRole("button", {name:"挂断"}).click();
  await page.waitForFunction(() => window.testTracks.length === 1 && window.testTracks[0].readyState === "ended" &&
    window.callContexts.length === 1 && window.callContexts[0].state === "closed");
  const after = await (await page.request.get("http://127.0.0.1:18767/observations")).json();
  if (after.input <= before.input || !after.played || after.closed !== before.closed + 1) throw Error(JSON.stringify(after));
  if (mutations.join() !== "/api/chat/private-call/ticket,/api/agent/tasks") throw Error("Unexpected business writes: " + mutations);
  const retained = await (await page.request.get(base + "/api/agent/tasks/" + task.id)).json();
  if (retained.id !== task.id || retained.status === "cancelled") throw Error("hangup lost the task");
  await page.evaluate(() => { window.holdPermission = true; });
  await page.getByRole("button", {name:"更多对话操作"}).click();
  await page.getByRole("button", {name:"私有语音通话"}).click();
  await page.getByRole("button", {name:"开始通话"}).click();
  await page.waitForFunction(() => typeof window.releasePermission === "function");
  await page.getByRole("button", {name:"挂断"}).click();
  await page.evaluate(() => window.releasePermission());
  await page.waitForFunction(() => window.testTracks.every(track => track.readyState === "ended") &&
    window.callContexts.every(context => context.state === "closed"));
  if (mutations.join() !== "/api/chat/private-call/ticket,/api/agent/tasks,/api/chat/private-call/ticket")
    throw Error("Late microphone permission triggered a business action");
  return after;
}
