import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearDirtyRegistry, hasAnyUnsavedChanges } from "@/shared/lib/dirtyRegistry";
import { LiveInteractionPage } from "./LiveInteractionPage";

function buildBaseRules(overrides: Record<string, unknown> = {}) {
  return {
    song_request: { mode: "free", min_coin: 0, gift_names: [] },
    draw_and_guess: {
      enabled: true,
      trigger_mode: "gift_only",
      min_coin: 0,
      round_duration_sec: 30,
      gift_names: [],
    },
    game_livestream: {
      enabled: true,
      active_game: "stardew_valley",
    },
    ...overrides,
  };
}

describe("LiveInteractionPage", () => {
  beforeEach(() => {
    clearDirtyRegistry();
  });

  afterEach(() => {
    clearDirtyRegistry();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("preserves zero thresholds, protects unsaved rules from refresh, and drops stale rule loads", async () => {
    const writes: Array<{ url: string; body: unknown }> = [];
    let resolveRulesFirst: ((res: Response) => void) | null = null;
    let rulesCallCount = 0;

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method || "GET";
        if (url.endsWith("/api/v1/live/interactive-rules") && method === "GET") {
          rulesCallCount += 1;
          if (rulesCallCount === 1) {
            return new Promise<Response>((resolve) => {
              resolveRulesFirst = resolve;
            });
          }
          return Response.json(buildBaseRules());
        }
        if (url.endsWith("/api/v1/live/interactive-rules") && method === "PUT") {
          const parsed = JSON.parse(String(init?.body || "{}"));
          writes.push({ url, body: parsed });
          return Response.json(parsed);
        }
        if (url.endsWith("/api/v1/live/games/status")) {
          return Response.json({
            active_game_id: "stardew_valley",
            owner: "studio",
            active_status: { display_name: "星露谷物语", summary: "农场中", current_activity: "浇水" },
          });
        }
        if (url.endsWith("/api/v1/live/games/device/stardew")) {
          return Response.json({ configured: false, online: false, state: null });
        }
        if (url.endsWith("/api/v1/live/draw-and-guess/status")) {
          return Response.json({ active: false, state: "idle" });
        }
        if (url.endsWith("/api/v1/live/draw-and-guess/start") && method === "POST") {
          const parsed = JSON.parse(String(init?.body || "{}"));
          writes.push({ url, body: parsed });
          return Response.json({ status: "started", spoken_text: "开始画画！" });
        }
        return Response.json({});
      })
    );

    const { rerender } = render(<LiveInteractionPage visible />);

    // Trigger a newer rules fetch before the first slow GET resolves
    rerender(<LiveInteractionPage visible={false} />);
    rerender(<LiveInteractionPage visible />);

    const dgCoinInput = document.getElementById("rule-dg-coin") as HTMLInputElement;
    await waitFor(() => {
      expect(dgCoinInput.value).toBe("0");
    });

    // Resolve the stale first GET with a different min_coin — must be ignored
    await act(async () => {
      resolveRulesFirst?.(
        Response.json(
          buildBaseRules({
            draw_and_guess: {
              enabled: true,
              trigger_mode: "gift_only",
              min_coin: 99,
              round_duration_sec: 60,
              gift_names: [],
            },
          })
        )
      );
    });
    expect(dgCoinInput.value).toBe("0");

    // User modifies input -> dirty flag guards against re-fetch overwrites
    fireEvent.change(dgCoinInput, { target: { value: "9" } });
    expect(hasAnyUnsavedChanges()).toBe(true);

    rerender(<LiveInteractionPage visible={false} />);
    rerender(<LiveInteractionPage visible />);
    await waitFor(() => {
      expect(rulesCallCount).toBeGreaterThanOrEqual(3);
    });
    expect(dgCoinInput.value).toBe("9");

    // Reset to "0" and save -> preserves 0 and game_livestream, clears dirty
    fireEvent.change(dgCoinInput, { target: { value: "0" } });
    fireEvent.submit(document.getElementById("interactive-rules-form") as HTMLFormElement);

    await waitFor(() => {
      expect(document.getElementById("interactive-rules-state")?.textContent).toBe("互动规则已保存。");
    });

    expect(writes[0]?.url).toBe("/api/v1/live/interactive-rules");
    expect(
      (writes[0]?.body as { draw_and_guess: { min_coin: number } }).draw_and_guess.min_coin
    ).toBe(0);
    expect((writes[0]?.body as { game_livestream: { active_game: string } }).game_livestream.active_game).toBe(
      "stardew_valley"
    );
    expect(hasAnyUnsavedChanges()).toBe(false);

    // Start draw-and-guess uses round_duration_sec = 30 from rules
    fireEvent.click(document.getElementById("dg-start-btn") as HTMLButtonElement);
    await waitFor(() => {
      expect(writes.some((w) => w.url === "/api/v1/live/draw-and-guess/start")).toBe(true);
    });
    const dgWrite = writes.find((w) => w.url === "/api/v1/live/draw-and-guess/start");
    expect((dgWrite?.body as { duration_sec: number }).duration_sec).toBe(30);
  });

  it("handles game switch, control, step, draw-and-guess stop, and Stardew device receipt polling", async () => {
    const calls: Array<{ url: string; method: string; body?: unknown }> = [];
    let dgActive = true;
    let currentOwner = "studio";
    let currentGame = "stardew_valley";
    let receiptPolls = 0;

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method || "GET";
        const body = init?.body ? JSON.parse(String(init.body)) : undefined;
        calls.push({ url, method, body });

        if (url.endsWith("/api/v1/live/interactive-rules")) {
          return Response.json(buildBaseRules());
        }
        if (url.endsWith("/api/v1/live/games/status")) {
          return Response.json({
            active_game_id: currentGame,
            owner: currentOwner,
            active_status: {
              display_name: currentGame === "pvz" ? "植物大战僵尸" : "星露谷物语",
              summary: currentGame === "pvz" ? "草坪防线" : "农场第5天",
              current_activity: "待命",
            },
          });
        }
        if (url.endsWith("/api/v1/live/games/switch") && method === "POST") {
          currentGame = (body as { game_id: string }).game_id;
          return Response.json({ spoken_text: "已切换至植物大战僵尸" });
        }
        if (url.endsWith("/api/v1/live/games/control") && method === "POST") {
          currentOwner = (body as { owner: string }).owner;
          return Response.json({
            active_game_id: currentGame,
            owner: currentOwner,
            active_status: { display_name: "植物大战僵尸", summary: "草坪防线", current_activity: "待命" },
          });
        }
        if (url.endsWith("/api/v1/live/games/step") && method === "POST") {
          return Response.json({ message: "推进了1回合" });
        }
        if (url.endsWith("/api/v1/live/games/action") && method === "POST") {
          return Response.json({ message: "已种植向日葵" });
        }
        if (url.endsWith("/api/v1/live/draw-and-guess/status")) {
          return Response.json(
            dgActive
              ? { active: true, remaining: 42.1, category: "水果", hint: "红瓤黑籽" }
              : { active: false, state: "finished", word: "西瓜", winner: null }
          );
        }
        if (url.endsWith("/api/v1/live/draw-and-guess/stop") && method === "POST") {
          dgActive = false;
          return Response.json({ status: "stopped", word: "西瓜" });
        }
        if (url.endsWith("/api/v1/live/games/device/stardew")) {
          return Response.json({
            configured: true,
            online: true,
            state: {
              session: "11111111-1111-1111-1111-111111111111",
              epoch: 3,
              owner: "studio",
              can_act: true,
              farm: {
                save: "测试农场",
                location: "农场",
                x: 12,
                y: 8,
                season: "春",
                day: 5,
                time: "09:00",
                stamina: 90,
                money: 500,
                slot: 0,
                inventory: [{ item: "防风草种子" }],
                front: { key: "13,8" },
              },
            },
          });
        }
        if (url.endsWith("/api/v1/live/games/device/stardew/actions") && method === "POST") {
          return Response.json({ status: "pending" });
        }
        if (url.includes("/api/v1/live/games/device/stardew/actions/11111111-1111-1111-1111-111111111111/")) {
          receiptPolls += 1;
          return Response.json({ status: receiptPolls >= 2 ? "succeeded" : "pending" });
        }
        return Response.json({});
      })
    );

    const onFeedback = vi.fn();
    render(<LiveInteractionPage visible onFeedback={onFeedback} />);

    await waitFor(() => {
      expect(document.getElementById("live-game-name")?.textContent).toBe("星露谷物语");
      expect(document.getElementById("device-state")?.textContent).toContain("测试农场");
      expect(document.getElementById("dg-live-status")?.textContent).toBe("作画中 (43s)");
    });

    // Switch game to PvZ
    fireEvent.click(screen.getByRole("button", { name: "切到《植物大战僵尸》" }));
    await waitFor(() => {
      expect(document.getElementById("live-game-name")?.textContent).toBe("植物大战僵尸");
    });

    // Action & step & control transfer
    fireEvent.click(screen.getByRole("button", { name: "🌻 种向日葵" }));
    await waitFor(() => {
      expect(calls.some((c) => c.url.endsWith("/api/v1/live/games/action"))).toBe(true);
    });

    fireEvent.click(screen.getByRole("button", { name: "推进一回合" }));
    await waitFor(() => {
      expect(calls.some((c) => c.url.endsWith("/api/v1/live/games/step"))).toBe(true);
    });

    fireEvent.click(document.getElementById("game-control") as HTMLButtonElement);
    await waitFor(() => {
      expect(document.getElementById("game-control")?.textContent).toBe("交回游戏控制");
    });

    // Stop Draw & Guess
    fireEvent.click(document.getElementById("dg-stop-btn") as HTMLButtonElement);
    await waitFor(() => {
      expect(document.getElementById("dg-live-status")?.textContent).toBe("上一局结束");
      expect(document.getElementById("dg-live-word")?.textContent).toContain("西瓜");
    });

    // Stardew front-tile device action
    const tillBtn = document.querySelector('[data-device-action="till_front"]') as HTMLButtonElement;
    expect(tillBtn.disabled).toBe(false);
    fireEvent.click(tillBtn);

    await waitFor(
      () => {
        expect(document.getElementById("device-result")?.textContent).toBe("游戏已确认。");
      },
      { timeout: 2000 }
    );

    const submitCall = calls.find((c) => c.url.endsWith("/api/v1/live/games/device/stardew/actions"));
    expect(submitCall?.body).toMatchObject({
      session: "11111111-1111-1111-1111-111111111111",
      epoch: 3,
      action: "till_front",
      target: "13,8",
      item: "防风草种子",
    });
  });
});
