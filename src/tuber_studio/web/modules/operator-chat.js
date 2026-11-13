import { $, node, safeMessage } from "./core.js?v=20261105.32";
import { createOperatorCard } from "./operator-chat-cards.js?v=20261113.06";
import { inferenceDisplay } from "./inference-status.js?v=20261111.07";

const path = "/api/chat/intelligent";
const pageSize = 20;
const shortcutInputs = {
  dynamic: { label: "写动态", placeholder: "输入动态主题", command: "发个动态" },
  browse: { label: "刷视频", placeholder: "输入话题、视频 BV 号或链接", command: "去刷视频" },
  full_video: { label: "看完整视频" },
};
const shortcutQueries = {
  public_experiences: "查看公开经历",
  video_activity: "今天刷了多少视频",
  operation_control: "自主运营",
  live_status: "查看直播状态",
  clock: "现在几点",
  activity: "查看运营动态",
};

function requestId() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("");
  return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join("-");
}

export function initOperatorChat(api, { applyAutomationStatus, getAutomationStatus } = {}) {
  const feed = $("operator-chat-feed"), timeline = $("operator-chat-timeline");
  const older = $("operator-chat-older"), historyNote = $("operator-chat-history-note");
  const form = $("operator-chat-form"), input = $("operator-chat-input"), send = $("operator-chat-send");
  const clear = $("operator-chat-clear"), status = $("operator-chat-status");
  const clearAction = $("operator-chat-clear-action"), videoFields = $("operator-chat-video-fields");
  const videoId = $("operator-chat-video-id"), rangeList = $("operator-chat-video-ranges");
  const modelState = $("operator-chat-model-state"), modelDetails = $("operator-chat-model-details");
  const turns = new Map(), controls = { pending: false };
  const menus = ["shortcuts", "more"].map(id => ({
    popup: $(`operator-chat-${id}`), toggle: $(`operator-chat-${id}-toggle`), panel: $(`operator-chat-${id}-panel`),
  }));
  const defaultInputLabel = input.placeholder;
  let active = false, pending = false, scope = null, nextCursor = null, selectedAction = null, retry = null;
  let epoch = 0, recentRequest = 0, historyPending = false, cardRefreshPending = false;
  const viewport = window.visualViewport;
  const shell = form.closest(".win-main-shell");

  function sizeChat() {
    if (active && viewport?.scale === 1) shell.style.setProperty("--operator-chat-height", `${viewport.height}px`);
    else shell.style.removeProperty("--operator-chat-height");
    resizeInput();
  }
  viewport?.addEventListener("resize", sizeChat);
  window.addEventListener("resize", resizeInput);

  function resizeInput() {
    if (input.hidden) return;
    input.style.height = "auto";
    input.style.height = `${input.scrollHeight}px`;
  }
  input.addEventListener("input", resizeInput);

  function setMenu(menu, open) {
    menu.panel.hidden = !open;
    menu.toggle.setAttribute("aria-expanded", String(open));
  }

  function closeMenus() {
    for (const menu of menus) setMenu(menu, false);
    modelDetails.open = false;
  }

  for (const menu of menus) {
    menu.toggle.onclick = () => {
      const open = menu.panel.hidden;
      closeMenus();
      if (!open) return;
      setMenu(menu, true);
      menu.panel.querySelector("button:not(:disabled)")?.focus({ preventScroll: true });
    };
    menu.popup.onkeydown = event => {
      if (menu.panel.hidden) return;
      if (event.key === "Escape") {
        event.preventDefault();
        setMenu(menu, false);
        menu.toggle.focus({ preventScroll: true });
      } else if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
        const items = [...menu.panel.querySelectorAll("button:not(:disabled)")];
        if (!items.length) return;
        event.preventDefault();
        const current = items.indexOf(document.activeElement);
        const index = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1 :
          (current + items.length + (event.key === "ArrowDown" ? 1 : -1)) % items.length;
        items[index].focus({ preventScroll: true });
      }
    };
  }
  document.addEventListener("pointerdown", event => {
    for (const menu of menus) if (!menu.popup.contains(event.target)) setMenu(menu, false);
    if (!modelDetails.contains(event.target)) modelDetails.open = false;
  });
  document.addEventListener("focusin", event => {
    for (const menu of menus) if (!menu.popup.contains(event.target)) setMenu(menu, false);
    if (!modelDetails.contains(event.target)) modelDetails.open = false;
  });
  modelDetails.onkeydown = event => {
    if (event.key === "Escape" && modelDetails.open) {
      modelDetails.open = false;
      modelDetails.querySelector("summary").focus({ preventScroll: true });
    }
  };

  function setMessage(text, error = false) {
    status.textContent = text;
    status.classList.toggle("operator-chat-status-error", error);
  }

  function setHistoryNote(text) {
    historyNote.textContent = text;
    historyNote.hidden = !text;
  }

  function setPending(value) {
    pending = send.disabled = clear.disabled = clearAction.disabled = value;
    videoFields.disabled = value || selectedAction !== "full_video";
    for (const menu of menus) menu.toggle.disabled = value;
    status.dataset.pending = String(value);
  }

  function setAction(action) {
    selectedAction = action;
    const videoMode = action === "full_video", shortcut = shortcutInputs[action];
    videoFields.hidden = !videoMode;
    videoFields.disabled = pending || !videoMode;
    $("operator-chat-input-label").hidden = input.hidden = input.disabled = videoMode;
    input.placeholder = shortcut?.placeholder || defaultInputLabel;
    clearAction.hidden = !shortcut;
    clearAction.textContent = shortcut ? `${shortcut.label} ×` : "";
    if (shortcut) clearAction.setAttribute("aria-label", `取消${shortcut.label}快捷操作`);
    send.setAttribute("aria-label", videoMode ? "安排只读取材" : "发送");
    resizeInput();
  }

  function focusComposer() {
    (selectedAction === "full_video" ? videoId : input).focus({ preventScroll: true });
  }

  function renderModel(status, settings) {
    const state = inferenceDisplay(status, settings);
    const failed = state.connected === false;
    const value = failed ? "" : state.loaded ? "已连接" : state.modelState || state.connection;
    modelState.hidden = failed;
    if (modelState.textContent !== value) modelState.textContent = value;
    modelDetails.hidden = !failed;
    if (!failed) modelDetails.open = false;
    const error = $("operator-chat-model-error");
    const detail = failed ? safeMessage(status.context?.profile_error || status.inference?.error || "推理服务连接失败") : "";
    if (error.textContent !== detail) error.textContent = detail;
  }

  const scrollEnd = () => { timeline.scrollTop = timeline.scrollHeight; };
  const nearEnd = () => timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 36;
  const privateTurns = () => Array.from(feed.children, element => turns.get(element.dataset.requestId)?.turn)
    .filter(turn => turn?.private && !turn.card).reverse();

  function syncAutomationCards(value) {
    for (const entry of turns.values()) {
      if (entry.turn.card?.type === "operation_control") entry.card.update(value);
    }
  }

  function reflectAutomation(value) {
    applyAutomationStatus?.(value);
    syncAutomationCards(getAutomationStatus?.() || value);
  }

  function mountTurn(turn) {
    const existing = turns.get(turn.request_id);
    if (existing) {
      if (turn.card) existing.card?.update(turn.card.type === "operation_control" ? getAutomationStatus?.() || turn.card.data : turn.card.data);
      return null;
    }
    const section = node("article", null, "operator-chat-turn");
    section.dataset.requestId = turn.request_id;
    section.append(node("p", turn.message, "operator-chat-user"), node("p", turn.reply_text, "operator-chat-reply"));
    let card;
    if (turn.card) {
      const cardRoot = node("div", null, "operator-chat-card");
      card = createOperatorCard(cardRoot, turn, { api, path, privateTurns, reflectAutomation, controls });
      section.append(cardRoot);
      if (turn.card.type === "operation_control" && getAutomationStatus?.()) card.update(getAutomationStatus());
    }
    turns.set(turn.request_id, { turn, section, card });
    return section;
  }

  function reset() {
    epoch++;
    turns.clear();
    feed.replaceChildren();
    input.value = videoId.value = "";
    rangeList.replaceChildren();
    retry = null;
    scope = nextCursor = null;
    historyPending = false;
    older.hidden = true;
    older.disabled = false;
    closeMenus();
    setAction(null);
    setPending(false);
    setHistoryNote("");
    setMessage("");
  }

  async function refresh() {
    if (!active) {
      active = true;
      sizeChat();
      focusComposer();
    }
    if (pending) return;
    const request = ++recentRequest, currentEpoch = epoch;
    try {
      const result = await api(`${path}?limit=${pageSize}`);
      if (!active || request !== recentRequest || currentEpoch !== epoch || pending) return;
      const nextScope = `${result.character_id || result.character}/${result.character_version ?? ""}`;
      if (scope !== null && scope !== nextScope) reset();
      const first = scope === null;
      const follow = first || nearEnd();
      scope = nextScope;
      $("operator-chat-title").textContent = result.character;
      for (const turn of result.turns) {
        const section = mountTurn(turn);
        if (section || first) feed.append(section || turns.get(turn.request_id).section);
      }
      if (first) nextCursor = result.next_cursor;
      older.hidden = !nextCursor;
      setHistoryNote("");
      if (follow) scrollEnd();
      if (first && document.activeElement === document.body) focusComposer();
    } catch (error) {
      if (active && currentEpoch === epoch && request === recentRequest) setHistoryNote(`对话读取失败：${safeMessage(error.message)}`);
    }
  }

  older.onclick = async () => {
    if (!nextCursor || historyPending) return;
    const currentEpoch = epoch;
    historyPending = older.disabled = true;
    setHistoryNote("正在读取更早消息…");
    try {
      const result = await api(`${path}?limit=${pageSize}&before=${encodeURIComponent(nextCursor)}`);
      if (currentEpoch !== epoch) return;
      const resultScope = `${result.character_id || result.character}/${result.character_version ?? ""}`;
      if (resultScope !== scope) { await refresh(); return; }
      setHistoryNote("");
      const oldHeight = timeline.scrollHeight;
      const anchor = feed.firstElementChild;
      for (const turn of result.turns) {
        const section = mountTurn(turn);
        if (section) feed.insertBefore(section, anchor);
      }
      nextCursor = result.next_cursor;
      older.hidden = !nextCursor;
      timeline.scrollTop += timeline.scrollHeight - oldHeight;
    } catch (error) {
      if (currentEpoch === epoch) setHistoryNote(`更早消息读取失败：${safeMessage(error.message)}`);
    } finally {
      if (currentEpoch === epoch) historyPending = older.disabled = false;
    }
  };

  async function refreshCards() {
    if (!active || cardRefreshPending) return;
    const automation = getAutomationStatus?.();
    if (scope && automation?.character?.id && scope !== `${automation.character.id}/${automation.character.version ?? ""}`) {
      await refresh();
      return;
    }
    if (automation) syncAutomationCards(automation);
    const bounds = timeline.getBoundingClientRect();
    const visible = [...turns.values()].filter(entry => {
      if (!entry.card?.needsRefresh?.()) return false;
      const rect = entry.section.getBoundingClientRect();
      return rect.top < bounds.bottom && rect.bottom > bounds.top;
    }).slice(-4);
    if (!visible.length) return;
    const currentEpoch = epoch;
    cardRefreshPending = true;
    try {
      await Promise.allSettled(visible.map(async entry => {
        const revision = entry.card.revision?.();
        const result = await api(`${path}/turns/${entry.turn.request_id}`);
        if (!active || currentEpoch !== epoch || revision !== entry.card.revision?.() || !entry.card.needsRefresh?.() || !result.card) return;
        const follow = nearEnd();
        entry.card.update(result.card.data);
        if (follow) scrollEnd();
      }));
    } finally {
      cardRefreshPending = false;
    }
  }

  function addRange() {
    const row = node("div", null, "operator-chat-range");
    for (const [name, caption, minimum, step] of [["part", "分 P", 1, 1], ["start", "起点（秒）", 0, "any"], ["end", "终点（秒）", 0, "any"]]) {
      const field = node("label", caption), control = node("input");
      control.type = "number";
      control.name = name;
      control.min = String(minimum);
      control.step = String(step);
      control.required = true;
      if (name === "part") control.value = "1";
      if (name === "start") control.value = "0";
      field.append(control);
      row.append(field);
    }
    const remove = node("button", "移除");
    remove.type = "button";
    remove.onclick = () => row.remove();
    row.append(remove);
    rangeList.append(row);
    row.querySelector('[name="end"]').focus();
  }

  function submission() {
    if (selectedAction !== "full_video") {
      const topic = input.value.trim();
      const shortcut = shortcutInputs[selectedAction];
      if (!shortcut) return { message: topic };
      return {
        message: topic ? `${shortcut.command}：${topic}` : shortcut.command,
        intent: { action: selectedAction, ...(topic ? { topic } : {}) },
      };
    }
    const bvid = /^(?:https:\/\/www\.bilibili\.com\/video\/)?(BV[0-9A-Za-z]{10})(?:\/?(?:[?#].*)?)$/.exec(videoId.value.trim())?.[1];
    if (!bvid) throw new Error("请输入有效的视频 BV 号或 B 站视频链接。");
    const ranges = Array.from(rangeList.children, row => {
      const [part, start, end] = Array.from(row.querySelectorAll("input"), field => field.valueAsNumber);
      if (end <= start) throw new Error("取材时段的终点须晚于起点。");
      return { part, start, end };
    });
    return { message: `看完整视频：${bvid}`, intent: { action: "full_video" }, video_selection: { bvid, ranges } };
  }

  async function submit(payload, fromComposer = true) {
    if (!payload.message || pending) return;
    const key = JSON.stringify(payload);
    const request = retry?.key === key ? retry.request : { request_id: requestId(), ...payload };
    const currentEpoch = epoch, initialValue = input.value, initialAction = selectedAction;
    ++recentRequest;
    setPending(true);
    setMessage(payload.video_selection ? "正在安排取材…" : "正在处理…");
    try {
      const result = await api(path, { method: "POST", body: request });
      if (currentEpoch !== epoch) return;
      if (fromComposer && selectedAction === initialAction) {
        if (payload.video_selection) {
          videoId.value = "";
          rangeList.replaceChildren();
          setAction(null);
        } else if (input.value === initialValue) {
          input.value = "";
          setAction(null);
        }
      }
      retry = null;
      const section = mountTurn({ ...request, ...result });
      if (section) feed.append(section);
      scrollEnd();
      setMessage("");
    } catch (error) {
      if (currentEpoch !== epoch) return;
      retry = { key, request };
      setMessage(`${safeMessage(error.message)} 可再次发送。`, true);
    } finally {
      if (currentEpoch === epoch) {
        setPending(false);
        if (active && (form.contains(document.activeElement) || document.activeElement === document.body)) focusComposer();
        if (active && scope === null) await refresh();
      }
    }
  }

  form.onsubmit = event => {
    event.preventDefault();
    try { return submit(submission()); }
    catch (error) { setMessage(safeMessage(error.message), true); }
  };
  input.onkeydown = event => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
      event.preventDefault();
      form.requestSubmit();
    }
  };
  for (const button of document.querySelectorAll("[data-chat-action]")) {
    button.onclick = () => {
      if (pending) return;
      closeMenus();
      const action = button.dataset.chatAction;
      if (Object.hasOwn(shortcutQueries, action)) {
        focusComposer();
        return submit({ message: shortcutQueries[action], intent: { action } }, false);
      }
      setAction(action);
      focusComposer();
    };
  }
  clearAction.onclick = () => { if (!pending) { setAction(null); focusComposer(); } };
  $("operator-chat-add-range").onclick = () => { if (!pending) addRange(); };
  clear.onclick = async () => {
    closeMenus();
    if (pending || !window.confirm("清空当前角色的私聊？已创建的任务和已确认公开经历会保留。")) {
      menus[1].toggle.focus({ preventScroll: true });
      return;
    }
    const currentEpoch = epoch;
    setPending(true);
    try {
      await api(path, { method: "DELETE" });
      if (currentEpoch === epoch) { reset(); if (active) input.focus({ preventScroll: true }); }
    } catch (error) {
      if (currentEpoch === epoch) setMessage(safeMessage(error.message), true);
    } finally {
      if (currentEpoch === epoch) setPending(false);
    }
  };
  return {
    refresh, refreshCards, renderModel,
    reset() { active = false; sizeChat(); reset(); },
    leave() { active = false; sizeChat(); closeMenus(); },
  };
}
