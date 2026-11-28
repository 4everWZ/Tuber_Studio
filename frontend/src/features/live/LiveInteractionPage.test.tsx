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

  it("controls Draw & Guess 2.0 theme/LLM, Turtle Soup, Roast Radio, and Cold-Room Director", async () => {
    const requests: Array<{ url: string; method: string; body?: unknown }> = [];
    let showsState: Record<string, unknown> = {
      draw_and_guess: { active: false, state: "idle", scoreboard: { 小明: 2 } },
      turtle_soup: { active: false, state: "idle", clues_found: [] },
      roast_radio: { active: false, mode: "idle", votes_a: 0, votes_b: 0 },
      cold_director: { tier: 1, elapsed_seconds: 125, tier_label: "暖场闲聊" },
    };

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method || "GET";
        const body = init?.body ? JSON.parse(String(init.body)) : undefined;
        requests.push({ url, method, body });

        if (url.endsWith("/api/v1/live/interactive-rules")) return Response.json(buildBaseRules());
        if (url.endsWith("/api/v1/live/games/status")) {
          return Response.json({ active_game_id: "stardew_valley", owner: "studio", active_status: { display_name: "星露谷" } });
        }
        if (url.endsWith("/api/v1/live/games/device/stardew")) {
          return Response.json({ configured: false, online: false, state: null });
        }
        if (url.endsWith("/api/v1/live/draw-and-guess/status")) {
          return Response.json(showsState.draw_and_guess);
        }
        if (url.endsWith("/api/v1/live/shows/status")) {
          return Response.json(showsState);
        }
        if (url.endsWith("/api/v1/live/cold-director/status")) {
          return Response.json(showsState.cold_director);
        }
        if (url.endsWith("/api/v1/live/draw-and-guess/start") && method === "POST") {
          showsState = {
            ...showsState,
            draw_and_guess: {
              active: true,
              state: "drawing",
              remaining: 60,
              category: "美食",
              theme: "热白饭",
              hint_level: 1,
              progressive_hint: "白花花一碗",
              scoreboard: { 小明: 2 },
            },
          };
          return Response.json({ status: "started", spoken_text: "开始画热白饭！" });
        }
        if (url.endsWith("/api/v1/live/shows/turtle-soup/start") && method === "POST") {
          showsState = {
            ...showsState,
            turtle_soup: {
              active: true,
              state: "playing",
              surface: "深夜冰箱门自己开了。",
              genre: "mystery",
              clues_found: [],
            },
          };
          return Response.json({ spoken_text: "开汤啦！" });
        }
        if (url.endsWith("/api/v1/live/shows/turtle-soup/hint") && method === "POST") {
          showsState = {
            ...showsState,
            turtle_soup: {
              ...(showsState.turtle_soup as Record<string, unknown>),
              clues_found: ["冰箱里有人偷吃白饭"],
            },
          };
          return Response.json({ spoken_text: "提示：注意偷吃的人。" });
        }
        if (url.endsWith("/api/v1/live/shows/turtle-soup/reveal") && method === "POST") {
          showsState = {
            ...showsState,
            turtle_soup: {
              active: false,
              state: "finished",
              surface: "深夜冰箱门自己开了。",
              answer: "是猫咪学会了开冰箱门。",
              clues_found: ["冰箱里有人偷吃白饭"],
            },
          };
          return Response.json({ answer: "是猫咪学会了开冰箱门。" });
        }
        if (url.endsWith("/api/v1/live/shows/roast-radio/start-debate") && method === "POST") {
          showsState = {
            ...showsState,
            roast_radio: {
              active: true,
              mode: "debate",
              topic: "白饭配老干妈还是蛋炒饭",
              side_a: "老干妈",
              side_b: "蛋炒饭",
              votes_a: 3,
              votes_b: 2,
            },
          };
          return Response.json({ spoken_text: "辩论开启！" });
        }
        if (url.endsWith("/api/v1/live/shows/roast-radio/roast") && method === "POST") {
          showsState = {
            ...showsState,
            roast_radio: {
              ...(showsState.roast_radio as Record<string, unknown>),
              last_roast: "选不出来就两碗都端走。",
            },
          };
          return Response.json({ spoken_text: "选不出来就两碗都端走。" });
        }
        if (url.endsWith("/api/v1/live/shows/roast-radio/stop") && method === "POST") {
          showsState = {
            ...showsState,
            roast_radio: { active: false, mode: "idle", votes_a: 0, votes_b: 0 },
          };
          return Response.json({ status: "stopped" });
        }
        if (url.endsWith("/api/v1/live/cold-director/trigger") && method === "POST") {
          showsState = {
            ...showsState,
            cold_director: {
              tier: 2,
              elapsed_seconds: 320,
              last_segment_kind: "solo_draw_and_guess",
              last_spoken_text: "没人说话本小姐自己画着玩！",
            },
          };
          return Response.json({ spoken_text: "没人说话本小姐自己画着玩！" });
        }
        return Response.json({});
      })
    );

    const onFeedback = vi.fn();
    render(<LiveInteractionPage visible onFeedback={onFeedback} />);

    await waitFor(() => {
      expect(document.getElementById("cold-director-tier")?.textContent).toBe("Tier 1");
      expect(document.getElementById("cold-director-idle")?.textContent).toContain("125s");
    });

    // Draw & Guess 2.0 theme + LLM start
    fireEvent.change(document.getElementById("dg-theme-input") as HTMLInputElement, {
      target: { value: "热白饭" },
    });
    fireEvent.click(document.getElementById("dg-start-btn") as HTMLButtonElement);
    await waitFor(() => {
      const startReq = requests.find((r) => r.url.endsWith("/api/v1/live/draw-and-guess/start"));
      expect(startReq?.body).toMatchObject({ theme: "热白饭", use_llm: true, mode: "aqua_draws" });
      expect(document.getElementById("dg-progressive-hint")?.textContent).toContain("提示 L1: 白花花一碗");
      expect(document.getElementById("dg-live-scoreboard")?.textContent).toContain("小明(2)");
    });

    // Turtle Soup start -> hint -> reveal
    fireEvent.change(document.getElementById("ts-genre-input") as HTMLInputElement, {
      target: { value: "mystery" },
    });
    fireEvent.click(document.getElementById("ts-start-btn") as HTMLButtonElement);
    await waitFor(() => {
      expect(document.getElementById("ts-panel-surface")?.textContent).toContain("深夜冰箱门自己开了");
    });
    fireEvent.click(document.getElementById("ts-hint-btn") as HTMLButtonElement);
    await waitFor(() => {
      expect(document.getElementById("ts-panel-clues")?.textContent).toContain("冰箱里有人偷吃白饭");
    });
    fireEvent.click(document.getElementById("ts-reveal-btn") as HTMLButtonElement);
    await waitFor(() => {
      expect(document.getElementById("ts-panel-answer")?.textContent).toContain("猫咪学会了开冰箱门");
    });

    // Roast Radio debate -> roast -> stop
    fireEvent.change(document.getElementById("rr-topic-input") as HTMLInputElement, {
      target: { value: "白饭配老干妈还是蛋炒饭" },
    });
    fireEvent.change(document.getElementById("rr-side-a-input") as HTMLInputElement, {
      target: { value: "老干妈" },
    });
    fireEvent.change(document.getElementById("rr-side-b-input") as HTMLInputElement, {
      target: { value: "蛋炒饭" },
    });
    fireEvent.click(document.getElementById("rr-start-debate-btn") as HTMLButtonElement);
    await waitFor(() => {
      expect(document.getElementById("rr-panel-topic")?.textContent).toContain("白饭配老干妈还是蛋炒饭");
    });
    fireEvent.click(document.getElementById("rr-roast-btn") as HTMLButtonElement);
    await waitFor(() => {
      expect(document.getElementById("rr-panel-roast")?.textContent).toContain("两碗都端走");
    });
    fireEvent.click(document.getElementById("rr-stop-btn") as HTMLButtonElement);
    await waitFor(() => {
      expect(document.getElementById("rr-live-status")?.textContent).toBe("空闲中");
    });

    // Cold-Room Director trigger
    fireEvent.click(document.getElementById("cold-trigger-btn") as HTMLButtonElement);
    await waitFor(() => {
      expect(document.getElementById("cold-director-tier")?.textContent).toBe("Tier 2");
      expect(document.getElementById("cold-director-spoken")?.textContent).toContain("自己画着玩");
    });
  });
});
