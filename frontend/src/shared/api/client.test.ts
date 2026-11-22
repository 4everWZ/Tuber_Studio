import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  apiRequest,
  setUnauthorizedHandler,
  studioPath,
  studioWsUrl,
} from "./client";
import { streamAgentChat, streamBatchPublish } from "./sse";

describe("shared api client", () => {
  beforeEach(() => {
    document.head.innerHTML = "";
    setUnauthorizedHandler(null);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    document.head.innerHTML = "";
    setUnauthorizedHandler(null);
  });

  it("resolves app requests and WebSocket URLs under <base href> without double-prefixing", async () => {
    const base = document.createElement("base");
    base.setAttribute("href", "/dafeiyu/");
    document.head.appendChild(base);

    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      expect(input).toBe("/dafeiyu/api/v1/system/settings");
      return new Response(JSON.stringify({ ready: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const res = await apiRequest("/api/v1/system/settings");
    expect(res).toEqual({ ready: true });
    expect(studioPath("/dafeiyu/assets/pet.gif")).toBe("/dafeiyu/assets/pet.gif");
    expect(studioPath("/dafeiyu?x=1")).toBe("/dafeiyu?x=1");
    expect(studioPath("https://example.com/asset.gif")).toBe("https://example.com/asset.gif");
    expect(studioPath("//cdn.example.com/a.js")).toBe("//cdn.example.com/a.js");
    expect(studioWsUrl("/ws/admin")).toMatch(/^wss?:\/\/[^/]+\/dafeiyu\/ws\/admin$/);
  });

  it("sends JSON, FormData, and File/Blob bodies in their original formats with same-origin credentials", async () => {
    const calls: Array<{ url: string; init: RequestInit }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
        calls.push({ url: String(input), init });
        return new Response(JSON.stringify({ saved: true }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      })
    );

    const jsonBody = { model: "demo" };
    const form = new FormData();
    form.set("title", "new song");
    const wav = new File(["RIFF"], "preview.wav", { type: "audio/wav" });

    await apiRequest("/api/v1/system/settings", { method: "PUT", body: jsonBody });
    await apiRequest("/api/v1/live/uploads", { method: "POST", body: form });
    await apiRequest("/api/v1/persona/artwork-references/whale/front", {
      method: "PUT",
      body: wav,
      headers: { "Content-Type": "audio/wav" },
    });

    expect(calls[0].url).toBe("/api/v1/system/settings");
    expect(new Headers(calls[0].init.headers).get("content-type")).toBe("application/json");
    expect(JSON.parse(String(calls[0].init.body))).toEqual(jsonBody);
    expect(calls[0].init.credentials).toBe("same-origin");

    expect(calls[1].url).toBe("/api/v1/live/uploads");
    expect(new Headers(calls[1].init.headers).has("content-type")).toBe(false);
    expect(calls[1].init.body).toBe(form);
    expect((calls[1].init.body as FormData).get("title")).toBe("new song");
    expect(calls[1].init.credentials).toBe("same-origin");

    expect(calls[2].url).toBe("/api/v1/persona/artwork-references/whale/front");
    expect(new Headers(calls[2].init.headers).get("content-type")).toBe("audio/wav");
    expect(calls[2].init.body).toBe(wav);
    const fileText = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.onerror = () => reject(reader.error);
      reader.readAsText(calls[2].init.body as Blob);
    });
    expect(fileText).toBe("RIFF");
    expect(calls[2].init.credentials).toBe("same-origin");
  });

  it("parses {code, message, details} ErrorEnvelope into ApiError and triggers 401 re-login handler", async () => {
    const envelope = {
      code: "OPERATION_CLAIM_CONFLICT",
      message: "当前讨论楼正由消息响应优先处理",
      details: { resource: "BV1xx", root_id: "12345" },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify(envelope), {
          status: 409,
          statusText: "Conflict",
          headers: { "Content-Type": "application/json" },
        })
      )
    );

    await expect(apiRequest("/api/v1/operations/automation")).rejects.toMatchObject({
      name: "ApiError",
      status: 409,
      code: "OPERATION_CLAIM_CONFLICT",
      message: "当前讨论楼正由消息响应优先处理",
      details: { resource: "BV1xx", root_id: "12345" },
    });

    // 401 triggers registered handler + per-call override
    let globalLoginCount = 0;
    let overrideLoginCount = 0;
    setUnauthorizedHandler(() => {
      globalLoginCount += 1;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ code: "UNAUTHORIZED", message: "Session expired", details: null }), {
          status: 401,
          headers: { "Content-Type": "application/json" },
        })
      )
    );

    await expect(
      apiRequest("/api/v1/system/status", {}, () => {
        overrideLoginCount += 1;
      })
    ).rejects.toBeInstanceOf(ApiError);
    expect(overrideLoginCount).toBe(1);
    expect(globalLoginCount).toBe(0);

    await expect(apiRequest("/api/v1/system/status")).rejects.toMatchObject({
      status: 401,
      code: "UNAUTHORIZED",
      message: "Session expired",
    });
    expect(globalLoginCount).toBe(1);
  });

  it("streams chat SSE frames, ignores keepalives, handles stream errors, and falls back only on 404/405", async () => {
    const encoder = new TextEncoder();
    const ssePayload = [
      ": keepalive\n\n",
      'event: stage\ndata: "generating"\n\n',
      'event: delta\ndata: "你好"\n\n',
      'event: delta\ndata: "，大肥鱼"\n\n',
      'event: done\ndata: {"request_id":"req-1","reply_text":"你好，大肥鱼"}\n\n',
    ];

    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        const stream = new ReadableStream({
          start(controller) {
            for (const chunk of ssePayload) {
              controller.enqueue(encoder.encode(chunk));
            }
            controller.close();
          },
        });
        return new Response(stream, {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        });
      })
    );

    const stages: string[] = [];
    const deltas: string[] = [];
    const result = await streamAgentChat({
      body: { message: "hi" },
      onStage: (s) => stages.push(s),
      onDelta: (d) => deltas.push(d),
    });
    expect(stages).toEqual(["generating"]);
    expect(deltas).toEqual(["你好", "，大肥鱼"]);
    expect(result).toEqual({ request_id: "req-1", reply_text: "你好，大肥鱼" });

    // Stream error event must NOT fall back to sync POST
    const errorFetch = vi.fn(async () => {
      const stream = new ReadableStream({
        start(controller) {
          controller.enqueue(
            encoder.encode('event: error\ndata: {"status":503,"code":"SERVICE_UNAVAILABLE","message":"上游繁忙"}\n\n')
          );
          controller.close();
        },
      });
      return new Response(stream, {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      });
    });
    vi.stubGlobal("fetch", errorFetch);
    await expect(streamAgentChat({ body: { message: "hi" } })).rejects.toMatchObject({
      streamEvent: true,
      status: 503,
      code: "SERVICE_UNAVAILABLE",
      message: "上游繁忙",
    });
    expect(errorFetch).toHaveBeenCalledTimes(1);

    // Non-stream 405 falls back to sync POST /api/v1/agent/chat
    const fallbackFetch = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ code: "METHOD_NOT_ALLOWED", message: "No SSE" }), {
          status: 405,
          headers: { "Content-Type": "application/json" },
        })
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ request_id: "req-sync", reply_text: "fallback" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      );
    vi.stubGlobal("fetch", fallbackFetch);
    const synced = await streamAgentChat({ body: { message: "hi" } });
    expect(synced).toEqual({ request_id: "req-sync", reply_text: "fallback" });
    expect(fallbackFetch).toHaveBeenCalledTimes(2);
    expect(fallbackFetch.mock.calls[1][0]).toBe("/api/v1/agent/chat");
  });

  it("streams batch-publish SSE progress events and detects incomplete EOF", async () => {
    const encoder = new TextEncoder();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        const stream = new ReadableStream({
          start(controller) {
            controller.enqueue(encoder.encode('event: progress\ndata: {"id":"d1","ok":true}\n\n'));
            controller.enqueue(encoder.encode('event: done\ndata: {"items":[{"id":"d1","ok":true}]}\n\n'));
            controller.close();
          },
        });
        return new Response(stream, {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        });
      })
    );

    const progress: unknown[] = [];
    const done = await streamBatchPublish({
      body: { items: [{ channel: "comments", id: "d1" }] },
      onProgress: (item) => progress.push(item),
    });
    expect(progress).toEqual([{ id: "d1", ok: true }]);
    expect(done).toEqual({ items: [{ id: "d1", ok: true }] });

    // Truncated stream without `done` event raises Chinese disconnect message
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        const stream = new ReadableStream({
          start(controller) {
            controller.enqueue(encoder.encode('event: stage\ndata: "queued"\n\n'));
            controller.close();
          },
        });
        return new Response(stream, {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        });
      })
    );
    await expect(streamAgentChat({ body: { message: "hi" } })).rejects.toThrow("回复连接已中断");
  });
});
