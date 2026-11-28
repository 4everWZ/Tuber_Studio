import { act, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Live2DAvatarCanvas, resetLive2DScriptCacheForTest } from "./Live2DAvatarCanvas";
import { OverlayPlayerApp } from "./OverlayPlayerApp";
import type { Live2DManifest } from "./types";

class MockWebSocket {
  static OPEN = 1;
  static instances: MockWebSocket[] = [];

  readonly url: string;
  readyState = MockWebSocket.OPEN;
  sent: Array<Record<string, unknown>> = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  close = vi.fn();

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  send(raw: string) {
    this.sent.push(JSON.parse(raw) as Record<string, unknown>);
  }

  triggerOpen() {
    this.onopen?.();
  }

  triggerMessage(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent);
  }

  triggerClose(code: number) {
    this.onclose?.({ code } as CloseEvent);
  }
}

class MockAudio {
  static instances: MockAudio[] = [];
  static nextPlayError: Error | null = null;

  preload = "";
  src = "";
  volume = 1;
  muted = false;
  paused = true;
  ended = false;
  currentTime = 0;
  error: { message: string } | null = null;
  onplaying: (() => void) | null = null;
  onended: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(initialSrc?: string) {
    if (initialSrc) this.src = initialSrc;
    MockAudio.instances.push(this);
  }

  removeAttribute(attr: string) {
    if (attr === "src") this.src = "";
  }

  load() {}

  pause() {
    this.paused = true;
  }

  async play() {
    if (MockAudio.nextPlayError) {
      const err = MockAudio.nextPlayError;
      MockAudio.nextPlayError = null;
      throw err;
    }
    this.paused = false;
    this.onplaying?.();
  }

  finishPlayback() {
    this.ended = true;
    this.paused = true;
    this.onended?.();
  }
}

function buildViewPayload() {
  return {
    scene: {
      width: 1920,
      height: 1080,
      title: "直播间",
      background: "ocean",
      items: [
        {
          id: "title",
          kind: "title",
          x: 60,
          y: 35,
          width: 400,
          height: 80,
          z: 1,
          visible: true,
          locked: false,
          text: "",
        },
      ],
    },
    queue: [],
    events: [],
    character: { id: "whale", version: 1, name: "大肥鱼" },
    performance: { pets_enabled: true, avatar_enabled: false },
    assets: [{ id: "idle", url: "/assets/whale/idle.png" }],
    live2d: null,
    playback: null,
    state: "live",
    muted: false,
  };
}

