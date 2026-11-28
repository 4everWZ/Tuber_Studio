import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { characterDisplayStore } from "@/shared/hooks/useCharacterDisplay";
import { clearDirtyRegistry } from "@/shared/lib/dirtyRegistry";
import { LiveDesk } from "./LiveDesk";
import { LiveFeature } from "./LiveFeature";
import { LiveMusicPage } from "./LiveMusicPage";
import { LiveSettingsPage } from "./LiveSettingsPage";
import { LiveStandaloneApp } from "./LiveStandaloneApp";
import { liveActionDisabled, moveSongInQueue } from "./liveControls";
import type { LiveQueueItem, LiveStudioData } from "./types";

function makeStudioView(overrides: Partial<LiveStudioData> = {}): LiveStudioData {
  return {
    session_id: "sess-1",
    state: "stopped",
    muted: false,
    live_enabled: true,
    overlay_connected: false,
    character: { id: "whale", version: 1, name: "大肥鱼" },
    performance: { pets_enabled: true, avatar_enabled: false },
    assets: [{ id: "idle", url: "/assets/whale/idle.png" }],
    live2d: null,
    scene: {
      width: 1920,
      height: 1080,
      title: "直播标题",
      background: "ocean",
      items: [],
    },
    queue: [
      {
        id: "q-song-1",
        type: "song",
        requester: "观众A",
        media: { mode: "direct", song: { id: "s1", title: "第一首歌", artist: "歌手A" } },
      },
      {
        id: "q-chat-1",
        type: "chat",
        requester: "观众B",
        text: "你好",
      },
      {
        id: "q-song-2",
        type: "song",
        requester: "观众C",
        media: { mode: "direct", song: { id: "s2", title: "第二首歌", artist: "歌手B" } },
      },
    ],
    events: [],
    chat: [{ id: 1, type: "chat", requester: "观众B", text: "你好", status: "queued", received_at: 1700000000 }],
    dialogue: [],
    current: null,
    last_spoken: null,
    playback: null,
    error: null,
    ...overrides,
  };
}

describe("liveControls parity", () => {
  it("matches session and feature gating rules for main and standalone live controls", () => {
    expect(liveActionDisabled("start", { state: "stopped" }, { enabled: false })).toBe(true);
    expect(liveActionDisabled("stop", { state: "live" }, { enabled: false })).toBe(false);
    expect(liveActionDisabled("start", { state: "stopped" }, { enabled: true })).toBe(false);
    expect(liveActionDisabled("pause", { state: "stopped" }, { enabled: true })).toBe(true);
    expect(liveActionDisabled("resume", { state: "paused" }, { enabled: true })).toBe(false);
  });

  it("reorders songs cleanly without shifting non-song items", () => {
    const queue: LiveQueueItem[] = [
      { id: "song-1", type: "song" },
      { id: "chat-1", type: "chat" },
      { id: "song-2", type: "song" },
    ];
    expect(moveSongInQueue(queue, "song-2", -1)).toEqual(["song-2", "chat-1", "song-1"]);
    expect(moveSongInQueue(queue, "song-1", -1)).toBeNull();
  });
});

