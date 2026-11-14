/**
 * API Client for Tuber Studio
 */

export async function api(path, options = {}, onUnauthorized = null) {
  const { body, headers } = options;
  const config = { ...options, headers: new Headers(headers), credentials: "same-origin" };
  if (body !== undefined && typeof body !== "string" && !(body instanceof FormData) && !(body instanceof Blob)) {
    config.headers.set("Content-Type", "application/json");
    config.body = JSON.stringify(body);
  }
  const response = await fetch(path, config);
  if (response.ok && options.onEvent && response.headers.get("content-type")?.includes("text/event-stream")) {
    const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = "";
    try {
      for (;;) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buffer += chunk.value;
        let boundary;
        while ((boundary = buffer.indexOf("\n\n")) !== -1) {
          const frame = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          if (frame.startsWith(":")) continue;
          const [event, data] = frame.split("\n");
          const value = JSON.parse(data.slice(6));
          switch (event.slice(7)) {
            case "done": return value;
            case "error": {
              const error = new Error(value.message);
              error.status = value.status;
              error.streamEvent = true;
              throw error;
            }
            default: options.onEvent(event.slice(7), value);
          }
        }
      }
      throw new Error("回复连接已中断");
    } finally {
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }
  const text = await response.text();
  let result;
  try {
    result = text ? JSON.parse(text) : {};
  } catch {
    result = { detail: text };
  }
  if (!response.ok) {
    if (response.status === 401 && typeof onUnauthorized === "function") {
      onUnauthorized();
    }
    const errMsg = (result && (result.detail || result.error || result.message)) || `${response.status} ${response.statusText}`;
    const error = new Error(typeof errMsg === "object" ? JSON.stringify(errMsg) : String(errMsg));
    error.status = response.status;
    error.body = result;
    throw error;
  }
  return result;
}
