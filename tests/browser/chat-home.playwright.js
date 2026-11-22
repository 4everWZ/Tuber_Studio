async page => {
  await page.setViewportSize({width:1280,height:800});
  await page.emulateMedia({reducedMotion:"no-preference"});
  const blocked = [], errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const domain = /\/api\/(voices|assets|characters|account\/bilibili|automation)(\/|\?|$)/;
  await page.route(domain, route => { blocked.push(route.request().url()); return route.fulfill({status: 503, body: "{}"}); });
  const base = await page.evaluate(() => ({origin: location.origin, host: location.hostname}));
  if (!["127.0.0.1", "localhost"].includes(base.host)) throw Error("Open an isolated local Studio before running this check");
  const gallery = /\/api\/memes(?:[/?]|$)/;
  const blockGallery = route => { blocked.push(route.request().url()); return route.fulfill({status: 503, body:"{}"}); };
  await page.route(gallery, blockGallery);
  await page.goto(base.origin);
  const pet = page.getByRole("button", {name:"为角色宠物选表情"});
  const menu = page.getByRole("button", {name:"更多对话操作"});
  await pet.waitFor();
  await page.waitForFunction(() => {
    const image = document.querySelector("#chat-agent-avatar");
    return image.naturalWidth > 0 && new URL(image.src).pathname === "/assets/whale/idle.gif";
  });
  await page.getByRole("textbox", {name:"给大肥鱼发消息"}).fill("保留草稿");
  await page.unroute(gallery, blockGallery);
  await pet.click();
  await page.getByRole("dialog", {name:"宠物表情"}).waitFor();
  const dialog = page.getByRole("dialog", {name:"宠物表情"});
  const original = dialog.locator("article").filter({has: page.getByRole("button", {name:"选用吃一口"})});
  await original.getByText("来源与许可").click();
  if (!(await original.getByRole("link", {name:"查看来源"}).getAttribute("href")).endsWith("/assets/whale/dense-eat.png")
      || !(await original.innerText()).includes("YunYueSama")) throw Error("pet atlas credit missing");
  const archive = dialog.locator("article").filter({has: page.getByRole("button", {name:"选用花花"})});
  await archive.getByText("来源与许可").click();
  if (!(await archive.innerText()).includes("1cyberlangke1") ||
      !(await archive.getByRole("link", {name:"查看来源"}).getAttribute("href")).endsWith("/issue-32-062.webp") ||
      !(await archive.getByRole("link", {name:"许可说明"}).getAttribute("href")).endsWith("/ai-girl-stickers/issues/32"))
    throw Error("archive image or grant is not attributed");
  await archive.getByRole("button", {name:"选用花花"}).click();
  await page.waitForFunction(() => document.querySelector("#chat-agent-avatar").naturalWidth > 0 && document.querySelector("#chat-agent-avatar").src.includes("/api/v1/persona/memes/"));
  if (await page.locator("#chat-agent-avatar").evaluate(async image => {
    await image.decode();
    return image.naturalWidth !== 898 || image.naturalHeight !== 898;
  })) throw Error("selected image does not match the archived original");
  if (await page.locator("#operator-chat-input").inputValue() !== "保留草稿") throw Error("draft changed");
  await menu.click(); await page.getByRole("button",{name:"隐藏角色宠物"}).click();
  await page.reload(); await page.getByRole("heading",{name:"大肥鱼",exact:true}).waitFor();
  if (await pet.isVisible()) throw Error("hidden preference not restored");
  await menu.click(); await page.getByRole("button",{name:"显示角色宠物"}).click();
  await pet.waitFor();
  await page.getByRole("button",{name:"面板",exact:true}).click();
  await page.getByRole("button",{name:"关闭面板"}).click();
  await page.waitForTimeout(3100);
  const paths = new Set(blocked.map(url => new URL(url).pathname));
  if (paths.size !== 2 || !paths.has("/api/v1/operations/account/bilibili") || !paths.has("/api/v1/operations/automation")) {
    throw Error("unexpected homepage dependencies: " + blocked.join(", "));
  }
  await page.getByRole("link",{name:"设置",exact:true}).click();
  await page.locator("#providers-form").waitFor();
  if (await page.locator("#inference-node-panel, [data-inference-mode]").count()) throw Error("node controls remain");
  await page.getByRole("link",{name:"对话",exact:true}).click();
  await page.setViewportSize({width:390,height:844});
  await page.emulateMedia({reducedMotion:"reduce"});
  await page.waitForFunction(() => document.querySelector("#chat-agent-avatar").src.endsWith(".png") && document.querySelector("#chat-agent-avatar").naturalWidth > 0);
  if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw Error("narrow viewport overflow");
  if (errors.length) throw Error(errors.join(", "));
  return "PASS: domain outages, history/composer, expressions, hide/reload/restore, activity, settings and mobile reduced motion";
}
