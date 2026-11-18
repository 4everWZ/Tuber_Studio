const assert = require("node:assert/strict");
const path = require("node:path");
const {createHash, createHmac} = require("node:crypto");
const { chromium } = require(require.resolve("playwright", {
  paths: [__dirname, path.join(path.dirname(process.execPath), "node_modules/@playwright/cli")],
}));

async function test(browser, origin, mode) {
  const selected = mode === "selected", correction = selected || mode === "correct";
  assert.equal(new URL(origin).hostname, "127.0.0.1");
  const context = await browser.newContext({serviceWorkers: "block"});
  await context.route("**/*", route => new URL(route.request().url()).origin === origin ?
    route.continue() : route.abort());
  const page = await context.newPage();
  page.on("pageerror", error => console.error(error.message));
  await page.addInitScript(() => {
    navigator.mediaDevices.getUserMedia = async () => {
      const context = new AudioContext(), signal = context.createOscillator(), output = context.createMediaStreamDestination();
      signal.connect(output); signal.start(); await context.resume();
      return output.stream;
    };
  });
  const voiceNote = "开场素材请按时间排序。";
  let sessions = 0, renewed;
  const resumed = new Promise(resolve => { renewed = resolve; });
  let send, sentTranscript = false, acknowledged = 0, markClosed;
  const originalClosed = new Promise(resolve => { markClosed = resolve; });
  await page.routeWebSocket(url => url.pathname === "/v1/realtime", socket => {
    if (!send) socket.onClose(markClosed);
    send = event => socket.send(JSON.stringify(event));
    socket.onMessage(raw => {
      const event = JSON.parse(raw);
      if (event.type === "session.update") {
        sessions++;
        assert.equal(event.session.instructions.includes(statement), sessions === 1);
        if (sessions === 2) {
          if (correction) assert.ok(event.session.instructions.includes(voiceNote));
          renewed();
        }
        send({type: "session.created", session: {capabilities: {implementation_level: "model_native_duplex",
          ...(selected ? {gateway_input_transcription: {mode:"explicit_once", sample_rate_hz:16000, max_seconds:15, receipt:"hmac-sha256-v1"}} : {})}}});
      } else if (selected && event.type === "gateway.input_transcription.start") {
        send({type:"gateway.input_transcription.started", capture_id:"selected-slice"});
      } else if (selected && event.type === "gateway.input_transcription.finish") {
        assert.equal(event.capture_id, "selected-slice");
        const machine = "好的，" + voiceNote;
        const ticketHash = createHash("sha256").update("session-fixture").digest("hex");
        const receipt = createHmac("sha256", "fixture").update(JSON.stringify(["omni.mic.v1", ticketHash, event.capture_id, 16000, machine])).digest("hex");
        const reply = {type:"gateway.input_transcription.completed", capture_id:event.capture_id,
          samples:16000, transcript:machine, source:"explicit_user_audio", receipt};
        send({...reply, capture_id:"other-slice", transcript:"错误录音"});
        send({...reply, source:"assistant", transcript:"助手的回复"});
        send(reply); send(reply);
      } else if (!selected && !sentTranscript && event.type === "input_audio_buffer.append") {
        sentTranscript = true;
        send({type: "conversation.item.created", item: {id: "mic", role: "user", type: "message",
          content: [{type: "input_audio"}]}});
        send({type: "input_audio_buffer.committed", item_id: "mic"});
        send({type: "conversation.item.input_audio_transcription.completed", item_id: "mic", content_index: 0,
          transcript: voiceNote});
      } else if (event.type === "playback.ack") acknowledged++;
    });
  });
  await page.goto(origin + "/#personalization");
  const statement = "开场素材请选海底厨房。";
  page.on("dialog", async dialog => {
    assert.ok([statement, voiceNote].some(text => dialog.message().includes(text)));
    await dialog.accept();
  });
  await page.locator("#owner-memory-statement").fill(statement);
  await page.getByRole("button", {name: "保存私有记忆"}).click();
  await page.locator("#owner-memory-save").getByText("已保存").waitFor();
  await page.locator("#open-memory-summary").click();
  await page.locator("#memory-summary-list p").filter({hasText: statement}).first().waitFor();
  await page.getByRole("button", {name: "关闭记忆摘要"}).click();
  const history = page.locator("#memory-history-enabled");
  const [saved] = await Promise.all([page.waitForResponse(r => r.request().method() === "PUT" &&
    r.url().endsWith("/daily-memory/preferences") && r.ok()), history.uncheck()]);
  assert.equal((await saved.json()).history_enabled, false);
  const [restored] = await Promise.all([page.waitForResponse(r => r.request().method() === "GET" &&
    r.url().endsWith("/daily-memory/preferences") && r.ok()), page.reload()]);
  assert.equal((await restored.json()).history_enabled, false);
  await page.waitForFunction(() => !document.querySelector("#memory-history-enabled:disabled, #memory-history-enabled:checked"));
  await page.goto(origin);
  await page.locator("#operator-chat-shortcuts-toggle").waitFor();
  await page.locator("#operator-chat-more-toggle").click();
  await page.locator("#temporary-chat-open").click();
  const dialog = page.locator("#temporary-chat");
  await dialog.getByLabel("临时消息").fill("只在这个窗口出现");
  await dialog.getByRole("button", {name: "发送"}).click();
  await dialog.getByRole("log").getByText("只在这个窗口出现", {exact: true}).waitFor();
  await dialog.getByRole("button", {name: "结束临时对话"}).click();
  await page.reload();
  await page.locator("#operator-chat-input").waitFor();
  assert.equal(await page.getByText("只在这个窗口出现", {exact: true}).count(), 0);
  await page.locator("#operator-chat-input").fill(statement);
  await page.locator("#operator-chat-send").click();
  await page.locator(".operator-chat-turn").getByText("为何提到它").click();
  await page.locator(".owner-memory-citations blockquote").filter({hasText: statement}).waitFor();

  await page.locator("#operator-chat-more-toggle").click();
  await page.locator("#private-call-open").click();
  const other = await browser.newContext();
  const original = await other.request.post(origin + "/api/agent/memory.search", {data: {query: statement}});
  const originalRow = (await original.json()).items[0];
  await page.locator("#private-call-topic").fill(correction ? originalRow.source_key : statement);
  await page.locator("#private-call-start").click();
  if (selected) {
    const start = page.getByRole("button", {name:"转认下一句"});
    await start.waitFor();
    assert.equal(await page.getByRole("button",{name:"将这句话交办"}).count(), 0);
    await start.click();
    await page.getByRole("button",{name:"结束并转认这句"}).click();
    await page.locator("#private-call-utterances blockquote").filter({hasText:voiceNote}).waitFor();
    assert.deepEqual(await page.locator("#private-call-utterances blockquote").allTextContents(), ["好的，"+voiceNote]);
    await page.getByLabel("号主确认的文字（可改正误听）").fill(voiceNote);
    assert.equal((await (await other.request.post(origin+"/api/agent/memory.get", {data:{id:originalRow.id}})).json()).revision,
      originalRow.revision);
  }
  if (correction) {
    const selector = page.getByLabel("更正本次引用的偏好");
    await selector.selectOption(String(originalRow.id));
    const updated = page.waitForResponse(r => r.url().endsWith("/api/agent/memory/confirm") && r.ok());
    await page.getByRole("button", {name: "确认用这句话更正"}).click();
    assert.deepEqual(await (await updated).json(), {id:originalRow.id, revision:originalRow.revision+1});
    await resumed;
    const current = await other.request.post(origin+"/api/agent/memory.get", {data:{id:originalRow.id}});
    const stored = await current.json();
    assert.equal(stored.quote, voiceNote);
    if (selected) assert.deepEqual(stored.voice_origin, {machine_text:"好的，"+voiceNote, samples:16000,
      capture_id:"selected-slice", owner_edited:true});
    const list = await other.request.get(origin+"/api/daily-memory");
    assert.equal((await list.json()).organized.filter(row => row.status === "active").length, 1);
    await other.close();
    return;
  }
  try {
    await page.getByRole("button", {name: "确认并记住原话"}).click({timeout: 10000});
  } catch (error) {
    console.error("Voice handshake", {socket: Boolean(send), sentTranscript, status: await page.locator("#private-call-state").innerText()});
    throw error;
  }
  await page.locator("#private-call-utterances").getByText("已保存").waitFor();
  const recalled = await other.request.post(origin + "/api/agent/memory.search", {data: {query: voiceNote}});
  assert.equal((await recalled.json()).items[0].quote, voiceNote);
  const {id} = originalRow;
  let entered, resume;
  const validating = new Promise(resolve => { entered = resolve; });
  const release = new Promise(resolve => { resume = resolve; });
  await page.route("**/api/agent/memory/validate", async route => { entered(); await release; await route.continue(); });
  send({type: "response.output_audio.delta", response_id: "revoked", item_id: "out",
    format: "pcm16", sample_rate_hz: 16000, delta: Buffer.alloc(32000, 1).toString("base64")});
  await validating;
  const deleted = await other.request.delete(origin + "/api/daily-memory/organized/" + id);
  assert.equal(deleted.status(), 200);
  resume();
  await resumed;
  await page.getByText(/麦克风已开启/).waitFor();
  assert.equal(acknowledged, 0);
  await other.close();
  console.log("Chromium", browser.version(), "settings, temporary chat, citations, confirmed voice and revocation passed");
}

(async () => {
  const browser = await chromium.launch({executablePath: process.env.CHROMIUM_EXECUTABLE || undefined,
    args: ["--disable-background-networking", "--use-fake-device-for-media-stream", "--autoplay-policy=no-user-gesture-required",
      "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"]});
  try { await test(browser, process.argv[2], process.argv[3]); } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
