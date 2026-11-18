import { $, node, safeMessage } from "./core.js?v=20261105.32";
import { createOperatorCard } from "./operator-chat-cards.js?v=20261116.3";
import { citations, rememberButton } from "./owner-memory.js";
import { inferenceDisplay } from "./inference-status.js?v=20261111.07";
import { renderAssistantAnswer } from "./operator-chat-answer.js";
import { initTaskActivity } from "./task-activity.js?v=20261116.3";

const path = "/api/chat/intelligent";
const pageSize = 20;
const shortcutInputs = {
  agent_task: { label: "交办任务", placeholder: "交代目标、材料和你需要的结果", command: "" },
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

export function initOperatorChat(api, { applyAutomationStatus, getAutomationStatus, setPet, refreshAutomation } = {}) {
  const feed = $("operator-chat-feed"), timeline = $("operator-chat-timeline");
  const older = $("operator-chat-older"), historyNote = $("operator-chat-history-note");
  const form = $("operator-chat-form"), input = $("operator-chat-input"), send = $("operator-chat-send");
  const clear = $("operator-chat-clear"), status = $("operator-chat-status");
  const progress = $("operator-chat-progress");
  const clearAction = $("operator-chat-clear-action"), videoFields = $("operator-chat-video-fields");
  const videoId = $("operator-chat-video-id"), rangeList = $("operator-chat-video-ranges");
  const modelState = $("operator-chat-model-state"), modelDetails = $("operator-chat-model-details");
  const turns = new Map(), controls = { pending: false };
  const menus = ["shortcuts", "more"].map(id => ({
    popup: $(`operator-chat-${id}`), toggle: $(`operator-chat-${id}-toggle`), panel: $(`operator-chat-${id}-panel`),
  }));
  const defaultInputLabel = input.placeholder;
  let active = false, pending = false, scope = null, nextCursor = null, selectedAction = null, retry = null;
  let inFlight = null;
  let epoch = 0, recentRequest = 0, historyPending = false, cardRefreshPending = false;
  const viewport = window.visualViewport;
  const shell = form.closest(".win-main-shell");
  let openedArtifact = null;
  const rememberArtifact = value => { openedArtifact = value; };
  const taskActivity = initTaskActivity(api, requestId, rememberArtifact);

  function sizeChat() {
    if (active && viewport?.scale === 1) shell.style.setProperty("--operator-chat-height", `${viewport.height}px`);
    else shell.style.removeProperty("--operator-chat-height");
    resizeInput();
  }
  viewport?.addEventListener("resize", sizeChat);
  window.addEventListener("resize", resizeInput);

  function resizeInput() {
    if (input.hidden) return;
    const follow = nearEnd();
    input.style.height = "auto";
    input.style.height = `${input.scrollHeight}px`;
    if (follow) scrollEnd();
  }
  form.addEventListener("input", () => {
    resizeInput();
    updateSend();
  });

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
    const follow = nearEnd();
    status.textContent = text;
    status.classList.toggle("operator-chat-status-error", error);
    if (follow) scrollEnd();
  }

  function setHistoryNote(text) {
    historyNote.textContent = text;
    historyNote.hidden = !text;
  }

  function setPending(value) {
    pending = clear.disabled = clearAction.disabled = value;
    videoFields.disabled = value || videoFields.hidden;
    for (const menu of menus) menu.toggle.disabled = value;
    updateSend();
  }

  function updateSend() {
    const stopping = inFlight?.stopping;
    const hasInput = selectedAction === "full_video" ? videoId.value.trim() : input.value.trim() || selectedAction;
    send.type = inFlight ? "button" : "submit";
    send.disabled = inFlight ? Boolean(stopping) : pending || !hasInput;
    send.dataset.generating = String(Boolean(inFlight));
    send.setAttribute("aria-label", stopping ? "正在停止生成" : inFlight ? "停止生成" :
      selectedAction === "full_video" ? "安排只读取材" : "发送");
  }

  function setAction(action) {
    selectedAction = action;
    const videoMode = action === "full_video", shortcut = shortcutInputs[action];
    videoFields.hidden = !videoMode && action !== "agent_task";
    videoFields.disabled = pending || videoFields.hidden;
    videoId.required = videoMode || Boolean(rangeList.children.length);
    $("operator-chat-input-label").hidden = input.hidden = input.disabled = videoMode;
    input.placeholder = shortcut?.placeholder || defaultInputLabel;
    clearAction.hidden = !shortcut;
    clearAction.textContent = shortcut ? `${shortcut.label} ×` : "";
    if (shortcut) clearAction.setAttribute("aria-label", `取消${shortcut.label}快捷操作`);
    updateSend();
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
    const cardTurn = turn.sources?.length && ["operation_events", "operation_event"].includes(turn.card?.type)
      ? { ...turn, card: null } : turn;
    if (existing) {
      if (existing.cardType === cardTurn.card?.type) {
        if (JSON.stringify([existing.turn.reply_text, existing.turn.reply_sections, existing.turn.sources]) !==
            JSON.stringify([turn.reply_text, turn.reply_sections, turn.sources])) {
          renderAssistantAnswer(existing.section.querySelector(".operator-chat-reply"), turn);
        }
        existing.turn = turn;
        const prior = existing.section.querySelector(".owner-memory-citations");
        if (turn.memory_citations?.length) {
          if (!prior) existing.section.append(citations(turn.memory_citations));
        } else prior?.remove();
        if (cardTurn.card) existing.card?.update(turn.card.type === "operation_control" ? getAutomationStatus?.() || turn.card.data : turn.card.data);
        return null;
      }
    }
    const section = node("article", null, "operator-chat-turn");
    section.dataset.requestId = turn.request_id;
    const answer = node("div", null, "operator-chat-reply");
    renderAssistantAnswer(answer, turn);
    section.append(node("p", turn.message, "operator-chat-user"), answer);
    if (turn.memory_citations?.length) section.append(citations(turn.memory_citations));
    if (turn.private && !turn.card && turn.message && [...turn.message].length <= 150) {
      const options = node("details");
      options.append(node("summary", "原话操作"), rememberButton(() => ({
        source: "turn", turn_id: turn.request_id, text: turn.message,
      })));
      section.append(options);
    }
    let card;
    if (cardTurn.card) {
      const cardRoot = node("div", null, "operator-chat-card");
      card = createOperatorCard(cardRoot, cardTurn, { api, path, reflectAutomation, controls, openTask: taskActivity.open, rememberArtifact });
      section.append(cardRoot);
      if (turn.card.type === "operation_control" && getAutomationStatus?.()) card.update(getAutomationStatus());
    }
    turns.set(turn.request_id, { turn, section, card, cardType: cardTurn.card?.type });
    if (existing) {
      existing.section.replaceWith(section);
      return null;
    }
    return section;
  }

  function reset() {
    setPet?.(null);
    epoch++;
    inFlight?.controller.abort();
    inFlight = null;
    turns.clear();
    openedArtifact = null;
    taskActivity.close(false);
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
    progress.textContent = "";
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
      const nextScope = `${result.character_id || result.character}/${result.character_version ?? ""}/${result.account_id || ""}`;
      if (scope !== null && scope !== nextScope) reset();
      const first = scope === null;
      const follow = first || nearEnd();
      scope = nextScope;
      $("operator-chat-title").textContent = result.character;
      setPet?.(result);
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
      const resultScope = `${result.character_id || result.character}/${result.character_version ?? ""}/${result.account_id || ""}`;
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
    void taskActivity.refreshAttention().catch(() => {});
    const bounds = timeline.getBoundingClientRect();
    if ([...turns.values()].some(entry => {
      if (entry.turn.card?.type !== "operation_control") return false;
      const rect = entry.section.getBoundingClientRect();
      return rect.top < bounds.bottom && rect.bottom > bounds.top;
    })) {
      await refreshAutomation?.();
      if (active && getAutomationStatus?.()) syncAutomationCards(getAutomationStatus());
    }
    const visible = [...turns.values()].filter(entry => {
      if (!entry.turn.memory_dependent && !entry.card?.needsRefresh?.()) return false;
      const rect = entry.section.getBoundingClientRect();
      return rect.top < bounds.bottom && rect.bottom > bounds.top;
    }).slice(-4);
    if (!visible.length) return;
    const currentEpoch = epoch;
    cardRefreshPending = true;
    try {
      await Promise.allSettled(visible.map(async entry => {
        const revision = entry.card?.revision?.();
        const result = await api(`${path}/turns/${entry.turn.request_id}`);
        if (!active || currentEpoch !== epoch || revision !== entry.card?.revision?.()) return;
        if (entry.turn.memory_dependent) {
          mountTurn(result);
          return;
        }
        if (!entry.card?.needsRefresh?.() || !result.card) return;
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
    remove.onclick = () => { row.remove(); videoId.required = selectedAction === "full_video" || Boolean(rangeList.children.length); };
    row.append(remove);
    rangeList.append(row);
    videoId.required = true;
    row.querySelector('[name="end"]').focus();
  }

  function submission() {
    if (selectedAction !== "full_video") {
      const topic = input.value.trim();
      const shortcut = shortcutInputs[selectedAction];
      if (!shortcut) return { message: topic, ...(openedArtifact ? { opened_artifact: openedArtifact } : {}) };
      return {
        ...(selectedAction === "agent_task" && (videoId.value.trim() || rangeList.children.length) ?
          { video_selection: videoSelection() } : {}),
        message: shortcut.command && topic ? `${shortcut.command}：${topic}` : topic || shortcut.command,
        intent: { action: selectedAction, ...(topic && selectedAction !== "agent_task" ? { topic } : {}) },
      };
    }
    const selection = videoSelection();
    return { message: `看完整视频：${selection.bvid}`, intent: { action: "full_video" }, video_selection: selection };
  }

  function videoSelection() {
    const bvid = /^(?:https:\/\/www\.bilibili\.com\/video\/)?(BV[0-9A-Za-z]{10})(?:\/?(?:[?#].*)?)$/.exec(videoId.value.trim())?.[1];
    if (!bvid) throw new Error("请输入有效的视频 BV 号或 B 站视频链接。");
    const ranges = Array.from(rangeList.children, row => {
      const [part, start, end] = Array.from(row.querySelectorAll("input"), field => field.valueAsNumber);
      if (end <= start) throw new Error("取材时段的终点须晚于起点。");
      return { part, start, end };
    });
    return { bvid, ranges };
  }

  async function submit(payload, fromComposer = true) {
    if (!payload.message || pending) return;
    const key = JSON.stringify(payload);
    const request = retry?.key === key ? retry.request : { request_id: requestId(), ...payload };
    const currentEpoch = epoch, initialValue = input.value, initialAction = selectedAction;
    const controller = new AbortController();
    inFlight = { request, stopping: false, controller };
    ++recentRequest;
    setPending(true);
    setMessage("");
    progress.textContent = payload.video_selection ? "正在安排取材…" : "正在生成回复…";
    const user = node("p", payload.message, "operator-chat-user");
    const answer = node("div", null, "operator-chat-reply");
    const transient = node("article", null, "operator-chat-turn");
    transient.append(user, answer);
    const follow = nearEnd();
    feed.append(transient);
    if (follow) scrollEnd();
    let partial = "", renderFrame = 0;
    const stages = {
      queued: "等待推理", preparing: "正在整理上下文", preflight: "正在检查模型", checking_model: "正在检查模型",
      counting: "正在准备模型输入", generating: "正在回复", working: "正在查询或安排任务",
    };
    try {
      let result;
      try {
        result = await api(`${path}/stream`, { method: "POST", body: request, signal: controller.signal, onEvent(event, value) {
          if (currentEpoch !== epoch) return;
          if (event === "stage" && !partial) {
            answer.replaceChildren(node("span", stages[value] || "", "hint"));
            progress.textContent = stages[value] || "";
          }
          if (event === "delta") {
            partial += value;
            if (!renderFrame) renderFrame = requestAnimationFrame(() => {
              renderFrame = 0;
              const follow = nearEnd();
              renderAssistantAnswer(answer, { reply_format: "markdown", reply_text: partial });
              if (follow) scrollEnd();
            });
          }
        } });
      } catch (error) {
        if (!error.streamEvent && (error.status === 404 || error.status === 405)) {
          result = await api(path, { method: "POST", body: request, signal: controller.signal });
        } else {
          throw error;
        }
      }
      if (currentEpoch !== epoch) return;
      if (result.stopped) {
        retry = null;
        progress.textContent = "已停止生成。";
        return;
      }
      if (fromComposer && selectedAction === initialAction) {
        if (payload.video_selection) {
          if (input.value === initialValue && initialAction === "agent_task") input.value = "";
          videoId.value = "";
          rangeList.replaceChildren();
          setAction(null);
        } else if (input.value === initialValue) {
          input.value = "";
          setAction(null);
        }
      }
      retry = null;
      if (request.opened_artifact === openedArtifact) openedArtifact = null;
      const follow = nearEnd();
      const section = mountTurn({ ...request, ...result });
      if (section) transient.replaceWith(section);
      else transient.remove();
      if (follow) scrollEnd();
      setMessage("");
      progress.textContent = "";
    } catch (error) {
      if (currentEpoch !== epoch) return;
      if (request.opened_artifact && error.status === 409) openedArtifact = null;
      retry = { key, request };
      progress.textContent = "";
      setMessage(safeMessage(error.message), true);
      const again = node("button", "重试", "quiet operator-chat-retry");
      again.type = "button";
      again.onclick = () => submit(payload, fromComposer);
      status.append(" ", again);
    } finally {
      cancelAnimationFrame(renderFrame);
      transient.remove();
      if (currentEpoch === epoch) {
        inFlight = null;
        setPending(false);
        if (active && (form.contains(document.activeElement) || document.activeElement === document.body)) focusComposer();
        if (active && scope === null) await refresh();
      }
    }
  }

  send.onclick = async () => {
    const current = inFlight;
    if (!current || current.stopping) return;
    current.stopping = true;
    updateSend();
    try {
      const result = await api(`${path}/turns/${current.request.request_id}/stop-generation`, { method: "POST" });
      if (inFlight !== current || result.stopping) return;
      progress.textContent = "当前请求正在收尾。";
    } catch (error) {
      if (inFlight === current) setMessage(`停止失败：${safeMessage(error.message)}`, true);
    }
    if (inFlight === current) {
      current.stopping = false;
      updateSend();
    }
  };

  form.onsubmit = event => {
    event.preventDefault();
    if (pending) return;
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
    leave() { active = false; sizeChat(); closeMenus(); taskActivity.close(false); },
  };
}
