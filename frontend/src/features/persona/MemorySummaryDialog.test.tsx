import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AppSessionProvider, type AppSessionValue } from "@/shared/hooks/useAppSession";
import { PersonalizationPage } from "./PersonalizationPage";

function makeSession(overrides: Partial<AppSessionValue> = {}): AppSessionValue {
  return {
    authenticated: true,
    localAccess: true,
    wsConnected: true,
    status: { state: "stopped", character: { id: "whale", version: 1 } },
    settings: { local_access: true },
    refreshStatus: vi.fn(async () => null),
    refreshSettings: vi.fn(async () => null),
    navigate: vi.fn(),
    logout: vi.fn(async () => {}),
    onUnauthorized: vi.fn(),
    ...overrides,
  };
}

function makeReport(scope: string) {
  return {
    scope,
    enabled: true,
    organization: { status: "idle" },
    organized: [],
    highlights: [],
    summary: [],
  };
}

describe("MemorySummaryDialog / PersonalizationPage", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("slow memory summary survives poll ticks and aborts on close, scope change, and timeout", async () => {
    const reads: Array<{
      signal: AbortSignal | undefined;
      resolve: (res: Response) => void;
      reject: (err: unknown) => void;
    }> = [];
    const deadlines: AbortController[] = [];

    vi.spyOn(AbortSignal, "timeout").mockImplementation(() => {
      const controller = new AbortController();
      deadlines.push(controller);
      return controller.signal;
    });

    let currentScope = "first";
    const prefResolvers: Array<(res: Response) => void> = [];
    let holdPref = false;

    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/api/v1/persona/daily-memory/preferences")) {
          if (holdPref) {
            return new Promise<Response>((resolve) => {
              prefResolvers.push(resolve);
            });
          }
          return Promise.resolve(Response.json({ scope: currentScope, enabled: true, history_enabled: true }));
        }
        if (url.includes("/api/v1/persona/daily-memory?limit=100")) {
          return new Promise<Response>((resolve, reject) => {
            reads.push({ signal: init?.signal ?? undefined, resolve, reject });
          });
        }
        return Promise.resolve(Response.json({}));
      })
    );

    render(
      <AppSessionProvider value={makeSession()}>
        <PersonalizationPage />
      </AppSessionProvider>
    );

    const opener = document.getElementById("open-memory-summary") as HTMLButtonElement;
    const loadState = document.getElementById("memory-load-state") as HTMLElement;
    const summaryContainer = document.getElementById("memory-summary") as HTMLElement;

    await waitFor(() => {
      expect(document.getElementById("memory-enabled")).not.toBeDisabled();
    });

    // 1. Open dialog -> starts read[0]; background 15s polls must NOT start a second read
    fireEvent.click(opener);
    expect(reads.length).toBe(1);
    expect(loadState.hidden).toBe(false);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    expect(reads.length).toBe(1);

    reads[0].resolve(Response.json(makeReport("first")));
    await waitFor(() => expect(loadState.hidden).toBe(true));
    expect(summaryContainer.hidden).toBe(false);
    expect(document.getElementById("memory-summary-list")?.textContent).toContain("暂无已保存的长期记忆。");

    // 2. Start background poll read[1], then change scope -> aborts read[1] and starts read[2]
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    expect(reads.length).toBe(2);

    currentScope = "second";
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    await waitFor(() => expect(reads.length).toBe(3));
    expect(reads[1].signal?.aborted).toBe(true);

    // Late resolution of aborted read[1] does not clear loading state
    reads[1].resolve(Response.json(makeReport("first")));
    await act(async () => {
      await Promise.resolve();
    });
    expect(loadState.hidden).toBe(false);

    reads[2].resolve(Response.json(makeReport("second")));
    await waitFor(() => expect(loadState.hidden).toBe(true));

    // 3. Background poll read[3] aborted when dialog closes
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    expect(reads.length).toBe(4);
    fireEvent.click(screen.getByRole("button", { name: "关闭记忆摘要" }));
    expect(reads[3].signal?.aborted).toBe(true);

    // 4. Reopen -> timeout deadline aborts read[4] -> displays timeout + retry
    fireEvent.click(opener);
    await waitFor(() => expect(reads.length).toBe(5));
    act(() => {
      deadlines[deadlines.length - 1].abort();
      reads[4].reject(new Error("timed out"));
    });
    await waitFor(() => expect(loadState.textContent).toContain("读取记忆摘要超时。"));

    const retryBtn = loadState.querySelector("button") as HTMLButtonElement;
    fireEvent.click(retryBtn);
    await waitFor(() => expect(reads.length).toBe(6));
    reads[5].resolve(Response.json(makeReport("second")));
    await waitFor(() => expect(loadState.hidden).toBe(true));

    // 5. Unconfirmed owner statement survives same-scope reload and clears on scope change
    fireEvent.click(screen.getByRole("button", { name: "关闭记忆摘要" }));
    const statement = document.getElementById("owner-memory-statement") as HTMLInputElement;
    fireEvent.change(statement, { target: { value: "这是尚未确认的原话" } });

    holdPref = true;
    act(() => {
      document.dispatchEvent(new Event("studio-character-change"));
    });
    expect(statement.disabled).toBe(true);

    prefResolvers.shift()!(Response.json({ scope: "second", enabled: true, history_enabled: true }));
    await waitFor(() => expect(statement.disabled).toBe(false));
    expect(statement.value).toBe("这是尚未确认的原话");

    currentScope = "third";
    act(() => {
      document.dispatchEvent(new Event("studio-character-change"));
    });
    expect(statement.disabled).toBe(true);
    prefResolvers.shift()!(Response.json({ scope: "third", enabled: true, history_enabled: true }));
    await waitFor(() => expect(statement.disabled).toBe(false));
    expect(statement.value).toBe("");
  });

  it("supports preference toggling, summary ask/update, highlight editing, and public incident creation", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const calls: Array<{ url: string; method: string; body: unknown }> = [];

    const reportData = {
      scope: "s1",
      enabled: true,
      organization: { status: "running" },
      summary_updated_at: 1700000000,
      summary: [
        {
          kind: "preference",
          notes: [{ id: 11, text: "汇报先写结论", visibility: "owner" }],
        },
      ],
      organized: [
        {
          id: 11,
          memory_kind: "private",
          headline: "汇报结构",
          quote: "汇报先写结论",
          anecdote: "汇报先写结论",
          status: "active",
          channels: [],
          source: { key: "turn:1", at: 1700000000, references: [] },
          updated_at: 1700000000,
          revision: 3,
          can_share: true,
          owner_only: true,
          visibility: "owner",
        },
      ],
      highlights: [
        {
          id: 21,
          headline: "海底趣事",
          anecdote: "差点忘记吃晚饭",
          highlight_date: "2026-11-20",
          quote: "先吃饭吧",
          public_sources: [],
        },
      ],
    };

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method || "GET";
        const body = init?.body ? JSON.parse(String(init.body)) : null;
        calls.push({ url, method, body });

        if (url.endsWith("/api/v1/persona/daily-memory/preferences") && method === "GET") {
          return Response.json({ scope: "s1", enabled: true, history_enabled: true });
        }
        if (url.endsWith("/api/v1/persona/daily-memory/preferences") && method === "PUT") {
          return Response.json({ scope: "s1", enabled: body.enabled, history_enabled: body.history_enabled });
        }
        if (url.includes("/api/v1/persona/daily-memory?limit=100")) {
          return Response.json(reportData);
        }
        if (url.endsWith("/api/v1/persona/daily-memory/summary") && method === "POST") {
          return Response.json({
            scope: "s1",
            status: "found",
            items: [reportData.organized[0]],
          });
        }
        if (url.includes("/api/v1/persona/daily-memory/organized/11") && method === "PATCH") {
          reportData.organized[0].anecdote = body.note || reportData.organized[0].anecdote;
          return Response.json(reportData.organized[0]);
        }
        if (url.includes("/api/v1/persona/daily-memory/21") && method === "PUT") {
          reportData.highlights[0].anecdote = body.anecdote;
          return Response.json(reportData.highlights[0]);
        }
        if (url.endsWith("/api/v1/persona/daily-memory/incidents") && method === "POST") {
          return Response.json({ id: 31, ...body });
        }
        return Response.json({});
      })
    );

    render(
      <AppSessionProvider value={makeSession()}>
        <PersonalizationPage />
      </AppSessionProvider>
    );

    const historyToggle = await screen.findByLabelText("回复时引用以前的私聊");
    await waitFor(() => expect(historyToggle).not.toBeDisabled());
    await user.click(historyToggle);
    await waitFor(() =>
      expect(
        calls.some(
          (c) =>
            c.url.endsWith("/api/v1/persona/daily-memory/preferences") &&
            c.method === "PUT" &&
            (c.body as { history_enabled: boolean }).history_enabled === false
        )
      ).toBe(true)
    );

    await user.click(document.getElementById("open-memory-summary")!);
    await within(document.getElementById("memory-summary-list")!).findAllByText(/汇报先写结论/);

    await user.type(document.getElementById("memory-summary-question")!, "汇报有什么要求？");
    await user.click(screen.getByRole("button", { name: "询问" }));
    await screen.findByText("找到 1 条相关记忆。");

    await user.click(document.getElementById("open-memory-items")!);
    expect(document.getElementById("daily-memory-notice")?.textContent).toBe("正在后台整理");

    await user.click(screen.getByText("早期记忆记录"));
    await screen.findByText("海底趣事");
    await user.click(screen.getByText("更正"));
    const highlightForm = document.querySelector("#daily-memory-list details form") as HTMLFormElement;
    const noteBox = highlightForm.querySelector("textarea") as HTMLTextAreaElement;
    fireEvent.change(noteBox, { target: { value: "更新后的趣事内容" } });
    fireEvent.submit(highlightForm);
    await waitFor(() =>
      expect(calls.some((c) => c.url.includes("/api/v1/persona/daily-memory/21") && c.method === "PUT")).toBe(true)
    );

    await user.click(screen.getByText("登记公开趣事"));
    fireEvent.change(document.getElementById("incident-key")!, { target: { value: "inc-1" } });
    fireEvent.change(document.getElementById("incident-headline")!, { target: { value: "直播接梗" } });
    fireEvent.change(document.getElementById("incident-anecdote")!, { target: { value: "和观众一起接梗" } });
    fireEvent.change(document.getElementById("incident-bvid")!, { target: { value: "BV1xx411c7mD" } });
    fireEvent.submit(document.getElementById("record-incident-form")!);
    await waitFor(() =>
      expect(
        calls.some((c) => c.url.endsWith("/api/v1/persona/daily-memory/incidents") && c.method === "POST")
      ).toBe(true)
    );
  });

  it("manages unified memories: filter by scope, add, inline edit, toggle active, and delete", async () => {
    const user = userEvent.setup();
    let store: Array<{
      id: string;
      character_id: string;
      scope: "owner" | "public";
      content: string;
      active: boolean;
      source: string;
      tags: string[];
    }> = [
      {
        id: "m-owner-1",
        character_id: "whale",
        scope: "owner",
        content: "只和号主约定深夜复盘",
        active: true,
        source: "manual",
        tags: [],
      },
      {
        id: "m-public-1",
        character_id: "whale",
        scope: "public",
        content: "公开评论区拒绝被叫大肥鱼",
        active: true,
        source: "manual",
        tags: [],
      },
    ];
    const calls: Array<{ url: string; method: string; body?: string }> = [];
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? "GET").toUpperCase();
        const rawBody = typeof init?.body === "string" ? init.body : undefined;
        calls.push({ url, method, body: rawBody });

        if (url.includes("/api/v1/persona/daily-memory/preferences")) {
          return Response.json({ enabled: true, statement: "", reference_chat_history: true });
        }
        const parsed = new URL(url, "http://localhost");
        if (parsed.pathname === "/api/v1/persona/memories" && method === "GET") {
          const scopeParam = parsed.searchParams.get("scope");
          const filtered = scopeParam ? store.filter((item) => item.scope === scopeParam) : store;
          return Response.json({ character_id: "whale", items: filtered });
        }
        if (parsed.pathname === "/api/v1/persona/memories" && method === "POST") {
          const payload = JSON.parse(rawBody ?? "{}") as { content: string; scope: "owner" | "public" };
          const created = {
            id: "m-new-1",
            character_id: "whale",
            scope: payload.scope,
            content: payload.content,
            active: true,
            source: "manual",
            tags: [],
          };
          store = [created, ...store];
          return Response.json(created, { status: 201 });
        }
        if (parsed.pathname.startsWith("/api/v1/persona/memories/") && method === "PATCH") {
          const memId = parsed.pathname.split("/").pop() ?? "";
          const payload = JSON.parse(rawBody ?? "{}") as { content?: string; active?: boolean };
          let updated = store.find((item) => item.id === memId);
          store = store.map((item) => {
            if (item.id !== memId) return item;
            updated = {
              ...item,
              ...(payload.content !== undefined ? { content: payload.content } : {}),
              ...(payload.active !== undefined ? { active: payload.active } : {}),
            };
            return updated;
          });
          return Response.json(updated ?? {});
        }
        if (parsed.pathname.startsWith("/api/v1/persona/memories/") && method === "DELETE") {
          const memId = parsed.pathname.split("/").pop() ?? "";
          store = store.filter((item) => item.id !== memId);
          return Response.json({ ok: true, id: memId });
        }
        return Response.json({});
      })
    );

    render(
      <AppSessionProvider value={makeSession()}>
        <PersonalizationPage />
      </AppSessionProvider>
    );

    await screen.findByText("只和号主约定深夜复盘");
    expect(screen.getByText("公开评论区拒绝被叫大肥鱼")).toBeTruthy();

    // Filter to public only
    await user.click(screen.getByRole("tab", { name: "公开互动" }));
    await waitFor(() => {
      expect(screen.queryByText("只和号主约定深夜复盘")).toBeNull();
    });
    expect(screen.getByText("公开评论区拒绝被叫大肥鱼")).toBeTruthy();

    // Switch back to all
    await user.click(screen.getByRole("tab", { name: "全部" }));
    await screen.findByText("只和号主约定深夜复盘");

    // Add a new public memory
    await user.selectOptions(screen.getByLabelText("新增记忆作用域"), "public");
    await user.type(screen.getByLabelText("新增记忆内容"), "评论区遇到造谣直接拆穿");
    await user.click(screen.getByRole("button", { name: "添加记忆" }));
    await screen.findByText("评论区遇到造谣直接拆穿");

    // Inline edit the newly added memory
    const editButtons = screen.getAllByRole("button", { name: "编辑" });
    await user.click(editButtons[0]!);
    const editInput = screen.getByLabelText("编辑记忆内容");
    await user.clear(editInput);
    await user.type(editInput, "评论区遇到造谣冷静拆穿");
    await user.click(screen.getByRole("button", { name: "保存" }));
    await screen.findByText("评论区遇到造谣冷静拆穿");

    // Toggle active
    const toggleButtons = screen.getAllByRole("button", { name: "已启用" });
    await user.click(toggleButtons[0]!);
    await screen.findByRole("button", { name: "已停用" });

    // Delete memory
    const deleteButtons = screen.getAllByRole("button", { name: "删除" });
    await user.click(deleteButtons[0]!);
    await waitFor(() => {
      expect(screen.queryByText("评论区遇到造谣冷静拆穿")).toBeNull();
    });
    expect(calls.some((c) => c.method === "DELETE" && c.url.includes("/api/v1/persona/memories/m-new-1"))).toBe(true);
  });
});
