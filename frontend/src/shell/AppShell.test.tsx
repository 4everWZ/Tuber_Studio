import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiGet } from "@/shared/api/client";
import { registerDirtySource, clearDirtyRegistry } from "@/shared/lib/dirtyRegistry";
import { AppShell } from "./AppShell";

class FakeWebSocket {
  static OPEN = 1;
  readyState = FakeWebSocket.OPEN;
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  close = vi.fn();

  constructor(_url: string) {
    setTimeout(() => {
      this.onopen?.();
    }, 0);
  }
}

describe("AppShell", () => {
  beforeEach(() => {
    window.location.hash = "#chat";
    clearDirtyRegistry();
    vi.stubGlobal("WebSocket", FakeWebSocket);
    window.scrollTo = vi.fn();
  });

  afterEach(() => {
    clearDirtyRegistry();
    vi.unstubAllGlobals();
  });

  it("authenticates into workspace, shows status pills, enforces beforeunload dirty guard, and falls back to LoginGate on 401", async () => {
    let return401 = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (return401) {
          return new Response(JSON.stringify({ code: "UNAUTHORIZED", message: "过期", details: null }), {
            status: 401,
            headers: { "Content-Type": "application/json" },
          });
        }
        if (url.endsWith("/api/v1/system/settings")) {
          return Response.json({ local_access: false, omni: { model: "demo" } });
        }
        if (url.endsWith("/api/v1/system/status")) {
          return Response.json({
            state: "stopped",
            character: { id: "dafeiyu", display_name: "大肥鱼" },
            context: { capacity_source: "model_card", verified: true },
          });
        }
        if (url.endsWith("/api/v1/system/settings/performance")) {
          return Response.json({ pets_enabled: true, avatar_enabled: false });
        }
        if (url.endsWith("/api/v1/operations/automation")) {
          return Response.json({ state: "off", policy: { enabled: false } });
        }
        if (url.endsWith("/api/v1/operations/account/bilibili")) {
          return Response.json({ state: "logged_in", profile: { name: "测试号" } });
        }
        if (url.endsWith("/api/v1/operations/discovery/records")) {
          return Response.json({ issue_history: [] });
        }
        return Response.json({});
      })
    );

    render(<AppShell renderRoute={(route) => <div data-testid="route-slot">{route.page}</div>} />);

    await waitFor(() => {
      expect(screen.getByTestId("route-slot")).toHaveTextContent("chat");
    });

    expect(document.getElementById("workspace-character")?.textContent).toBe("大肥鱼");
    expect(document.getElementById("service-status-capsule")?.textContent).toContain("已连接");

    // Verify beforeunload dirty registry protection
    const unregister = registerDirtySource("test-form", () => true);
    const dirtyEvent = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(dirtyEvent);
    expect(dirtyEvent.defaultPrevented).toBe(true);

    unregister();
    const cleanEvent = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(cleanEvent);
    expect(cleanEvent.defaultPrevented).toBe(false);

    // 401 from any API call transitions back to LoginGate
    return401 = true;
    await act(async () => {
      await expect(apiGet("/api/v1/system/status")).rejects.toMatchObject({ status: 401 });
    });

    await waitFor(() => {
      expect(document.getElementById("login-form")).not.toBeNull();
    });
  });

  it("keeps polling characterDisplayStore even when WS status pushes arrive every second", async () => {
    vi.useFakeTimers();
    let performanceFetches = 0;
    let lastSocket: FakeWebSocket | null = null;
    class TrackingWebSocket extends FakeWebSocket {
      constructor(url: string) {
        super(url);
        // eslint-disable-next-line @typescript-eslint/no-this-alias
        lastSocket = this;
      }
    }
    vi.stubGlobal("WebSocket", TrackingWebSocket);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/system/settings")) {
          return Response.json({ local_access: false, omni: { model: "demo" } });
        }
        if (url.endsWith("/api/v1/system/status")) {
          return Response.json({
            state: "stopped",
            character: { id: "dafeiyu", display_name: "大肥鱼" },
            context: { capacity_source: "model_card", verified: true },
          });
        }
        if (url.endsWith("/api/v1/system/settings/performance")) {
          performanceFetches += 1;
          return Response.json({ pets_enabled: performanceFetches === 1, avatar_enabled: false });
        }
        return Response.json({});
      })
    );

    try {
      render(<AppShell renderRoute={(route) => <div data-testid="route-slot">{route.page}</div>} />);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(50);
      });
      expect(screen.getByTestId("route-slot")).toHaveTextContent("chat");
      const initialFetches = performanceFetches;

      // Simulate 1s /ws/admin status pushes arriving while the 2.5s poll runs.
      for (let i = 0; i < 3; i += 1) {
        await act(async () => {
          lastSocket?.onmessage?.(
            new MessageEvent("message", {
              data: JSON.stringify({
                type: "status",
                data: {
                  state: "stopped",
                  character: { id: "dafeiyu", display_name: "大肥鱼" },
                  context: { capacity_source: "model_card", verified: true },
                },
              }),
            })
          );
          await vi.advanceTimersByTimeAsync(1000);
        });
      }

      expect(performanceFetches).toBeGreaterThan(initialFetches);
    } finally {
      vi.useRealTimers();
    }
  });
});
