import { act, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import styles from "./DrawGuessOverlay.module.css";
import { DrawGuessOverlayApp } from "./DrawGuessOverlayApp";

describe("DrawGuessOverlayApp", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/overlay/draw-guess");
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("stays transparent and hidden by default when idle (no ?preview=1)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          draw_and_guess: { active: false, state: "idle" },
          turtle_soup: { active: false, state: "idle" },
          roast_radio: { active: false },
          poll: null,
          rundown: { active: false, paused: false, status: "idle", current_index: 0, remaining_seconds: 0 },
        })
      )
    );

    render(<DrawGuessOverlayApp />);

    await waitFor(() => {
      const root = document.getElementById("draw-guess-overlay-root") as HTMLElement;
      expect(root.classList.contains(styles.rootPreview)).toBe(false);
      const container = document.getElementById("draw-guess-container") as HTMLElement;
      expect(container.hidden).toBe(true);
      expect(document.getElementById("show-standby-placeholder")).toBeNull();
      expect(document.getElementById("live-poll-card")).toBeNull();
    });
  });

  it("renders checkerboard preview and #show-standby-placeholder when ?preview=1 and idle", async () => {
    window.history.replaceState(null, "", "/overlay/draw-guess?preview=1");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          draw_and_guess: { active: false, state: "idle" },
          turtle_soup: { active: false, state: "idle" },
          roast_radio: { active: false },
          poll: null,
          rundown: {
            active: true,
            paused: false,
            status: "running",
            current_index: 0,
            remaining_seconds: 600,
            current_segment: {
              id: "seg-1",
              title: "开场白饭闲聊",
              kind: "chat",
              duration_minutes: 15,
              obs_layout: "chat",
              auto_start_show: false,
              graceful_finish: true,
              settings: {},
            },
          },
        })
      )
    );

    render(<DrawGuessOverlayApp />);

    await waitFor(() => {
      const root = document.getElementById("draw-guess-overlay-root") as HTMLElement;
      expect(root.classList.contains(styles.rootPreview)).toBe(true);
      const standby = document.getElementById("show-standby-placeholder");
      expect(standby).not.toBeNull();
      expect(standby?.textContent).toContain("待机");
      expect(standby?.textContent).toContain("开场白饭闲聊");
    });
  });

  it("renders #live-poll-card with numbered options, live counts, percentages, countdown, and fallback note", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          draw_and_guess: { active: false, state: "idle" },
          poll: {
            id: "poll-game-1",
            topic: "下一轮玩什么节目？",
            options: ["你画我猜", "海龟汤", "继续闲聊"],
            counts: { 你画我猜: 3, 海龟汤: 1, 继续闲聊: 0 },
            total_votes: 4,
            remaining_seconds: 42.3,
            phase: "started",
          },
        })
      )
    );

    render(<DrawGuessOverlayApp />);

    await waitFor(() => {
      const card = document.getElementById("live-poll-card");
      expect(card).not.toBeNull();
      expect(document.getElementById("live-poll-topic")?.textContent).toBe("下一轮玩什么节目？");
      expect(document.getElementById("live-poll-countdown")?.textContent).toBe("43s");
      const optionsText = document.getElementById("live-poll-options")?.textContent ?? "";
      expect(optionsText).toContain("1. 你画我猜");
      expect(optionsText).toContain("3 票 · 75%");
      expect(optionsText).toContain("2. 海龟汤");
      expect(optionsText).toContain("1 票 · 25%");
      expect(optionsText).toContain("3. 继续闲聊");
      expect(optionsText).toContain("0 票 · 0%");
      expect(document.getElementById("live-poll-fallback")?.textContent).toContain(
        "无人投票将按默认节目单推进"
      );
      expect(document.getElementById("show-standby-placeholder")).toBeNull();
    });
  });

  it("renders SVG strokes in #draw-guess-container and hides 4s after round ends", async () => {
    vi.useFakeTimers();
    let payload: Record<string, unknown> = {
      active: true,
      state: "drawing",
      remaining: 25.2,
      category: "水果",
      hint: "圆滚滚",
      trigger_user: "小明",
      character: "Aqua",
      revealed_strokes: [
        { type: "path", d: "M 0 0 L 10 10", stroke: "#38bdf8", width: 4 },
        { type: "circle", cx: 20, cy: 20, r: 5, fill: "#ef4444" },
        { type: "ellipse", cx: 40, cy: 40, rx: 8, ry: 4 },
      ],
    };

    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json(payload))
    );

    render(<DrawGuessOverlayApp />);

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    const container = document.getElementById("draw-guess-container") as HTMLElement;
    expect(container.hidden).toBe(false);
    expect(document.getElementById("draw-guess-title")?.textContent).toBe("Aqua · 你画我猜");
    expect(document.getElementById("timer-badge")?.textContent).toBe("26s");
    const svg = document.querySelector("svg#drawing-board") as SVGSVGElement;
    expect(svg.querySelectorAll("path, circle, ellipse")).toHaveLength(3);

    // Finished state shows winner banner
    payload = {
      active: false,
      state: "finished",
      word: "西瓜",
      winner: "小红",
      remaining: 0,
      revealed_strokes: [],
    };
    await act(async () => {
      await vi.advanceTimersByTimeAsync(800);
    });
    expect(document.getElementById("winner-banner")?.classList.contains("hidden")).toBe(false);
    expect(document.getElementById("banner-answer")?.textContent).toBe("答案：【西瓜】");

    // Idle hides after 4000ms
    payload = { active: false, state: "idle" };
    await act(async () => {
      await vi.advanceTimersByTimeAsync(800);
    });
    expect(container.hidden).toBe(false);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000);
    });
    expect(container.hidden).toBe(true);
  });

  it("renders rect/line/polygon strokes safely, theme/LLM badge, progressive hint, scoreboard, and Turtle Soup / Roast Radio cards from /shows/status", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/live/shows/status")) {
          return Response.json({
            character: { name: "Aqua" },
            draw_and_guess: {
              active: true,
              state: "drawing",
              remaining: 45,
              category: "日常",
              theme: "热白饭",
              llm_generated: true,
              hint_level: 2,
              progressive_hint: "装在碗里热气腾腾",
              trigger_user: "主播",
              scoreboard: { 小明: 3, 小红: 1 },
              revealed_strokes: [
                {
                  type: "rect",
                  x: 10,
                  y: 20,
                  width: 80,
                  height: 40,
                  rx: 6,
                  fill: "#38bdf8",
                  stroke: "javascript:alert(1)",
                },
                { type: "line", x1: 0, y1: 0, x2: 120, y2: 90, stroke: "#fde047", width: 3 },
                { type: "polygon", points: "10,10 50,10 30,50", fill: "#34d399" },
              ],
            },
            turtle_soup: {
              active: true,
              state: "playing",
              genre: "科幻",
              surface: "宇航员在空间站发现多出一双筷子。",
              clues_found: ["刚来过货运飞船"],
              qa_history: [{ user: "路人甲", text: "是补给带的吗？", verdict: "yes" }],
            },
            roast_radio: {
              active: true,
              mode: "debate",
              topic: "白饭配拌饭酱还是配汤",
              side_a: "拌饭酱",
              side_b: "配汤",
              votes_a: 6,
              votes_b: 4,
              last_roast: "选不出来说明今天只配喝水。",
            },
          });
        }
        return Response.json({});
      })
    );

    render(<DrawGuessOverlayApp />);

    await waitFor(() => {
      const svg = document.querySelector("svg#drawing-board") as SVGSVGElement;
      expect(svg.querySelectorAll("rect")).toHaveLength(1);
      expect(svg.querySelectorAll("line")).toHaveLength(1);
      expect(svg.querySelectorAll("polygon")).toHaveLength(1);
      expect(svg.querySelector("rect")?.getAttribute("stroke")).toBeNull();
      expect(document.getElementById("theme-llm-badge")?.textContent).toBe("主题: 热白饭 · AI");
      expect(document.getElementById("clue-text")?.textContent).toBe("提示 L2: 装在碗里热气腾腾");
      expect(document.getElementById("dg-scoreboard")?.textContent).toContain("小明 3分");

      expect(document.getElementById("ts-surface")?.textContent).toContain("多出一双筷子");
      expect(document.getElementById("ts-clues")?.textContent).toContain("刚来过货运飞船");
      expect(document.getElementById("ts-qa-list")?.textContent).toContain("是补给带的吗？");

      expect(document.getElementById("rr-topic")?.textContent).toBe("白饭配拌饭酱还是配汤");
      expect(document.getElementById("rr-vote-bar")?.textContent).toContain("6票 · 60%");
      expect(document.getElementById("rr-last-roast")?.textContent).toContain("今天只配喝水");
    });
  });

  it("falls back to /api/v1/live/draw-and-guess/overlay when /api/v1/live/shows/status fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/live/shows/status")) {
          return new Response("Not Found", { status: 404 });
        }
        if (url.endsWith("/api/v1/live/draw-and-guess/overlay")) {
          return Response.json({
            active: true,
            state: "drawing",
            remaining: 15,
            category: "水果",
            hint: "弯弯的黄色水果",
            revealed_strokes: [{ type: "line", x1: 10, y1: 10, x2: 100, y2: 100 }],
          });
        }
        return Response.json({});
      })
    );

    render(<DrawGuessOverlayApp />);

    await waitFor(() => {
      const container = document.getElementById("draw-guess-container") as HTMLElement;
      expect(container.hidden).toBe(false);
      expect(document.querySelectorAll("svg#drawing-board line")).toHaveLength(1);
    });
  });
});
