async page => {
  const assert = require("node:assert/strict");
  const origin = new URL(page.url()).origin;
  const browser = page.context().browser();
  const remote = await browser.newContext();
  const chat = await remote.newPage(), desk = await remote.newPage(), overlay = await remote.newPage();
  const unexpected = [];
  for (const p of [page, chat, desk, overlay]) {
    p.on("pageerror", err => unexpected.push(err.message));
    p.on("request", req => {
      if (/\/whep|\/private-call\/avatars|\/live\/avatar\/|private-call-avatar\.js/.test(req.url())) unexpected.push(req.method() + " " + new URL(req.url()).pathname);
    });
    await p.addInitScript(() => {
      window.actorPeers = 0;
      window.RTCPeerConnection = class extends RTCPeerConnection {
        constructor(...args) { super(...args); window.actorPeers++; }
      };
    });
  }
  try {
    await page.goto(origin + "/#settings");
    const pets = page.getByLabel("显示角色宠物"), avatar = page.getByLabel("直播 Avatar（TBD／暂不可用）");
    await page.waitForFunction(() => !document.querySelector("[data-display-setting]").disabled);
    assert.equal(await pets.isChecked(), true);
    assert.equal(await avatar.isChecked(), false);
    await chat.goto(origin);
    await desk.goto(origin + "/live");
    await overlay.addInitScript(() => {
      window.playedSegments = [];
      window.Audio = class extends Audio {
        constructor(...args) {
          super(...args);
          this.addEventListener("ended", event => {
            if (event.isTrusted && this.currentTime > 0 && !this.muted && this.volume > 0)
              window.playedSegments.push(this.currentTime);
          });
        }
      };
    });
    const settings = await (await page.request.get(origin + "/api/v1/system/settings")).json();
    await overlay.goto(settings.overlay_url);
    const expectPet = async (p, selector, visible) => {
      await p.locator(selector).first().waitFor({state: visible ? "visible" : "hidden"});
      assert.equal(await p.locator(selector + ":visible").count(), visible ? 1 : 0);
    };
    for (const [petsOn, savedAvatar] of [[true,false], [false,false], [true,true], [false,true]]) {
      // Older devices may already have persisted the preference before the pause.
      assert.ok((await page.request.put(origin + "/api/v1/system/settings/performance", {data:{avatar_enabled:savedAvatar}})).ok());
      if (await pets.isChecked() !== petsOn) {
        const saved = page.waitForResponse(r => r.url().endsWith("/settings/performance") && r.request().method() === "PUT");
        await pets.setChecked(petsOn);
        assert.ok((await saved).ok());
      }
      await expectPet(chat, ".chat-pet-float #chat-pet", petsOn);
      await chat.getByRole("button", {name:"私有语音通话"}).click();
      await expectPet(chat, "#private-call-dialog #chat-pet", petsOn);
      assert.equal(await chat.locator("#private-call-dialog video,#private-call-dialog canvas").count(), 0);
      await chat.getByRole("button", {name:"关闭", exact:true}).click();
      for (const p of [desk, overlay]) await expectPet(p, ".scene-avatar img", petsOn);
      await page.reload();
      await page.waitForFunction(() => !document.querySelector("[data-display-setting]").disabled);
      assert.equal(await pets.isChecked(), petsOn);
      assert.equal(await avatar.isDisabled(), true);
      assert.equal(await avatar.isChecked(), false);
      const stored = await (await page.request.get(origin + "/api/v1/system/settings/performance")).json();
      assert.equal(stored.avatar_enabled, savedAvatar);
    }
    await desk.locator("[data-desk-action=start]").click();
    await desk.getByText("离线事件调试", {exact:true}).click();
    const form = desk.locator("[data-desk-mock]");
    await form.getByLabel("类型").selectOption("sfx");
    await form.getByLabel("音效素材 ID").fill("sample1");
    await form.locator("button[type=submit]").click();
    await overlay.waitForFunction(() => window.playedSegments.length > 0);
    const playedSeconds = await overlay.evaluate(() => window.playedSegments[0]);
    const variants = page.locator("#artwork-reference-variants fieldset");
    assert.equal(await variants.count(), 2);
    for (const group of await variants.all()) {
      const upload = group.locator("input"), status = group.locator("p");
      await upload.waitFor();
      await page.waitForFunction(() => !document.querySelector("#artwork-reference-variants input").disabled);
      const initial = await status.innerText();
      if (initial === "默认图") await group.locator("img").evaluate(img => img.decode());
      await upload.setInputFiles("assets/whale/memes/nap.png");
      await group.getByText("已上传", {exact:true}).waitFor();
      await group.locator("img").evaluate(img => img.decode());
      page.once("dialog", dialog => dialog.accept());
      await group.getByRole("button", {name:"恢复默认"}).click();
      await group.getByText(initial, {exact:true}).waitFor();
      if (initial === "默认图") await group.locator("img").evaluate(img => img.decode());
    }
    for (const p of [page, chat, desk, overlay]) assert.equal(await p.evaluate(() => window.actorPeers), 0);
    assert.deepEqual(unexpected, []);
    return {combinations:4, independentBrowserContexts:2, referenceVariants:2, peers:0, playedSeconds};
  } finally { await remote.close(); }
}
