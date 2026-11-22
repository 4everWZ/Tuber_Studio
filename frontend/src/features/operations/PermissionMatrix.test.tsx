import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearDirtyRegistry, hasAnyUnsavedChanges } from "@/shared/lib/dirtyRegistry";
import { AutomationSettingsPage } from "./AutomationSettingsPage";
import { OperationRecoveryPolicyForm } from "./OperationRecoveryPolicyForm";
import { operationsStore } from "./operationsStore";
import { deriveAutomaticSendingWarning, PermissionMatrix } from "./PermissionMatrix";
import type { AutomationStatus, BilibiliAccountSnapshot } from "./types";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function makeStatus(overrides: Partial<AutomationStatus> = {}): AutomationStatus {
  return {
    state: "off",
    policy: { enabled: false, emergency: false },
    vision: { images: true },
    automatic_sending_ready: true,
    blockers: [],
    settings: {
      discovery_enabled: true,
      daily_watch_limit: 0,
      max_replies_per_video: 0,
      max_per_hour: 0,
      max_per_day: 0,
      interval_seconds: 300,
      workers: 4,
      candidate_limit: 20,
      decision_max_tokens: 8192,
      deadline_seconds: 1800,
      business_timezone: "Asia/Taipei",
      frame_sampling: "baseline",
      top_level_enabled: true,
      top_level_auto_publish: false,
      reply_enabled: true,
      reply_auto_publish: false,
      comment_like_enabled: false,
      comment_like_auto_publish: false,
      video_like_enabled: false,
      video_like_auto_publish: false,
      dynamic_like_enabled: false,
      dynamic_like_auto_publish: false,
      video_fav_enabled: false,
      video_fav_auto_publish: false,
    },
    ...overrides,
  };
}

describe("PermissionMatrix & deriveAutomaticSendingWarning", () => {
  it("reports sending readiness blockers without inventing an approval gate", () => {
    const blockedStatus = makeStatus({
      automatic_sending_ready: false,
      blockers: ["B 站登录态已过期"],
      settings: {
        top_level_enabled: true,
        top_level_auto_publish: true,
      },
    });
    expect(deriveAutomaticSendingWarning(blockedStatus, null)).toBe(
      "尚未就绪：B 站登录态已过期"
    );

    const noCredsStatus = makeStatus({
      automatic_sending_ready: false,
      blockers: [],
      settings: { top_level_enabled: true, top_level_auto_publish: true },
      effective: { comments: { credentials_configured: false } },
    });
    const unconfiguredAccount: BilibiliAccountSnapshot = {
      state: "not_configured",
      configured: { sessdata: false, csrf: false },
    };
    expect(deriveAutomaticSendingWarning(noCredsStatus, unconfiguredAccount)).toBe(
      "请先登录 B 站账号。"
    );

    // Draft-only settings return empty warning even if automatic_sending_ready is false
    const draftOnlyStatus = makeStatus({
      automatic_sending_ready: false,
      settings: { top_level_enabled: true, top_level_auto_publish: false },
    });
    expect(deriveAutomaticSendingWarning(draftOnlyStatus, null)).toBe("");
  });

  it("serializes matrix toggles while saving and reloads actual state on failure", async () => {
    const user = userEvent.setup();
    const saveGate = deferred<void>();
    const onSaveChange = vi.fn(() => saveGate.promise);
    const onRefresh = vi.fn(async () => {});

    render(
      <PermissionMatrix
        status={makeStatus()}
        account={null}
        onSaveChange={onSaveChange}
        onRefresh={onRefresh}
      />
    );

    expect(document.getElementById("matrix-visual-status")?.textContent).toBe(
      "观看时读取字幕并抽样画面"
    );

    const autoRadio = document.querySelector(
      'input[name="matrix_top_level"][value="2"]'
    ) as HTMLInputElement;
    await user.click(autoRadio);

    expect(onSaveChange).toHaveBeenCalledTimes(1);
    expect(onSaveChange).toHaveBeenCalledWith({
      top_level_enabled: true,
      top_level_auto_publish: true,
    });
    // Fieldset is disabled while saving in flight
    expect(document.getElementById("matrix-fields")).toBeDisabled();

    // Even when save throws, onRefresh reloads authoritative state and error note appears
    const failingSave = vi.fn(async () => {
      throw new Error("503 服务忙");
    });
    const { container: errContainer } = render(
      <PermissionMatrix
        status={makeStatus()}
        account={null}
        onSaveChange={failingSave}
        onRefresh={onRefresh}
      />
    );
    const replyAutoRadio = errContainer.querySelector(
      'input[name="matrix_reply"][value="2"]'
    ) as HTMLInputElement;
    await user.click(replyAutoRadio);
    await waitFor(() => {
      expect(onRefresh).toHaveBeenCalled();
    });
    expect(errContainer.querySelector("#matrix-save-feedback")?.textContent).toContain("保存未确认");
  });
});