describe("LiveDesk", () => {
  beforeEach(() => {
    clearDirtyRegistry();
  });

  afterEach(() => {
    clearDirtyRegistry();
    vi.unstubAllGlobals();
  });

  it("renders status, handles start/pause/stop actions, queue reorder & conflict error, mock sfx, and feed tabs", async () => {
    let currentView = makeStudioView();
    const requests: Array<{ url: string; method: string; body?: unknown }> = [];
    let forceQueueConflict = false;

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method || "GET";
        const body = init?.body ? JSON.parse(String(init.body)) : undefined;
        requests.push({ url, method, body });

        if (url.endsWith("/api/v1/live/studio")) {
          return Response.json(currentView);
        }
        if (url.endsWith("/api/v1/live/start") && method === "POST") {
          currentView = { ...currentView, state: "live" };
          return Response.json({ state: "live" });
        }
        if (url.endsWith("/api/v1/live/pause") && method === "POST") {
          currentView = { ...currentView, state: "paused" };
          return Response.json({ state: "paused" });
        }
        if (url.endsWith("/api/v1/live/queue") && method === "PUT") {
          if (forceQueueConflict) {
            return new Response(
              JSON.stringify({ code: "CONFLICT", message: "队列已改变", details: null }),
              { status: 409, headers: { "Content-Type": "application/json" } }
            );
          }
          return Response.json({ queue: body });
        }
        if (url.endsWith("/api/v1/live/mock-event") && method === "POST") {
          return Response.json({ status: "queued" });
        }
        return Response.json({});
      })
    );

    render(
      <LiveDesk
        settings={{ overlay_url: "http://127.0.0.1:8020/overlay#token=abc", features: { live_enabled: true } }}
        visible
      />
    );

    await waitFor(() => {
      expect(document.querySelector("[data-desk-session]")?.textContent).toBe("未开播");
      expect(document.querySelector("[data-desk-song-count]")?.textContent).toBe("2 首待播");
      expect(document.querySelector("[data-desk-other-count]")?.textContent).toBe("另有 1 条待播互动");
    });

    // Start live session
    fireEvent.click(document.querySelector('[data-desk-action="start"]') as HTMLButtonElement);
    await waitFor(() => {
      expect(document.querySelector("[data-desk-session]")?.textContent).toBe("本地会话运行中");
    });

    // Pause live session
    fireEvent.click(document.querySelector('[data-desk-action="pause"]') as HTMLButtonElement);
    await waitFor(() => {
      expect(document.querySelector("[data-desk-session]")?.textContent).toBe("队列已暂停");
    });

    // Reorder second song up
    const upBtn = document.querySelector(
      '[data-queue-id="q-song-2"][data-queue-action="up"]'
    ) as HTMLButtonElement;
    fireEvent.click(upBtn);
    await waitFor(() => {
      const putReq = requests.find((r) => r.url.endsWith("/api/v1/live/queue") && r.method === "PUT");
      expect(putReq?.body).toEqual(["q-song-2", "q-chat-1", "q-song-1"]);
    });

    // 409 race displays friendly conflict message
    forceQueueConflict = true;
    fireEvent.click(upBtn);
    await waitFor(() => {
      expect(document.querySelector("[data-desk-action-error]")?.textContent).toBe(
        "队列已更新，请重新选择。"
      );
    });

    // Switch to SFX mock event -> asset field visible & submits metadata.asset_id
    const mockForm = document.querySelector("[data-desk-mock]") as HTMLFormElement;
    const typeSelect = mockForm.querySelector('select[name="type"]') as HTMLSelectElement;
    fireEvent.change(typeSelect, { target: { value: "sfx" } });
    const assetLabel = document.querySelector("[data-desk-asset-label]") as HTMLLabelElement;
    expect(assetLabel.hidden).toBe(false);
    fireEvent.change(mockForm.querySelector('input[name="asset"]') as HTMLInputElement, {
      target: { value: "sample1" },
    });
    fireEvent.submit(mockForm);

    await waitFor(() => {
      const mockReq = requests.find((r) => r.url.endsWith("/api/v1/live/mock-event"));
      expect(mockReq?.body).toMatchObject({
        type: "sfx",
        metadata: { asset_id: "sample1" },
      });
    });

    // Switch feed tab via keyboard ArrowRight
    const chatTab = document.getElementById("desk-chat-tab") as HTMLButtonElement;
    fireEvent.keyDown(chatTab, { key: "ArrowRight" });
    expect(document.getElementById("desk-dialogue-tab")?.getAttribute("aria-selected")).toBe("true");
  });

  it("manages scheduled live streams, Overtime Theater controls, Cold-Room trigger, and quick mock triggers", async () => {
    const currentView = makeStudioView({ state: "live" });
    let scheduleList = [
      {
        id: "sch-1",
        kind: "daily_window",
        label: "夜间档",
        status: "active",
        enabled: true,
        start_local_display: "2026-11-28 20:00",
        end_local_display: "2026-11-28 22:00",
        max_overtime_minutes: 120,
        auto_pause_operations: true,
      },
    ];
    let overtimeState: Record<string, unknown> = {
      active: false,
      energy: 0,
      threshold: 100,
      overtime_minutes: 0,
      max_overtime_minutes: 120,
      extensions_count: 0,
      remaining_seconds: 0,
      last_event_line: null,
    };
    const requests: Array<{ url: string; method: string; body?: unknown }> = [];

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method || "GET";
        const body = init?.body ? JSON.parse(String(init.body)) : undefined;
        requests.push({ url, method, body });

        if (url.endsWith("/api/v1/live/studio")) return Response.json(currentView);
        if (url.endsWith("/api/v1/live/schedules") && method === "GET") {
          return Response.json(scheduleList);
        }
        if (url.endsWith("/api/v1/live/schedules") && method === "POST") {
          const created = { id: "sch-2", enabled: true, ...(body as Record<string, unknown>) };
          scheduleList = [...scheduleList, created as (typeof scheduleList)[number]];
          return Response.json(created);
        }
        if (url.endsWith("/api/v1/live/schedules/sch-1") && method === "PATCH") {
          scheduleList = scheduleList.map((s) =>
            s.id === "sch-1" ? { ...s, enabled: Boolean((body as { enabled: boolean }).enabled) } : s
          );
          return Response.json({ id: "sch-1" });
        }
        if (url.endsWith("/api/v1/live/schedules/sch-2") && method === "DELETE") {
          scheduleList = scheduleList.filter((s) => s.id !== "sch-2");
          return Response.json({ removed: "sch-2" });
        }
        if (url.endsWith("/api/v1/live/overtime") && method === "GET") {
          return Response.json(overtimeState);
        }
        if (url.endsWith("/api/v1/live/overtime/start") && method === "POST") {
          overtimeState = {
            active: true,
            energy: 40,
            threshold: 100,
            overtime_minutes: 15,
            max_overtime_minutes: 120,
            extensions_count: 1,
            remaining_seconds: 2700,
            last_event_line: "白饭还没吃上，加班条又涨了！",
          };
          return Response.json(overtimeState);
        }
        if (url.endsWith("/api/v1/live/overtime/stop") && method === "POST") {
          overtimeState = { ...overtimeState, active: false };
          return Response.json(overtimeState);
        }
        if (url.endsWith("/api/v1/live/cold-director/status")) {
          return Response.json({ tier: 2, elapsed_seconds: 310 });
        }
        if (url.endsWith("/api/v1/live/cold-director/trigger") && method === "POST") {
          return Response.json({ spoken_text: "暖场小剧场启动！" });
        }
        if (url.endsWith("/api/v1/live/mock-event") && method === "POST") {
          const parsed = body as { text?: string } | undefined;
          if (parsed?.text === "1") {
            return Response.json({ status: "recorded", reason: "辩论已计票", vote: { side: "a" } });
          }
          return Response.json({ status: "queued" });
        }
        return Response.json({});
      })
    );

    render(<LiveDesk settings={{ features: { live_enabled: true } }} visible />);

    await waitFor(() => {
      expect(document.querySelector("[data-desk-schedule-count]")?.textContent).toBe("1 条排班");
      const rowText = document.querySelector('[data-schedule-id="sch-1"]')?.textContent || "";
      expect(rowText).toContain("夜间档");
      expect(rowText).toContain("(开播中)");
      expect(rowText).toContain("2026-11-28 20:00 ~ 2026-11-28 22:00");
    });

    // Create new schedule
    const schedForm = document.querySelector("[data-desk-schedule-form]") as HTMLFormElement;
    fireEvent.submit(schedForm);
    await waitFor(() => {
      expect(document.querySelector("[data-desk-schedule-count]")?.textContent).toBe("2 条排班");
    });

    // Switching to once_window uses YYYY-MM-DD HH:MM inputs
    const kindSelect = schedForm.querySelector('select[name="schedule_kind"]') as HTMLSelectElement;
    fireEvent.change(kindSelect, { target: { value: "once_window" } });
    const startInput = schedForm.querySelector('input[name="start_local"]') as HTMLInputElement;
    const endInput = schedForm.querySelector('input[name="end_local"]') as HTMLInputElement;
    expect(startInput.value).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
    expect(endInput.value).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
    fireEvent.change(kindSelect, { target: { value: "duration" } });

    // Toggle sch-1
    fireEvent.click(document.querySelector('[data-schedule-toggle="sch-1"]') as HTMLButtonElement);
    await waitFor(() => {
      expect(document.querySelector('[data-schedule-id="sch-1"]')?.textContent).toContain("已暂停");
    });

    // Delete sch-2
    fireEvent.click(document.querySelector('[data-schedule-delete="sch-2"]') as HTMLButtonElement);
    await waitFor(() => {
      expect(document.querySelector("[data-desk-schedule-count]")?.textContent).toBe("1 条排班");
    });

    // Overtime start & stop
    fireEvent.click(document.getElementById("overtime-start-btn") as HTMLButtonElement);
    await waitFor(() => {
      expect(document.querySelector("[data-desk-overtime-state]")?.textContent).toBe("加班中 +15m");
      expect(document.querySelector("[data-desk-overtime-energy]")?.textContent).toContain("40 / 100 (40%)");
      expect(document.querySelector("[data-desk-overtime-line]")?.textContent).toContain("白饭还没吃上");
    });

    fireEvent.click(document.getElementById("overtime-stop-btn") as HTMLButtonElement);
    await waitFor(() => {
      expect(document.querySelector("[data-desk-overtime-state]")?.textContent).toBe("未开启加班条");
    });

    // Cold-Room Director quick trigger
    fireEvent.click(document.getElementById("desk-cold-trigger-btn") as HTMLButtonElement);
    await waitFor(() => {
      expect(document.querySelector("[data-desk-action-state]")?.textContent).toBe("暖场小剧场启动！");
    });

    // Quick mock gift button -> sends price & count matching InteractiveRulesService.extract_gift_info
    fireEvent.click(document.querySelector('[data-quick-mock="gift-rice"]') as HTMLButtonElement);
    await waitFor(() => {
      const mockReq = requests.find((r) => r.url.endsWith("/api/v1/live/mock-event"));
      expect(mockReq?.body).toMatchObject({
        type: "gift",
        text: "白饭应援",
        metadata: { gift_name: "白饭应援", count: 1, price: 100 },
      });
    });

    // Quick mock debate vote -> accepts { status: "recorded", reason: "辩论已计票" }
    fireEvent.click(document.querySelector('[data-quick-mock="chat-vote"]') as HTMLButtonElement);
    await waitFor(() => {
      expect(document.querySelector("[data-desk-action-error]")?.textContent).toBe("");
      expect(document.querySelector("[data-desk-action-state]")?.textContent).toBe("操作已提交。");
    });
  });
});

