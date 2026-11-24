import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ManageMemoriesModal } from "./ManageMemoriesModal";
import type { UnifiedMemoryItem } from "./types";

function makeMemory(
  id: string,
  overrides: Partial<UnifiedMemoryItem> = {}
): UnifiedMemoryItem {
  return {
    id,
    character_id: "aqua",
    scope: "owner",
    content: `记忆内容 ${id}`,
    active: true,
    source: "manual",
    tags: [],
    created_at: 1730000000,
    updated_at: 1730000000,
    ...overrides,
  };
}

describe("ManageMemoriesModal", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("supports capacity meter, over_limit alert, legacy purge, scope tabs, search, CRUD, and clear confirmation", async () => {
    const user = userEvent.setup();
    let store: UnifiedMemoryItem[] = [
      makeMemory("mig:old-1", {
        scope: "owner",
        content: "早期迁入的旧习惯",
        source: "operation",
        source_key: "mig:daily:1",
        tags: ["迁移"],
      }),
      makeMemory("m-owner-1", {
        scope: "owner",
        content: "晚上十点后不催进度",
        tags: ["作息"],
      }),
      makeMemory("m-public-1", {
        scope: "public",
        content: "评论区喜欢聊海洋冷知识",
        source: "operator_chat",
        tags: ["科普"],
      }),
    ];
    let overLimitFlag = true;
    const calls: Array<{ url: string; method: string; body?: unknown }> = [];
    const onChanged = vi.fn();

    const legacyCount = () => store.filter((item) => item.id.startsWith("mig:")).length;
    const activeCount = () => store.filter((item) => item.active).length;

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? "GET").toUpperCase();
        const body =
          typeof init?.body === "string" ? (JSON.parse(init.body) as Record<string, unknown>) : undefined;
        calls.push({ url, method, body });

        const parsed = new URL(url, "http://localhost");
        if (parsed.pathname === "/api/v1/persona/memories" && method === "GET") {
          return Response.json({
            character_id: "aqua",
            items: store,
            count: overLimitFlag ? 102 : activeCount(),
            total_count: store.length,
            limit: 100,
            over_limit: overLimitFlag,
            legacy_migrated_count: legacyCount(),
          });
        }
        if (parsed.pathname === "/api/v1/persona/memories/purge-migrated" && method === "POST") {
          const before = store.length;
          store = store.filter((item) => !item.id.startsWith("mig:"));
          const purged = before - store.length;
          overLimitFlag = false;
          return Response.json({
            ok: true,
            purged,
            count: activeCount(),
            total_count: store.length,
            limit: 100,
            over_limit: false,
            legacy_migrated_count: 0,
          });
        }
        if (parsed.pathname === "/api/v1/persona/memories/clear" && method === "POST") {
          const deleted = store.length;
          store = [];
          overLimitFlag = false;
          return Response.json({
            ok: true,
            deleted,
            count: 0,
            total_count: 0,
            limit: 100,
            over_limit: false,
            legacy_migrated_count: 0,
          });
        }
        if (parsed.pathname === "/api/v1/persona/memories" && method === "POST") {
          const created = makeMemory("m-created-1", {
            scope: (body?.scope as "owner" | "public") || "owner",
            content: String(body?.content ?? ""),
          });
          store = [created, ...store];
          return Response.json(created, { status: 201 });
        }
        if (parsed.pathname.startsWith("/api/v1/persona/memories/") && method === "PATCH") {
          const id = parsed.pathname.split("/").pop() ?? "";
          let updated: UnifiedMemoryItem | undefined;
          store = store.map((item) => {
            if (item.id !== id) return item;
            updated = {
              ...item,
              ...(typeof body?.content === "string" ? { content: body.content } : {}),
              ...(typeof body?.active === "boolean" ? { active: body.active } : {}),
            };
            return updated;
          });
          return Response.json(updated ?? {});
        }
        if (parsed.pathname.startsWith("/api/v1/persona/memories/") && method === "DELETE") {
          const id = parsed.pathname.split("/").pop() ?? "";
          store = store.filter((item) => item.id !== id);
          return Response.json({ ok: true, id });
        }
        return Response.json({});
      })
    );

    const onClose = vi.fn();
    render(<ManageMemoriesModal open onClose={onClose} onChanged={onChanged} />);

    // Capacity meter + over_limit warning + prominent legacy purge banner
    const counter = await waitFor(() => {
      const el = document.getElementById("memory-capacity-counter");
      expect(el?.textContent).toBe("102 / 100");
      return el;
    });
    expect(counter).toBeTruthy();
    expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("102");
    expect(screen.getByRole("alert").textContent).toContain("已超过建议上限（100 条）");

    const purgeBtn = screen.getByRole("button", { name: "清理历史遗留迁移记录 (1条)" });
    await user.click(purgeBtn);
    await waitFor(() => {
      expect(document.getElementById("purge-migrated-memories")).toBeNull();
    });
    expect(screen.getByText("已清理 1 条历史遗留迁移记录。")).toBeTruthy();
    expect(document.getElementById("memory-capacity-counter")?.textContent).toBe("2 / 100");
    expect(onChanged).toHaveBeenLastCalledWith({
      count: 2,
      total_count: 2,
      limit: 100,
      over_limit: false,
      legacy_migrated_count: 0,
    });

    // Pill tabs filter and sync default add scope; operator_chat displays localized source label
    await user.click(screen.getByRole("tab", { name: "公开互动 (1)" }));
    expect(screen.queryByText("晚上十点后不催进度")).toBeNull();
    const publicRow = screen.getByText("评论区喜欢聊海洋冷知识").closest("li")!;
    expect(within(publicRow).getByText(/对话记住/)).toBeTruthy();
    expect(within(publicRow).queryByText(/operator_chat/)).toBeNull();
    expect((screen.getByLabelText("新增记忆作用域") as HTMLSelectElement).value).toBe("public");

    // Back to all and search by tag/content
    await user.click(screen.getByRole("tab", { name: "全部 (2)" }));
    const searchInput = screen.getByLabelText("搜索记忆");
    await user.type(searchInput, "作息");
    expect(screen.getByText("晚上十点后不催进度")).toBeTruthy();
    expect(screen.queryByText("评论区喜欢聊海洋冷知识")).toBeNull();
    await user.clear(searchInput);

    // Inline add
    await user.selectOptions(screen.getByLabelText("新增记忆作用域"), "public");
    await user.type(screen.getByLabelText("新增记忆内容"), "评论回复尽量用温和吐槽");
    await user.click(screen.getByRole("button", { name: "添加记忆" }));
    await screen.findByText("评论回复尽量用温和吐槽");

    // Inline edit
    const list = screen.getByRole("list", { name: "记忆条目列表" });
    const createdRow = within(list).getByText("评论回复尽量用温和吐槽").closest("li")!;
    await user.click(within(createdRow).getByRole("button", { name: "编辑" }));
    const editBox = within(createdRow).getByLabelText("编辑记忆内容");
    fireEvent.change(editBox, { target: { value: "评论回复保持简洁温柔" } });
    await user.click(within(createdRow).getByRole("button", { name: "保存" }));
    await screen.findByText("评论回复保持简洁温柔");

    // Toggle active -> disabled -> count drops
    const editedRow = within(list).getByText("评论回复保持简洁温柔").closest("li")!;
    await user.click(within(editedRow).getByRole("button", { name: "已启用" }));
    await within(editedRow).findByRole("button", { name: "已停用" });
    expect(document.getElementById("memory-capacity-counter")?.textContent).toBe("2 / 100");

    // Single-item delete
    await user.click(within(editedRow).getByRole("button", { name: "删除" }));
    await waitFor(() => {
      expect(screen.queryByText("评论回复保持简洁温柔")).toBeNull();
    });

    // Clear all memories: cancel first, then confirm
    await user.click(document.getElementById("clear-all-memories")!);
    const confirmDialog = screen.getByRole("alertdialog", { name: "确认清空记忆" });
    await user.click(within(confirmDialog).getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("alertdialog", { name: "确认清空记忆" })).toBeNull();

    await user.click(document.getElementById("clear-all-memories")!);
    await user.click(document.getElementById("confirm-clear-memories")!);
    await screen.findByText("已清空 2 条记忆。");
    expect(document.getElementById("memory-capacity-counter")?.textContent).toBe("0 / 100");
    expect(calls.some((c) => c.url.endsWith("/api/v1/persona/memories/clear") && c.method === "POST")).toBe(
      true
    );
  });

  it("highlights memory matching highlightMemoryId or highlightSnippet and initializes tab scope", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          character_id: "aqua",
          items: [
            makeMemory("m-owner-1", { scope: "owner", content: "私聊记住用户喜欢咖啡" }),
            makeMemory("m-public-7", { scope: "public", content: "公开区喜欢蓝白配色头像" }),
          ],
          count: 2,
          total_count: 2,
          limit: 100,
          over_limit: false,
          legacy_migrated_count: 0,
        })
      )
    );

    const { rerender } = render(
      <ManageMemoriesModal
        open
        initialScope="public"
        highlightMemoryId="m-public-7"
        onClose={vi.fn()}
      />
    );

    const highlighted = await screen.findByText("公开区喜欢蓝白配色头像");
    const li = highlighted.closest("li")!;
    expect(li.getAttribute("data-highlighted")).toBe("true");
    expect(within(li).getByText("刚更新")).toBeTruthy();
    expect(screen.getByRole("tab", { name: "公开互动 (1)" }).getAttribute("aria-selected")).toBe("true");

    // Fallback snippet highlight when highlightMemoryId is omitted
    rerender(
      <ManageMemoriesModal
        open
        initialScope="all"
        highlightSnippet="喜欢咖啡"
        onClose={vi.fn()}
      />
    );
    const ownerEl = await screen.findByText("私聊记住用户喜欢咖啡");
    expect(ownerEl.closest("li")?.getAttribute("data-highlighted")).toBe("true");
  });
});
