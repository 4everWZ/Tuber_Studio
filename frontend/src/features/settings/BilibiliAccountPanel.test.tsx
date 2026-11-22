import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AppSessionProvider, type AppSessionValue } from "@/shared/hooks/useAppSession";
import { BilibiliAccountPanel } from "./BilibiliAccountPanel";
import type { BilibiliAccountStatus } from "./types";

function makeSession(overrides: Partial<AppSessionValue> = {}): AppSessionValue {
  return {
    authenticated: true,
    localAccess: true,
    wsConnected: true,
    status: null,
    settings: null,
    refreshStatus: vi.fn().mockResolvedValue(null),
    refreshSettings: vi.fn().mockResolvedValue(null),
    navigate: vi.fn(),
    logout: vi.fn().mockResolvedValue(undefined),
    onUnauthorized: vi.fn(),
    ...overrides,
  };
}

function makeLoggedInAccount(overrides: Partial<BilibiliAccountStatus> = {}): BilibiliAccountStatus {
  return {
    state: "logged_in",
    configured: { sessdata: true, csrf: true },
    renewal: { state: "available" },
    profile: {
      uid: "123456",
      name: "大肥鱼本鱼",
      avatar_url: "https://i0.hdslb.com/bfs/face/example.jpg",
      coins: 42.5,
      fans: 980,
      bio: "鱼塘日常",
    },
    checked_at: 1710000000,
    verification_stale: false,
    warnings: [],
    ...overrides,
  };
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function errorResponse(status: number, message: string): Response {
  return new Response(
    JSON.stringify({ code: "ERROR", message }),
    {
      status,
      headers: { "Content-Type": "application/json" },
    }
  );
}

describe("BilibiliAccountPanel", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("retains cached profile, avatar, and timestamp on read error while surfacing error state", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let accountGetCount = 0;

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method || "GET").toUpperCase();
        if (url === "/api/v1/operations/account/bilibili" && method === "GET") {
          accountGetCount += 1;
          if (accountGetCount === 1) {
            return jsonResponse(makeLoggedInAccount());
          }
          return errorResponse(503, "B站接口暂时不可用");
        }
        return jsonResponse({});
      })
    );

    const { container } = render(
      <AppSessionProvider value={makeSession()}>
        <BilibiliAccountPanel />
      </AppSessionProvider>
    );

    await waitFor(() => {
      expect(container.querySelector("[data-name]")?.textContent).toBe("大肥鱼本鱼");
    });

    const avatar = container.querySelector<HTMLImageElement>("[data-avatar]")!;
    expect(avatar.getAttribute("src")).toContain("/api/v1/operations/account/bilibili/avatar?v=123456");
    fireEvent.load(avatar);
    expect(avatar.hidden).toBe(false);
    expect(container.querySelector<HTMLElement>("[data-profile-note]")?.hidden).toBe(true);

    // Trigger 30s background poll which fails
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });

    await waitFor(() => {
      expect(container.querySelector("[data-status]")?.textContent).toBe("状态读取失败");
    });

    expect(container.querySelector("[data-status]")?.getAttribute("data-state")).toBe("error");
    expect(container.querySelector("[data-settings-status]")?.textContent).toBe("状态读取失败");
    expect(container.querySelector("[data-name]")?.textContent).toBe("大肥鱼本鱼");
    expect(container.querySelector("[data-uid]")?.textContent).toBe("UID 123456");
    expect(container.querySelector("[data-coins]")?.textContent).toBe("硬币 42.5");
    expect(container.querySelector("[data-fans]")?.textContent).toBe("粉丝 980");
    expect(avatar.hidden).toBe(false);

    const profileNote = container.querySelector<HTMLElement>("[data-profile-note]")!;
    expect(profileNote.hidden).toBe(false);
    expect(profileNote.textContent).toContain("上次资料");

    const headerError = container.querySelector<HTMLElement>("[data-header-error]")!;
    expect(headerError.hidden).toBe(false);
    expect(headerError.textContent).toContain("B站接口暂时不可用");
    expect(container.querySelector("[data-refresh]")?.textContent).toBe("重试读取");

    // Retry read succeeds and restores logged_in state
    accountGetCount = 0;
    fireEvent.click(container.querySelector("[data-refresh]")!);
    await waitFor(() => {
      expect(container.querySelector("[data-status]")?.textContent).toBe("已登录");
    });
    expect(headerError.hidden).toBe(true);
  });

  it("keeps cached profile visible when explicit refresh fails or marks stale", async () => {
    let refreshAttempt = 0;

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method || "GET").toUpperCase();
        if (url === "/api/v1/operations/account/bilibili" && method === "GET") {
          return jsonResponse(makeLoggedInAccount());
        }
        if (url === "/api/v1/operations/account/bilibili/refresh" && method === "POST") {
          refreshAttempt += 1;
          if (refreshAttempt === 1) {
            return errorResponse(502, "刷新超时");
          }
          return jsonResponse(
            makeLoggedInAccount({
              state: "expired",
              verification_stale: true,
              error: "登录态已失效，请重新扫码",
            })
          );
        }
        return jsonResponse({});
      })
    );

    const { container } = render(
      <AppSessionProvider value={makeSession()}>
        <BilibiliAccountPanel />
      </AppSessionProvider>
    );

    await waitFor(() => {
      expect(container.querySelector("[data-status]")?.textContent).toBe("已登录");
    });

    const avatar = container.querySelector<HTMLImageElement>("[data-avatar]")!;
    fireEvent.load(avatar);

    // 1. Refresh fails with HTTP 502
    fireEvent.click(container.querySelector("[data-refresh]")!);
    await waitFor(() => {
      expect(container.querySelector("[data-status]")?.textContent).toBe("状态读取失败");
    });

    expect(container.querySelector("[data-name]")?.textContent).toBe("大肥鱼本鱼");
    expect(avatar.hidden).toBe(false);
    expect(container.querySelector<HTMLElement>("[data-profile-note]")?.hidden).toBe(false);
    expect(container.querySelector("[data-account-feedback]")?.textContent).toContain("刷新超时");

    // 2. Manual verify returns stale expired payload with profile preserved
    fireEvent.click(container.querySelector("[data-verify]")!);
    await waitFor(() => {
      expect(container.querySelector("[data-status]")?.textContent).toBe("凭证已过期");
    });
    expect(container.querySelector("[data-name]")?.textContent).toBe("大肥鱼本鱼");
    expect(avatar.hidden).toBe(false);
    expect(container.querySelector<HTMLElement>("[data-profile-note]")?.hidden).toBe(false);
    expect(container.querySelector("[data-checked]")?.textContent).toContain("等待复核");
    expect(container.querySelector("[data-account-feedback]")?.textContent).toContain(
      "登录态已失效，请重新扫码"
    );
  });

  it("clears displayed identity and hides avatar only on not_configured", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const onChange = vi.fn();

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method || "GET").toUpperCase();
        if (url === "/api/v1/operations/account/bilibili" && method === "GET") {
          return jsonResponse(makeLoggedInAccount());
        }
        if (url === "/api/v1/operations/account/bilibili" && method === "PUT") {
          return jsonResponse({
            state: "not_configured",
            configured: { sessdata: false, csrf: false },
            renewal: { state: "idle" },
            profile: null,
            checked_at: null,
            verification_stale: false,
            warnings: [],
          } satisfies BilibiliAccountStatus);
        }
        return jsonResponse({});
      })
    );

    const { container } = render(
      <AppSessionProvider value={makeSession()}>
        <BilibiliAccountPanel onChange={onChange} />
      </AppSessionProvider>
    );

    await waitFor(() => {
      expect(container.querySelector("[data-name]")?.textContent).toBe("大肥鱼本鱼");
    });

    const avatar = container.querySelector<HTMLImageElement>("[data-avatar]")!;
    fireEvent.load(avatar);
    expect(avatar.hidden).toBe(false);

    fireEvent.click(container.querySelector("[data-clear]")!);

    await waitFor(() => {
      expect(container.querySelector("[data-status]")?.textContent).toBe("未配置");
    });

    expect(container.querySelector("[data-name]")?.textContent).toBe("B站账号");
    expect(container.querySelector("[data-uid]")?.textContent).toBe("UID —");
    expect(container.querySelector("[data-coins]")?.textContent).toBe("硬币 —");
    expect(container.querySelector("[data-fans]")?.textContent).toBe("粉丝 —");
    expect(avatar.hidden).toBe(true);
    expect(container.querySelector<HTMLElement>("[data-profile-note]")?.hidden).toBe(true);
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ state: "not_configured" }));
  });

  it("handles QR scan login lifecycle and cleans up pending poll timer on unmount", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let pollStep = 0;

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method || "GET").toUpperCase();
        if (url === "/api/v1/operations/account/bilibili" && method === "GET") {
          return jsonResponse({
            state: "not_configured",
            configured: { sessdata: false, csrf: false },
            renewal: { state: "idle" },
            profile: null,
          });
        }
        if (url === "/api/v1/operations/account/bilibili/login" && method === "POST") {
          return jsonResponse({ id: "qr-sess-1", expires_in: 180 });
        }
        if (url === "/api/v1/operations/account/bilibili/login/qr-sess-1/poll" && method === "POST") {
          pollStep += 1;
          if (pollStep === 1) {
            return jsonResponse({ state: "scanned" });
          }
          return jsonResponse({
            state: "complete",
            account: makeLoggedInAccount(),
          });
        }
        return jsonResponse({});
      })
    );

    const onChange = vi.fn();
    const { container, unmount } = render(
      <AppSessionProvider value={makeSession()}>
        <BilibiliAccountPanel onChange={onChange} />
      </AppSessionProvider>
    );

    await waitFor(() => {
      expect(container.querySelector("[data-status]")?.textContent).toBe("未配置");
    });

    fireEvent.click(container.querySelector("[data-scan-login]")!);

    await waitFor(() => {
      expect(container.querySelector<HTMLElement>("[data-login-qr]")?.hidden).toBe(false);
    });
    expect(container.querySelector<HTMLImageElement>("[data-qr-image]")?.getAttribute("src")).toBe(
      "/api/v1/operations/account/bilibili/login/qr-sess-1/image"
    );

    // First poll -> scanned
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    await waitFor(() => {
      expect(container.querySelector("[data-qr-status]")?.textContent).toBe(
        "已扫码，请在手机上确认。"
      );
    });

    // Second poll -> complete
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    await waitFor(() => {
      expect(container.querySelector("[data-status]")?.textContent).toBe("已登录");
    });
    expect(container.querySelector<HTMLElement>("[data-login-qr]")?.hidden).toBe(true);
    expect(container.querySelector("[data-account-feedback]")?.textContent).toContain(
      "B站登录凭证已保存，已启用自动续期"
    );
    expect(onChange).toHaveBeenCalled();

    // Start another QR session and verify unmount does not throw or leak timer polls
    fireEvent.click(container.querySelector("[data-scan-login]")!);
    await waitFor(() => {
      expect(container.querySelector<HTMLElement>("[data-login-qr]")?.hidden).toBe(false);
    });
    const pollsBeforeUnmount = pollStep;
    unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6000);
    });
    expect(pollStep).toBe(pollsBeforeUnmount);
  });

  it("masks secret values if manual credential verification returns an error containing them", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method || "GET").toUpperCase();
        if (url === "/api/v1/operations/account/bilibili" && method === "GET") {
          return jsonResponse({
            state: "not_configured",
            configured: { sessdata: false, csrf: false },
            renewal: { state: "idle" },
            profile: null,
          });
        }
        if (url === "/api/v1/operations/account/bilibili" && method === "PUT") {
          return jsonResponse({
            state: "unverified",
            configured: { sessdata: true, csrf: true },
            renewal: { state: "manual" },
            profile: null,
          });
        }
        if (url === "/api/v1/operations/account/bilibili/refresh" && method === "POST") {
          return errorResponse(502, "upstream rejected cookie secret-cookie-999");
        }
        return jsonResponse({});
      })
    );

    const { container } = render(
      <AppSessionProvider value={makeSession()}>
        <BilibiliAccountPanel />
      </AppSessionProvider>
    );

    await waitFor(() => {
      expect(container.querySelector("[data-status]")?.textContent).toBe("未配置");
    });

    const sessInput = container.querySelector<HTMLInputElement>("[data-sessdata]")!;
    const csrfInput = container.querySelector<HTMLInputElement>("[data-csrf]")!;
    fireEvent.change(sessInput, { target: { value: "secret-cookie-999" } });
    fireEvent.change(csrfInput, { target: { value: "csrf-val-888" } });
    fireEvent.submit(container.querySelector<HTMLFormElement>("[data-credentials-form]")!);

    await waitFor(() => {
      expect(container.querySelector("[data-account-feedback]")?.textContent).toContain("[凭证已隐藏]");
    });

    expect(container.querySelector("[data-account-feedback]")?.textContent).not.toContain(
      "secret-cookie-999"
    );
    expect(sessInput.value).toBe("");
    expect(csrfInput.value).toBe("");
  });

  it("supports remote management token reset dialog and defers onUnauthorized until acknowledged", async () => {
    const onUnauthorized = vi.fn();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method || "GET").toUpperCase();
        if (url === "/api/v1/operations/account/bilibili" && method === "GET") {
          return jsonResponse(makeLoggedInAccount());
        }
        if (url === "/api/v1/system/auth/reset-token" && method === "POST") {
          return jsonResponse({ token: "new-mgmt-token-xyz", authenticated: false });
        }
        return jsonResponse({});
      })
    );

    const { container } = render(
      <AppSessionProvider value={makeSession({ localAccess: false, onUnauthorized })}>
        <BilibiliAccountPanel />
      </AppSessionProvider>
    );

    await waitFor(() => {
      expect(container.querySelector("[data-status]")?.textContent).toBe("已登录");
    });

    const remoteCard = container.querySelector<HTMLElement>("[data-remote-management]")!;
    expect(remoteCard.hidden).toBe(false);

    fireEvent.click(container.querySelector("[data-reset-open]")!);
    const dialog = container.querySelector<HTMLDialogElement>("dialog.account-dialog")!;
    expect(dialog.open || dialog.hasAttribute("open")).toBe(true);

    fireEvent.click(container.querySelector("[data-reset-confirm-button]")!);

    await waitFor(() => {
      expect(container.querySelector<HTMLTextAreaElement>("[data-new-token]")?.value).toBe(
        "new-mgmt-token-xyz"
      );
    });
    expect(onUnauthorized).not.toHaveBeenCalled();

    fireEvent.click(container.querySelector("[data-token-copy]")!);
    await waitFor(() => {
      expect(writeText).toHaveBeenCalledWith("new-mgmt-token-xyz");
    });
    expect(screen.getByText("已复制新令牌。保存后可返回登录。")).toBeTruthy();

    fireEvent.click(container.querySelector("[data-token-done]")!);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    expect(container.querySelector<HTMLTextAreaElement>("[data-new-token]")?.value).toBe("");
  });
});
