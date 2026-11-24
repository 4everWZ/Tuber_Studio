import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AppSessionProvider, type AppSessionValue } from "@/shared/hooks/useAppSession";
import { PersonalizationPage } from "./PersonalizationPage";
import { OPEN_MANAGE_MEMORIES_EVENT } from "./types";

function makeSession(overrides: Partial<AppSessionValue> = {}): AppSessionValue {
  return {
    authenticated: true,
    localAccess: true,
    wsConnected: true,
    status: {
      state: "stopped",
      character: { id: "aqua", name: "Aqua", display_name: "Aqua", version: 2 },
    },
    settings: { local_access: true },
    refreshStatus: vi.fn(async () => null),
    refreshSettings: vi.fn(async () => null),
    navigate: vi.fn(),
    logout: vi.fn(async () => {}),
    onUnauthorized: vi.fn(),
    ...overrides,
  };
}

describe("PersonalizationPage (Minimal ChatGPT-Style Settings)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("renders calm switches, manage-memories button with counter, custom instructions form, collapsed drawer, and never renders memory list inline", async () => {
    const user = userEvent.setup();
    const calls: Array<{ url: string; method: string; body?: Record<string, unknown> }> = [];
    const characterCard = {
      id: "aqua",
      name: "Aqua",
      display_name: "Aqua",
      version: 2,
      persona: "元气海洋主播",
      owner_address: "船长",
      style: {
        tone: "温柔直率",
        instructions: "回复简短明确",
      },
      preferences: { like: [], avoid: [] },
      boundaries: [],
    };

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? "GET").toUpperCase();
        const body =
          typeof init?.body === "string"
            ? (JSON.parse(init.body) as Record<string, unknown>)
            : undefined;
        calls.push({ url, method, body });

        if (url.endsWith("/api/v1/persona/daily-memory/preferences") && method === "GET") {
          return Response.json({
            scope: "owner:aqua:v2",
            enabled: true,
            history_enabled: true,
            reference_chat_history: true,
            statement: "",
          });
        }
        if (url.endsWith("/api/v1/persona/daily-memory/preferences") && method === "PUT") {
          return Response.json({
            scope: "owner:aqua:v2",
            enabled: body?.enabled ?? true,
            history_enabled: body?.history_enabled ?? true,
            reference_chat_history: body?.history_enabled ?? true,
            statement: "",
          });
        }
        if (url.includes("/api/v1/persona/memories")) {
          return Response.json({
            character_id: "aqua",
            items: [
              {
                id: "m-hidden-inline",
                character_id: "aqua",
                scope: "owner",
                content: "绝对不应直接展开在设置页上的原条记忆",
                active: true,
                source: "manual",
                tags: [],
              },
            ],
            count: 14,
            total_count: 15,
            limit: 100,
            over_limit: false,
            legacy_migrated_count: 3,
          });
        }
        if (url.endsWith("/api/v1/persona/characters/aqua") && method === "GET") {
          return Response.json(characterCard);
        }
        if (url.endsWith("/api/v1/persona/characters/aqua") && method === "PUT") {
          return Response.json({ ...characterCard, ...body });
        }
        return Response.json({});
      })
    );

    const session = makeSession();
    render(
      <AppSessionProvider value={session}>
        <PersonalizationPage />
      </AppSessionProvider>
    );

    // Switches use expected Copy & Switch roles
    const savedMemorySwitch = (await screen.findByRole("switch", {
      name: /引用已保存记忆/,
    })) as HTMLInputElement;
    const chatHistorySwitch = screen.getByRole("switch", {
      name: /引用历史对话/,
    }) as HTMLInputElement;
    expect(savedMemorySwitch.checked).toBe(true);
    expect(chatHistorySwitch.checked).toBe(true);

    // Single entry button with live counter and legacy hint, NO inline raw memory list or hidden duplicate dialog
    const manageBtn = await screen.findByRole("button", { name: "管理记忆 (14 / 100)" });
    expect(manageBtn).toBeTruthy();
    expect(screen.getByText(/检测到 3 条历史遗留迁移记录待清理/)).toBeTruthy();
    expect(screen.queryByText("绝对不应直接展开在设置页上的原条记忆")).toBeNull();
    expect(document.querySelectorAll("#manage-memories-dialog").length).toBe(0);

    // Collapsed drawer starts closed
    const drawer = screen.getByText("高级上下文与历史工具").closest("details")!;
    expect(drawer.open).toBe(false);

    // Toggle 引用已保存记忆
    await user.click(savedMemorySwitch);
    await waitFor(() => {
      expect(
        calls.some(
          (c) =>
            c.url.endsWith("/api/v1/persona/daily-memory/preferences") &&
            c.method === "PUT" &&
            c.body?.enabled === false
        )
      ).toBe(true);
    });

    // Edit custom instructions & owner_address and save
    const addrInput = await screen.findByDisplayValue("船长");
    const instrInput = screen.getByDisplayValue("回复简短明确");
    await user.clear(addrInput);
    await user.type(addrInput, "领航员");
    await user.clear(instrInput);
    await user.type(instrInput, "优先给出核心结论和下一步建议");

    await user.click(document.getElementById("save-personalization-instructions")!);
    await screen.findByText("已保存个性化偏好。");
    const putCall = calls.find(
      (c) => c.url.endsWith("/api/v1/persona/characters/aqua") && c.method === "PUT"
    );
    expect(putCall?.body?.owner_address).toBe("领航员");
    expect((putCall?.body?.style as { instructions?: string })?.instructions).toBe(
      "优先给出核心结论和下一步建议"
    );
    expect(session.refreshStatus).toHaveBeenCalled();

    // Dispatches global event first when ManageMemoriesHost is active
    const hostSpy = vi.fn((event: Event) => {
      event.preventDefault();
    });
    window.addEventListener(OPEN_MANAGE_MEMORIES_EVENT, hostSpy);
    await user.click(manageBtn);
    expect(hostSpy).toHaveBeenCalledTimes(1);
    window.removeEventListener(OPEN_MANAGE_MEMORIES_EVENT, hostSpy);
  });
});