describe("LiveMusicPage and LiveSettingsPage", () => {
  beforeEach(() => {
    clearDirtyRegistry();
    characterDisplayStore.reset();
  });

  afterEach(() => {
    clearDirtyRegistry();
    characterDisplayStore.reset();
    vi.unstubAllGlobals();
  });

  it("uploads and deletes local songs, gates singing option, and filters online song covers", async () => {
    let songs = [
      { id: "song-1", title: "深海摇篮曲", artist: "大肥鱼", live_allowed: true, vocal: "abc" },
    ];
    const requests: Array<{ url: string; method: string; body?: unknown }> = [];

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method || "GET";
        requests.push({ url, method });

        if (url.endsWith("/api/v1/live/music")) {
          return Response.json({
            settings: { default_mode: "direct", volume: 0.8, queue_limit: 20, cooldown_seconds: 30 },
            songs,
            singing: { available: false, reason: "演唱模型尚未接入" },
          });
        }
        if (url.endsWith("/api/v1/live/online")) {
          return Response.json({
            settings: {
              enabled: true,
              usage: "local_test",
              sources: ["bodian", "migu"],
              quality: "flac_preferred",
              fallback_bitrate: 320000,
              allow_lower_quality: false,
              cache_limit_mib: 15360,
              max_download_mib: 96,
              max_duration_seconds: 600,
              license_note: "test",
              live_allowed: true,
              commercial_allowed: false,
            },
            component: { installed: true, running: true, migu_configured: false },
            cache_bytes: 0,
            candidates: [],
          });
        }
        if (url.includes("/api/v1/live/online/search")) {
          return Response.json({
            songs: [
              {
                id: "ol-1",
                title: "允许封面曲",
                artist: "歌手X",
                duration: 185,
                cover: "http://p2.music.126.net/ok.jpg",
              },
              {
                id: "ol-2",
                title: "非法封面曲",
                artist: "歌手Y",
                duration: 200,
                cover: "https://untrusted.example/bad.jpg",
              },
            ],
          });
        }
        if (url.endsWith("/api/v1/live/songs/song-1") && method === "DELETE") {
          songs = [];
          return Response.json({ removed: "song-1" });
        }
        if (url.endsWith("/api/v1/live/uploads") && method === "POST") {
          return Response.json({ id: "a".repeat(64), format: "wav", duration: 10 });
        }
        if (url.includes("/api/v1/live/songs/") && method === "PUT") {
          const savedSong = JSON.parse(String(init?.body || "{}"));
          songs = [...songs, savedSong];
          return Response.json(savedSong);
        }
        if (url.endsWith("/api/v1/live/online/confirm") && method === "POST") {
          return Response.json({ status: "queued" });
        }
        return Response.json({});
      })
    );

    vi.stubGlobal("confirm", vi.fn(() => true));

    render(
      <LiveMusicPage
        settings={{ features: { live_enabled: true } }}
        visible
        onFeedback={vi.fn()}
      />
    );

    await waitFor(() => {
      expect(screen.getByText("深海摇篮曲")).toBeInTheDocument();
    });

    const singOption = document.querySelector('#request-mode option[value="sing"]') as HTMLOptionElement;
    expect(singOption.disabled).toBe(true);
    expect(singOption.textContent).toBe("演唱（模型待接入）");

    // Search online songs and verify cover allowlist
    fireEvent.change(document.getElementById("online-query") as HTMLInputElement, {
      target: { value: "海" },
    });
    fireEvent.submit(document.getElementById("online-search-form") as HTMLFormElement);

    await waitFor(() => {
      expect(screen.getByText("允许封面曲")).toBeInTheDocument();
      expect(screen.getByText("非法封面曲")).toBeInTheDocument();
    });
    const covers = document.querySelectorAll("#online-results img");
    expect(covers).toHaveLength(1);
    expect(covers[0]?.getAttribute("src")).toBe("https://p2.music.126.net/ok.jpg");

    // Delete local song
    fireEvent.click(screen.getByRole("button", { name: "移除歌曲 深海摇篮曲" }));
    await waitFor(() => {
      expect(requests.some((r) => r.url.endsWith("/api/v1/live/songs/song-1") && r.method === "DELETE")).toBe(
        true
      );
    });
  });

  it("keeps gated Live2D option disabled in LiveSettingsPage even when avatar_enabled was stored", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/live/options")) {
          return Response.json({
            timezone: "Asia/Shanghai",
            web_fetch: false,
            weather: null,
            idle: false,
            idle_text: "",
          });
        }
        if (url.endsWith("/api/v1/live/view")) {
          return Response.json({
            scene: { width: 1920, height: 1080, title: "live", background: "ocean", items: [] },
            queue: [],
            events: [],
            state: "stopped",
            live2d: null,
            activities: [{ topic: "夜宵选择", options: ["泡面", "水果"], result: "泡面胜出" }],
          });
        }
        if (url.endsWith("/api/v1/system/settings/performance")) {
          return Response.json({ pets_enabled: true, avatar_enabled: true });
        }
        if (url.endsWith("/api/v1/system/cache")) {
          return Response.json({
            categories: {
              music: {
                bytes: 2048,
                limit_bytes: 4096,
                reclaimable_bytes: 512,
                protected_bytes: 1536,
              },
            },
            settings: { music_limit_mib: 1024 },
          });
        }
        return Response.json({});
      })
    );

    render(<LiveSettingsPage visible />);

    await waitFor(() => {
      const petsCheckbox = screen.getByLabelText("显示角色宠物") as HTMLInputElement;
      expect(petsCheckbox.disabled).toBe(false);
      expect(petsCheckbox.checked).toBe(true);
      expect((document.getElementById("live-timezone") as HTMLInputElement).value).toBe("Asia/Shanghai");
      expect(document.getElementById("live-poll-state")?.textContent).toBe("夜宵选择 · 泡面胜出");
      expect(document.querySelector('a[href="#cache-panel"]')?.textContent).toContain("前往统一缓存设置");
    });

    const avatarCheckbox = screen.getByLabelText("直播 Avatar（TBD／暂不可用）") as HTMLInputElement;
    expect(avatarCheckbox.disabled).toBe(true);
    expect(avatarCheckbox.checked).toBe(false);
  });

  it("renders all five LiveFeature workbench tabs cleanly", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/live/studio")) return Response.json(makeStudioView());
        if (url.endsWith("/api/v1/live/view")) {
          return Response.json({
            scene: { width: 1920, height: 1080, title: "live", background: "ocean", items: [] },
            queue: [],
            events: [],
            state: "stopped",
            live2d: null,
          });
        }
        if (url.endsWith("/api/v1/live/music")) {
          return Response.json({
            settings: { default_mode: "direct", volume: 0.8, queue_limit: 20, cooldown_seconds: 30 },
            songs: [],
            singing: { available: false, reason: "待接入" },
          });
        }
        if (url.endsWith("/api/v1/live/online")) {
          return Response.json({
            settings: {
              enabled: false,
              usage: "local_test",
              sources: ["bodian"],
              quality: "flac_preferred",
              fallback_bitrate: 320000,
              allow_lower_quality: false,
              cache_limit_mib: 15360,
              max_download_mib: 96,
              max_duration_seconds: 600,
              license_note: "",
              live_allowed: false,
              commercial_allowed: false,
            },
            component: { installed: true, running: false },
            cache_bytes: 0,
            candidates: [],
          });
        }
        if (url.endsWith("/api/v1/live/options")) {
          return Response.json({ timezone: null, web_fetch: false, weather: null, idle: false, idle_text: "" });
        }
        if (url.endsWith("/api/v1/system/settings/performance")) {
          return Response.json({ pets_enabled: true, avatar_enabled: false });
        }
        if (url.endsWith("/api/v1/system/cache")) {
          return Response.json({ categories: {}, settings: {} });
        }
        if (url.endsWith("/api/v1/live/interactive-rules")) {
          return Response.json({
            song_request: { mode: "free", min_coin: 0, gift_names: [] },
            draw_and_guess: { enabled: true, trigger_mode: "gift_only", min_coin: 10, round_duration_sec: 60, gift_names: [] },
          });
        }
        if (url.endsWith("/api/v1/live/games/status")) {
          return Response.json({ active_game_id: "stardew_valley", owner: "studio", active_status: { display_name: "星露谷物语" } });
        }
        if (url.endsWith("/api/v1/live/games/device/stardew")) {
          return Response.json({ configured: false, online: false, state: null });
        }
        if (url.endsWith("/api/v1/live/draw-and-guess/status")) {
          return Response.json({ active: false, state: "idle" });
        }
        return Response.json({});
      })
    );

    const tabs = ["live", "live-scene", "live-music", "live-settings", "live-interaction"] as const;
    for (const page of tabs) {
      const { unmount } = render(
        <LiveFeature
          route={{
            page,
            section: "live",
            anchor: null,
            routeKind: undefined,
            routeId: undefined,
            taskId: undefined,
            hash: `#${page}`,
          }}
        />
      );
      expect(document.querySelector(`[data-section="${page}"]`)).not.toBeNull();
      await waitFor(() => {
        expect(document.querySelector("[data-live-page]")).not.toBeNull();
      });
      unmount();
    }
  });

  it("uploads local song via /api/v1/live/uploads and PUT /api/v1/live/songs/{id} and confirms online candidate", async () => {
    const calls: Array<{ url: string; method: string; body?: unknown }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method || "GET";
        calls.push({ url, method, body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined });

        if (url.endsWith("/api/v1/live/music")) {
          return Response.json({
            settings: { default_mode: "direct", volume: 0.8, queue_limit: 20, cooldown_seconds: 30 },
            songs: [],
            singing: { available: false, reason: "待接入" },
          });
        }
        if (url.endsWith("/api/v1/live/online")) {
          return Response.json({
            settings: {
              enabled: true,
              usage: "local_test",
              sources: ["bodian"],
              quality: "flac_preferred",
              fallback_bitrate: 320000,
              allow_lower_quality: false,
              cache_limit_mib: 15360,
              max_download_mib: 96,
              max_duration_seconds: 600,
              license_note: "test",
              live_allowed: true,
              commercial_allowed: false,
            },
            component: { installed: true, running: true, migu_configured: true },
            cache_bytes: 0,
            candidates: [
              {
                token: "tok_12345678901234567890",
                requester: "观众甲",
                songs: [{ id: "cand-1", title: "候选曲", artist: "歌手Z", duration: 120 }],
              },
            ],
          });
        }
        if (url.endsWith("/api/v1/live/uploads") && method === "POST") {
          return Response.json({ id: "b".repeat(64) });
        }
        if (url.includes("/api/v1/live/songs/") && method === "PUT") {
          return Response.json({});
        }
        if (url.endsWith("/api/v1/live/online/confirm") && method === "POST") {
          return Response.json({ status: "queued" });
        }
        return Response.json({});
      })
    );

    render(<LiveMusicPage settings={{ features: { live_enabled: true } }} visible onFeedback={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText("候选曲")).toBeInTheDocument();
    });

    // Confirm candidate
    fireEvent.click(screen.getByRole("button", { name: "确认 1" }));
    await waitFor(() => {
      const confirmCall = calls.find((c) => c.url.endsWith("/api/v1/live/online/confirm"));
      expect(confirmCall?.body).toEqual({ token: "tok_12345678901234567890", number: 1 });
    });

    // Upload local song
    fireEvent.click(document.getElementById("import-song") as HTMLButtonElement);
    fireEvent.change(document.getElementById("song-title") as HTMLInputElement, {
      target: { value: "新上传歌" },
    });
    fireEvent.change(document.getElementById("song-license") as HTMLTextAreaElement, {
      target: { value: "自有原创" },
    });
    fireEvent.change(document.getElementById("song-lyrics") as HTMLTextAreaElement, {
      target: { value: "[00:05.00]第一句" },
    });
    const fileInput = document.getElementById("song-file") as HTMLInputElement;
    const file = new File(["RIFF"], "song.wav", { type: "audio/wav" });
    Object.defineProperty(fileInput, "files", { value: [file], configurable: true });
    fireEvent.submit(document.getElementById("song-form") as HTMLFormElement);

    await waitFor(() => {
      expect(calls.some((c) => c.url.includes("/api/v1/live/songs/") && c.method === "PUT")).toBe(
        true
      );
    });
    const putSong = calls.find((c) => c.url.includes("/api/v1/live/songs/") && c.method === "PUT");
    expect(putSong?.body).toMatchObject({
      title: "新上传歌",
      audio: "b".repeat(64),
      lyrics: [{ time: 5, text: "第一句" }],
    });
  });

  it("renders LiveStandaloneApp login gate on 401 and boots into desk after login", async () => {
    let authed = false;
    window.history.replaceState(null, "", "/live");

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/api/v1/system/auth/login") && init?.method === "POST") {
          authed = true;
          return Response.json({ authenticated: true });
        }
        if (url.endsWith("/api/v1/system/settings")) {
          if (!authed) {
            return new Response(JSON.stringify({ code: "UNAUTHORIZED", message: "请登录", details: null }), {
              status: 401,
              headers: { "Content-Type": "application/json" },
            });
          }
          return Response.json({ features: { live_enabled: true }, overlay_url: "http://127.0.0.1/overlay" });
        }
        if (url.endsWith("/api/v1/live/studio")) {
          return Response.json(makeStudioView());
        }
        if (url.endsWith("/api/v1/operations/account/bilibili")) {
          return Response.json({ state: "not_configured" });
        }
        return Response.json({});
      })
    );

    render(<LiveStandaloneApp />);

    await waitFor(() => {
      expect(document.getElementById("login-form")).not.toBeNull();
    });

    fireEvent.change(document.getElementById("token") as HTMLInputElement, {
      target: { value: "admin-tok" },
    });
    fireEvent.submit(document.getElementById("login-form") as HTMLFormElement);

    await waitFor(() => {
      expect(document.getElementById("live-desk")).not.toBeNull();
      expect(document.querySelector("[data-desk-session]")?.textContent).toBe("未开播");
    });
  });
});