describe("OverlayPlayerApp", () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    MockAudio.instances = [];
    MockAudio.nextPlayError = null;
    window.history.replaceState(null, "", "/overlay");
    vi.stubGlobal("WebSocket", MockWebSocket);
    vi.stubGlobal("Audio", MockAudio);
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: vi.fn(() => "blob:silent-wav"),
      revokeObjectURL: vi.fn(),
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    document.querySelector("base")?.remove();
  });

  it("exchanges hash token, connects WS /ws/overlay, emits started/progress/ended, and honors standby", async () => {
    vi.useFakeTimers();
    const base = document.createElement("base");
    base.href = "/studio/";
    document.head.append(base);
    window.history.replaceState(null, "", "/studio/overlay#token=tok-123");

    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/v1/system/auth/overlay") && init?.method === "POST") {
        expect(JSON.parse(String(init.body))).toEqual({ token: "tok-123" });
        return Response.json({ authenticated: true, scope: "playback_only" });
      }
      if (url.endsWith("/api/v1/live/view")) {
        return Response.json(buildViewPayload());
      }
      return Response.json({});
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<OverlayPlayerApp />);

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(MockWebSocket.instances).toHaveLength(1);
    const ws = MockWebSocket.instances[0];
    expect(ws.url).toMatch(/\/studio\/ws\/overlay$/);
    expect(window.location.hash).toBe("");

    await act(async () => {
      ws.triggerOpen();
      ws.triggerMessage({ type: "hello" });
      await Promise.resolve();
    });
    expect(document.getElementById("connection")?.textContent).toBe("");

    // Play job
    await act(async () => {
      ws.triggerMessage({
        type: "play",
        job_id: "job-1",
        playback_token: "pt-1",
        audio_ref: "/media/audio/line1.wav",
        volume: 0.75,
        duration: 2.0,
        text: "欢迎来到直播间",
        activity: { step: 0.1, envelope: [0.3, 0.9] },
      });
      await Promise.resolve();
    });

    const audio = MockAudio.instances[0];
    expect(audio.src).toBe("/studio/media/audio/line1.wav");
    expect(audio.volume).toBe(0.75);
    expect(ws.sent[0]).toEqual({
      type: "started",
      job_id: "job-1",
      playback_token: "pt-1",
    });

    // Advance 500ms -> emits progress with mouth envelope value
    audio.currentTime = 0.12;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(ws.sent.some((m) => m.type === "progress" && m.mouth === 0.9 && m.paused === false)).toBe(
      true
    );

    // End audio -> emits ended
    await act(async () => {
      audio.finishPlayback();
    });
    expect(ws.sent.at(-1)).toEqual({
      type: "ended",
      job_id: "job-1",
      playback_token: "pt-1",
    });

    // Standby preempts owner and stops playback
    await act(async () => {
      ws.triggerMessage({ type: "play", job_id: "job-2", playback_token: "pt-2", audio_ref: "/media/audio/line2.wav" });
      await Promise.resolve();
      ws.triggerMessage({ type: "standby", reason: "已有播放页面，当前待机" });
    });
    expect(audio.paused).toBe(true);
    expect(document.getElementById("connection")?.textContent).toBe("已有播放页面，当前待机");
  });

  it("reveals #enable-audio on NotAllowedError and unlocks via silent WAV blob", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json(buildViewPayload()))
    );

    render(<OverlayPlayerApp />);
    await waitFor(() => {
      expect(MockWebSocket.instances).toHaveLength(1);
    });

    const ws = MockWebSocket.instances[0];
    await act(async () => {
      ws.triggerOpen();
      ws.triggerMessage({ type: "hello" });
    });

    const notAllowed = new Error("Autoplay blocked");
    notAllowed.name = "NotAllowedError";
    MockAudio.nextPlayError = notAllowed;

    await act(async () => {
      ws.triggerMessage({
        type: "play",
        job_id: "job-blocked",
        playback_token: "pt-blocked",
        audio_ref: "/media/audio/blocked.wav",
      });
      await Promise.resolve();
    });

    const unlockBtn = document.getElementById("enable-audio") as HTMLButtonElement;
    expect(unlockBtn.hidden).toBe(false);
    expect(ws.sent.at(-1)).toMatchObject({
      type: "failed",
      job_id: "job-blocked",
      playback_token: "pt-blocked",
    });

    // Clicking #enable-audio unlocks audio with silent WAV and hides button
    await act(async () => {
      fireEvent.click(unlockBtn);
      await Promise.resolve();
    });
    expect(unlockBtn.hidden).toBe(true);
    expect(document.getElementById("connection")?.textContent).toBe("音频已启用，等待下一次播报");
    expect(MockAudio.instances.some((a) => a.src === "blob:silent-wav")).toBe(true);
  });

  it("halts reconnect on auth close codes [1008, 4401, 4403] and retries in 3s otherwise", async () => {
    vi.useFakeTimers();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json(buildViewPayload()))
    );

    for (const authCode of [1008, 4401, 4403]) {
      MockWebSocket.instances = [];
      const { unmount } = render(<OverlayPlayerApp />);
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(MockWebSocket.instances).toHaveLength(1);

      await act(async () => {
        MockWebSocket.instances[0].triggerClose(authCode);
        await vi.advanceTimersByTimeAsync(5000);
      });
      expect(MockWebSocket.instances).toHaveLength(1);
      expect(document.getElementById("connection")?.textContent).toContain("播放器凭证无效");
      unmount();
    }

    // Non-auth close 1006 triggers 3000ms reconnect
    MockWebSocket.instances = [];
    render(<OverlayPlayerApp />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(MockWebSocket.instances).toHaveLength(1);
    await act(async () => {
      MockWebSocket.instances[0].triggerClose(1006);
      await vi.advanceTimersByTimeAsync(2999);
    });
    expect(MockWebSocket.instances).toHaveLength(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(MockWebSocket.instances).toHaveLength(2);
  });

  it("renders transparent Overtime HUD and Cold-Room Tier badge from /api/v1/live/shows/status", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/live/view")) {
          return Response.json(buildViewPayload());
        }
        if (url.endsWith("/api/v1/live/shows/status")) {
          return Response.json({
            overtime: {
              active: true,
              energy: 75,
              threshold: 100,
              overtime_minutes: 30,
              max_overtime_minutes: 120,
              remaining_seconds: 185,
              stage: "overtime",
              last_event_line: "白饭还没咽下去呢又被投喂续钟了！",
            },
            cold_director: {
              tier: 2,
              elapsed_seconds: 360,
            },
          });
        }
        return Response.json({});
      })
    );

    render(<OverlayPlayerApp />);

    await waitFor(() => {
      const hud = document.getElementById("overtime-hud");
      expect(hud).not.toBeNull();
      expect(document.getElementById("overtime-energy-pct")?.textContent).toBe("能量 75%");
      expect(document.getElementById("overtime-minutes")?.textContent).toContain("+30 分钟");
      expect(document.getElementById("overtime-countdown")?.textContent).toContain("03:05");
      expect(document.getElementById("overtime-status-line")?.textContent).toContain("白饭还没咽下去");

      const badge = document.getElementById("cold-director-badge");
      expect(badge?.getAttribute("data-cold-tier")).toBe("2");
      expect(badge?.textContent).toContain("Tier 2");
    });
  });

  it("appends ?layout=game to /api/v1/live/view and renders #rundown-stage-pill when rundown is active", async () => {
    window.history.replaceState(null, "", "/overlay?layout=game");
    const requestedUrls: string[] = [];

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        requestedUrls.push(url);
        if (url.endsWith("/api/v1/live/view?layout=game")) {
          const payload = buildViewPayload();
          payload.scene.background = "transparent";
          return Response.json(payload);
        }
        if (url.endsWith("/api/v1/live/shows/status")) {
          return Response.json({
            overtime: { active: false },
            cold_director: { tier: 0 },
            rundown: {
              active: true,
              paused: false,
              status: "running",
              current_index: 1,
              remaining_seconds: 725,
              current_segment: {
                id: "seg-dg",
                title: "你画我猜专场",
                kind: "draw_and_guess",
                duration_minutes: 20,
                obs_layout: "game",
                auto_start_show: true,
                graceful_finish: true,
                settings: {},
              },
            },
          });
        }
        return Response.json({});
      })
    );

    render(<OverlayPlayerApp />);

    await waitFor(() => {
      expect(requestedUrls.some((u) => u.endsWith("/api/v1/live/view?layout=game"))).toBe(true);
      const pill = document.getElementById("rundown-stage-pill");
      expect(pill).not.toBeNull();
      expect(pill?.textContent).toContain("你画我猜专场");
      expect(pill?.textContent).toContain("12:05");
    });
  });
});

