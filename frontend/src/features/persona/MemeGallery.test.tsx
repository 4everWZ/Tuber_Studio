import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AssetsPage } from "./AssetsPage";

interface StubMeme {
  asset_id: string;
  title: string;
  enabled: boolean;
  missing?: boolean;
  animated?: boolean;
  frames?: number;
  width?: number;
  height?: number;
  bytes?: number;
  usage_scope?: string;
  emotions?: string[];
  tags?: string[];
  description?: string;
  preview_url?: string;
  source?: {
    kind?: string;
    author?: string;
    author_id?: string;
    url?: string;
  };
  license?: {
    name?: string;
  };
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("MemeGallery & AssetsPage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("lists, filters, edits labels, toggles enabled state, and deletes a disabled meme", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);

    let memes: StubMeme[] = [
      {
        asset_id: "meme-1",
        title: "开心摸鱼",
        enabled: true,
        animated: true,
        frames: 12,
        width: 256,
        height: 256,
        bytes: 4096,
        emotions: ["cheerful"],
        tags: ["摸鱼", "开心"],
        description: "举手欢呼",
        preview_url: "/api/v1/persona/memes/meme-1/preview",
      },
      {
        asset_id: "meme-2",
        title: "害羞躲角落",
        enabled: false,
        animated: false,
        width: 128,
        height: 128,
        bytes: 2048,
        emotions: ["shy"],
        tags: ["害羞"],
        preview_url: "/api/v1/persona/memes/meme-2/preview",
      },
    ];

    const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method || "GET").toUpperCase();
      if (url === "/api/v1/persona/memes" && method === "GET") {
        return jsonResponse({ items: memes });
      }
      if (url === "/api/v1/persona/assets" && method === "GET") {
        return jsonResponse([
          {
            id: "audio-intro",
            kind: "audio",
            url: "/assets/intro.wav",
            tag: "开场白",
            character_id: "fish",
            license_status: "原创",
          },
        ]);
      }
      if (url === "/api/v1/persona/memes/meme-2/labels" && method === "PUT") {
        const body = JSON.parse(String(init?.body || "{}"));
        memes = memes.map((item) =>
          item.asset_id === "meme-2"
            ? { ...item, tags: body.tags, description: body.description }
            : item
        );
        return jsonResponse(memes.find((m) => m.asset_id === "meme-2"));
      }
      if (url === "/api/v1/persona/memes/meme-1" && method === "PUT") {
        const body = JSON.parse(String(init?.body || "{}"));
        memes = memes.map((item) =>
          item.asset_id === "meme-1" ? { ...item, enabled: body.enabled } : item
        );
        return jsonResponse(memes.find((m) => m.asset_id === "meme-1"));
      }
      if (url === "/api/v1/persona/memes/meme-1" && method === "DELETE") {
        memes = memes.filter((item) => item.asset_id !== "meme-1");
        return jsonResponse({ deleted: true });
      }
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", fetchSpy);

    const { container } = render(<AssetsPage />);

    await waitFor(() => {
      expect(container.querySelectorAll(".meme-card").length).toBe(2);
    });
    expect(container.querySelector("[data-meme-state]")?.textContent).toBe("2 张图片 · 1 张可用");
    expect(container.querySelector("#asset-grid audio")).not.toBeNull();

    // Filter by tag
    const filterInput = container.querySelector<HTMLInputElement>("[data-meme-filter]")!;
    fireEvent.change(filterInput, { target: { value: "害羞" } });
    expect(container.querySelectorAll(".meme-card").length).toBe(1);
    expect(container.querySelector(".meme-card h3")?.textContent).toBe("害羞躲角落");

