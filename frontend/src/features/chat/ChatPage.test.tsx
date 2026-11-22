import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AppSessionProvider, type AppSessionValue } from "@/shared/hooks/useAppSession";
import { characterDisplayStore } from "@/shared/hooks/useCharacterDisplay";
import { ChatPage } from "./ChatPage";

function sseResponse(frames: string[]): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const frame of frames) {
        controller.enqueue(encoder.encode(frame));
      }
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

function makeSession(overrides: Partial<AppSessionValue> = {}): AppSessionValue {
  return {
    authenticated: true,
    localAccess: true,
    wsConnected: true,
    status: {
      character: { id: "whale", version: 1, name: "大肥鱼" },
      context: { capacity_source: "model_card", verified: true },
    },
    settings: { omni: { model: "qwen3-omni" } },
    refreshStatus: async () => null,
    refreshSettings: async () => null,
    navigate: vi.fn(),
    logout: async () => {},
    onUnauthorized: vi.fn(),
    ...overrides,
  };
}

function installDialogPolyfill() {
  if (!HTMLDialogElement.prototype.showModal) {
    HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
      this.open = true;
    };
  }
  if (!HTMLDialogElement.prototype.close) {
    HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) {
      this.open = false;
      this.dispatchEvent(new Event("close"));
    };
  }
}

