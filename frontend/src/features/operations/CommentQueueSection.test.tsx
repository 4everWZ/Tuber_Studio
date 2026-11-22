import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearDirtyRegistry } from "@/shared/lib/dirtyRegistry";
import { parseHashRoute } from "@/shell/navigation";
import { CommentQueueSection, queueViewForStatus } from "./CommentQueueSection";
import { CommentsImportForm } from "./CommentsImportForm";
import { CommentsPage } from "./CommentsPage";
import { CommentsSourcesSection } from "./CommentsSourcesSection";
import { commentCanGenerate, commentFinished, reconcileCommentSelection } from "./commentState";
import { operationsStore } from "./operationsStore";
import type { CommentRecord, QueueDetail, QueueRow } from "./types";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function makeQueueRow(id: string, overrides: Partial<QueueRow> = {}): QueueRow {
  return {
    channel: "comments",
    id,
    status: "pending_review",
    source: "manual",
    video_id: "BV1qtest",
    excerpt: `草稿摘要 ${id}`,
    updated_at: 1700000100,
    can_publish: true,
    ...overrides,
  };
}

function makeQueueDetail(id: string, overrides: Partial<QueueDetail> = {}): QueueDetail {
  return {
    ...makeQueueRow(id, overrides),
    text: `回复正文 ${id}`,
    source_text: `原评论 ${id}`,
    can_attach_image: true,
    can_publish: true,
    author: "UP主粉丝",
    account_id: "acc-1",
    character_id: "feiyu",
    ...overrides,
  };
}

describe("commentState & queueViewForStatus", () => {
  it("maps statuses to queue views and classifies finished comments accurately", () => {
    expect(queueViewForStatus("pending_review")).toBe("review");
    expect(queueViewForStatus("send_queued")).toBe("authorized");
    expect(queueViewForStatus("send_waiting")).toBe("authorized");
    expect(queueViewForStatus("send_blocked")).toBe("blocked");
    expect(queueViewForStatus("publish_uncertain")).toBe("verify");
    expect(queueViewForStatus("queued")).toBe("processing");
    expect(queueViewForStatus("published")).toBe("history");

    expect(commentFinished({ status: "published" })).toBe(true);
    expect(commentFinished({ status: "private_handoff" })).toBe(true);
    expect(commentFinished({ status: "pending_review" })).toBe(false);

    const openComment = { id: 1, video_id: "BV1", content: "hi", draft: null } as unknown as CommentRecord;
    const doneComment = {
      id: 2,
      video_id: "BV1",
      content: "done",
      draft: { status: "published" },
    } as unknown as CommentRecord;
    expect(commentCanGenerate(openComment)).toBe(true);
    expect(commentCanGenerate(doneComment)).toBe(false);
    const reconciled = reconcileCommentSelection([openComment, doneComment], new Set([1, 2]));
    expect(Array.from(reconciled)).toEqual([1]);
  });
});

