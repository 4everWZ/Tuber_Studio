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
      character: { id: "whale", version: 1, name: "Aqua" },
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
            character: "Aqua",
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

  it("renders tool_activity capsules, clickable memory_updated pill dispatching aqua:open-manage-memories, and calm delegation statuses", async () => {
    const memoryEvents: Array<Record<string, unknown>> = [];
    const onMemoryOpen = (event: Event) => {
      memoryEvents.push((event as CustomEvent<Record<string, unknown>>).detail);
    };
    window.addEventListener("aqua:open-manage-memories", onMemoryOpen);

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.startsWith("/api/v1/agent/chat?limit=")) {
          return new Response(
            JSON.stringify({
              character: "Aqua",
              character_id: "whale",
              turns: [
                {
                  request_id: "calm-task-1",
                  message: "整理一下材料",
                  reply_text: "先停在这里。",
                  reply_format: "plain",
                  card: {
                    type: "agent_task",
                    data: {
                      id: "task-calm",
                      status: "failed",
                      message: "并行委派只允许独立读取。工坊号主请补充说明。",
                      steps: [],
                    },
                  },
                },
              ],
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
          return new Response("{}", { status: 404, headers: { "Content-Type": "application/json" } });
        }
        if (url === "/api/v1/agent/chat/stream") {
          const body = JSON.parse(String(init?.body)) as { request_id: string };
          return sseResponse([
            'event: stage\ndata: "working"\n\n',
            `event: tool_activity\ndata: ${JSON.stringify({
              id: `act:${body.request_id}:save_memory`,
              tool: "save_memory",
              action: "save_memory",
              label: "更新长期记忆",
              status: "running",
            })}\n\n`,
            `event: memory_updated\ndata: ${JSON.stringify({
              action: "created",
              scope: "owner",
              character_id: "whale",
              memory_id: 42,
              snippet: "喜欢喝无糖乌龙茶",
              count: 12,
              limit: 100,
              soft_limit_reached: false,
            })}\n\n`,
            `event: tool_activity\ndata: ${JSON.stringify({
              id: `act:${body.request_id}:save_memory`,
              tool: "save_memory",
              action: "save_memory",
              label: "更新长期记忆",
              status: "completed",
              summary: "好，这件事我已经记下来了。",
            })}\n\n`,
            `event: done\ndata: ${JSON.stringify({
              request_id: body.request_id,
              reply_text: "好，我已经记下你喜欢无糖乌龙茶啦。",
              reply_format: "markdown",
              card: {
                type: "memory_change",
                data: { target: "已保存记忆", text: "喜欢喝无糖乌龙茶" },
              },
            })}\n\n`,
          ]);
        }
        throw new Error(`Unexpected request: ${url}`);
      })
    );

    try {
      const { container } = render(
        <AppSessionProvider value={makeSession()}>
          <ChatPage />
        </AppSessionProvider>
      );

      // Calm task card sanitizes raw internal guard & "工坊号主" and never shows raw "失败"
      await waitFor(() => {
        expect(container.querySelector('[data-request-id="calm-task-1"]')).not.toBeNull();
      });
      const calmCard = container.querySelector('[data-request-id="calm-task-1"] .operator-chat-card')!;
      expect(calmCard.textContent).toContain("暂未完成");
      expect(calmCard.textContent).toContain("这几步不能同时改动状态");
      expect(calmCard.textContent).not.toContain("工坊号主");
      expect(calmCard.textContent).not.toContain("失败");

      // Stream turn with tool_activity + memory_updated
      const input = container.querySelector<HTMLTextAreaElement>("#operator-chat-input")!;
      fireEvent.change(input, { target: { value: "记住我喜欢无糖乌龙茶" } });
      fireEvent.submit(container.querySelector<HTMLFormElement>("#operator-chat-form")!);

      await waitFor(() => {
        expect(screen.getByText("好，我已经记下你喜欢无糖乌龙茶啦。")).toBeInTheDocument();
      });

      // Progressive disclosure "思考与工具" capsule
      expect(screen.getByText(/思考与工具 · 更新长期记忆/)).toBeInTheDocument();
      expect(screen.getByText("更新长期记忆")).toBeInTheDocument();

      // Clickable "已更新记忆" pill under assistant message (and suppressed duplicate memory_change card)
      const memPill = screen.getByRole("button", { name: "已更新记忆" });
      expect(memPill.getAttribute("title")).toContain("喜欢喝无糖乌龙茶");
      expect(document.querySelectorAll(".chat-memory-pill")).toHaveLength(1);
      expect(document.querySelector(".chat-memory-fallback-link")?.getAttribute("href")).toBe("#personalization");

      fireEvent.click(memPill);
      expect(memoryEvents).toEqual([
        {
          memoryId: 42,
          snippet: "喜欢喝无糖乌龙茶",
          scope: "owner",
        },
      ]);
    } finally {
      window.removeEventListener("aqua:open-manage-memories", onMemoryOpen);
    }
  });

  it("syncs 开始/停止运营 quick controls cleanly with live starting and running states", async () => {
    const submittedActions: string[] = [];
    const syncEvents: Array<Record<string, unknown>> = [];
    const onSync = (event: Event) => {
      syncEvents.push((event as CustomEvent<Record<string, unknown>>).detail);
    };
    window.addEventListener("aqua:automation-status", onSync);

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.startsWith("/api/v1/agent/chat?limit=")) {
          return new Response(
            JSON.stringify({ character: "Aqua", character_id: "whale", turns: [] }),
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
          const body = JSON.parse(String(init?.body)) as {
            request_id: string;
            intent?: { action?: string };
          };
          const action = body.intent?.action || "";
          submittedActions.push(action);
          if (action === "start_operations") {
            return sseResponse([
              `event: tool_activity\ndata: ${JSON.stringify({
                id: `act:${body.request_id}:start_operations`,
                tool: "start_operations",
                label: "开启自主运营",
                status: "completed",
                data: { automation_state: "running" },
              })}\n\n`,
              `event: done\ndata: ${JSON.stringify({
                request_id: body.request_id,
                reply_text: "自主运营已经开启，状态已同步。",
                card: {
                  type: "operation_control",
                  data: { state: "running", policy: { enabled: true } },
                },
              })}\n\n`,
            ]);
          }
          return sseResponse([
            `event: done\ndata: ${JSON.stringify({
              request_id: body.request_id,
              reply_text: "已请求自主运营收尾。",
              card: {
                type: "operation_control",
                data: { state: "off", policy: { enabled: false } },
              },
            })}\n\n`,
          ]);
        }
        throw new Error(`Unexpected request: ${url}`);
      })
    );

    try {
      const { container, rerender } = render(
        <AppSessionProvider
          value={makeSession({
            status: {
              character: { id: "whale", version: 1, name: "Aqua" },
              context: { capacity_source: "model_card", verified: true },
              automation: { state: "starting", enabled: false },
            },
          })}
        >
          <ChatPage />
        </AppSessionProvider>
      );

      // While automation is "starting", quick control shows "运营启动中…" and stays disabled
      const quickBtn = container.querySelector<HTMLButtonElement>("#operator-chat-ops-quick")!;
      await waitFor(() => {
        expect(quickBtn.textContent).toBe("运营启动中…");
      });
      expect(quickBtn.disabled).toBe(true);
      expect(quickBtn.dataset.state).toBe("starting");

      // When live session updates to "off", quick control offers "开始运营"
      rerender(
        <AppSessionProvider
          value={makeSession({
            status: {
              character: { id: "whale", version: 1, name: "Aqua" },
              context: { capacity_source: "model_card", verified: true },
              automation: { state: "off", enabled: false },
            },
          })}
        >
          <ChatPage />
        </AppSessionProvider>
      );

      await waitFor(() => {
        expect(quickBtn.textContent).toBe("开始运营");
        expect(quickBtn.disabled).toBe(false);
      });

      fireEvent.click(quickBtn);
      await waitFor(() => {
        expect(quickBtn.textContent).toBe("停止运营");
      });
      expect(submittedActions).toEqual(["start_operations"]);
      expect(syncEvents.some((d) => (d.automation as { state?: string })?.state === "running")).toBe(true);
      expect(screen.getByRole("button", { name: "停止自主运营" })).toBeInTheDocument();

      // Clicking "停止运营" sends stop intent
      fireEvent.click(quickBtn);
      await waitFor(() => {
        expect(quickBtn.textContent).toBe("开始运营");
      });
      expect(submittedActions).toEqual(["start_operations", "stop"]);
    } finally {
      window.removeEventListener("aqua:automation-status", onSync);
    }
  });

  it("renders calm constrained tool_activity and sanitizes TaskActivityDrawer step errors", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.startsWith("/api/v1/agent/chat?limit=")) {
          return new Response(
            JSON.stringify({ character: "Aqua", character_id: "whale", turns: [] }),
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
          const body = JSON.parse(String(init?.body)) as { request_id: string };
          return sseResponse([
            'event: stage\ndata: "constrained"\n\n',
            `event: tool_activity\ndata: ${JSON.stringify({
              id: `act:${body.request_id}:web_lookup`,
              tool: "web_search",
              label: "联网检索",
              status: "constrained",
              summary: "ValidationError: pydantic schema mismatch",
            })}\n\n`,
            `event: done\ndata: ${JSON.stringify({
              request_id: body.request_id,
              reply_text: "这一步需要调整条件，请补充说明后再试。",
              reply_format: "plain",
            })}\n\n`,
          ]);
        }
        if (url === "/api/v1/agent/tasks") {
          return new Response(
            JSON.stringify({
              tasks: [
                {
                  id: "task-drawer-1",
                  goal: "检查视频片段",
                  status: "failed",
                  revision: 1,
                  steps: [],
                },
              ],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          );
        }
        if (url === "/api/v1/agent/tasks/task-drawer-1") {
          return new Response(
            JSON.stringify({
              id: "task-drawer-1",
              goal: "检查视频片段",
              status: "failed",
              revision: 1,
              message: "Traceback (most recent call last): internal",
              steps: [
                {
                  id: "step-1",
                  tool: "workshop_lookup",
                  result: { error: "工坊号主请求超出本轮委派范围" },
                },
              ],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          );
        }
        if (url === "/api/v1/operations/account/bilibili") {
          return new Response(JSON.stringify({ state: "logged_in", profile: { name: "测试" } }), {
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

    const input = container.querySelector<HTMLTextAreaElement>("#operator-chat-input")!;
    fireEvent.change(input, { target: { value: "查一下今天新闻" } });
    fireEvent.submit(container.querySelector<HTMLFormElement>("#operator-chat-form")!);

    await waitFor(() => {
      expect(screen.getByText("这一步需要调整条件，请补充说明后再试。")).toBeInTheDocument();
    });
    expect(screen.getByText(/思考与工具 · 联网检索/)).toBeInTheDocument();
    expect(screen.getByText("已暂缓")).toBeInTheDocument();
    expect(screen.getByText("操作参数没能匹配当前要求，可以调整说法后再试。")).toBeInTheDocument();
    expect(container.textContent).not.toContain("ValidationError");

    fireEvent.click(container.querySelector("#chat-activity-toggle")!);
    await waitFor(() => {
      expect(screen.getByText("任务与权限")).toBeInTheDocument();
      expect(screen.getByText("检查视频片段")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("检查视频片段"));
    await waitFor(() => {
      expect(screen.getByText("查询历史记录 · 暂未取得有效结果")).toBeInTheDocument();
    });
    const drawer = container.querySelector("#chat-activity")!;
    expect(drawer.textContent).toContain("未完成");
    expect(drawer.textContent).toContain("操作参数没能匹配当前要求，可以调整说法后再试。");
    expect(drawer.textContent).not.toContain("工坊面板");
    expect(drawer.textContent).not.toContain("Traceback");
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
            JSON.stringify({ character: "Aqua", character_id: "whale", turns: [] }),
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
      expect(container.querySelector("#operator-chat-title")!.textContent).toBe("Aqua");
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
            JSON.stringify({ character: "Aqua", character_id: "whale", turns: [] }),
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
          return new Response(JSON.stringify({ state: "logged_in", profile: { name: "主人" } }), {
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
              character: "Aqua",
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
          return new Response(JSON.stringify({ configured: true, character: "Aqua" }), {
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
    expect(container.querySelector("#private-call-title")!.textContent).toBe("与 Aqua 通话");
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
