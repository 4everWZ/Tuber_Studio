import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearDirtyRegistry, hasAnyUnsavedChanges } from "@/shared/lib/dirtyRegistry";
import { LiveSceneCanvas } from "./LiveSceneCanvas";
import { LiveScenePage } from "./LiveScenePage";
import { sanitizeCoverUrl } from "./liveControls";
import type { LiveViewData, SceneData } from "./types";

function baseScene(): SceneData {
  return {
    width: 1920,
    height: 1080,
    title: "深海直播间",
    background: "ocean",
    items: [
      {
        id: "avatar",
        kind: "avatar",
        x: 220,
        y: 120,
        width: 400,
        height: 400,
        z: 2,
        visible: true,
        locked: false,
        text: "",
      },
      {
        id: "now",
        kind: "now",
        x: 1300,
        y: 180,
        width: 500,
        height: 240,
        z: 1,
        visible: true,
        locked: false,
        text: "",
      },
      {
        id: "title",
        kind: "title",
        x: 60,
        y: 35,
        width: 600,
        height: 120,
        z: 1,
        visible: true,
        locked: true,
        text: "",
      },
    ],
  };
}

function baseView(overrides: Partial<LiveViewData> = {}): LiveViewData {
  return {
    scene: baseScene(),
    queue: [],
    events: [],
    character: { id: "whale", version: 1, name: "大肥鱼" },
    performance: { pets_enabled: true, avatar_enabled: false },
    assets: [{ id: "idle", url: "/assets/whale/idle.gif", still_url: "/assets/whale/idle_still.png" }],
    live2d: null,
    playback: null,
    state: "live",
    muted: false,
    ...overrides,
  };
}