    // Edit labels on meme-2
    const card2 = container.querySelector<HTMLElement>(".meme-card")!;
    fireEvent.change(card2.querySelector<HTMLInputElement>('input[name="tags"]')!, {
      target: { value: "害羞，脸红" },
    });
    fireEvent.change(card2.querySelector<HTMLTextAreaElement>('textarea[name="description"]')!, {
      target: { value: "缩在气泡里" },
    });
    fireEvent.submit(card2.querySelector<HTMLFormElement>("form")!);
    await waitFor(() => {
      expect(container.querySelector("[data-meme-feedback]")?.textContent).toBe(
        "已保存「害羞躲角落」的标签与描述。"
      );
    });
    expect(card2.textContent).toContain("害羞 · 脸红");
    expect(card2.textContent).toContain("缩在气泡里");

    // Clear filter and verify delete guard on enabled meme-1 -> disable -> delete
    fireEvent.change(filterInput, { target: { value: "" } });
    expect(container.querySelectorAll(".meme-card").length).toBe(2);

    const card1 = container.querySelectorAll<HTMLElement>(".meme-card")[0];
    const toggleBtn = screen.getAllByRole("button", { name: "禁用" })[0];
    const deleteBtn1 = card1.querySelector<HTMLButtonElement>("button.danger")!;
    expect(deleteBtn1.disabled).toBe(true);

    fireEvent.click(toggleBtn);
    await waitFor(() => {
      expect(container.querySelector("[data-meme-feedback]")?.textContent).toBe("已禁用此表情包。");
    });

    const refreshedCard1 = container.querySelectorAll<HTMLElement>(".meme-card")[0];
    const deleteBtnUnlocked = refreshedCard1.querySelector<HTMLButtonElement>("button.danger")!;
    expect(deleteBtnUnlocked.disabled).toBe(false);
    fireEvent.click(deleteBtnUnlocked);

