import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AppSessionProvider, type AppSessionValue } from "@/shared/hooks/useAppSession";
import { clearDirtyRegistry, hasAnyUnsavedChanges } from "@/shared/lib/dirtyRegistry";
import { CachePanel } from "./CachePanel";
import { OverviewPage } from "./OverviewPage";
import { RetentionPanel } from "./RetentionPanel";
import { buildRetentionRequest, type CacheStatusResponse, type RetentionPolicyResponse } from "./types";

function makeSession(overrides: Partial<AppSessionValue> = {}): AppSessionValue {
  return {
    authenticated: true,
    localAccess: true,
    wsConnected: true,
    status: {
      state: "idle",
      queue_length: 2,
      overlay_connected: true,
      character: { id: "fish", display_name: "大肥鱼", voice_profile: "voice-1" },
      context: {
        scope: "live",
        configured_tokens: 16384,
        server_tokens: 32768,
        native_tokens: 32768,
        active_tokens: 1024,
        compact_count: 1,
      },
    } as unknown as NonNullable<AppSessionValue["status"]>,
    settings: { voice_id: "voice-1" } as unknown as NonNullable<AppSessionValue["settings"]>,
    refreshStatus: vi.fn().mockResolvedValue(null),
    refreshSettings: vi.fn().mockResolvedValue(null),
    navigate: vi.fn(),
    logout: vi.fn().mockResolvedValue(undefined),
    onUnauthorized: vi.fn(),
    ...overrides,
  };
}

