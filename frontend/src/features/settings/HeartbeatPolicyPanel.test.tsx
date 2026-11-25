import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearDirtyRegistry } from "@/shared/lib/dirtyRegistry";
import { HeartbeatPolicyPanel, type HeartbeatEndpointResponse } from "./HeartbeatPolicyPanel";

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("HeartbeatPolicyPanel", () => {
  afterEach(() => {
    clearDirtyRegistry();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("loads status pills, pending badges, suppressions, and saves updated policy via PUT /api/v1/system/heartbeat", async () => {
    let serverState: HeartbeatEndpointResponse = {
      policy: {
        enabled: true,
        tick_interval_seconds: 300,
        pulse_cooldown_seconds: 10800,
        event_cooldown_seconds: 900,
        max_pulses_per_day: 3,
        max_unreplied_pulses: 1,
        hot_topic_dedup_seconds: 604800,
        quiet_hours_start: "23:00",
        quiet_hours_end: "08:30",
        curiosity_research_enabled: true,
        curiosity_max_queries_per_tick: 2,
        surface_to_chat: true,
      },
      status: {
        enabled: true,
        running: true,
        in_quiet_hours: true,
        unreplied_pulses: 1,
        pending_summary: {
          dynamics_pending_review: 2,
          dm_pending: 1,
          inbox_pending_events: 4,
          uncertain_writes: 1,
        },
        recent_suppressions: [
          {
            at: 1760000000,
            reason: "quiet_hours",
            dedup_key: "hot_topic:BV1abc",
            kind: "hot_topic",
            detail: "gated_before_research",
          },
        ],
      },
    };

    const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method || "GET").toUpperCase();
      if (url === "/api/v1/system/heartbeat" && method === "GET") {
        return jsonResponse(serverState);
      }
      if (url === "/api/v1/system/heartbeat" && method === "PUT") {
        const body = JSON.parse(String(init?.body || "{}"));
        serverState = {
          policy: { ...serverState.policy, ...body },
          status: {
            ...serverState.status,
            in_quiet_hours: body.quiet_hours_start ? Boolean(serverState.status?.in_quiet_hours) : false,
          },
        };
        return jsonResponse(serverState);
      }
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", fetchSpy);

    const { container } = render(<HeartbeatPolicyPanel />);

    await waitFor(() => {
      expect(container.querySelector("#heartbeat-running-pill")?.textContent).toBe("运行中");
    });

    expect(container.querySelector("#heartbeat-quiet-pill")?.textContent).toBe("勿扰时段中");
    expect(container.querySelector("#heartbeat-unreplied-pill")?.textContent).toBe("未回脉冲 1");
    expect(container.querySelector("#heartbeat-badge-dynamics")?.textContent).toBe("待审动态 2");
    expect(container.querySelector("#heartbeat-badge-dm")?.textContent).toBe("待处理私信 1");
    expect(container.querySelector("#heartbeat-badge-inbox")?.textContent).toBe("待看互动 4");
    expect(container.querySelector("#heartbeat-badge-uncertain")?.textContent).toBe("待核对发送 1");
    expect(container.querySelector("#heartbeat-suppressions-list")?.textContent).toContain("勿扰时段拦截");
    expect(container.querySelector("#heartbeat-suppressions-list")?.textContent).toContain("hot_topic:BV1abc");

    const maxDailyInput = container.querySelector<HTMLInputElement>('input[name="max_pulses_per_day"]')!;
    const maxUnrepliedInput = container.querySelector<HTMLInputElement>('input[name="max_unreplied_pulses"]')!;
    const quietStartInput = container.querySelector<HTMLInputElement>('input[name="quiet_hours_start"]')!;
    const saveBtn = container.querySelector<HTMLButtonElement>("#heartbeat-policy-save")!;

    expect(saveBtn.disabled).toBe(true);
    fireEvent.change(maxDailyInput, { target: { value: "0" } });
    fireEvent.change(maxUnrepliedInput, { target: { value: "0" } });
    fireEvent.change(quietStartInput, { target: { value: "" } });
    expect(saveBtn.disabled).toBe(false);

    fireEvent.submit(container.querySelector("#heartbeat-policy-form")!);

    await waitFor(() => {
      expect(screen.getByRole("status").textContent).toContain("心跳策略已保存");
    });

    const putCall = fetchSpy.mock.calls.find(
      ([u, init]) => String(u) === "/api/v1/system/heartbeat" && init?.method === "PUT"
    );
    expect(putCall).toBeTruthy();
    const sent = JSON.parse(String(putCall?.[1]?.body || "{}"));
    expect(sent.max_pulses_per_day).toBe(0);
    expect(sent.max_unreplied_pulses).toBe(0);
    expect(sent.quiet_hours_start).toBe("");
    expect(container.querySelector("#heartbeat-quiet-pill")?.textContent).toBe("正常时段");
  });
});