describe("CommentQueueSection", () => {
  beforeEach(() => {
    operationsStore.reset();
    clearDirtyRegistry();
    if (!HTMLDialogElement.prototype.showModal) {
      HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
        this.setAttribute("open", "");
      };
    }
    if (!HTMLDialogElement.prototype.close) {
      HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) {
        this.removeAttribute("open");
      };
    }
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    operationsStore.reset();
    clearDirtyRegistry();
  });

  it("deep-links #comments/history/comments/42 directly into history view", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.startsWith("/api/v1/operations/comments/workbench?view=history")) {
          return jsonResponse({
            items: [makeQueueRow("42", { status: "published" })],
            total: 1,
          });
        }
        if (url === "/api/v1/operations/comments/workbench/comments/42") {
          return jsonResponse(makeQueueDetail("42", { status: "published" }));
        }
        return jsonResponse({});
      })
    );

    const { container } = render(
      <CommentQueueSection
        initialView="history"
        initialRouteKey="comments/42"
        inferRouteView={false}
        generation={null}
        sourcesContent={null}
      />
    );

    const radio = container.querySelector('input[name="queue-status"][value="history"]') as HTMLInputElement;
    expect(radio).not.toBeNull();
    expect(radio.checked).toBe(true);
    expect(await screen.findByRole("heading", { name: "平台已受理" })).toBeInTheDocument();
  });

  it("supports inline edit, retryable publish failure recovery, and MemePickerModal artwork swap", async () => {
    const user = userEvent.setup();
    const requests: Array<{ url: string; method: string; body: unknown }> = [];
    let currentDetail = makeQueueDetail("10");

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method || "GET").toUpperCase();
        const body = init?.body ? JSON.parse(String(init.body)) : undefined;
        requests.push({ url, method, body });

        if (url.startsWith("/api/v1/operations/comments/workbench?view=review")) {
          return jsonResponse({ items: [makeQueueRow("10", { status: currentDetail.status })], total: 1 });
        }
        if (url === "/api/v1/operations/comments/workbench/comments/10") {
          return jsonResponse(currentDetail);
        }
        if (url.startsWith("/api/v1/persona/memes")) {
          return jsonResponse({
            items: [{ asset_id: "meme-smile", title: "开心鱼", enabled: true }],
            assets: [{ asset_id: "meme-smile", title: "开心鱼", enabled: true }],
          });
        }
        if (url === "/api/v1/operations/comments/drafts/10/review" && method === "POST") {
          const next = {
            ...currentDetail,
            status: body.action === "approve" ? "approved" : currentDetail.status,
            text: body.text ?? currentDetail.text,
            illustration: body.image_ref ?? currentDetail.illustration,
          };
          currentDetail = next;
          return jsonResponse(next);
        }
        if (url === "/api/v1/operations/comments/drafts/10/publish" && method === "POST") {
          return jsonResponse({ detail: "B站限流，请稍后再试" }, 503);
        }
        return jsonResponse({});
      })
    );

    render(
      <CommentQueueSection
        initialView="review"
        initialRouteKey={null}
        inferRouteView={false}
        generation={null}
        sourcesContent={null}
      />
    );

    const rowLink = await screen.findByText("草稿摘要 10");
    await user.click(rowLink);

    const textarea = (await screen.findByLabelText("草稿正文")) as HTMLTextAreaElement;
    expect(textarea.value).toBe("回复正文 10");

    await user.clear(textarea);
    await user.type(textarea, "改写后的评论回复");

    // Publish approves with text then attempts publish (fails with 503 without wiping detail)
    await user.click(screen.getByRole("button", { name: "发布" }));
    await waitFor(() => {
      expect(
        requests.some((r) => r.url === "/api/v1/operations/comments/drafts/10/publish")
      ).toBe(true);
      expect(screen.getByText(/B站限流/)).toBeInTheDocument();
    });
    expect(screen.getByLabelText("草稿正文")).toHaveValue("改写后的评论回复");
  });

  it("attaches selected artwork via MemePickerModal", async () => {
    const user = userEvent.setup();
    const requests: Array<{ url: string; method: string; body: unknown }> = [];
    let currentDetail = makeQueueDetail("10");

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method || "GET").toUpperCase();
        const body = init?.body ? JSON.parse(String(init.body)) : undefined;
        requests.push({ url, method, body });

        if (url.startsWith("/api/v1/operations/comments/workbench?view=review")) {
          return jsonResponse({ items: [makeQueueRow("10")], offset: 0, total: 1 });
        }
        if (url === "/api/v1/operations/comments/workbench/comments/10") {
          return jsonResponse(currentDetail);
        }
        if (url.startsWith("/api/v1/persona/memes")) {
          return jsonResponse({
            items: [{ asset_id: "meme-smile", title: "开心鱼", enabled: true }],
            assets: [{ asset_id: "meme-smile", title: "开心鱼", enabled: true }],
          });
        }
        if (url === "/api/v1/operations/comments/drafts/10/review" && method === "POST") {
          currentDetail = { ...currentDetail, illustration: body.image_ref };
          return jsonResponse(currentDetail);
        }
        return jsonResponse({});
      })
    );

    const { container } = render(
      <CommentQueueSection
        initialView="review"
        initialRouteKey={null}
        inferRouteView={false}
        generation={null}
        sourcesContent={null}
      />
    );

    await screen.findByLabelText("草稿正文");
    await user.click(screen.getByRole("button", { name: "选择配图" }));
    const memeItem = await waitFor(() => {
      const btn = container.querySelector('.meme-picker [data-picker-grid] button, .meme-picker [data-meme-id]');
      if (!btn && !screen.queryByText(/开心鱼/)) throw new Error("waiting for meme");
      return (btn || screen.getByText(/开心鱼/).closest("button") || screen.getByText(/开心鱼/)) as HTMLElement;
    });
    await user.click(memeItem);

    await waitFor(() => {
      expect(
        requests.some(
          (r) =>
            r.url === "/api/v1/operations/comments/drafts/10/review" &&
            (r.body as { image_ref?: { asset_id?: string } }).image_ref?.asset_id === "meme-smile"
        )
      ).toBe(true);
    });
  });

  it("preserves cross-page checkedDrafts, approves via #queue-approval dialog, and streams SSE batch-publish", async () => {
    const user = userEvent.setup();
    const postCalls: Array<{ url: string; body: unknown }> = [];
    const page1Rows = Array.from({ length: 10 }, (_, idx) => makeQueueRow(`p1-${idx + 1}`));
    const page2Rows = [makeQueueRow("p2-1"), makeQueueRow("p2-2")];

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method || "GET").toUpperCase();
        const body = init?.body ? JSON.parse(String(init.body)) : undefined;

        if (url.startsWith("/api/v1/operations/comments/workbench?")) {
          const parsed = new URL(url, "http://localhost");
          const offset = Number(parsed.searchParams.get("offset") || "0");
          return jsonResponse({
            items: offset >= 10 ? page2Rows : page1Rows,
            offset,
            total: 12,
          });
        }
        if (url.startsWith("/api/v1/operations/comments/workbench/comments/")) {
          const id = url.split("/").pop() || "x";
          return jsonResponse(makeQueueDetail(id));
        }
        if (url === "/api/v1/operations/comments/workbench/batch-review/preview" && method === "POST") {
          postCalls.push({ url, body });
          return jsonResponse({
            items: (body.items as Array<{ channel: string; id: string }>).map((item) => ({
              ...item,
              status: "ready",
              account_id: "acc-1",
            })),
          });
        }
        if (
          (url === "/api/v1/operations/comments/workbench/batch-review/save" ||
            url === "/api/v1/operations/comments/workbench/batch-review/queue") &&
          method === "POST"
        ) {
          postCalls.push({ url, body });
          const isQueue = url.endsWith("/queue");
          return jsonResponse({
            items: (body.items as Array<{ channel: string; id: string }>).map((item) => ({
              ...item,
              status: isQueue ? "send_queued" : "approved",
            })),
          });
        }
        if (url === "/api/v1/operations/comments/workbench/batch-publish" && method === "POST") {
          postCalls.push({ url, body });
          const encoder = new TextEncoder();
          const sseText =
            'event: progress\ndata: {"channel":"comments","id":"p1-1","status":"published"}\n\n' +
            'event: done\ndata: {"items":[{"channel":"comments","id":"p1-1","status":"published"},{"channel":"comments","id":"p2-1","status":"published"}]}\n\n';
          return new Response(
            new ReadableStream({
              start(controller) {
                controller.enqueue(encoder.encode(sseText));
                controller.close();
              },
            }),
            { status: 200, headers: { "Content-Type": "text/event-stream" } }
          );
        }
        if (url === "/api/v1/agent/reply-selections" && method === "POST") {
          postCalls.push({ url, body });
          return jsonResponse({ ok: true, id: "sel-1", count: 2 });
        }
        return jsonResponse({});
      })
    );

    const { container } = render(
      <CommentQueueSection
        initialView="review"
        initialRouteKey={null}
        inferRouteView={false}
        generation={null}
        sourcesContent={null}
      />
    );

    // Check p1-1 on page 1
    const chk1 = (await screen.findByLabelText("选择草稿：草稿摘要 p1-1")) as HTMLInputElement;
    await user.click(chk1);
    expect(document.getElementById("queue-batch-count")?.textContent).toBe("已选 1 条");

    // Navigate to page 2 and check p2-1
    await user.click(document.getElementById("queue-next") as HTMLElement);
    const chk2 = (await screen.findByLabelText("选择草稿：草稿摘要 p2-1")) as HTMLInputElement;
    await user.click(chk2);
    expect(document.getElementById("queue-batch-count")?.textContent).toBe("已选 2 条");

    // Batch publish
    await user.click(document.getElementById("queue-batch-stream-publish") as HTMLElement);
    await waitFor(() => {
      expect(
        postCalls.some((c) => c.url === "/api/v1/operations/comments/workbench/batch-publish")
      ).toBe(true);
      expect(container.querySelector("#queue-batch-result")?.textContent).toContain("已直接发布 2 条");
    });

    // Check p2-1 again, test reply handoff delegation (/api/v1/agent/reply-selections), then approval modal queue
    await user.click(await screen.findByLabelText("选择草稿：草稿摘要 p2-1"));
    await user.click(document.getElementById("queue-batch-publish") as HTMLElement);
    const confirmBtn = await waitFor(() => {
      const btn = document.getElementById("queue-approval-submit");
      expect(btn).not.toBeNull();
      return btn as HTMLElement;
    });
    await user.click(confirmBtn);
    await waitFor(() => {
      expect(
        postCalls.some(
          (c) => c.url === "/api/v1/operations/comments/workbench/batch-review/queue"
        )
      ).toBe(true);
    });
  });

  it("clears clean selection and protects dirty edits on background refresh", async () => {
    const user = userEvent.setup();
    let rowExists = true;

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.startsWith("/api/v1/operations/comments/workbench?view=review")) {
          return jsonResponse({
            items: rowExists ? [makeQueueRow("20")] : [],
            total: rowExists ? 1 : 0,
          });
        }
        if (url === "/api/v1/operations/comments/workbench/comments/20") {
          return jsonResponse(
            makeQueueDetail("20", {
              status: rowExists ? "pending_review" : "published",
            })
          );
        }
        return jsonResponse({});
      })
    );

    const { rerender } = render(
      <CommentQueueSection
        initialView="review"
        initialRouteKey={null}
        inferRouteView={false}
        generation={null}
        sourcesContent={null}
        refreshTick={0}
      />
    );

    await user.click(await screen.findByText("草稿摘要 20"));
    const textarea = (await screen.findByLabelText("草稿正文")) as HTMLTextAreaElement;
    await user.clear(textarea);
    await user.type(textarea, "未保存的重要修改");

    // Simulate draft moving to published in background while dirty -> editor retained as readOnly
    rowExists = false;
    rerender(
      <CommentQueueSection
        initialView="review"
        initialRouteKey={null}
        inferRouteView={false}
        generation={null}
        sourcesContent={null}
        refreshTick={1}
      />
    );

    await waitFor(() => {
      expect(screen.getByLabelText("本地未保存修改")).toHaveValue("未保存的重要修改");
    });
    expect(screen.getByLabelText("本地未保存修改")).toHaveAttribute("readonly");
  });
});