describe("Live2DAvatarCanvas", () => {
  beforeEach(() => {
    resetLive2DScriptCacheForTest();
  });

  afterEach(() => {
    resetLive2DScriptCacheForTest();
    delete window.PIXI;
    vi.unstubAllGlobals();
  });

  it("loads scripts sequentially, caps PIXI FPS at 30, updates clamped mouth, and destroys on webglcontextlost", async () => {
    const setParam = vi.fn();
    const destroyApp = vi.fn();
    const hooks: { beforeUpdate: (() => void) | null } = { beforeUpdate: null };
    const loadedOrder: string[] = [];

    const origAppend = document.head.append.bind(document.head);
    vi.spyOn(document.head, "append").mockImplementation((node: Node | string) => {
      if (node instanceof HTMLScriptElement) {
        loadedOrder.push(node.getAttribute("src") || "");
        setTimeout(() => node.onload?.(new Event("load")), 0);
        return undefined;
      }
      return origAppend(node);
    });

    const appInstance = {
      ticker: { maxFPS: 0 },
      stage: { addChild: vi.fn() },
      renderer: { resize: vi.fn() },
      destroy: destroyApp,
    };

    window.PIXI = {
      Application: vi.fn(() => appInstance),
      Ticker: {},
      live2d: {
        Live2DModel: {
          registerTicker: vi.fn(),
          from: vi.fn(async () => ({
            internalModel: {
              coreModel: {
                getModel: () => ({ parameters: { ids: ["ParamAngleX", "ParamMouthOpenY"] } }),
                setParameterValueByIndex: setParam,
              },
              motionManager: { expressionManager: { resetExpression: vi.fn() } },
              on: (_event: string, fn: () => void) => {
                hooks.beforeUpdate = fn;
              },
            },
            scale: { set: vi.fn() },
            position: { set: vi.fn() },
            getLocalBounds: () => ({ x: 0, y: 0, width: 200, height: 300 }),
            motion: vi.fn(async () => {}),
            expression: vi.fn(async () => {}),
            destroy: vi.fn(),
          })),
        },
      },
    };

    const manifest: Live2DManifest = {
      status: "available",
      model: "/media/live2d/model/whale/whale.model3.json",
      scripts: ["/media/live2d/sdk/pixi.min.js", "/media/live2d/sdk/cubism4.min.js"],
      mouth_parameter: "ParamMouthOpenY",
      mouth_gain: 5,
      motions: {},
      expressions: {},
    };

    const onState = vi.fn();
    const { container } = render(
      <div style={{ width: 400, height: 400 }}>
        <Live2DAvatarCanvas manifest={manifest} readMouth={() => 0.4} onState={onState} />
      </div>
    );

    await waitFor(() => {
      expect(onState).toHaveBeenCalledWith(true);
    });
    expect(loadedOrder).toEqual([
      "/media/live2d/sdk/pixi.min.js",
      "/media/live2d/sdk/cubism4.min.js",
    ]);
    expect(appInstance.ticker.maxFPS).toBe(30);

    // Mouth gain = 5, readMouth = 0.4 -> clamped to min(1, 0.4 * 5) = 1
    hooks.beforeUpdate?.();
    expect(setParam).toHaveBeenCalledWith(1, 1);

    // webglcontextlost triggers destroy & fallback onState(false)
    const canvas = container.querySelector("canvas") as HTMLCanvasElement;
    fireEvent(canvas, new Event("webglcontextlost"));
    expect(destroyApp).toHaveBeenCalled();
    expect(onState).toHaveBeenCalledWith(false);
  });
});