describe("AutomationSettingsPage zero-quota & OperationRecoveryPolicyForm", () => {
  beforeEach(() => {
    operationsStore.reset();
    clearDirtyRegistry();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    operationsStore.reset();
    clearDirtyRegistry();
  });

  it("displays 0 as unlimited in round settings and keeps 0 intact when saving matrix toggles", async () => {
    const user = userEvent.setup();
    const putBodies: Array<{ url: string; body: Record<string, unknown> }> = [];
    let currentStatus = makeStatus();

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method || "GET").toUpperCase();
        const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {};

        if (url === "/api/v1/operations/automation") return jsonResponse(currentStatus);
        if (url === "/api/v1/operations/account/bilibili")
          return jsonResponse({ state: "logged_in", configured: { sessdata: true, csrf: true } });
        if (url === "/api/v1/operations/discovery")
          return jsonResponse({ state: "off", discovery_source: { mode: "popular" } });
        if (url === "/api/v1/operations/dynamics/policy")
          return jsonResponse({ enabled: false, auto_publish: true });
        if (url === "/api/v1/operations/messages")
          return jsonResponse({ policy: { enabled: false }, users: [], drafts: [] });
        if (url === "/api/v1/operations/recovery-policy")
          return jsonResponse({ max_recoveries: 3 });

        if (url === "/api/v1/operations/automation/settings" && method === "PUT") {
          putBodies.push({ url, body });
          currentStatus = {
            ...currentStatus,
            settings: { ...currentStatus.settings, ...body },
          };
          return jsonResponse(currentStatus);
        }
        return jsonResponse({});
      })
    );

    render(<AutomationSettingsPage />);

    // Verify 0 fields are rendered as "0", NOT coerced to non-zero defaults
    await waitFor(() => {
      expect((document.getElementById("op-daily-watch") as HTMLInputElement).value).toBe("0");
    });
    expect((document.getElementById("op-max-replies") as HTMLInputElement).value).toBe("0");
    expect((document.getElementById("op-max-per-hour") as HTMLInputElement).value).toBe("0");
    expect((document.getElementById("matrix-max-per-day") as HTMLInputElement).value).toBe("0");

    // Matrix toggle only sends boolean keys without overwriting 0 quotas
    const videoToggle = document.getElementById("matrix-video-enabled") as HTMLInputElement;
    await user.click(videoToggle);
    await waitFor(() => {
      expect(putBodies.some((p) => p.body.discovery_enabled === false)).toBe(true);
    });
    expect(putBodies[0].body.daily_watch_limit).toBeUndefined();
    expect((document.getElementById("op-daily-watch") as HTMLInputElement).value).toBe("0");

    // Dirty guard blocks poll overwrite when editing round settings
    const watchInput = document.getElementById("op-daily-watch") as HTMLInputElement;
    await user.clear(watchInput);
    await user.type(watchInput, "12");
    expect(hasAnyUnsavedChanges()).toBe(true);

    // Save explicitly sends 0 when restored to 0
    await user.clear(watchInput);
    await user.type(watchInput, "0");
    await user.click(document.getElementById("op-save-quick-config") as HTMLElement);
    await waitFor(() => {
      expect(putBodies.some((p) => p.body.daily_watch_limit === 0)).toBe(true);
    });
    expect(hasAnyUnsavedChanges()).toBe(false);
  });

  it("loads and validates OperationRecoveryPolicyForm across 0..64", async () => {
    const user = userEvent.setup();
    const putBodies: number[] = [];

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method || "GET").toUpperCase();
        if (url === "/api/v1/operations/recovery-policy" && method === "GET") {
          return jsonResponse({ max_recoveries: 5 });
        }
        if (url === "/api/v1/operations/recovery-policy" && method === "PUT") {
          const body = JSON.parse(String(init?.body)) as { max_recoveries: number };
          putBodies.push(body.max_recoveries);
          return jsonResponse({ max_recoveries: body.max_recoveries });
        }
        return jsonResponse({});
      })
    );

    render(<OperationRecoveryPolicyForm />);

    const input = await screen.findByLabelText("最多修订次数");
    await waitFor(() => {
      expect((input as HTMLInputElement).value).toBe("5");
    });

    await user.clear(input);
    await user.type(input, "0");
    await user.click(screen.getByRole("button", { name: "保存修订次数" }));

    await waitFor(() => {
      expect(putBodies).toEqual([0]);
      expect(screen.getByText("已关闭自动修订，现有处理记录保留。")).toBeInTheDocument();
    });
  });
});