describe("CommentsSourcesSection, CommentsImportForm & CommentsPage", () => {
  beforeEach(() => {
    operationsStore.reset();
    clearDirtyRegistry();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("preserves checked source selection when generate-drafts fails and clears on success", async () => {
    const user = userEvent.setup();
    let shouldFail = true;
    const onSelectionChange = vi.fn();

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url === "/api/v1/operations/comments/drafts" && init?.method === "POST") {
          if (shouldFail) return jsonResponse({ detail: "网关超限" }, 429);
          return jsonResponse({ task_id: "tsk-1", queued: 1, comment_ids: [101] });
        }
        return jsonResponse({});
      })
    );

    const comments: CommentRecord[] = [
      {
        id: 101,
        video_id: "BV1src",
        thread_id: "t-1",
        display_name: "观众1",
        content: "请问下次直播什么时候",
        category: "question",
        draft: null,
      } as CommentRecord,
    ];

    const onDraftsRequested = vi.fn();
    render(
      <CommentsSourcesSection
        comments={comments}
        filters={{ videoId: "", threadId: "", category: "", status: "" }}
        sourcePage={0}
        selectedCommentId={null}
        selectedComments={new Set([101])}
        onFiltersChange={vi.fn()}
        onPageChange={vi.fn()}
        onSelectComment={vi.fn()}
        onSelectedCommentsChange={onSelectionChange}
        onCommentUpdated={vi.fn()}
        onRefreshComments={vi.fn(async () => {})}
        onDraftsRequested={onDraftsRequested}
        onFocusThread={vi.fn()}
      />
    );

    // Failed generation MUST NOT clear selectedComments
    await user.click(document.getElementById("generate-drafts") as HTMLElement);
    await waitFor(() => {
      expect(onDraftsRequested).not.toHaveBeenCalled();
    });
    expect(onSelectionChange).not.toHaveBeenCalledWith(new Set());

    // Success clears selectedComments and calls onDraftsRequested
    shouldFail = false;
    await user.click(document.getElementById("generate-drafts") as HTMLElement);
    await waitFor(() => {
      expect(onDraftsRequested).toHaveBeenCalledWith("tsk-1", [101]);
    });
  });

  it("imports JSON in a single write without re-previewing and fetches comments by video & pages", async () => {
    const user = userEvent.setup();
    const postCalls: Array<{ url: string; body: unknown }> = [];

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const body = init?.body ? JSON.parse(String(init.body)) : undefined;
        postCalls.push({ url, body });
        if (url === "/api/v1/operations/comments/fetch") {
          return jsonResponse({ count: 5, video_id: "BV1fetch1" });
        }
        if (url === "/api/v1/operations/comments/import") {
          return jsonResponse({ imported: 1, video_id: "BV1imp1" });
        }
        return jsonResponse({});
      })
    );

    const onScopeChanged = vi.fn();
    render(
      <CommentsImportForm
        sourcesOpen
        onSourcesOpenChange={vi.fn()}
        onScopeChanged={onScopeChanged}
      />
    );

    await user.type(document.getElementById("comment-video") as HTMLInputElement, "BV1fetch1");
    await user.selectOptions(document.getElementById("comment-pages") as HTMLSelectElement, "3");
    await user.click(document.getElementById("submit-fetch") as HTMLElement);
    await waitFor(() => {
      expect(postCalls).toContainEqual({
        url: "/api/v1/operations/comments/fetch",
        body: { video: "BV1fetch1", max_pages: 3 },
      });
    });

    await user.click(screen.getByText("导入 JSON / CSV"));
    await user.type(document.getElementById("import-video") as HTMLInputElement, "BV1imp1");
    const importCommentsEl = document.getElementById("import-comments") as HTMLTextAreaElement;
    await user.click(importCommentsEl);
    await user.paste('[{"comment_id":"c1","content":"测试导入"}]');
    await user.click(document.getElementById("submit-import") as HTMLElement);

    await waitFor(() => {
      const importPosts = postCalls.filter((c) => c.url === "/api/v1/operations/comments/import");
      expect(importPosts).toHaveLength(1);
      expect(postCalls.some((c) => c.url === "/api/v1/operations/comments/import/preview")).toBe(
        false
      );
    });
  });

  it("renders task-scoped generation view from #comments/review/task/:taskId", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/v1/operations/comments/tasks/task-77") {
          return jsonResponse({ id: "task-77", status: "running", total: 2, done: 1 });
        }
        if (url.startsWith("/api/v1/operations/comments/workbench")) {
          return jsonResponse({ items: [makeQueueRow("77")], total: 1 });
        }
        if (url.startsWith("/api/v1/operations/comments/tasks")) {
          return jsonResponse([{ id: "task-77", status: "running", total: 2, done: 1 }]);
        }
        if (url.startsWith("/api/v1/operations/comments")) {
          return jsonResponse([]);
        }
        return jsonResponse({});
      })
    );

    render(<CommentsPage route={parseHashRoute("#comments/review/task/task-77")} />);
    expect(await screen.findByText("本次生成的回复")).toBeInTheDocument();
  });
});