    await waitFor(() => {
      expect(container.querySelectorAll(".meme-card").length).toBe(1);
    });
    expect(container.querySelector("[data-meme-feedback]")?.textContent).toBe("图片已从图库删除。");
  });

  it("uploads a multi-file batch with import_id and retries only failed items", async () => {
    const uploadedPayloads: Array<{ title: string; import_id: string }> = [];
    let failSecondOnce = true;
    const uploadedItems: StubMeme[] = [];

    const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method || "GET").toUpperCase();
      if (url === "/api/v1/persona/memes" && method === "GET") {
        return jsonResponse({ items: uploadedItems });
      }
      if (url === "/api/v1/persona/assets" && method === "GET") {
        return jsonResponse([]);
      }
      if (url === "/api/v1/persona/memes" && method === "POST") {
        const body = JSON.parse(String(init?.body || "{}"));
        uploadedPayloads.push({ title: body.title, import_id: body.import_id });
        if (body.title === "second" && failSecondOnce) {
          failSecondOnce = false;
          return new Response(JSON.stringify({ code: "ERROR", message: "尺寸超出限制" }), {
            status: 422,
            headers: { "Content-Type": "application/json" },
          });
        }
        uploadedItems.push({
          asset_id: `uploaded-${uploadedItems.length + 1}`,
          title: body.title,
          enabled: true,
          emotions: body.emotions,
        });
        return jsonResponse(uploadedItems[uploadedItems.length - 1]);
      }
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", fetchSpy);

    const { container } = render(<AssetsPage />);
    await waitFor(() => {
      expect(container.querySelector("[data-meme-state]")?.textContent).toBe("0 张图片 · 0 张可用");
    });

    const uploadForm = container.querySelector<HTMLFormElement>("[data-meme-upload]")!;
    const fileInput = uploadForm.querySelector<HTMLInputElement>('input[name="file"]')!;
    const file1 = new File(["img1"], "first.png", { type: "image/png" });
    const file2 = new File(["img2"], "second.png", { type: "image/png" });

    fireEvent.change(fileInput, { target: { files: [file1, file2] } });
    fireEvent.change(uploadForm.querySelector<HTMLInputElement>('input[name="author"]')!, {
      target: { value: "画师A" },
    });
    fireEvent.change(uploadForm.querySelector<HTMLInputElement>('input[name="license_name"]')!, {
      target: { value: "CC-BY-4.0" },
    });

    fireEvent.submit(uploadForm);

    await waitFor(() => {
      expect(container.querySelector("[data-meme-feedback]")?.textContent).toContain(
        "已导入 1 张，1 张失败。再次上传时只重试失败项。"
      );
    });
    expect(container.querySelector("[data-meme-failures]")?.textContent).toContain(
      "second.png · 尺寸超出限制"
    );
    expect(uploadedPayloads.map((p) => p.title)).toEqual(["first", "second"]);
    const secondImportId = uploadedPayloads[1].import_id;

    // Retry submit -> should ONLY re-POST "second" with the exact same import_id
    fireEvent.submit(uploadForm);
    await waitFor(() => {
      expect(container.querySelector("[data-meme-feedback]")?.textContent).toBe("已导入 2 张图片。");
    });
    expect(uploadedPayloads.map((p) => p.title)).toEqual(["first", "second", "second"]);
    expect(uploadedPayloads[2].import_id).toBe(secondImportId);
    expect(container.querySelectorAll(".meme-card").length).toBe(2);
  });

  it("supports usage_scope filter/toggle, tag pills, inbox review/batch, and subscription management", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);

    let memes: StubMeme[] = [
      {
        asset_id: "m_all",
        title: "全渠道举手",
        enabled: true,
        usage_scope: "all_channels",
        emotions: ["cheerful"],
        tags: ["举手", "开心"],
        source: { kind: "user_upload", author: "作者A", url: "https://example.com/a" },
        license: { name: "CC-BY-4.0" },
      },
      {
        asset_id: "m_int",
        title: "仅互动吐槽",
        enabled: true,
        usage_scope: "interaction_only",
        emotions: ["angry"],
        tags: ["吐槽"],
        source: { kind: "bilibili_comment", author: "评论路人", author_id: "99" },
        license: { name: "来源待确认" },
      },
    ];

    let inbox = [
      {
        item_id: "inb_1",
        sha256: "a".repeat(64),
        bytes: 4096,
        image_info: { width: 200, height: 200, frames: 6, animated: true },
        title: "动图候选",
        emotions: ["cheerful"],
        tags: ["二创"],
        description: "晃动尾巴",
        usage_scope: "all_channels",
        confidence: 0.91,
        ai_tagged: true,
        status: "pending",
        source: { kind: "bilibili_creator", author: "二创画师", author_id: "1001", url: "https://t.bilibili.com/123" },
      },
      {
        item_id: "inb_2",
        sha256: "b".repeat(64),
        bytes: 2048,
        image_info: { width: 150, height: 150, frames: 1, animated: false },
        title: "待视觉打标图",
        emotions: ["idle"],
        tags: ["评论捡图"],
        description: "",
        usage_scope: "interaction_only",
        ai_tagged: false,
        status: "pending_vision",
        source: { kind: "bilibili_comment", author: "路人", url: "https://www.bilibili.com/video/BV1xx411c7mD" },
      },
    ];

    let harvesterConfig = {
      comment_harvest_enabled: true,
      default_sync_interval_hours: 12,
      meme_inbox_limit_mib: 0,
      meme_inbox_max_items: 0,
    };

    let sources = [
      {
        source_id: "src_1",
        kind: "remote_feed",
        target_kind: "github_dir",
        name: "GitHub 表情包仓库",
        url: "https://github.com/org/memes/tree/main/pack",
        enabled: true,
        auto_approve: true,
        sync_interval_hours: 12,
        last_status: "error",
        last_error: "HTTP 403 rate limited",
      },
    ];

    const calls: Array<{ url: string; method: string; body?: unknown }> = [];

    const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method || "GET").toUpperCase();
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      calls.push({ url, method, body });

      if (url === "/api/v1/persona/memes" && method === "GET") {
        return jsonResponse({ items: memes });
      }
      if (url === "/api/v1/persona/assets" && method === "GET") {
        return jsonResponse([]);
      }
      if (url === "/api/v1/persona/memes/m_all" && method === "PUT") {
        memes = memes.map((m) => (m.asset_id === "m_all" ? { ...m, ...body } : m));
        return jsonResponse(memes[0]);
      }
      if (url === "/api/v1/persona/memes/inbox" && method === "GET") {
        return jsonResponse({
          items: inbox,
          pending_count: inbox.length,
          pending_vision_count: inbox.filter((i) => i.status === "pending_vision").length,
          cache: { bytes: 6144, limit_bytes: 0, max_items: 0, unlimited: true },
        });
      }
      if (url === "/api/v1/persona/memes/inbox/inb_1/approve" && method === "POST") {
        inbox = inbox.filter((i) => i.item_id !== "inb_1");
        memes = [
          ...memes,
          {
            asset_id: "m_approved",
            title: body.title,
            enabled: true,
            usage_scope: body.usage_scope,
            emotions: body.emotions,
            tags: body.tags,
            source: { kind: "bilibili_creator", author: "二创画师", author_id: "1001", url: "https://t.bilibili.com/123" },
            license: { name: "来源待确认" },
          },
        ];
        return jsonResponse({ approved: true });
      }
      if (url === "/api/v1/persona/memes/inbox/inb_2/reject" && method === "POST") {
        inbox = inbox.filter((i) => i.item_id !== "inb_2");
        return jsonResponse({ rejected: true });
      }
      if (url === "/api/v1/persona/memes/inbox/batch" && method === "POST") {
        inbox = inbox.filter((i) => !(body.item_ids as string[]).includes(i.item_id));
        return jsonResponse({ ok: true });
      }
      if (url === "/api/v1/persona/memes/sources" && method === "GET") {
        return jsonResponse({ items: sources });
      }
      if (url === "/api/v1/persona/memes/harvester/config" && method === "GET") {
        return jsonResponse(harvesterConfig);
      }
      if (url === "/api/v1/persona/memes/harvester/config" && method === "PUT") {
        harvesterConfig = { ...harvesterConfig, ...body };
        return jsonResponse(harvesterConfig);
      }
      if (url === "/api/v1/persona/memes/sources" && method === "POST") {
        sources = [
          ...sources,
          {
            source_id: "src_2",
            ...body,
            enabled: true,
            last_status: "idle",
            last_error: "",
          },
        ];
        return jsonResponse(sources[sources.length - 1]);
      }
      if (url === "/api/v1/persona/memes/sources/src_1" && method === "PATCH") {
        sources = sources.map((s) => (s.source_id === "src_1" ? { ...s, ...body } : s));
        return jsonResponse(sources[0]);
      }
      if (url === "/api/v1/persona/memes/sources/sync" && method === "POST") {
        return jsonResponse({ ok: true });
      }
      if (url === "/api/v1/persona/memes/sources/src_1" && method === "DELETE") {
        sources = sources.filter((s) => s.source_id !== "src_1");
        return jsonResponse({ deleted: true });
      }
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", fetchSpy);

    const { container } = render(<AssetsPage />);
    await waitFor(() => {
      expect(container.querySelectorAll(".meme-card").length).toBe(2);
    });
    expect(container.querySelector("[data-inbox-badge]")?.textContent).toBe("2");

    // Quick tag pill filter
    fireEvent.click(screen.getByRole("button", { name: /吐槽 \(1\)/ }));
    expect(container.querySelectorAll(".meme-card").length).toBe(1);
    expect(container.querySelector(".meme-card h3")?.textContent).toBe("仅互动吐槽");
    fireEvent.click(screen.getByRole("button", { name: "全部标签" }));
    expect(container.querySelectorAll(".meme-card").length).toBe(2);

    // usage_scope filter & toggle
    const scopeSelect = container.querySelector<HTMLSelectElement>("[data-usage-scope-filter]")!;
    fireEvent.change(scopeSelect, { target: { value: "all_channels" } });
    expect(container.querySelectorAll(".meme-card").length).toBe(1);

    fireEvent.click(screen.getByRole("button", { name: "切换为仅互动" }));
    await waitFor(() => {
      expect(calls.some((c) => c.url === "/api/v1/persona/memes/m_all" && c.method === "PUT")).toBe(true);
    });
    const scopeToggleCall = calls.find((c) => c.url === "/api/v1/persona/memes/m_all" && c.method === "PUT");
    expect(scopeToggleCall?.body).toEqual({ usage_scope: "interaction_only" });

    // Switch to 待审收件箱 tab
    fireEvent.click(screen.getByRole("tab", { name: /待审收件箱/ }));
    await waitFor(() => {
      expect(container.querySelector("[data-inbox-summary]")?.textContent).toContain("不限");
    });
    expect(screen.getByText("AI 置信度 91%")).toBeInTheDocument();
    expect(screen.getByText("等待视觉打标 (pending_vision)")).toBeInTheDocument();
    expect(
      container.querySelector('img[src="/api/v1/persona/memes/inbox/inb_1/preview"]')
    ).not.toBeNull();

    // Edit and approve item 1
    const card1 = container.querySelector<HTMLElement>('[data-inbox-item="inb_1"]')!;
    fireEvent.change(card1.querySelector<HTMLSelectElement>('select[name="inbox_usage_scope"]')!, {
      target: { value: "interaction_only" },
    });
    fireEvent.click(card1.querySelector<HTMLButtonElement>("button.primary")!);
    await waitFor(() => {
      expect(container.querySelector("[data-inbox-badge]")?.textContent).toBe("1");
    });

    // Batch select remaining item & reject
    fireEvent.click(screen.getByRole("button", { name: "全选" }));
    fireEvent.click(screen.getByRole("button", { name: "批量拒绝" }));
    await waitFor(() => {
      expect(container.querySelector("[data-inbox-badge]")?.textContent).toBe("0");
    });

    // Switch to 订阅源管理 tab
    fireEvent.click(screen.getByRole("tab", { name: "订阅源管理" }));
    await waitFor(() => {
      expect(screen.getByText("GitHub 表情包仓库")).toBeInTheDocument();
    });
    expect(screen.getByText(/HTTP 403 rate limited/)).toBeInTheDocument();

    // Switch add source kind to bilibili_creator -> auto_approve must NOT be shown
    const addForm = container.querySelector<HTMLFormElement>("[data-add-source-form]")!;
    fireEvent.change(addForm.querySelector<HTMLSelectElement>('select[name="source_kind"]')!, {
      target: { value: "bilibili_creator" },
    });
    expect(addForm.querySelector('input[name="auto_approve"]')).toBeNull();

    fireEvent.change(addForm.querySelector<HTMLInputElement>('input[name="source_name"]')!, {
      target: { value: "二创作者B" },
    });
    fireEvent.change(addForm.querySelector<HTMLInputElement>('input[name="source_url"]')!, {
      target: { value: "12345" },
    });
    fireEvent.submit(addForm);
    await waitFor(() => {
      expect(screen.getByText("二创作者B")).toBeInTheDocument();
    });

    // Save global harvester config
    const commentToggle = container.querySelector<HTMLInputElement>('input[name="comment_harvest_enabled"]')!;
    fireEvent.click(commentToggle);
    fireEvent.click(screen.getByRole("button", { name: "保存设置" }));
    await waitFor(() => {
      expect(
        calls.some(
          (c) =>
            c.url === "/api/v1/persona/memes/harvester/config" &&
            c.method === "PUT" &&
            (c.body as { comment_harvest_enabled?: boolean }).comment_harvest_enabled === false
        )
      ).toBe(true);
    });
  });
});