describe("LiveSceneCanvas", () => {
  beforeEach(() => {
    clearDirtyRegistry();
  });

  afterEach(() => {
    clearDirtyRegistry();
    vi.unstubAllGlobals();
  });

  it("swaps animated GIF to still_url or placeholder when prefers-reduced-motion: reduce", () => {
    vi.stubGlobal("matchMedia", vi.fn(() => ({
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })));

    const scene = baseScene();
    const { container, rerender } = render(
      <LiveSceneCanvas scene={scene} view={baseView()} editable={false} />
    );

    const imgWithStill = container.querySelector(".scene-avatar img") as HTMLImageElement;
    expect(imgWithStill).not.toBeNull();
    expect(imgWithStill.getAttribute("src")).toBe("/assets/whale/idle_still.png");

    // When still_url is absent and asset is a GIF, falls back to static .avatar-empty placeholder (no <img>)
    rerender(
      <LiveSceneCanvas
        scene={scene}
        view={baseView({
          assets: [{ id: "idle", url: "/assets/whale/idle.gif", still_url: null }],
        })}
        editable={false}
      />
    );
    expect(container.querySelector(".scene-avatar img")).toBeNull();
    expect(container.querySelector(".scene-avatar .avatar-empty")?.textContent).toBe("大肥鱼");
  });

  it("suppresses failed assets into failedAssets and degrades to next asset or empty fallback", () => {
    const onWarning = vi.fn();
    const view = baseView({
      assets: [
        { id: "idle", url: "/assets/whale/broken.png" },
        { id: "backup", url: "/assets/whale/backup.png" },
      ],
    });
    const { container } = render(
      <LiveSceneCanvas scene={view.scene} view={view} onWarning={onWarning} />
    );

    const firstImg = container.querySelector(".scene-avatar img") as HTMLImageElement;
    expect(firstImg.getAttribute("src")).toBe("/assets/whale/broken.png");

    fireEvent.error(firstImg);
    expect(onWarning).toHaveBeenCalledWith("角色素材加载失败，画面已降级为可用素材");

    const secondImg = container.querySelector(".scene-avatar img") as HTMLImageElement;
    expect(secondImg.getAttribute("src")).toBe("/assets/whale/backup.png");

    fireEvent.error(secondImg);
    expect(container.querySelector(".scene-avatar img")).toBeNull();
    expect(container.querySelector(".scene-avatar .avatar-empty")?.textContent).toBe("等待角色素材");
  });

  it("restricts external song covers strictly to .music.126.net and upgrades to https", () => {
    expect(sanitizeCoverUrl("http://p1.music.126.net/abc/cover.jpg")).toBe(
      "https://p1.music.126.net/abc/cover.jpg"
    );
    expect(sanitizeCoverUrl("https://p2.music.126.net/xyz.png")).toBe(
      "https://p2.music.126.net/xyz.png"
    );
    expect(sanitizeCoverUrl("https://music.126.net.evil.example/a.jpg")).toBeNull();
    expect(sanitizeCoverUrl("https://evil.example/a.jpg")).toBeNull();
    expect(sanitizeCoverUrl("javascript:alert(1)")).toBeNull();
    expect(sanitizeCoverUrl("ftp://p1.music.126.net/a.jpg")).toBeNull();

    const scene = baseScene();
    const { container, rerender } = render(
      <LiveSceneCanvas
        scene={scene}
        view={baseView({
          playback: {
            song: {
              mode: "direct",
              requester: "观众",
              song: { id: "s1", title: "Allowed", cover: "http://p1.music.126.net/ok.jpg" },
            },
          },
        })}
      />
    );
    const okCover = container.querySelector(".scene-now img") as HTMLImageElement;
    expect(okCover).not.toBeNull();
    expect(okCover.getAttribute("src")).toBe("https://p1.music.126.net/ok.jpg");
    expect(okCover.getAttribute("referrerpolicy")).toBe("no-referrer");

    rerender(
      <LiveSceneCanvas
        scene={scene}
        view={baseView({
          playback: {
            song: {
              mode: "direct",
              requester: "观众",
              song: { id: "s2", title: "Blocked", cover: "https://cdn.example.com/bad.jpg" },
            },
          },
        })}
      />
    );
    expect(container.querySelector(".scene-now img")).toBeNull();
  });

  it("nudges selected unlocked item on Arrow and Shift+Arrow and respects locked items", () => {
    class MockPointerEvent extends MouseEvent {
      readonly pointerId: number;
      constructor(type: string, init: PointerEventInit = {}) {
        super(type, init);
        this.pointerId = init.pointerId ?? 1;
      }
    }
    vi.stubGlobal("PointerEvent", MockPointerEvent);

    const onChange = vi.fn();
    const onSelect = vi.fn();
    const scene = baseScene();
    const { container, rerender } = render(
      <LiveSceneCanvas
        scene={scene}
        view={baseView()}
        editable
        selectedId="avatar"
        onSelect={onSelect}
        onChange={onChange}
      />
    );

    const canvas = container.querySelector(".scene-canvas") as HTMLElement;
    fireEvent.keyDown(canvas, { key: "ArrowRight" });
    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({
        items: expect.arrayContaining([
          expect.objectContaining({ id: "avatar", x: 221, y: 120 }),
        ]),
      })
    );

    fireEvent.keyDown(canvas, { key: "ArrowDown", shiftKey: true });
    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({
        items: expect.arrayContaining([
          expect.objectContaining({ id: "avatar", x: 220, y: 130 }),
        ]),
      })
    );

    // Locked title item ignores keyboard nudge
    onChange.mockClear();
    rerender(
      <LiveSceneCanvas
        scene={scene}
        view={baseView()}
        editable
        selectedId="title"
        onSelect={onSelect}
        onChange={onChange}
      />
    );
    fireEvent.keyDown(canvas, { key: "ArrowRight" });
    expect(onChange).not.toHaveBeenCalled();

    // Pointer drag & resize on unlocked avatar item
    onChange.mockClear();
    rerender(
      <LiveSceneCanvas
        scene={scene}
        view={baseView()}
        editable
        selectedId="avatar"
        onSelect={onSelect}
        onChange={onChange}
      />
    );
    const avatarEl = container.querySelector('[data-id="avatar"]') as HTMLElement;
    fireEvent.pointerDown(avatarEl, { button: 0, clientX: 100, clientY: 100, pointerId: 1 });
    fireEvent.pointerMove(avatarEl, { clientX: 115, clientY: 120 });
    fireEvent.pointerUp(avatarEl);
    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({
        items: expect.arrayContaining([
          expect.objectContaining({ id: "avatar", x: 235, y: 140 }),
        ]),
      })
    );

    const resizeHandle = avatarEl.querySelector(".scene-resize") as HTMLElement;
    fireEvent.pointerDown(resizeHandle, { button: 0, clientX: 200, clientY: 200, pointerId: 2 });
    fireEvent.pointerMove(avatarEl, { clientX: 250, clientY: 230 });
    fireEvent.pointerUp(avatarEl);
    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({
        items: expect.arrayContaining([
          expect.objectContaining({ id: "avatar", width: 450, height: 430 }),
        ]),
      })
    );
  });

  it("preserves unsaved dirty edits in LiveScenePage across background polls and saves/undoes cleanly", async () => {
    let remoteTitle = "初始标题";
    let putCalls = 0;

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/api/v1/live/view")) {
          const v = baseView();
          v.scene.title = remoteTitle;
          return Response.json(v);
        }
        if (url.endsWith("/api/v1/system/status")) {
          return Response.json({ character: { voice_profile: "whale_v1" } });
        }
        if (url.endsWith("/api/v1/live/scene") && init?.method === "PUT") {
          putCalls += 1;
          const body = JSON.parse(String(init.body)) as SceneData;
          remoteTitle = body.title;
          return Response.json(body);
        }
        return Response.json({});
      })
    );

    const onFeedback = vi.fn();
    const { rerender } = render(<LiveScenePage visible onFeedback={onFeedback} />);

    await waitFor(() => {
      expect((document.getElementById("scene-title") as HTMLInputElement).value).toBe("初始标题");
    });

    // Edit title -> marks dirty & registers in global beforeunload registry
    fireEvent.change(document.getElementById("scene-title") as HTMLInputElement, {
      target: { value: "未保存本地标题" },
    });
    expect(document.getElementById("save-state")?.textContent).toBe("布局尚未保存");
    expect(hasAnyUnsavedChanges()).toBe(true);

    // Remote changes while dirty must NOT overwrite local edits
    remoteTitle = "远端新标题";
    rerender(<LiveScenePage visible={false} onFeedback={onFeedback} />);
    rerender(<LiveScenePage visible onFeedback={onFeedback} />);
    await waitFor(() => {
      expect(screen.getByText("布局尚未保存")).toBeInTheDocument();
    });
    expect((document.getElementById("scene-title") as HTMLInputElement).value).toBe("未保存本地标题");

    // Save layout -> sends PUT and clears dirty
    fireEvent.click(document.getElementById("save-layout") as HTMLButtonElement);
    await waitFor(() => {
      expect(putCalls).toBe(1);
      expect(document.getElementById("save-state")?.textContent).toBe("布局已保存");
    });
    expect(hasAnyUnsavedChanges()).toBe(false);
    expect(onFeedback).toHaveBeenCalledWith("布局已保存，OBS画面将同步更新。");
  });
});