function makeCacheStatus(overrides: Partial<CacheStatusResponse> = {}): CacheStatusResponse {
  return {
    settings: {
      video_limit_mib: 4096,
      music_limit_mib: 1024,
      speech_limit_mib: 256,
      temporary_limit_mib: 512,
      browsing_limit_mib: 256,
      assets_limit_mib: 512,
      meme_inbox_limit_mib: 512,
      meme_inbox_max_items: 200,
    },
    summary: {
      bytes: 17825792,
      files: 12,
      protected_bytes: 9961472,
      reclaimable_bytes: 7864320,
      limit_bytes: 6979321856,
    },
    categories: {
      video: {
        bytes: 4194304,
        files: 4,
        limit_bytes: 4294967296,
        reclaimable_bytes: 4194304,
        protected_bytes: 0,
        writing: false,
      },
      music: {
        bytes: 10485760,
        files: 3,
        limit_bytes: 1073741824,
        reclaimable_bytes: 2097152,
        protected_bytes: 8388608,
        writing: false,
      },
      speech: {
        bytes: 1048576,
        files: 2,
        limit_bytes: 268435456,
        reclaimable_bytes: 524288,
        protected_bytes: 524288,
        writing: true,
      },
      temporary: {
        bytes: 0,
        files: 0,
        limit_bytes: 536870912,
        reclaimable_bytes: 0,
        protected_bytes: 0,
        writing: false,
      },
      browsing: {
        bytes: 2097152,
        files: 2,
        limit_bytes: 268435456,
        reclaimable_bytes: 1048576,
        protected_bytes: 1048576,
        writing: false,
      },
      assets: {
        bytes: 0,
        files: 1,
        limit_bytes: 536870912,
        reclaimable_bytes: 0,
        protected_bytes: 0,
        writing: false,
      },
    },
    ...overrides,
  };
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("RetentionPanel, CachePanel & OverviewPage", () => {
  afterEach(() => {
    vi.useRealTimers();
    clearDirtyRegistry();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("buildRetentionRequest validates dirty flag, never-clean mode, and UTC date bounds", () => {
    const activePolicy: RetentionPolicyResponse = { days: 30, enabled: true };
    const neverPolicy: RetentionPolicyResponse = { days: null, enabled: false };

    expect(() => buildRetentionRequest(activePolicy, true, "")).toThrow(/请先保存留存策略/);
    expect(buildRetentionRequest(activePolicy, false, "")).toEqual({});
    expect(() => buildRetentionRequest(neverPolicy, false, "")).toThrow(/手动清理请指定截至日期/);

    expect(buildRetentionRequest(neverPolicy, false, "2025-01-15")).toEqual({
      before: "2025-01-16T00:00:00.000Z",
    });

    const todayUtc = new Date().toISOString().slice(0, 10);
    expect(() => buildRetentionRequest(activePolicy, false, todayUtc)).toThrow(/已结束的 UTC 日期/);
  });

  it("toggles retention-never to save { days: null } and runs preview -> confirm -> cleanup", async () => {
    let currentPolicy: RetentionPolicyResponse = { days: 30, enabled: true, last_run: null, next_run: null };

    const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method || "GET").toUpperCase();
      if (url === "/api/v1/system/retention" && method === "GET") {
        return jsonResponse(currentPolicy);
      }
      if (url === "/api/v1/system/retention" && method === "PUT") {
        const body = JSON.parse(String(init?.body || "{}"));
        currentPolicy = {
          days: body.days,
          enabled: body.days != null,
          last_run: null,
          next_run: null,
        };
        return jsonResponse(currentPolicy);
      }
      if (url === "/api/v1/system/retention/preview" && method === "POST") {
        return jsonResponse({
          cutoff: "2025-01-16T00:00:00.000Z",
          counts: { context_windows: 12, events: 34 },
        });
      }
      if (url === "/api/v1/system/retention/cleanup" && method === "POST") {
        return jsonResponse({ counts: { context_windows: 12, events: 34 } });
      }
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", fetchSpy);

    const { container } = render(
      <AppSessionProvider value={makeSession()}>
        <RetentionPanel />
      </AppSessionProvider>
    );

    const neverCheckbox = container.querySelector<HTMLInputElement>("#retention-never")!;
    const daysInput = container.querySelector<HTMLInputElement>("#retention-days")!;
    const saveBtn = container.querySelector<HTMLButtonElement>("#retention-save")!;
    const previewBtn = container.querySelector<HTMLButtonElement>("#retention-preview")!;

    await waitFor(() => {
      expect(daysInput.value).toBe("30");
    });
    expect(saveBtn.disabled).toBe(true);
    expect(previewBtn.disabled).toBe(false);

    // Check "never auto clean"
    fireEvent.click(neverCheckbox);
    expect(neverCheckbox.checked).toBe(true);
    expect(daysInput.disabled).toBe(true);
    expect(saveBtn.disabled).toBe(false);
    expect(previewBtn.disabled).toBe(true);
    expect(hasAnyUnsavedChanges()).toBe(true);

    // Save { days: null }
    fireEvent.submit(container.querySelector("#retention-form")!);
    await waitFor(() => {
      expect(container.querySelector("#retention-status")?.textContent).toContain("永不自动清理");
    });
    expect(container.querySelector("#retention-status")?.textContent).toContain("已关闭");
    expect(hasAnyUnsavedChanges()).toBe(false);

    const putCall = fetchSpy.mock.calls.find(
      ([u, init]) => String(u) === "/api/v1/system/retention" && init?.method === "PUT"
    );
    expect(JSON.parse(String(putCall?.[1]?.body))).toEqual({ days: null });

    // Specify past date -> preview -> require confirm -> cleanup
    fireEvent.change(container.querySelector("#retention-before")!, {
      target: { value: "2025-01-15" },
    });
    fireEvent.click(previewBtn);

    await waitFor(() => {
      expect(container.querySelector<HTMLElement>("#retention-preview-panel")?.hidden).toBe(false);
    });
    expect(container.querySelector("#retention-preview-cutoff")?.textContent).toContain(
      "2025-01-16T00:00:00.000Z"
    );
    expect(container.querySelector("#retention-preview-counts")?.textContent).toContain("上下文窗口");
    expect(container.querySelector("#retention-preview-counts")?.textContent).toContain("12");

    const confirmBox = container.querySelector<HTMLInputElement>("#retention-confirm")!;
    const cleanupBtn = container.querySelector<HTMLButtonElement>("#retention-cleanup")!;
    expect(cleanupBtn.disabled).toBe(true);

    fireEvent.click(confirmBox);
    expect(cleanupBtn.disabled).toBe(false);
    fireEvent.click(cleanupBtn);

    await waitFor(() => {
      expect(container.querySelector("#retention-cleanup-state")?.textContent).toContain("清理已完成");
    });
    const cleanupCall = fetchSpy.mock.calls.find(
      ([u, init]) => String(u) === "/api/v1/system/retention/cleanup" && init?.method === "POST"
    );
    expect(JSON.parse(String(cleanupCall?.[1]?.body))).toEqual({
      before: "2025-01-16T00:00:00.000Z",
      confirm: true,
    });
  });

  it("CachePanel preserves dirty input edits across background refresh and clears/saves limits", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let cacheState = makeCacheStatus();

    const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method || "GET").toUpperCase();
      if (url === "/api/v1/system/cache" && method === "GET") {
        return jsonResponse(cacheState);
      }
      if (url === "/api/v1/system/cache/music/clear" && method === "POST") {
        return jsonResponse({
          ...cacheState,
          result: { freed_bytes: 2097152, removed_files: 3 },
        });
      }
      if (url === "/api/v1/system/cache/clear-all" && method === "POST") {
        return jsonResponse({
          ...cacheState,
          result: { freed_bytes: 7864320, removed_files: 8 },
        });
      }
      if (url === "/api/v1/system/cache" && method === "PUT") {
        const body = JSON.parse(String(init?.body || "{}"));
        cacheState = makeCacheStatus({ settings: body });
        return jsonResponse(cacheState);
      }
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", fetchSpy);

    const { container } = render(<CachePanel />);

    const speechLimit = container.querySelector<HTMLInputElement>('input[name="speech_limit_mib"]')!;
    const videoLimit = container.querySelector<HTMLInputElement>('input[name="video_limit_mib"]')!;
    await waitFor(() => {
      expect(speechLimit.value).toBe("256");
      expect(videoLimit.value).toBe("4096");
    });
    expect(container.querySelector("[data-cache-summary]")?.textContent).toContain("文件总数 12");
    expect(container.querySelector('[data-category="assets"] [data-stats]')?.textContent).toContain("文件 1");

    // Clear music cache
    fireEvent.click(screen.getByRole("button", { name: "清理音乐可回收缓存" }));
    await waitFor(() => {
      expect(container.querySelector('[data-state]')?.textContent).toContain("已回收 2.0 MiB，清理 3 个文件");
    });

    // Bulk clear-all requires confirmation checkbox
    const clearAllConfirm = container.querySelector<HTMLInputElement>("#cache-clear-all-confirm")!;
    const clearAllBtn = container.querySelector<HTMLButtonElement>("#cache-clear-all-btn")!;
    expect(clearAllBtn.disabled).toBe(true);
    fireEvent.click(clearAllConfirm);
    expect(clearAllBtn.disabled).toBe(false);
    fireEvent.click(clearAllBtn);
    await waitFor(() => {
      expect(container.querySelector('[data-state]')?.textContent).toContain("全部可回收缓存已清理");
    });
    expect(clearAllConfirm.checked).toBe(false);

    // Edit speech limit -> mark dirty
    fireEvent.change(speechLimit, { target: { value: "384" } });
    fireEvent.change(videoLimit, { target: { value: "2048" } });
    expect(speechLimit.value).toBe("384");
    expect(videoLimit.value).toBe("2048");
    expect(hasAnyUnsavedChanges()).toBe(true);

    // 5s background poll returns server value 256, must NOT clobber dirty edit 384
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(speechLimit.value).toBe("384");

    // Save limits clears dirty guard
    fireEvent.submit(container.querySelector("form")!);
    await waitFor(() => {
      expect(container.querySelector('[data-state]')?.textContent).toContain("容量设置已保存");
    });
    expect(hasAnyUnsavedChanges()).toBe(false);
  });

  it("OverviewPage renders runtime metrics and executes context inspect/compact/rebuild", async () => {
    const refreshStatus = vi.fn().mockResolvedValue(null);
    const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method || "GET").toUpperCase();
      if (url === "/api/v1/persona/voices") {
        return jsonResponse([{ id: "voice-1", name: "元气鱼声" }]);
      }
      if (url.startsWith("/api/v1/persona/context/status") && method === "GET") {
        return jsonResponse({
          scope: "comments:BV1xx/th-9",
          configured_tokens: 16384,
          server_tokens: 32768,
          native_tokens: 32768,
          active_tokens: 512,
          compact_count: 2,
        });
      }
      if (url === "/api/v1/persona/context/compact" && method === "POST") {
        return jsonResponse({ message: "已完成 1 轮压缩。" });
      }
      if (url === "/api/v1/persona/context/rebuild" && method === "POST") {
        return jsonResponse({ rebuilt: true, prefilled: true, input_tokens: 420 });
      }
      if (url === "/api/v1/system/cache") {
        return jsonResponse(makeCacheStatus());
      }
      if (url === "/api/v1/system/retention") {
        return jsonResponse({ days: 30, enabled: true });
      }
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", fetchSpy);

    const { container } = render(
      <AppSessionProvider value={makeSession({ refreshStatus })}>
        <OverviewPage />
      </AppSessionProvider>
    );

    await waitFor(() => {
      expect(container.querySelector("#overview-speech")?.textContent).toBe("元气鱼声");
    });
    expect(container.querySelector("#overview-queue")?.textContent).toBe("2");
    expect(container.querySelector("#overview-player")?.textContent).toBe("已连接");

    // Live compact
    fireEvent.click(container.querySelector("#compact-live")!);
    await waitFor(() => {
      expect(container.querySelector("#compaction-result")?.textContent).toBe("已完成 1 轮压缩。");
    });
    expect(refreshStatus).toHaveBeenCalled();

    // Comment context inspect & rebuild require video+thread for rebuild
    fireEvent.click(container.querySelector("#rebuild-context-settings")!);
    expect(container.querySelector("#compaction-result")?.textContent).toContain("请先填写评论视频 ID 与单个楼ID");

    fireEvent.change(container.querySelector("#compact-video-id")!, { target: { value: "BV1xx" } });
    fireEvent.change(container.querySelector("#compact-thread-id")!, { target: { value: "th-9" } });

    fireEvent.click(container.querySelector("#inspect-context-status")!);
    await waitFor(() => {
      expect(container.querySelector("#context-details")?.textContent).toContain("comments:BV1xx/th-9");
    });

    fireEvent.click(container.querySelector("#rebuild-context-settings")!);
    await waitFor(() => {
      expect(container.querySelector("#compaction-result")?.textContent).toContain("模型 prefill 已完成，输入 420 tokens");
    });
  });
});
