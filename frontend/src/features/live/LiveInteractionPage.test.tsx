import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearDirtyRegistry, hasAnyUnsavedChanges } from "@/shared/lib/dirtyRegistry";
import type { RundownStatusResponse } from "./consoleTypes";
import { LiveInteractionPage } from "./LiveInteractionPage";
import { LiveScenePage } from "./LiveScenePage";
import { createDefaultRundownPlan } from "./RundownBuilderPanel";
import type { LiveScheduleItem, LiveViewData, SceneData } from "./types";

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

function buildBaseRundown(): RundownStatusResponse {
  const defaultPlan = createDefaultRundownPlan();
  return {
    plan: defaultPlan,
    state: {
      active: false,
      paused: false,
      status: "idle",
      current_index: 0,
      remaining_seconds: 0,
    },
    presets: [
      {
        id: "multi_game_chat",
        name: "多游戏轮播→闲聊",
        description: "开场闲聊后投票选游戏",
        plan: defaultPlan,
      },
      {
        id: "device_and_shows",
        name: "游戏实机+互动→闲聊",
        description: "实机游戏与小剧场",
        plan: {
          ...defaultPlan,
          name: "游戏实机+互动→闲聊",
          enabled_games: ["stardew_valley", "pvz", "draw_and_guess"],
        },
      },
      {
        id: "late_night_llm",
        name: "纯LLM轻综艺夜场",
        description: "夜场海龟汤与锐评",
        plan: {
          ...defaultPlan,
          name: "纯LLM轻综艺夜场",
          enabled_games: ["turtle_soup", "roast_radio", "song_party"],
        },
      },
    ],
    game_catalog: [
      { kind: "draw_and_guess", label: "你画我猜 2.0", default_layout: "game", description: "作画抢答" },
      { kind: "turtle_soup", label: "灵魂海龟汤", default_layout: "game", description: "推理线索" },
      { kind: "roast_radio", label: "锐评辩论", default_layout: "chat", description: "正反辩论" },
      { kind: "song_party", label: "点歌听歌会", default_layout: "chat", description: "听歌伴播" },
      { kind: "stardew_valley", label: "星露谷物语", default_layout: "game", description: "牧场实机" },
      { kind: "pvz", label: "植物大战僵尸", default_layout: "game", description: "草坪防线" },
    ],
  };
}

function buildFineSettings() {
  return {
    cold_director: {
      tier1_delay_sec: 90,
      tier2_delay_sec: 300,
      tier3_delay_sec: 900,
      tier1_cooldown_sec: 90,
      tier2_cooldown_sec: 150,
      tier3_cadence_sec: 300,
    },
    overtime: {
      threshold: 100,
      gift_coin_multiplier: 1.5,
      chat_base_energy: 6,
      chat_cooldown_seconds: 15,
      decay_per_minute: 4,
      default_base_duration_minutes: 60,
      default_max_overtime_minutes: 120,
      default_extension_step_minutes: 15,
    },
    draw_and_guess: {
      duration_sec: 60,
      use_llm: true,
      mode: "aqua_draws",
      hint_l2_ratio: 0.35,
      hint_l3_ratio: 0.7,
      default_theme: "",
    },
    turtle_soup: {
      use_llm: true,
      default_genre: "",
      max_questions: 20,
      auto_hint_every_n_questions: 5,
    },
  };
}

function makeScene(layout: "chat" | "game", title: string): SceneData {
  return {
    width: 1920,
    height: 1080,
    title,
    background: layout === "game" ? "transparent" : "ocean",
    items: [
      {
        id: "avatar",
        kind: "avatar",
        x: layout === "game" ? 1440 : 220,
        y: layout === "game" ? 620 : 120,
        width: layout === "game" ? 440 : 400,
        height: layout === "game" ? 420 : 400,
        z: 2,
        visible: true,
        locked: false,
        text: "",
      },
      {
        id: "title",
        kind: "title",
        x: 40,
        y: 30,
        width: 500,
        height: 90,
        z: 1,
        visible: true,
        locked: false,
        text: "",
      },
    ],
  };
}

