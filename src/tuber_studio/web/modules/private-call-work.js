import {api} from "./api.js";
import {renderAssistantAnswer} from "./operator-chat-answer.js";

const path = "/api/chat/private-call";
const changed = () => document.dispatchEvent(new CustomEvent("operator-chat-changed"));
const status = {
  queued: "已交办", running: "处理中", waiting: "等待工具结果", scheduled: "已预约",
  awaiting_approval: "等待确认", needs_input: "等待你的答复", cancelled: "已停止",
  completed: "交办完成", failed: "交办遇到问题", paused: "已暂停",
};

export function callWork(call, context, output, note) {
  const rows = new Map();
  const signal = call.abort.signal;
  let update, readback;
  let routing = 0, queue = Promise.resolve();
  const submit = (origin, confirmation) => api(path + "/utterances", {
    method: "POST", signal, body: {origin, ...(confirmation === undefined ? {} : {confirmation})},
  });

  function stopReading() {
    if (!readback) return;
    readback.request.abort();
    if (readback.node) { readback.node.stop(); readback.node.disconnect(); }
    readback = undefined;
  }

  async function refresh() {
    if (update) return update;
    update = (async () => {
      const {tasks} = await api(path + "/work/status", {method: "POST", signal, body: context});
      if (signal.aborted) return;
      const next = new Map(tasks.map(task => [task.id, task]));
      if (readback) {
        const revised = next.get(readback.task.id);
        if (revised?.revision !== readback.task.revision || revised.status !== "completed" ||
            revised.message !== readback.task.message) stopReading();
      }
      let changedCard = false;
      for (const [id, row] of rows) {
        if (next.has(id)) continue;
        row.root.remove();
        rows.delete(id);
        changedCard = true;
      }
      for (const task of tasks) {
        let row = rows.get(task.id);
        if (!row) {
          const root = document.createElement("section"), heading = document.createElement("p");
          const detail = document.createElement("div"), button = document.createElement("button");
          heading.setAttribute("role", "status");
          button.type = "button";
          button.textContent = "朗读结果";
          root.append(heading, detail, button);
          output.append(root);
          row = {root, heading, detail, button};
          rows.set(task.id, row);
          button.onclick = () => read(row.task);
        }
        if (row.task?.updated === task.updated && row.task?.message === task.message) continue;
        const headline = `${status[task.status] ?? "交办"} · ${task.goal}`;
        if (row.heading.textContent !== headline) row.heading.textContent = headline;
        row.button.hidden = task.status !== "completed" || !task.message;
        row.detail.hidden = !task.message;
        if (row.task?.message !== task.message)
          renderAssistantAnswer(row.detail, {reply_format: "markdown", reply_text: task.message});
        row.task = task;
        changedCard = true;
      }
      output.hidden = rows.size === 0;
      if (changedCard) changed();
    })();
    try { await update; }
    catch (error) {
      stopReading();
      if ([401, 409].includes(error.status)) {
        rows.clear();
        output.replaceChildren();
        output.hidden = true;
      }
      throw error;
    } finally { update = undefined; }
  }

  async function read(task) {
    stopReading();
    const selected = {task, request: new AbortController()};
    readback = selected;
    try {
      const response = await fetch(`${path}/work/${task.id}/read`, {method: "POST",
        headers: {"Content-Type": "application/json"}, signal: selected.request.signal,
        body: JSON.stringify({call: context, revision: task.revision})});
      if (!response.ok) throw Error((await response.json()).detail);
      selected.buffer = await call.context.decodeAudioData(await response.arrayBuffer());
    } catch (error) {
      if (readback !== selected || signal.aborted) return;
      stopReading();
      note.textContent = error.message;
    }
  }

  function idle() {
    return call.context.state === "running" && !call.playback.muted && !call.speaking &&
      !call.pendingAudio && !call.playback.queue.length &&
      performance.now() - (call.lastVoice ?? 0) > 800 && !call.transcription?.pending && !routing;
  }

  async function playWhenIdle() {
    const selected = readback;
    if (!selected?.buffer || selected.node || selected.checking || !idle()) return;
    selected.checking = true;
    try {
      if (update) await update;
      await refresh();
      if (readback !== selected || !idle() || signal.aborted) return;
      const node = selected.node = call.context.createBufferSource();
      node.buffer = selected.buffer;
      node.connect(call.context.destination);
      node.onended = () => {
        node.disconnect();
        if (readback === selected) readback = undefined;
      };
      node.start();
    } catch { stopReading(); }
    finally { selected.checking = false; }
  }

  async function handoff(origin, confirmation) {
    const receipt = await submit(origin, confirmation);
    if (receipt.turn_id) {
      changed();
      await refresh().catch(() => {});
      if (!receipt.task_id) note.textContent = receipt.message;
    }
    return receipt;
  }

  const poll = setInterval(() => { if (rows.size) void refresh().catch(() => {}); }, 1800);
  const breaks = setInterval(() => void playWhenIdle(), 250);
  signal.addEventListener("abort", () => {
    clearInterval(poll);
    clearInterval(breaks);
    stopReading();
    output.replaceChildren();
    output.hidden = true;
    note.textContent = "";
  }, {once: true});
  return {
    accept(origin) {
      routing++;
      queue = queue.then(async () => {
        if (!signal.aborted) await handoff(origin);
      }).catch(error => {
        if (!signal.aborted) note.textContent = error.message;
      }).finally(() => { routing--; });
    },
    confirm: handoff,
    refresh() { void refresh().catch(() => {}); },
    interrupt() { if (readback?.node) stopReading(); },
    stopReading,
  };
}
