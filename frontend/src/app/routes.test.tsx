import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { parseHashRoute } from "@/shell/navigation";

vi.mock("@/features/chat", () => ({
  ChatFeature: () => <div data-testid="chat-feature-stub">chat page</div>,
}));

vi.mock("@/features/live", () => ({
  LiveFeature: () => <div data-testid="live-feature-stub">live page</div>,
}));

vi.mock("@/features/settings", () => ({
  SettingsFeature: () => <div data-testid="settings-feature-stub">settings page</div>,
}));

vi.mock("@/features/persona", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/features/persona")>();
  return {
    ...actual,
    PersonaFeature: () => <div data-testid="personalization-feature-stub">persona page</div>,
  };
});

import { operationsStore } from "@/shared/operations/operationsStore";
import { AutomationStatusBridge, ManageMemoriesHost, renderWorkbenchRoute } from "./routes";

describe("routes.tsx ManageMemoriesHost & AutomationStatusBridge", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    window.history.replaceState(null, "", "#chat");
  });

  it("opens global ManageMemoriesModal on #chat when aqua:open-manage-memories is dispatched with detail", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          character_id: "aqua",
          items: [
            {
              id: "m-public-9",
              character_id: "aqua",
              scope: "public",
              content: "视频标题偏好简洁直白",
              active: true,
              source: "tool",
              tags: [],
            },
          ],
          count: 1,
          total_count: 1,
          limit: 100,
          over_limit: false,
          legacy_migrated_count: 0,
        })
      )
    );

    render(<>{renderWorkbenchRoute(parseHashRoute("#chat"))}</>);
    expect(screen.getByTestId("chat-feature-stub")).toBeTruthy();

    const evt = new CustomEvent("aqua:open-manage-memories", {
      cancelable: true,
      detail: { scope: "public", snippet: "简洁直白", memoryId: "m-public-9" },
    });
    let unhandled = true;
    await act(async () => {
      unhandled = window.dispatchEvent(evt);
    });

    // ManageMemoriesHost prevents duplicate standalone modal fallback
    expect(unhandled).toBe(false);
    const matched = await screen.findByText("视频标题偏好简洁直白");
    expect(matched.closest("li")?.getAttribute("data-highlighted")).toBe("true");
    expect(screen.getByRole("tab", { name: "公开互动 (1)" }).getAttribute("aria-selected")).toBe("true");
  });

  it("opens Personalization slot and ManageMemoriesModal on #personalization#manage-memories and cleans hash on close", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          character_id: "aqua",
          items: [],
          count: 0,
          total_count: 0,
          limit: 100,
          over_limit: false,
          legacy_migrated_count: 0,
        })
      )
    );
    window.history.replaceState(null, "", "#personalization#manage-memories");

    const parsed = parseHashRoute("#personalization#manage-memories");
    render(<>{renderWorkbenchRoute(parsed)}</>);

    expect(screen.getByTestId("personalization-feature-stub")).toBeTruthy();
    await waitFor(() => {
      expect(document.getElementById("manage-memories-dialog")?.hasAttribute("open")).toBe(true);
    });

    await user.click(screen.getByRole("button", { name: "关闭管理记忆" }));
    expect(window.location.hash).toBe("#personalization");
  });

  it("bridges aqua:automation-status events into operationsStore methods", () => {
    const liveSpy = vi.spyOn(operationsStore, "applyLiveStatus").mockImplementation(() => {});
    const fullSpy = vi.spyOn(operationsStore, "applyAutomationStatus").mockImplementation(() => true);
    const refreshSpy = vi.spyOn(operationsStore, "refreshStatus").mockResolvedValue(null);

    render(
      <>
        <ManageMemoriesHost currentHash="#chat" />
        <AutomationStatusBridge />
      </>
    );

    window.dispatchEvent(
      new CustomEvent("aqua:automation-status", {
        detail: { automation: { state: "starting", enabled: true }, refresh: false },
      })
    );
    expect(liveSpy).toHaveBeenCalledWith({ state: "starting", enabled: true });

    const fullPayload = { state: "running", policy: { enabled: true } };
    window.dispatchEvent(
      new CustomEvent("aqua:automation-status", {
        detail: { automation: fullPayload, refresh: true },
      })
    );
    expect(fullSpy).toHaveBeenCalledWith(fullPayload);
    expect(refreshSpy).toHaveBeenCalledTimes(1);
  });
});
