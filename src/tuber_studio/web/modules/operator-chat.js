import { $, node, safeMessage } from "./core.js?v=20261105.32";

const path = "/api/chat/intelligent";
const statuses = {
  queued: "排队中", running: "运行中", completed: "已完成", paused: "已暂停",
  partial: "部分完成", failed: "失败", cancelled: "已停止",
  pending_review: "待审核", approved: "已审核", rejected: "已拒绝",
  published: "已发布", publish_uncertain: "待核对", publishing: "提交中",
  invalidated: "来源已变化", unavailable: "草稿已不可用",
  stopped: "已停止", live: "直播中", mock: "模拟中", idle: "待机",
};
const label = value => statuses[value] || value || "未知";

function requestId() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("");
  return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join("-");
}

function button(text) {
  const element = node("button", text);
  element.type = "button";
  return element;
}

function link(text, href) {
  const element = node("a", text);
  element.href = href;
  return element;
}

export function initOperatorChat(api) {
  const feed = $("operator-chat-feed");
  const input = $("operator-chat-input");
  const send = $("operator-chat-send");
  const clear = $("operator-chat-clear");
  const status = $("operator-chat-status");
  let pending = false;
  let retry = null;
  let epoch = 0;

  function message(text, error = false) {
    status.textContent = text;
    status.classList.toggle("operator-chat-status-error", error);
  }

  function dynamicCard(section, data) {
    section.append(node("h2", "动态草稿"));
    const outcome = node("p", label(data.status), "muted");
    outcome.setAttribute("role", "status");
    section.append(outcome);
    if (!data.id) return;
    const editable = ["pending_review", "approved"].includes(data.status);
    const body = editable ? node("textarea") : node("p", data.content);
    if (editable) {
      body.value = data.content;
      body.rows = 4;
      body.setAttribute("aria-label", "动态正文");
    }
    section.append(body);
    const actions = node("div", null, "operator-chat-suggestions");
    if (editable || data.status === "publish_uncertain") {
      const submit = button(editable ? "发布" : "核对回执");
      submit.className = "primary";
      actions.append(submit);
      submit.onclick = async () => {
        submit.disabled = true;
        outcome.textContent = "正在处理…";
        try {
          const updated = await api(`/api/dynamics/posts/${data.id}/publish`, {
            method: "POST",
            ...(editable ? { body: { text: body.value.trim() } } : {}),
          });
          section.replaceChildren();
          dynamicCard(section, updated);
        } catch (error) {
          outcome.textContent = safeMessage(error.message);
          submit.disabled = false;
        }
      };
    }
    actions.append(link("打开动态工作台", "#dynamics"));
    section.append(actions);
    if (data.error) section.append(node("p", safeMessage(data.error), "muted"));
    if (data.status === "published" && data.receipt) {
      const id = data.receipt.id_str || data.receipt.dynamic_id_str || data.receipt.dynamic_id || data.receipt.id;
      if (/^\d+$/.test(String(id))) section.append(link("查看已发布动态", `https://t.bilibili.com/${id}`));
    }
  }

  function experienceControl(turn, section) {
    const details = node("details");
    details.append(node("summary", "选为公开经历"));
    const form = node("form");
    const fact = node("textarea");
    fact.rows = 2;
    fact.required = true;
    fact.maxLength = 150;
    fact.setAttribute("aria-label", "可公开的事实");
    fact.placeholder = "写下你愿意让大肥鱼公开回忆的事实";
    const submit = node("button", "确认加入公开经历");
    submit.type = "submit";
    const result = node("p", "", "muted");
    result.setAttribute("role", "status");
    form.append(fact, submit, result);
    details.append(form);
    form.onsubmit = async event => {
      event.preventDefault();
      submit.disabled = true;
      try {
        await api(`${path}/${turn.request_id}/public-experience`, {
          method: "POST", body: { fact: fact.value.trim() },
        });
        fact.disabled = true;
        result.textContent = "已加入公开经历。";
      } catch (error) {
        result.textContent = safeMessage(error.message);
        submit.disabled = false;
      }
    };
    section.append(details);
  }

  function renderTurn(turn) {
    const section = node("article", null, "operator-chat-turn");
    section.append(node("p", turn.message, "operator-chat-user"), node("p", turn.reply_text));
    if (turn.card) {
      const card = node("div", null, "operator-chat-card");
      const { type, data } = turn.card;
      if (type === "dynamic_draft") dynamicCard(card, data);
      if (type === "video_browsing") {
        card.append(node("h2", "只读浏览"), node("p", label(data.status), "muted"),
          link("查看轮次", `#automation/round/${encodeURIComponent(data.id)}`));
      }
      if (type === "live_studio_hud") {
        card.append(node("h2", "直播状态"), node("p", label(data.state) + (data.muted ? " · 静音" : ""), "muted"),
          link("打开直播工作台", "#live"));
      }
      section.append(card);
    } else if (turn.private) {
      experienceControl(turn, section);
    }
    feed.append(section);
  }

  async function refresh() {
    if (pending) return;
    const requestEpoch = ++epoch;
    const result = await api(path);
    if (requestEpoch !== epoch || pending) return;
    $("operator-chat-title").textContent = `与${result.character}对话`;
    feed.replaceChildren();
    for (const turn of result.turns) renderTurn(turn);
  }

  function reset() {
    epoch++;
    feed.replaceChildren();
    input.value = "";
    retry = null;
    message("");
  }

  $("operator-chat-form").onsubmit = async event => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text || pending) return;
    const request = retry?.message === text ? retry : { request_id: requestId(), message: text };
    const requestEpoch = ++epoch;
    pending = send.disabled = clear.disabled = true;
    message("正在处理…");
    try {
      const result = await api(path, { method: "POST", body: request });
      if (requestEpoch !== epoch) return;
      if (input.value.trim() === text) input.value = "";
      retry = null;
      renderTurn({ ...request, ...result });
      feed.lastElementChild?.scrollIntoView({ block: "nearest" });
      message("");
    } catch (error) {
      if (requestEpoch !== epoch) return;
      retry = request;
      message(safeMessage(error.message), true);
    } finally {
      pending = send.disabled = clear.disabled = false;
    }
  };

  input.onkeydown = event => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      $("operator-chat-form").requestSubmit();
    }
  };
  for (const prompt of document.querySelectorAll("[data-chat-prompt]")) {
    prompt.onclick = () => { input.value = prompt.dataset.chatPrompt; input.focus(); };
  }
  clear.onclick = async () => {
    if (!window.confirm("清空当前角色的私聊？已创建的任务和已确认公开经历会保留。")) return;
    clear.disabled = send.disabled = true;
    try {
      await api(path, { method: "DELETE" });
      reset();
    } catch (error) {
      message(safeMessage(error.message), true);
    } finally {
      clear.disabled = send.disabled = false;
    }
  };
  return { refresh, reset };
}
