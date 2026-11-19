import { $, node } from "./core.js";
import { rememberButton, voiceSourceButton } from "./owner-memory.js";

const endpoint = "/api/daily-memory";
export const memoryVisibility = { owner: "仅号主", shared: "可跨端使用", live_rule: "直播规则" };
export const memoryGroupNames = { preference: "长期偏好", decision: "持续决定", experience: "可复用经验" };
const clip = (text, limit) => {
  const line = text.replace(/\s+/g, " ").trim();
  return line.length > limit ? line.slice(0, limit) + "…" : line;
};
const memoryDate = value => new Date(value * 1000).toLocaleString("zh-CN", {
  dateStyle: "medium", timeStyle: "short",
});

function sourceLink(label, href) {
  if (!/^https:\/\/(?:www\.bilibili\.com\/video\/BV[0-9A-Za-z]+|t\.bilibili\.com\/\d+)(?:[/?#]|$)/.test(href || "")) {
    return null;
  }
  const link = node("a", label);
  link.href = href;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}

function errorState(area, text, retry) {
  area.hidden = false;
  const button = node("button", "重试", "personalization-button");
  button.type = "button";
  button.onclick = retry;
  area.replaceChildren(node("p", text), button);
}

function renderSources(item) {
  const details = node("details");
  details.append(node("summary", "查看来源"));
  appendSources(details, item);
  return details;
}

function appendSources(details, item) {
  if (item.memory_kind === "private") {
    const when = item.source.at ? ` · ${memoryDate(item.source.at)}` : "";
    const voice = item.source.key?.startsWith("confirmed_voice:");
    details.append(node("p", (voice ? "私有通话" : "号主原话") + when, "hint"));
    if (voice) details.append(voiceSourceButton(item.id, item.source.key));
  }
  if (item.quote) details.append(node("blockquote", item.quote));
  for (const ref of item.source.references || []) {
    const link = sourceLink(ref.text, ref.href);
    if (link) {
      const line = node("p");
      line.append(link);
      details.append(line);
    }
  }
}

export function initPersonalization(model, api) {
  $("owner-memory-save")?.append(rememberButton(() => ({
    source: "settings", text: $("owner-memory-statement").value.trim(),
  }), "保存私有记忆"));
  const memory = $("memory-summary-dialog");
  const recent = $("recent-events-dialog");
  const toggle = $("memory-enabled");
  const historyToggle = $("memory-history-enabled");
  const settingsStatus = $("memory-switch-status");
  const preview = $("recent-memory-preview-text");
  const memoryLoad = $("memory-load-state");
  const recentLoad = $("recent-load-state");
  const manageButton = $("open-memory-items");
  const manager = $("memory-items");
  const summary = $("memory-summary");
  const summaryUpdated = $("memory-summary-updated");
  const requestForm = $("memory-summary-request");
  const requestInput = $("memory-summary-question");
  const requestStatus = $("memory-summary-request-status");
  const requestResult = $("memory-summary-request-result");
  const recentList = $("recent-memory-list");
  const organizerStatus = $("daily-memory-notice");
  const retryPreference = $("retry-memory-preference");
  let preference;
  let pendingDraft;
  let report;
  let recentSignature;
  let recentScope;
  let scopeVersion = 0;
  let readingPreference = 0;
  let readingRecent = 0;
  let savingPreference = false;
  let memoryWriting = false;
  let summaryRequest;
  let reportRequest;

  function cancelReportLoad() {
    reportRequest?.abort();
    reportRequest = undefined;
  }

  function resetSummaryRequest() {
    summaryRequest?.abort();
    summaryRequest = undefined;
    requestForm.reset();
    requestStatus.textContent = "";
    requestResult.replaceChildren();
    for (const button of requestForm.querySelectorAll("button")) button.disabled = false;
  }

  function showPreference(value) {
    preference = value;
    const statement = $("owner-memory-statement");
    statement.disabled = !value.enabled;
    $("owner-memory-save").inert = !value.enabled;
    if (pendingDraft && pendingDraft.scope === value.scope && value.enabled && !statement.value) {
      statement.value = pendingDraft.text;
    }
    pendingDraft = undefined;
    toggle.checked = value.enabled;
    historyToggle.checked = value.history_enabled !== false;
    historyToggle.disabled = savingPreference;
    toggle.disabled = savingPreference;
    retryPreference.hidden = true;
    settingsStatus.textContent = "";
  }

  async function loadPreference({ background = false } = {}) {
    if (savingPreference) return;
    const read = ++readingPreference;
    try {
      const value = await api(endpoint + "/preferences");
      if (read !== readingPreference || savingPreference) return;
      if ((preference?.scope && preference.scope !== value.scope) ||
          (report?.scope && report.scope !== value.scope) || (recentScope && recentScope !== value.scope)) {
        resetScope();
        refresh();
      }
      showPreference(value);
    } catch (error) {
      if (read !== readingPreference || (background && preference)) return;
      toggle.disabled = true;
      settingsStatus.textContent = `读取记忆设置失败：${error.message}`;
      retryPreference.hidden = false;
    }
  }

  toggle.onchange = async () => {
    const old = preference;
    if (!old) return;
    const version = scopeVersion;
    const enabled = toggle.checked;
    savingPreference = true;
    toggle.disabled = true;
    historyToggle.disabled = true;
    settingsStatus.textContent = "正在保存…";
    let failure;
    try {
      const value = await api(endpoint + "/preferences", { method: "PUT", body: {
        enabled, scope: old.scope, history_enabled: historyToggle.checked,
      } });
      if (version === scopeVersion) showPreference(value);
    } catch (error) {
      failure = error;
    } finally {
      savingPreference = false;
    }
    if (version !== scopeVersion || failure?.status === 409) {
      refreshScope();
      return;
    }
    toggle.disabled = false;
    historyToggle.disabled = false;
    if (failure) {
      toggle.checked = old.enabled;
      historyToggle.checked = old.history_enabled !== false;
      settingsStatus.textContent = `保存失败：${failure.message}。可再次切换重试。`;
    } else if (memory.open) {
      loadReport({ background: true });
    }
  };
  historyToggle.onchange = toggle.onchange;
  retryPreference.onclick = () => loadPreference();

  function showDialog(dialog, opener) {
    dialog.showModal();
    dialog.addEventListener("close", () => {
      if (!opener.closest("[hidden]")) opener.focus({ preventScroll: true });
    }, { once: true });
  }
  for (const dialog of [memory, recent]) {
    dialog.addEventListener("keydown", event => {
      if (event.key === "Escape" && !event.isComposing) {
        event.preventDefault();
        dialog.close();
      }
    });
  }
  memory.addEventListener("close", () => {
    cancelReportLoad();
    resetSummaryRequest();
  });
  recent.addEventListener("close", () => { readingRecent++; });

  function showManager(open, focus = true) {
    summary.hidden = open;
    manager.hidden = !open;
    if (focus) (open ? $("back-to-memory-summary") : manageButton).focus();
  }
  $("open-memory-summary").onclick = event => {
    showManager(false, false);
    showDialog(memory, event.currentTarget);
    loadReport();
  };
  manageButton.onclick = () => showManager(true);
  $("back-to-memory-summary").onclick = () => showManager(false);
  for (const id of ["open-recent-events", "open-role-recent-events"]) {
    $(id).onclick = event => {
      showDialog(recent, event.currentTarget);
      loadRecent();
    };
  }

  function renderReport(value, redraw) {
    const state = value.organization;
    const status = {
      running: "正在后台整理", waiting: "等待模型空闲", disabled: "自动整理已停用",
      retry: state.error ? `后台整理失败：${state.error}` : "后台整理失败，稍后重试",
    }[state.status];
    organizerStatus.textContent = status || "";
    if (!redraw) return;

    summaryUpdated.hidden = !value.summary_updated_at;
    if (value.summary_updated_at) {
      summaryUpdated.dateTime = new Date(value.summary_updated_at * 1000).toISOString();
      summaryUpdated.textContent = `最近更新 ${memoryDate(value.summary_updated_at)}`;
    }
    const overview = $("memory-summary-list");
    overview.replaceChildren();
    const groups = value.summary || [];
    for (const group of groups) {
      const entry = node("article");
      entry.append(node("h3", memoryGroupNames[group.kind]));
      const sources = node("details");
      sources.append(node("summary", "来源"));
      for (const note of group.notes) {
        const paragraph = node("p", note.text + " ");
        if (memoryVisibility[note.visibility]) paragraph.append(node("small", memoryVisibility[note.visibility], "hint"));
        entry.append(paragraph);
        const item = value.organized.find(item => item.id === note.id);
        if (!item) continue;
        const reference = renderSources(item);
        reference.firstElementChild.textContent = item.headline;
        sources.append(reference);
      }
      const changes = node("details");
      changes.append(node("summary", "更正或删除"));
      for (const note of group.notes) {
        const item = value.organized.find(item => item.id === note.id);
        if (!item) continue;
        const form = node("form");
        const label = node("label", `${item.headline} · ${memoryVisibility[item.visibility]}`);
        const input = node(item.choices ? "select" : "textarea");
        for (const choice of item.choices || []) input.append(new Option(choice, choice));
        input.value = note.text;
        input.required = true;
        input.maxLength = 150;
        const save = node("button", "保存更正", "win-btn");
        save.type = "submit";
        const path = `/organized/${note.id}`;
        form.onsubmit = event => {
          event.preventDefault();
          return mutate(path, { method: "PATCH", body: { note: input.value.trim() } }, save, "记忆已更新。");
        };
        label.append(input);
        form.append(label, save, action("删除", event => {
          if (confirm("确认删除这条记忆吗？")) mutate(path, { method: "DELETE" }, event.currentTarget, "记忆已删除。");
        }));
        changes.append(form);
      }
      if (sources.childElementCount > 1) entry.append(sources);
      entry.append(changes);
      overview.append(entry);
    }
    if (!groups.length) overview.append(node("p", "暂无已保存的长期记忆。", "hint"));
    renderEntries();
    renderEarlier(value.highlights);
  }

  requestForm.onsubmit = async event => {
    event.preventDefault();
    const text = requestInput.value.trim();
    if (!text || !report || summaryRequest || memoryWriting) return;
    const mode = event.submitter?.value === "update" ? "update" : "ask";
    const controller = new AbortController();
    summaryRequest = controller;
    const version = scopeVersion;
    const scope = report.scope;
    const buttons = requestForm.querySelectorAll("button[type='submit']");
    for (const button of buttons) button.disabled = true;
    requestResult.replaceChildren();
    requestStatus.textContent = mode === "ask" ? "正在查找记忆…" : "正在识别要更新的记忆…";
    try {
      const result = await api(endpoint + "/summary", {
        method: "POST", body: { scope, action: mode, text }, signal: controller.signal,
      });
      if (controller.signal.aborted || version !== scopeVersion || !memory.open) return;
      if (result.scope !== scope) {
        refreshScope();
        return;
      }
      if (result.status === "found") {
        for (const item of result.items) {
          const entry = node("article");
          entry.append(node("strong", item.headline), node("p", item.anecdote), renderSources(item));
          requestResult.append(entry);
        }
        requestStatus.textContent = `找到 ${result.items.length} 条相关记忆。`;
      } else if (result.status === "no_match") {
        requestStatus.textContent = "没有找到相关的已保存记忆。";
      } else if (result.status === "updated") {
        await loadReport();
        if (controller.signal.aborted || version !== scopeVersion || !memory.open) return;
        requestStatus.textContent = `已更新「${result.item.headline}」。`;
        if (requestInput.value.trim() === text) requestInput.value = "";
      } else if (result.status === "proposal") {
        const { item, note } = result;
        requestStatus.textContent = "请核对要更新的条目。";
        const entry = node("article");
        entry.append(node("strong", item.headline), node("p", `当前内容 · ${item.anecdote}`),
          node("p", `更新为 · ${note}`), action("应用更正", async event => {
            const saved = await mutate(`/organized/${item.id}`, { method: "PATCH", body: { note } },
              event.currentTarget, "记忆已更新。", item.updated_at);
            if (saved && version === scopeVersion && memory.open) {
              requestResult.replaceChildren();
              requestStatus.textContent = `已更新「${item.headline}」。`;
              requestInput.focus({ preventScroll: true });
            }
          }));
        requestResult.append(entry);
      } else {
        requestStatus.textContent = "请写出一条记忆的标题和更正后的内容，或从条目中直接编辑。";
      }
    } catch (error) {
      if (controller.signal.aborted || version !== scopeVersion || !memory.open) return;
      if (error.status === 409) refreshScope();
      else requestStatus.textContent = `操作失败：${error.message}`;
    } finally {
      if (summaryRequest === controller) {
        summaryRequest = undefined;
        for (const button of buttons) button.disabled = false;
      }
    }
  };

  async function loadReport({ background = false } = {}) {
    if (background && (memoryWriting || reportRequest)) return;
    cancelReportLoad();
    const request = new AbortController();
    const deadline = AbortSignal.timeout(30_000);
    reportRequest = request;
    if (!background && !report) {
      memoryLoad.hidden = false;
      memoryLoad.textContent = "正在读取记忆摘要…";
      summary.hidden = true;
      manager.hidden = true;
    }
    try {
      const value = await api(endpoint + "?limit=100", { signal: AbortSignal.any([request.signal, deadline]) });
      if (reportRequest !== request || !memory.open) return;
      if ((preference && preference.scope !== value.scope) || (report && report.scope !== value.scope)) {
        refreshScope();
        return;
      }
      const first = !report;
      const redraw = first || JSON.stringify([report.summary, report.organized, report.highlights]) !==
        JSON.stringify([value.summary, value.organized, value.highlights]);
      const deferRedraw = redraw && background &&
        (manager.contains(document.activeElement) || summary.contains(document.activeElement));
      if (!deferRedraw) report = value;
      memoryLoad.hidden = true;
      if (first) showManager(false, false);
      renderReport(value, redraw && !deferRedraw);
    } catch (error) {
      if (reportRequest !== request || !memory.open || (background && report)) return;
      const failure = deadline.aborted ? "读取记忆摘要超时。" : `读取记忆摘要失败：${error.message}`;
      errorState(memoryLoad, failure, () => loadReport());
    } finally {
      if (reportRequest === request) reportRequest = undefined;
    }
  }

  async function loadRecent({ background = false } = {}) {
    const read = ++readingRecent;
    if (!background && !recentSignature && recent.open) {
      recentLoad.hidden = false;
      recentLoad.textContent = "正在读取近期互动…";
    }
    try {
      const result = await api(endpoint + "/recent-events");
      if (read !== readingRecent) return;
      if ((preference && preference.scope !== result.scope) || (recentScope && recentScope !== result.scope)) {
        refreshScope();
        return;
      }
      recentScope = result.scope;
      const top = result.items[0];
      preview.textContent = top ? clip(top.source.text, 70) : "近期暂无有来源的运营事件";
      recentLoad.hidden = true;
      if (!recent.open) return;
      const signature = JSON.stringify(result);
      if (recentSignature === signature) return;
      if (background && recentList.contains(document.activeElement)) return;
      recentSignature = signature;
      recentList.replaceChildren();
      for (const event of result.items) {
        const entry = node("article");
        const paragraph = node("p");
        const title = /^BV[0-9A-Za-z]+$/.test(event.title) ? "视频" : event.title;
        paragraph.append(node("strong", clip(title, 55)), ` · ${clip(event.source.text, 125)}`);
        for (const link of [
          sourceLink(event.source.kind === "comment" ? "原评论" : "原视频", event.source.href),
          sourceLink("回复", event.reply?.href),
        ].filter(Boolean)) paragraph.append(" ", link);
        entry.append(paragraph);
        recentList.append(entry);
      }
      if (!top) recentList.append(node("p", "近期暂无有来源的运营事件。", "hint"));
    } catch (error) {
      if (read !== readingRecent || (background && recentSignature)) return;
      preview.textContent = "读取失败，可打开重试";
      if (recent.open) errorState(recentLoad, `读取近期趣事失败：${error.message}`, () => loadRecent());
    }
  }

  function action(label, handler) {
    const button = node("button", label, "win-btn");
    button.type = "button";
    button.onclick = handler;
    return button;
  }

  async function mutate(path, options, target, message, revision) {
    if (!report || target.disabled || memoryWriting || summaryRequest) return;
    const scope = report.scope;
    const version = scopeVersion;
    target.disabled = true;
    memoryWriting = true;
    const fromSummary = summary.contains(target);
    const feedback = requestForm.contains(target) ? requestStatus :
      fromSummary ? $("memory-summary-feedback") : organizerStatus;
    const query = new URLSearchParams({ scope });
    const organized = /^\/organized\/(\d+)$/.exec(path);
    if (organized) {
      const current = report.organized.find(item => item.id === Number(organized[1]));
      if (current?.revision) query.set("expected_revision", current.revision);
    }
    if (revision !== undefined) query.set("revision", revision);
    feedback.textContent = "";
    try {
      await api(`${endpoint}${path}?${query}`, options);
      if (version !== scopeVersion) return;
      await loadReport();
      if (version !== scopeVersion || !memory.open) return;
      feedback.textContent = message;
      if (!target.isConnected) (fromSummary ? manageButton : $("search-memory")).focus({ preventScroll: true });
      return true;
    } catch (error) {
      if (version !== scopeVersion) return;
      if (error.status === 409) refreshScope();
      else feedback.textContent = `操作失败：${error.message}`;
    } finally {
      memoryWriting = false;
      target.disabled = false;
    }
  }

  function renderEntries() {
    if (!report) return;
    const list = $("organized-memory-list");
    const filter = $("filter-memory").value;
    const search = $("search-memory").value.trim().toLocaleLowerCase();
    const statuses = { active: "已保存", archived: "已归档" };
    const visible = report.organized.filter(item => (filter === "all" || item.status === filter) &&
      (!search || [item.headline, item.anecdote, item.quote].some(value => value?.toLocaleLowerCase().includes(search))));
    list.replaceChildren();
    for (const item of visible) {
      const entry = node("article");
      entry.dataset.memoryId = String(item.id);
      const archived = item.status === "archived";
      const actions = node("div", null, "actions");
      const edit = node("details");
      const path = `/organized/${item.id}`;
      const change = (body, target, message) => mutate(path, { method: "PATCH", body }, target, message);
      const date = new Date(item.updated_at * 1000).toLocaleString("zh-CN");
      entry.append(node("strong", item.headline), node("p", item.anecdote),
        node("p", [memoryVisibility[item.visibility], archived ? statuses[item.status] : "", date].filter(Boolean).join(" · "), "hint"));
      edit.append(node("summary", "来源与编辑"));
      appendSources(edit, item);
      const form = node("form");
      const title = node("input");
      const note = node(item.choices ? "select" : "textarea");
      for (const choice of item.choices || []) note.append(new Option(choice, choice));
      title.value = item.headline;
      title.maxLength = 40;
      title.required = true;
      note.value = item.anecdote;
      note.maxLength = 150;
      note.required = true;
      const titleLabel = node("label", "记忆标题");
      titleLabel.append(title);
      const noteLabel = node("label", "记忆内容");
      noteLabel.append(note);
      form.append(titleLabel, noteLabel);
      if (item.can_share) {
        const label = node("label", null, "memory-sharing");
        const toggle = node("input");
        toggle.type = "checkbox";
        toggle.setAttribute("role", "switch");
        toggle.checked = item.owner_only;
        toggle.onchange = async () => {
          const saved = await change({ owner_only: toggle.checked }, toggle, "记忆使用范围已更新。");
          if (!saved) toggle.checked = item.owner_only;
        };
        label.append(toggle, "仅号主可用");
        entry.append(label);
      }
      const save = node("button", "保存修改", "win-btn");
      save.type = "submit";
      form.append(save);
      form.onsubmit = event => {
        event.preventDefault();
        const body = { headline: title.value.trim(), note: note.value.trim() };
        change(body, save, "记忆已更新。");
      };
      edit.append(form);
      actions.append(action("删除", event => {
        if (confirm("确认删除这条记忆吗？")) mutate(path, { method: "DELETE" }, event.currentTarget, "记忆已删除。");
      }));
      entry.append(edit, actions);
      list.append(entry);
    }
    if (!visible.length) list.append(node("p", "暂无符合条件的记忆。", "hint"));
  }

  function renderEarlier(items) {
    const list = $("daily-memory-list");
    list.replaceChildren();
    for (const item of items) {
      const entry = node("article");
      const actions = node("div", null, "actions");
      const path = `/${item.id}`;
      entry.append(node("strong", item.headline), node("p", item.anecdote),
        node("p", item.highlight_date, "hint"), node("blockquote", item.quote || ""));
      for (const ref of item.public_sources || []) {
        const link = sourceLink(ref.quote, `https://www.bilibili.com/video/${ref.bvid}#reply${ref.comment_id}`);
        if (link) { const line = node("p"); line.append(link); entry.append(line); }
      }
      const edit = node("details");
      edit.append(node("summary", "更正"));
      const form = node("form");
      const title = node("input");
      title.value = item.headline;
      const note = node("textarea");
      note.value = item.anecdote;
      const titleLabel = node("label", "记忆标题");
      titleLabel.append(title);
      const noteLabel = node("label", "记忆内容");
      noteLabel.append(note);
      const save = node("button", "保存修改", "win-btn");
      form.append(titleLabel, noteLabel, save);
      form.onsubmit = event => {
        event.preventDefault();
        mutate(path, { method: "PUT", body: { headline: title.value.trim(), anecdote: note.value.trim() } },
          save, "记忆已更新。");
      };
      edit.append(form);
      entry.append(edit);
      actions.append(action("删除", event => {
        if (confirm("确认删除这条记忆吗？")) mutate(path, { method: "DELETE" }, event.currentTarget, "记忆已删除。");
      }));
      entry.append(actions);
      list.append(entry);
    }
    if (!items.length) list.append(node("p", "暂无早期记忆记录。", "hint"));
  }

  $("filter-memory").onchange = renderEntries;
  $("search-memory").oninput = renderEntries;

  function resetScope(restoreSameScope = false) {
    const statement = $("owner-memory-statement");
    if (!restoreSameScope) pendingDraft = undefined;
    else if (preference) pendingDraft = { scope: preference.scope, text: statement.value };
    scopeVersion++;
    readingPreference++;
    cancelReportLoad();
    readingRecent++;
    preference = undefined;
    report = undefined;
    recentSignature = undefined;
    recentScope = undefined;
    toggle.disabled = true;
    toggle.checked = false;
    historyToggle.disabled = true;
    historyToggle.checked = false;
    statement.value = "";
    statement.disabled = true;
    $("owner-memory-save").inert = true;
    settingsStatus.textContent = "";
    preview.textContent = "正在读取…";
    recentList.replaceChildren();
    resetSummaryRequest();
    summaryUpdated.hidden = true;
    $("memory-summary-list").replaceChildren();
    $("organized-memory-list").replaceChildren();
    $("daily-memory-list").replaceChildren();
    $("filter-memory").value = "all";
    $("search-memory").value = "";
    if (memory.open) loadReport();
    if (recent.open) loadRecent();
  }

  function refreshScope() {
    resetScope(true);
    refresh();
  }

  function refresh({ background = false } = {}) {
    if (model.page === "personalization" || memory.open || recent.open) loadPreference({ background });
    if (!recent.open && model.page === "characters") loadRecent({ background });
    if (background && recent.open) loadRecent({ background });
    if (background && memory.open) loadReport({ background });
  }
  return { refresh, refreshScope, close() {
    if (recent.open) recent.close();
    if (memory.open) memory.close();
    resetScope();
  } };
}
