// Arm the owner's revocation stream before opening either media transport.
export async function guardCall(call, use, invalidate) {
  const response = await fetch("/api/chat/private-call/memory-watch", {
    method: "POST", signal: call.abort.signal, headers: {"Content-Type": "application/json"},
    body: JSON.stringify(use)
  });
  if (response.status === 409) { invalidate("changed"); return; }
  if (!response.ok) throw Error("无法订阅记忆更新");
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  async function next() {
    while (!buffer.includes("\n")) {
      const {done, value} = await reader.read();
      if (done) throw Error("订阅已断开");
      buffer += value;
    }
    const end = buffer.indexOf("\n"), record = JSON.parse(buffer.slice(0, end));
    buffer = buffer.slice(end + 1);
    return record;
  }
  const first = await next();
  if (first.state !== "valid") { invalidate(first.state); return; }
  if (!first.guard) throw Error("记忆订阅无效");
  (async () => {
    while (!call.abort.signal.aborted) {
      const timeout = setTimeout(() => invalidate("closed"), 3500);
      try {
        const {state} = await next();
        if (state !== "valid") { invalidate(state); return; }
      } catch { if (!call.abort.signal.aborted) invalidate("closed"); return; }
      finally { clearTimeout(timeout); }
    }
  })();
  return first.guard;
}
