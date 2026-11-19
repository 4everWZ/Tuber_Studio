const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require(require.resolve("playwright", {
  paths: [__dirname, path.join(path.dirname(process.execPath), "node_modules/@playwright/cli")],
}));

async function test(browser, origin, mode) {
  const selected = mode === "selected", correction = selected || mode === "correct";
  assert.equal(new URL(origin).hostname, "127.0.0.1");
  const context = await browser.newContext({serviceWorkers: "block"});
  await context.route(url => url.origin !== origin, route => route.abort());
  const page = await context.newPage();
  page.on("pageerror", error => console.error(error.message));
  page.on("requestfailed", request => {
    if (["script", "document"].includes(request.resourceType())) {
      console.error("Failed asset", new URL(request.url()).pathname, request.failure()?.errorText);
    }
  });
  async function waitForWorkspace(control) {
    try {
      await control.waitFor({state:"visible"});
    } catch (error) {
      console.error("Workspace after navigation", await page.evaluate(() => ({
        hash:location.hash, workspaceHidden:document.querySelector("#workspace")?.hidden,
        loginError:document.querySelector("#login-error")?.textContent,
        section:document.querySelector("[data-section]:not([hidden])")?.dataset.section,
      })));
      throw error;
    }
  }
  // Deliver the actual initial role status while the settings input has focus.
  let releaseStatus;
  const typing = new Promise(resolve => { releaseStatus = resolve; });
  if (mode === "save") {
    await page.routeWebSocket(url => url.pathname === "/ws/admin", socket => {
      const server = socket.connectToServer();
      server.onMessage(async message => {
        if (JSON.parse(message).type === "status") await typing;
        socket.send(message);
      });
    });
  }
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
    assert.equal(new URL(socket.url()).searchParams.has("input_transcription"), false);
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
        send({type: "session.created", session: {capabilities: {implementation_level: "model_native_duplex"}}});
      } else if (!sentTranscript && event.type === "input_audio_buffer.append") {
        sentTranscript = true;
        send({type: "conversation.item.created", item: {id: "mic", role: "user", type: "message",
          content: [{type: "input_audio"}]}});
        send({type: "input_audio_buffer.committed", item_id: "mic"});
        send({type: "conversation.item.input_audio_transcription.completed", item_id: "mic", content_index: 0,
          transcript: voiceNote});
      } else if (event.type === "playback.ack") acknowledged++;
      assert.notEqual(event.type, "input_audio_buffer.commit");
    });
  });
  await page.goto(origin + "/#personalization");
  const statement = "开场素材请选海底厨房。";
  page.on("dialog", async dialog => {
    assert.ok([statement, voiceNote].some(text => dialog.message().includes(text)));
    assert.doesNotMatch(dialog.message(), /（v\d+）/);
    await dialog.accept();
  });
  await waitForWorkspace(page.locator("#owner-memory-statement"));
  await page.locator("#owner-memory-statement").fill(statement);
  releaseStatus();
  if (mode === "save") {
    await page.waitForFunction(() => document.querySelector("#overview-queue").textContent.trim() === "0");
    assert.equal(await page.locator("#owner-memory-statement").evaluate(input => input === document.activeElement), true);
    assert.equal(await page.locator("#owner-memory-statement").inputValue(), statement);
  }
  await page.getByRole("button", {name: "保存私有记忆"}).click();
  await page.locator("#owner-memory-save").getByText("已保存").waitFor();
  await page.locator("#open-memory-summary").click();
  await page.locator("#memory-summary-list p").filter({hasText: statement}).first().waitFor();
  await page.getByRole("button", {name: "关闭记忆摘要"}).click();
  const history = page.locator("#memory-history-enabled");
  const [saved] = await Promise.all([page.waitForResponse(r => r.request().method() === "PUT" &&
    r.url().endsWith("/daily-memory/preferences") && r.ok()), history.uncheck()]);
  assert.equal((await saved.json()).history_enabled, false);
  await page.reload();
  await waitForWorkspace(history);
  await page.waitForFunction(() => {
    const input = document.getElementById("memory-history-enabled");
    return input && !input.disabled && !input.checked;
  });
  const restored = await context.request.get(origin + "/api/daily-memory/preferences");
  assert.equal(restored.ok(), true);
  assert.equal((await restored.json()).history_enabled, false);
  await page.locator('a[data-page="chat"]').click();
  await page.locator("#operator-chat-shortcuts-toggle").waitFor();
  await page.locator("#operator-chat-more-toggle").click();
  await page.locator("#temporary-chat-open").click();
  const dialog = page.locator("#temporary-chat");
  await dialog.getByLabel("临时消息").fill("只在这个窗口出现");
  await dialog.getByRole("button", {name: "发送"}).click();
  await dialog.getByRole("log").getByText("只在这个窗口出现", {exact: true}).waitFor();
  await dialog.getByRole("button", {name: "结束临时对话"}).click();
  await page.reload();
  await waitForWorkspace(page.locator("#operator-chat-input"));
  assert.equal(await page.getByText("只在这个窗口出现", {exact: true}).count(), 0);
  await page.locator("#operator-chat-input").fill(statement);
  await page.locator("#operator-chat-send").click();
  const turn = page.locator(".operator-chat-turn").filter({has: page.locator(".operator-chat-user", {hasText: statement})}).last();
  await turn.locator(".operator-chat-reply").waitFor();
  assert.equal(await turn.getByText(/原话操作|记住这句话|为何提到它/).count(), 0);

  await page.locator("#private-call-open").click();
  assert.equal(await page.locator("#private-call-finish-utterance,#private-call-transcribe").count(), 0);
  const other = await browser.newContext();
  const original = await other.request.post(origin + "/api/agent/memory.search", {data: {query: statement}});
  const originalRow = (await original.json()).items[0];
  const memoryWrites = [];
  page.on("request", request => {
    if (/\/api\/agent\/memory(?:\.propose|\/confirm)$/.test(request.url())) memoryWrites.push(request.url());
  });
  assert.equal(await page.locator("#private-call-citations").count(), 0);
  assert.equal(await page.locator("#private-call-topic").isVisible(), false);
  if (selected) {
    const prefs = await (await other.request.get(origin + "/api/daily-memory/preferences")).json();
    const enabled = await other.request.put(origin + "/api/daily-memory/preferences",
      {data:{scope: prefs.scope, enabled:prefs.enabled, history_enabled:true}});
    assert.equal(enabled.ok(), true);
  } else {
    await page.getByText("指定通话话题", {exact:true}).click();
    await page.getByLabel("话题（可选）").fill(correction ? originalRow.source_key : statement);
  }
  const ticket = page.waitForRequest(r => r.url().endsWith("/api/chat/private-call/ticket"));
  await page.locator("#private-call-start").click();
  const body = (await ticket).postDataJSON();
  if (selected) assert.deepEqual(body, {temporary:false, topic_turn:await turn.getAttribute("data-request-id")});
  await page.getByText("查看通话转写", {exact:true}).waitFor();
  assert.equal(await page.locator("#private-call-review").evaluate(panel => panel.open), false);
  assert.equal(await page.getByRole("button",{name:"确认并记住原话"}).count(), 0);
  await page.getByText("查看通话转写", {exact:true}).click();
  const picker = page.getByLabel("选择要处理的转写");
  assert.deepEqual(await picker.locator("option").allTextContents(), ["请选择一句", voiceNote]);
  await picker.selectOption("1");
  assert.deepEqual(memoryWrites, []);
  if (correction) {
    const selector = page.getByLabel("更正本次引用的偏好");
    const option = selector.locator(`option[value="${originalRow.id}"]`);
    assert.equal(await option.textContent(), originalRow.note);
    if (await selector.locator("option").count() === 1) {
      assert.equal(await selector.inputValue(), String(originalRow.id));
    } else {
      await selector.selectOption({label: originalRow.note});
    }
    const updated = page.waitForResponse(r => r.url().endsWith("/api/agent/memory/confirm") && r.ok());
    await page.getByRole("button", {name: "确认用这句话更正"}).click();
    assert.deepEqual(await (await updated).json(), {id:originalRow.id, revision:originalRow.revision+1});
    await resumed;
    const current = await other.request.post(origin+"/api/agent/memory.get", {data:{id:originalRow.id}});
    const stored = await current.json();
    assert.equal(stored.quote, voiceNote);
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