describe("LiveInteractionPage & Dual-Layout LiveScenePage", () => {
  beforeEach(() => {
    clearDirtyRegistry();
  });

  afterEach(() => {
    clearDirtyRegistry();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("manages rundown presets, 6-game pool toggles, chat opening settings, segment CRUD, and PUT /api/v1/live/rundown", async () => {
    const writes: Array<{ url: string; method: string; body: unknown }> = [];
    const rundownPayload = buildBaseRundown();

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method || "GET";
        const body = init?.body ? JSON.parse(String(init.body)) : undefined;
        if (method !== "GET") writes.push({ url, method, body });

        if (url.endsWith("/api/v1/live/rundown") && method === "GET") {
          return Response.json(rundownPayload);
        }
        if (url.endsWith("/api/v1/live/rundown") && method === "PUT") {
          return Response.json({ ...rundownPayload, plan: body });
        }
        if (url.endsWith("/api/v1/live/interactive-rules")) return Response.json(buildBaseRules());
        if (url.endsWith("/api/v1/live/schedules")) return Response.json([]);
        if (url.endsWith("/api/v1/live/fine-settings")) return Response.json(buildFineSettings());
        if (url.endsWith("/api/v1/live/games/status")) {
          return Response.json({ active_game_id: "stardew_valley", owner: "studio", active_status: { display_name: "星露谷物语" } });
        }
        if (url.endsWith("/api/v1/live/games/device/stardew")) {
          return Response.json({ configured: false, online: false, state: null });
        }
        return Response.json({});
      })
    );

    const onFeedback = vi.fn();
    render(<LiveInteractionPage visible onFeedback={onFeedback} />);

    // Clean link to Broadcast Console + duplicated show trigger buttons removed
    expect(document.getElementById("goto-live-desk-link")?.textContent).toContain("前往导播台实时控场");
    expect(document.getElementById("dg-start-btn")).toBeNull();
    expect(document.getElementById("ts-start-btn")).toBeNull();
    expect(document.getElementById("rr-start-debate-btn")).toBeNull();
    expect(document.getElementById("cold-trigger-btn")).toBeNull();

    // Wait for rundown presets & 6 game pool toggles
    await waitFor(() => {
      expect(document.querySelectorAll("[data-game-pool-toggle]").length).toBe(6);
    });

    // Apply preset "纯LLM轻综艺夜场"
    fireEvent.change(document.getElementById("rundown-preset-select") as HTMLSelectElement, {
      target: { value: "late_night_llm" },
    });
    fireEvent.click(document.getElementById("rundown-apply-preset-btn") as HTMLButtonElement);
    expect((document.getElementById("rundown-plan-name") as HTMLInputElement).value).toBe(
      "纯LLM轻综艺夜场"
    );
    expect(hasAnyUnsavedChanges()).toBe(true);

    // Toggle stardew_valley into the enabled_games pool
    const stardewToggle = document.querySelector(
      '[data-game-pool-toggle="stardew_valley"]'
    ) as HTMLInputElement;
    expect(stardewToggle.checked).toBe(false);
    fireEvent.click(stardewToggle);
    expect(stardewToggle.checked).toBe(true);

    // Edit chat_entry opening mode, opening minutes, min_votes = 0, and hot-chat threshold
    fireEvent.change(document.getElementById("rundown-opening-mode") as HTMLSelectElement, {
      target: { value: "direct_rundown" },
    });
    fireEvent.change(document.getElementById("rundown-opening-minutes") as HTMLInputElement, {
      target: { value: "20" },
    });
    fireEvent.change(document.getElementById("rundown-min-votes") as HTMLInputElement, {
      target: { value: "0" },
    });
    fireEvent.change(document.getElementById("rundown-hot-threshold") as HTMLInputElement, {
      target: { value: "18" },
    });

    // Add a new segment, move up, edit title & kind to draw_and_guess, edit fine duration
    const beforeSegCount = document.querySelectorAll("[data-rundown-segment]").length;
    fireEvent.click(document.getElementById("rundown-add-segment-btn") as HTMLButtonElement);
    const segCards = document.querySelectorAll("[data-rundown-segment]");
    expect(segCards.length).toBe(beforeSegCount + 1);
    const newSegId = segCards[segCards.length - 1].getAttribute("data-rundown-segment") as string;

    fireEvent.click(
      document.querySelector(`[data-segment-up="${newSegId}"]`) as HTMLButtonElement
    );
    fireEvent.change(
      document.querySelector(`[data-segment-title="${newSegId}"]`) as HTMLInputElement,
      { target: { value: "加赛你画我猜" } }
    );
    fireEvent.change(
      document.querySelector(`[data-segment-kind="${newSegId}"]`) as HTMLSelectElement,
      { target: { value: "draw_and_guess" } }
    );
    fireEvent.change(
      document.querySelector(`[data-segment-dg-duration="${newSegId}"]`) as HTMLInputElement,
      { target: { value: "90" } }
    );

    // Save rundown via PUT /api/v1/live/rundown
    fireEvent.click(document.getElementById("rundown-save-btn") as HTMLButtonElement);
    await waitFor(() => {
      expect(document.getElementById("rundown-save-state")?.textContent).toBe("节目单与投票配置已保存。");
    });

    const putReq = writes.find((w) => w.url.endsWith("/api/v1/live/rundown") && w.method === "PUT");
    expect(putReq).toBeDefined();
    const savedBody = putReq?.body as RundownStatusResponse["plan"];
    expect(savedBody.enabled_games).toContain("stardew_valley");
    expect(savedBody.chat_entry.opening_mode).toBe("direct_rundown");
    expect(savedBody.chat_entry.opening_chat_minutes).toBe(20);
    expect(savedBody.chat_entry.min_votes).toBe(0);
    expect(savedBody.chat_entry.chat_hot_threshold_per_2m).toBe(18);
    expect(
      savedBody.segments.some(
        (s) =>
          s.id === newSegId &&
          s.title === "加赛你画我猜" &&
          s.kind === "draw_and_guess" &&
          s.obs_layout === "game" &&
          s.settings.round_duration_sec === 90
      )
    ).toBe(true);
    expect(hasAnyUnsavedChanges()).toBe(false);
  });

  it("handles schedules CRUD (duration, once_window, daily_window) and fine-settings save", async () => {
    const writes: Array<{ url: string; method: string; body: unknown }> = [];
    let schedules: LiveScheduleItem[] = [
      {
        id: "sched-1",
        kind: "duration",
        label: "日常晚场",
        enabled: true,
        duration_minutes: 60,
        max_overtime_minutes: 120,
      },
    ];

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method || "GET";
        const body = init?.body ? JSON.parse(String(init.body)) : undefined;
        if (method !== "GET") writes.push({ url, method, body });

        if (url.endsWith("/api/v1/live/rundown")) return Response.json(buildBaseRundown());
        if (url.endsWith("/api/v1/live/interactive-rules")) return Response.json(buildBaseRules());
        if (url.endsWith("/api/v1/live/fine-settings") && method === "GET") {
          return Response.json(buildFineSettings());
        }
        if (url.endsWith("/api/v1/live/fine-settings") && method === "PUT") {
          return Response.json(body);
        }
        if (url.endsWith("/api/v1/live/schedules") && method === "GET") {
          return Response.json({ schedules });
        }
        if (url.endsWith("/api/v1/live/schedules") && method === "POST") {
          const created: LiveScheduleItem = {
            id: `sched-${schedules.length + 1}`,
            enabled: true,
            ...(body as Omit<LiveScheduleItem, "id">),
          };
          schedules = [...schedules, created];
          return Response.json({ schedule: created });
        }
        if (url.endsWith("/api/v1/live/schedules/sched-1") && method === "PATCH") {
          schedules = schedules.map((s) =>
            s.id === "sched-1" ? { ...s, enabled: Boolean((body as { enabled: boolean }).enabled) } : s
          );
          return Response.json({ ok: true });
        }
        if (url.endsWith("/api/v1/live/schedules/sched-1") && method === "DELETE") {
          schedules = schedules.filter((s) => s.id !== "sched-1");
          return Response.json({ deleted: "sched-1" });
        }
        if (url.endsWith("/api/v1/live/games/status")) {
          return Response.json({ active_game_id: "stardew_valley", owner: "studio" });
        }
        if (url.endsWith("/api/v1/live/games/device/stardew")) {
          return Response.json({ configured: false, online: false, state: null });
        }
        return Response.json({});
      })
    );

    render(<LiveInteractionPage visible />);

    // Switch to 排班与加班参数 sub-tab
    fireEvent.click(document.getElementById("interaction-tab-schedules") as HTMLButtonElement);

    await waitFor(() => {
      expect(document.getElementById("schedules-count-badge")?.textContent).toBe("1 条排班");
    });

    // Toggle & delete existing schedule
    fireEvent.click(document.querySelector('[data-schedule-toggle="sched-1"]') as HTMLButtonElement);
    await waitFor(() => {
      expect(
        writes.some((w) => w.url.endsWith("/api/v1/live/schedules/sched-1") && w.method === "PATCH")
      ).toBe(true);
    });

    // Create once_window schedule
    fireEvent.change(document.getElementById("schedule-kind-select") as HTMLSelectElement, {
      target: { value: "once_window" },
    });
    fireEvent.change(document.getElementById("schedule-label-input") as HTMLInputElement, {
      target: { value: "周末特别回" },
    });
    fireEvent.change(document.getElementById("schedule-once-start-input") as HTMLInputElement, {
      target: { value: "2026-12-01 20:00" },
    });
    fireEvent.change(document.getElementById("schedule-once-end-input") as HTMLInputElement, {
      target: { value: "2026-12-01 22:30" },
    });
    fireEvent.submit(document.getElementById("live-schedule-form") as HTMLFormElement);

    await waitFor(() => {
      expect(document.getElementById("schedules-count-badge")?.textContent).toBe("2 条排班");
    });

    // Create daily_window schedule with weekday toggle
    fireEvent.change(document.getElementById("schedule-kind-select") as HTMLSelectElement, {
      target: { value: "daily_window" },
    });
    fireEvent.click(document.querySelector('[data-schedule-weekday="5"]') as HTMLInputElement);
    fireEvent.submit(document.getElementById("live-schedule-form") as HTMLFormElement);

    await waitFor(() => {
      expect(document.getElementById("schedules-count-badge")?.textContent).toBe("3 条排班");
    });
    const dailyCreate = writes.filter(
      (w) => w.url.endsWith("/api/v1/live/schedules") && w.method === "POST"
    )[1];
    expect((dailyCreate.body as { weekdays: number[] }).weekdays).toContain(5);

    // Delete sched-1
    fireEvent.click(document.querySelector('[data-schedule-delete="sched-1"]') as HTMLButtonElement);
    await waitFor(() => {
      expect(document.getElementById("schedules-count-badge")?.textContent).toBe("2 条排班");
    });

    // Edit & save fine-settings
    fireEvent.change(document.getElementById("fine-cd-t1-delay") as HTMLInputElement, {
      target: { value: "75" },
    });
    fireEvent.change(document.getElementById("fine-ot-gift-mult") as HTMLInputElement, {
      target: { value: "2.5" },
    });
    fireEvent.change(document.getElementById("fine-dg-l2-ratio") as HTMLInputElement, {
      target: { value: "0.4" },
    });
    fireEvent.change(document.getElementById("fine-ts-max-q") as HTMLInputElement, {
      target: { value: "25" },
    });
    fireEvent.change(document.getElementById("fine-ts-auto-hint") as HTMLInputElement, {
      target: { value: "4" },
    });
    expect(hasAnyUnsavedChanges()).toBe(true);

    fireEvent.submit(document.getElementById("fine-settings-form") as HTMLFormElement);
    await waitFor(() => {
      expect(document.getElementById("fine-settings-state")?.textContent).toBe("冷场与加班精调参数已保存。");
    });

    const finePut = writes.find((w) => w.url.endsWith("/api/v1/live/fine-settings") && w.method === "PUT");
    expect(finePut?.body).toMatchObject({
      cold_director: expect.objectContaining({ tier1_delay_sec: 75 }),
      overtime: expect.objectContaining({ gift_coin_multiplier: 2.5 }),
      draw_and_guess: expect.objectContaining({ hint_l2_ratio: 0.4 }),
      turtle_soup: expect.objectContaining({ max_questions: 25, auto_hint_every_n_questions: 4 }),
    });
    expect(hasAnyUnsavedChanges()).toBe(false);
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
        if (url.endsWith("/api/v1/live/rundown")) return Response.json(buildBaseRundown());
        if (url.endsWith("/api/v1/live/schedules")) return Response.json([]);
        if (url.endsWith("/api/v1/live/fine-settings")) return Response.json(buildFineSettings());
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
        return Response.json({});
      })
    );

    const { rerender } = render(<LiveInteractionPage visible initialTab="rules" />);

    // Trigger a newer rules fetch before the first slow GET resolves
    rerender(<LiveInteractionPage visible={false} initialTab="rules" />);
    rerender(<LiveInteractionPage visible initialTab="rules" />);

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

    rerender(<LiveInteractionPage visible={false} initialTab="rules" />);
    rerender(<LiveInteractionPage visible initialTab="rules" />);
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
  });

  it("handles game switch, control, step, and Stardew device receipt polling on games sub-tab", async () => {
    const calls: Array<{ url: string; method: string; body?: unknown }> = [];
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

        if (url.endsWith("/api/v1/live/rundown")) return Response.json(buildBaseRundown());
        if (url.endsWith("/api/v1/live/schedules")) return Response.json([]);
        if (url.endsWith("/api/v1/live/fine-settings")) return Response.json(buildFineSettings());
        if (url.endsWith("/api/v1/live/interactive-rules")) return Response.json(buildBaseRules());
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

    render(<LiveInteractionPage visible onFeedback={vi.fn()} />);

    // Click games sub-tab
    fireEvent.click(document.getElementById("interaction-tab-games") as HTMLButtonElement);
    expect(document.getElementById("live-game-panel")).not.toBeNull();

    await waitFor(() => {
      expect(document.getElementById("live-game-name")?.textContent).toBe("星露谷物语");
      expect(document.getElementById("device-state")?.textContent).toContain("测试农场");
    });

    fireEvent.click(screen.getByRole("button", { name: "切到《植物大战僵尸》" }));
    await waitFor(() => {
      expect(document.getElementById("live-game-name")?.textContent).toBe("植物大战僵尸");
    });

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

  it("supports dual OBS layout editing (chat vs game), transparent checkerboard preview, and layout-scoped reset/save in LiveScenePage", async () => {
    const requests: Array<{ url: string; method: string; body?: unknown }> = [];
    const profiles: Record<"chat" | "game", SceneData> = {
      chat: makeScene("chat", "深海闲聊主舞台"),
      game: makeScene("game", "游戏 PiP 透明层"),
    };

    vi.stubGlobal("confirm", vi.fn(() => true));
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method || "GET";
        const body = init?.body ? JSON.parse(String(init.body)) : undefined;
        requests.push({ url, method, body });

        if (url.endsWith("/api/v1/live/view")) {
          const view: LiveViewData = {
            scene: profiles.chat,
            queue: [],
            events: [],
            character: { id: "aqua", version: 1, name: "Aqua" },
            state: "live",
          };
          return Response.json(view);
        }
        if (url.endsWith("/api/v1/system/status")) {
          return Response.json({ character: { voice_profile: "aqua_v1" } });
        }
        if (url.endsWith("/api/v1/live/scene/profiles")) {
          return Response.json({
            active_layout: "chat",
            layout_locked: true,
            profiles,
            defaults: profiles,
          });
        }
        if (url.includes("/api/v1/live/scene/default") && method === "GET") {
          const layout = url.includes("layout=game") ? "game" : "chat";
          return Response.json(makeScene(layout, layout === "game" ? "默认游戏布局" : "默认闲聊布局"));
        }
        if (url.includes("/api/v1/live/scene") && method === "PUT") {
          const layout = url.includes("layout=game") ? "game" : "chat";
          profiles[layout] = body as SceneData;
          return Response.json(body);
        }
        return Response.json({});
      })
    );

    const onFeedback = vi.fn();
    render(<LiveScenePage visible onFeedback={onFeedback} />);

    // Initial load shows chat profile + locked badge
    await waitFor(() => {
      expect((document.getElementById("scene-title") as HTMLInputElement).value).toBe("深海闲聊主舞台");
      expect(document.getElementById("scene-layout-locked-badge")?.textContent).toContain("已手动锁定");
    });

    // Switch to game profile -> loads transparent PiP layout & shows checkerboard preview
    fireEvent.click(document.querySelector('[data-scene-profile="game"]') as HTMLButtonElement);
    await waitFor(() => {
      expect((document.getElementById("scene-title") as HTMLInputElement).value).toBe("游戏 PiP 透明层");
    });
    expect(document.querySelector('[data-scene-background="transparent"]')).not.toBeNull();
    expect(document.querySelector('[data-transparent-preview="true"]')).not.toBeNull();

    // Reset default for game layout -> fetches ?layout=game
    fireEvent.click(document.getElementById("reset-layout") as HTMLButtonElement);
    await waitFor(() => {
      expect((document.getElementById("scene-title") as HTMLInputElement).value).toBe("默认游戏布局");
    });
    expect(
      requests.some((r) => r.url.includes("/api/v1/live/scene/default?layout=game") && r.method === "GET")
    ).toBe(true);

    // Edit & save game layout -> writes PUT /api/v1/live/scene?layout=game
    fireEvent.change(document.getElementById("scene-title") as HTMLInputElement, {
      target: { value: "我的游戏布局" },
    });
    fireEvent.click(document.getElementById("save-layout") as HTMLButtonElement);
    await waitFor(() => {
      expect(document.getElementById("save-state")?.textContent).toBe("布局已保存");
    });
    const saveReq = requests.find(
      (r) => r.url.includes("/api/v1/live/scene?layout=game") && r.method === "PUT"
    );
    expect(saveReq).toBeDefined();
    expect((saveReq?.body as SceneData).title).toBe("我的游戏布局");
    expect((saveReq?.body as SceneData).background).toBe("transparent");
  });
});
