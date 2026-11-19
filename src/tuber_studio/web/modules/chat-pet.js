import { MemePicker, memePreviewUrl } from "../memes.js?v=20261118.1";

export function hasPetSource(asset) {
  return Boolean(asset.source?.author?.trim() && asset.license?.name?.trim() &&
    [asset.source.url, asset.license.url].every(value => {
      try { return new URL(value).protocol === "https:"; } catch { return false; }
    }));
}

export function initChatPet(api, display, report) {
  const pet = document.getElementById("chat-pet");
  const image = document.getElementById("chat-agent-avatar");
  const toggle = document.getElementById("chat-pet-toggle");
  const motion = matchMedia("(prefers-reduced-motion: reduce)");
  const dialog = document.getElementById("private-call-dialog");
  const slot = document.getElementById("private-call-pet-slot");
  const home = pet.parentElement;
  let character = null, expression = null;
  const picker = new MemePicker(api, (id, asset) => {
    expression = asset;
    render();
  }, {
    title: "宠物表情",
    showSources: true,
    description: "只改变你眼前的宠物，不会发送消息。更多来源可从共享图库导入。",
    accept: asset => hasPetSource(asset) && !(motion.matches && asset.animated),
    emptyText: "没有合适的表情。可到图库导入带作者、来源链接和许可链接的图片。",
  });
  function render() {
    const hidden = !display.value?.pets_enabled;
    toggle.disabled = !character || !display.value || display.saving;
    toggle.textContent = hidden ? "显示角色宠物" : "隐藏角色宠物";
    const host = dialog.open ? slot : home;
    if (pet.parentElement !== host) host.prepend(pet);
    pet.hidden = !character || hidden;
    let url = character?.avatar;
    if (motion.matches) url = url === "/assets/whale/idle.gif" ? "/assets/whale/memes/nap.png" : null;
    if (expression && !(motion.matches && expression.animated)) url = memePreviewUrl(expression);
    image.hidden = pet.hidden || !url;
    pet.classList.toggle("pet-placeholder", !url);
    pet.title = expression ? `${expression.title} · 换个表情` : "换个表情";
    if (image.hidden) image.removeAttribute("src");
    else if (image.getAttribute("src") !== url) image.src = url;
  }
  image.onerror = () => { image.hidden = true; pet.classList.add("pet-placeholder"); };
  pet.onclick = () => picker.open(expression?.asset_id);
  toggle.onclick = async () => {
    try { await display.save("pets_enabled", !display.value.pets_enabled); }
    catch (error) { report(error.message); }
    document.getElementById("operator-chat-more-toggle").click();
    document.getElementById("operator-chat-more-toggle").focus();
  };
  motion.addEventListener("change", () => { picker.close(true); render(); });
  display.addEventListener("change", render);
  new MutationObserver(render).observe(dialog, {attributes: true, attributeFilter: ["open"]});
  render();
  return { setCharacter(next) {
    if (!next || character?.character_id !== next.character_id || character?.character_version !== next.character_version) {
      picker.close(true);
      expression = null;
    }
    character = next && { character_id: next.character_id, character_version: next.character_version, avatar: next.avatar };
    render();
  } };
}