describe("ChatPage", () => {
  beforeEach(() => {
    installDialogPolyfill();
    characterDisplayStore.reset();
    characterDisplayStore.publish({ pets_enabled: true, avatar_enabled: false });
    vi.stubGlobal("matchMedia", () => ({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }));
  });

  afterEach(() => {
    characterDisplayStore.reset();
    vi.unstubAllGlobals();
  });

  it("streams stage/delta/done SSE replies, falls back on 405, and preserves request_id on incomplete EOF retry", async () => {
    const chatStreamRequests: Array<{ request_id: string; message: string }> = [];
    let streamBehavior: "sse" | "405" | "eof" | "retry_ok" = "sse";

    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/v1/agent/chat?limit=")) {
        return new Response(
          JSON.stringify({
            character: "大肥鱼",
            character_id: "whale",
            character_version: 1,
            avatar: "/assets/whale/idle.gif",
            turns: [
              {
                request_id: "initial-1",
                message: "之前聊到哪了",
                reply_text: "之前在看评论区。",
                reply_format: "markdown",
              },
            ],
            next_cursor: null,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url === "/api/v1/agent/attention") {
        return new Response(JSON.stringify({ count: 0 }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url === "/api/v1/agent/private-call") {
        return new Response(JSON.stringify({ configured: false }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url === "/api/v1/agent/chat/events") {
        return new Response(JSON.stringify({ message: "not streaming in this test" }), {
          status: 404,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url === "/api/v1/agent/chat/stream") {
        const body = JSON.parse(String(init?.body)) as { request_id: string; message: string };
        chatStreamRequests.push(body);
        if (streamBehavior === "sse") {
          return sseResponse([
            'event: stage\ndata: "generating"\n\n',
            'event: delta\ndata: "你好"\n\n',
            'event: delta\ndata: "世界"\n\n',
            `event: done\ndata: ${JSON.stringify({
              request_id: body.request_id,
              reply_text: "你好世界",
              reply_format: "markdown",
            })}\n\n`,
          ]);
        }
        if (streamBehavior === "405") {
          return new Response(JSON.stringify({ message: "Method Not Allowed" }), {
            status: 405,
            headers: { "Content-Type": "application/json" },
          });
        }
        if (streamBehavior === "eof") {
          streamBehavior = "retry_ok";
          return sseResponse(['event: delta\ndata: "半截"\n\n']);
        }
        return sseResponse([
          `event: done\ndata: ${JSON.stringify({
            request_id: body.request_id,
            reply_text: "重试成功",
            reply_format: "plain",
          })}\n\n`,
        ]);
      }
      if (url === "/api/v1/agent/chat" && init?.method === "POST") {
        const body = JSON.parse(String(init.body)) as { request_id: string; message: string };
        return new Response(
          JSON.stringify({
            request_id: body.request_id,
            reply_text: "同步降级回复",
            reply_format: "plain",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const { container } = render(
      <AppSessionProvider value={makeSession()}>
        <ChatPage />
      </AppSessionProvider>
    );

    await waitFor(() => {
      expect(container.querySelector('[data-request-id="initial-1"]')).not.toBeNull();
    });

    // Initial render must not perform unrequested meme, pet gallery, automation, or bilibili account fetches
    const extraHomeUrls = fetchMock.mock.calls
      .map(([u]) => String(u))
      .filter(
        (u) =>
          u.includes("/memes") ||
          u.includes("/pets/") ||
          u.includes("/operations/automation") ||
          u.includes("/operations/account/bilibili")
      );
    expect(extraHomeUrls).toEqual([]);

    // 1. Streaming SSE turn
    const input = container.querySelector<HTMLTextAreaElement>("#operator-chat-input")!;
    const form = container.querySelector<HTMLFormElement>("#operator-chat-form")!;
    fireEvent.change(input, { target: { value: "早上好" } });
    fireEvent.submit(form);

    await waitFor(() => {
      expect(screen.getByText("你好世界")).toBeInTheDocument();
    });

    // 2. Fallback to POST /api/v1/agent/chat ONLY on HTTP 405
    streamBehavior = "405";
    fireEvent.change(input, { target: { value: "走同步接口" } });
    fireEvent.submit(form);

    await waitFor(() => {
      expect(screen.getByText("同步降级回复")).toBeInTheDocument();
    });

    // 3. Incomplete EOF shows "回复连接已中断" and preserves request_id on retry
    streamBehavior = "eof";
    fireEvent.change(input, { target: { value: "重发同一条" } });
    fireEvent.submit(form);

    await waitFor(() => {
      expect(container.querySelector("#operator-chat-status")!.textContent).toContain("回复连接已中断");
    });
    const retryBtn = container.querySelector<HTMLButtonElement>(".operator-chat-retry")!;
    expect(retryBtn).not.toBeNull();

    fireEvent.click(retryBtn);
    await waitFor(() => {
      expect(screen.getByText("重试成功")).toBeInTheDocument();
    });

    const retried = chatStreamRequests.filter((r) => r.message === "重发同一条");
    expect(retried).toHaveLength(2);
    expect(retried[0].request_id).toBe(retried[1].request_id);
  });

  it("stops generation on demand via stop-generation endpoint", async () => {
    let finishStream: (() => void) | null = null;
    let stoppedRequestId: string | null = null;

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.startsWith("/api/v1/agent/chat?limit=")) {
          return new Response(
            JSON.stringify({ character: "大肥鱼", character_id: "whale", turns: [] }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          );
        }
        if (url === "/api/v1/agent/attention") {
          return new Response(JSON.stringify({ count: 0 }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        if (url === "/api/v1/agent/private-call") {
          return new Response(JSON.stringify({ configured: false }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        if (url === "/api/v1/agent/chat/events") {
          return new Response("{}", { status: 404, headers: { "Content-Type": "application/json" } });
        }
        if (url === "/api/v1/agent/chat/stream") {
          const encoder = new TextEncoder();
          const stream = new ReadableStream<Uint8Array>({
            start(controller) {
              controller.enqueue(encoder.encode('event: stage\ndata: "generating"\n\n'));
              finishStream = () => {
                controller.enqueue(encoder.encode(`event: done\ndata: ${JSON.stringify({ stopped: true })}\n\n`));
                controller.close();
              };
            },
          });
          return new Response(stream, {
            status: 200,
            headers: { "Content-Type": "text/event-stream" },
          });
        }
        const match = /^\/api\/v1\/agent\/chat\/turns\/([^/]+)\/stop-generation$/.exec(url);
        if (match) {
          stoppedRequestId = match[1];
          setTimeout(() => finishStream?.(), 0);
          return new Response(JSON.stringify({ stopping: true }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        throw new Error(`Unexpected request: ${url}`);
      })
    );

    const { container } = render(
      <AppSessionProvider value={makeSession()}>
        <ChatPage />
      </AppSessionProvider>
    );

    await waitFor(() => {
      expect(container.querySelector("#operator-chat-title")!.textContent).toBe("大肥鱼");
    });

    const input = container.querySelector<HTMLTextAreaElement>("#operator-chat-input")!;
    fireEvent.change(input, { target: { value: "生成一条长回复" } });
    fireEvent.submit(container.querySelector<HTMLFormElement>("#operator-chat-form")!);

    const sendBtn = container.querySelector<HTMLButtonElement>("#operator-chat-send")!;
    await waitFor(() => {
      expect(sendBtn.dataset.generating).toBe("true");
    });

    fireEvent.click(sendBtn);

    await waitFor(() => {
      expect(container.querySelector("#operator-chat-progress")!.textContent).toBe("已停止生成。");
    });
    expect(stoppedRequestId).toBeTruthy();
  });

  it("delivers shared background/voice-call task completions via /chat/events SSE and displays them in TaskActivityDrawer", async () => {
    let pushDelivery: ((frame: string) => void) | null = null;
    const closeStreamRef = { close: () => {} };

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.startsWith("/api/v1/agent/chat?limit=")) {
          return new Response(
            JSON.stringify({ character: "大肥鱼", character_id: "whale", turns: [] }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          );
        }
        if (url === "/api/v1/agent/attention") {
          return new Response(JSON.stringify({ count: 1 }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        if (url === "/api/v1/agent/private-call") {
          return new Response(JSON.stringify({ configured: false }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        if (url === "/api/v1/agent/chat/events") {
          const encoder = new TextEncoder();
          const stream = new ReadableStream<Uint8Array>({
            start(controller) {
              controller.enqueue(encoder.encode('event: ready\ndata: {"ok":true}\n\n'));
              pushDelivery = (frame: string) => controller.enqueue(encoder.encode(frame));
              closeStreamRef.close = () => {
                try {
                  controller.close();
                } catch {
                  // Already closed.
                }
              };
            },
          });
          return new Response(stream, {
            status: 200,
            headers: { "Content-Type": "text/event-stream" },
          });
        }
        if (url === "/api/v1/agent/tasks") {
          return new Response(
            JSON.stringify({
              tasks: [
                {
                  id: "task-9b-call",
                  goal: "通话交办：核对粉丝热评",
                  status: "completed",
                  revision: 2,
                  steps: [],
                },
              ],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          );
        }
        if (url === "/api/v1/operations/account/bilibili") {
          return new Response(JSON.stringify({ state: "logged_in", profile: { name: "号主" } }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        throw new Error(`Unexpected request: ${url}`);
      })
    );

    const { container, unmount } = render(
      <AppSessionProvider value={makeSession()}>
        <ChatPage />
      </AppSessionProvider>
    );

    await waitFor(() => {
      expect(pushDelivery).not.toBeNull();
    });

    act(() => {
      pushDelivery!(
        `event: delivery\ndata: ${JSON.stringify({
          request_id: "bg-delivery-9b",
          assistant_only: true,
          reply_text: "通话中交办的热评统计已完成。",
          card: {
            type: "task_delivery",
            data: {
              task_id: "task-9b-call",
              revision: 2,
              status: "completed",
              goal: "通话交办：核对粉丝热评",
            },
          },
        })}\n\n`
      );
    });

    await waitFor(() => {
      expect(container.querySelector('[data-request-id="bg-delivery-9b"]')).not.toBeNull();
      expect(screen.getByText("通话中交办的热评统计已完成。")).toBeInTheDocument();
    });

    fireEvent.click(container.querySelector("#chat-activity-toggle")!);
    await waitFor(() => {
      expect(screen.getByText("通话交办：核对粉丝热评")).toBeInTheDocument();
    });

    closeStreamRef.close();
    unmount();
  });

  it("opens #private-call-dialog with zero WHEP/avatar downloads or video/canvas when avatar profiles=[]", async () => {
    const rtcSpy = vi.fn();
    vi.stubGlobal("isSecureContext", true);
    vi.stubGlobal("RTCPeerConnection", rtcSpy);
    vi.stubGlobal("AudioWorkletNode", vi.fn());
    vi.stubGlobal("AudioContext", vi.fn());
    Object.defineProperty(window.navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn() },
    });

    const requestedUrls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        requestedUrls.push(url);
        if (url.startsWith("/api/v1/agent/chat?limit=")) {
          return new Response(
            JSON.stringify({
              character: "大肥鱼",
              character_id: "whale",
              avatar: "/assets/whale/idle.gif",
              turns: [],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          );
        }
        if (url === "/api/v1/agent/attention") {
          return new Response(JSON.stringify({ count: 0 }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        if (url === "/api/v1/agent/chat/events") {
          return new Response("{}", { status: 404, headers: { "Content-Type": "application/json" } });
        }
        if (url === "/api/v1/agent/private-call") {
          return new Response(JSON.stringify({ configured: true, character: "大肥鱼" }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        if (url === "/api/v1/agent/private-call/avatars") {
          return new Response(JSON.stringify({ profiles: [] }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        throw new Error(`Unexpected request: ${url}`);
      })
    );

    const { container } = render(
      <AppSessionProvider value={makeSession()}>
        <ChatPage />
      </AppSessionProvider>
    );

    const openCallBtn = container.querySelector<HTMLButtonElement>("#private-call-open")!;
    await waitFor(() => {
      expect(openCallBtn.hidden).toBe(false);
      expect(openCallBtn.disabled).toBe(false);
    });

    fireEvent.click(openCallBtn);

    const dialog = container.querySelector<HTMLDialogElement>("#private-call-dialog")!;
    await waitFor(() => {
      expect(dialog.open).toBe(true);
      expect(requestedUrls).toContain("/api/v1/agent/private-call/avatars");
    });

    expect(openCallBtn.getAttribute("aria-label")).toBe("私有语音通话");
    expect(container.querySelector("#private-call-title")!.textContent).toBe("与 大肥鱼 通话");
    expect(container.querySelector("#private-call-start")!.textContent).toBe("开始通话");
    expect(container.querySelector("#private-call-review summary")!.textContent).toBe("查看通话转写");
    expect(container.querySelector("#private-call-pet-slot #chat-pet")).not.toBeNull();
    expect(document.querySelectorAll("#chat-pet")).toHaveLength(1);

    // Zero avatar rendering & zero WHEP/avatar downloads on baseline voice-only response
    expect(dialog.querySelector("video")).toBeNull();
    expect(dialog.querySelector("canvas")).toBeNull();
    expect(rtcSpy).not.toHaveBeenCalled();
    expect(
      requestedUrls.some((u) => u.includes("/whep") || u.includes("/private-call/avatars/"))
    ).toBe(false);
  });
});
