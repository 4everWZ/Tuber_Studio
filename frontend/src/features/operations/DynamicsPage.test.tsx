import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearDirtyRegistry } from "@/shared/lib/dirtyRegistry";
import { parseHashRoute } from "@/shell/navigation";
import { DynamicsPage } from "./DynamicsPage";
import { operationsStore } from "./operationsStore";
import type { DynamicPost } from "./types";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function makePost(id: number, overrides: Partial<DynamicPost> = {}): DynamicPost {
  return {
    id,
    status: "pending_review",
    content: `初始动态正文 ${id}`,
    review_key: `rk-${id}`,
    created_at: 1700000000,
    ...overrides,
  };
}

describe("DynamicsPage", () => {
  beforeEach(() => {
    operationsStore.reset();
    clearDirtyRegistry();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    operationsStore.reset();
    clearDirtyRegistry();
  });

  it("preserves inline draft edits across manual refreshes and clears them on reject", async () => {
    const user = userEvent.setup();
    let currentPosts: DynamicPost[] = [makePost(1), makePost(2)];
    const reviewCalls: Array<{ postId: string; body: unknown }> = [];

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method || "GET").toUpperCase();
        const body = init?.body ? JSON.parse(String(init.body)) : undefined;

        if (url === "/api/v1/operations/dynamics/policy") {
          return jsonResponse({ enabled: true, auto_publish: false });
        }
        if (url === "/api/v1/operations/dynamics/posts") return jsonResponse(currentPosts);
        if (url === "/api/v1/operations/dynamics/decisions") return jsonResponse([]);
        if (url === "/api/v1/operations/dynamics/materials") return jsonResponse({ sources: [] });
        if (url.startsWith("/api/v1/operations/dynamics/memories")) return jsonResponse([]);

        if (url.endsWith("/review") && method === "POST") {
          const postId = url.split("/").slice(-2)[0];
          reviewCalls.push({ postId, body });
          if (body.action === "reject") {
            currentPosts = currentPosts.map((p) =>
              String(p.id) === postId ? { ...p, status: "rejected" } : p
            );
          }
          return jsonResponse(currentPosts[0]);
        }
        return jsonResponse({});
      })
    );

    const { container } = render(<DynamicsPage route={parseHashRoute("#dynamics")} />);

    const post1Btn = (await container.querySelector('[data-post-id="1"]')) ||
      (await screen.findByText(/#1 · 待审核/));
    await user.click(post1Btn);

    const textarea = container.querySelector("[data-dynamic-textarea]") as HTMLTextAreaElement;
    await user.clear(textarea);
    await user.type(textarea, "编辑但尚未保存的动态文案");

    // Manual refresh preserves unsaved local edit for post #1
    await user.click(container.querySelector("[data-refresh-dynamics]") as HTMLElement);
    await waitFor(() => {
      expect(
        (container.querySelector("[data-dynamic-textarea]") as HTMLTextAreaElement).value
      ).toBe("编辑但尚未保存的动态文案");
    });

    // Reject clears edited text and deselects post
    await user.click(container.querySelector("[data-reject-dynamic]") as HTMLElement);
    await waitFor(() => {
      expect(reviewCalls.some((c) => c.postId === "1" && (c.body as { action: string }).action === "reject")).toBe(true);
      expect(container.querySelector("[data-dynamic-textarea]")).toBeNull();
    });
  });

  it("refreshes into publish_uncertain and disables publish on publish error", async () => {
    const user = userEvent.setup();
    let currentPost = makePost(5, { status: "approved", content: "待发布的动态" });

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method || "GET").toUpperCase();
        if (url === "/api/v1/operations/dynamics/policy") {
          return jsonResponse({ enabled: true, auto_publish: false });
        }
        if (url === "/api/v1/operations/dynamics/posts") return jsonResponse([currentPost]);
        if (url === "/api/v1/operations/dynamics/decisions") return jsonResponse([]);
        if (url === "/api/v1/operations/dynamics/materials") return jsonResponse({ sources: [] });
        if (url.startsWith("/api/v1/operations/dynamics/memories")) return jsonResponse([]);
        if (url === "/api/v1/operations/dynamics/posts/5/publish" && method === "POST") {
          currentPost = makePost(5, {
            status: "publish_uncertain",
            content: "待发布的动态",
            error: "响应超时",
          });
          return jsonResponse({ detail: "提交响应超时" }, 504);
        }
        return jsonResponse({});
      })
    );

    const { container } = render(<DynamicsPage route={parseHashRoute("#dynamics")} />);
    await user.click(await screen.findByText(/#5 · 已审核/));

    const publishBtn = container.querySelector("[data-publish-dynamic]") as HTMLButtonElement;
    expect(publishBtn).toBeEnabled();
    await user.click(publishBtn);

    await waitFor(() => {
      expect(publishBtn).toBeDisabled();
      expect(container.querySelector("[data-publish-uncertain-notice]")?.textContent).toContain(
        "请核对 B 站动态与发布回执。"
      );
    });
  });

  it("handles background poll throttling and keeps dirty editor readOnly when removed to terminal state", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    let postsFetchCount = 0;
    let currentPosts: DynamicPost[] = [makePost(9, { status: "pending_review" })];

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/v1/operations/dynamics/policy") return jsonResponse({ enabled: true });
        if (url === "/api/v1/operations/dynamics/posts") {
          postsFetchCount += 1;
          return jsonResponse(currentPosts);
        }
        if (url === "/api/v1/operations/dynamics/decisions") return jsonResponse([]);
        if (url === "/api/v1/operations/dynamics/materials") return jsonResponse({ sources: [] });
        if (url.startsWith("/api/v1/operations/dynamics/memories")) return jsonResponse([]);
        return jsonResponse({});
      })
    );

    const { container } = render(<DynamicsPage route={parseHashRoute("#dynamics")} />);
    await waitFor(() => expect(postsFetchCount).toBeGreaterThanOrEqual(1));

    await user.click(await screen.findByText(/#9 · 待审核/));
    const textarea = container.querySelector("[data-dynamic-textarea]") as HTMLTextAreaElement;
    await user.type(textarea, " · 本地追加");

    // Advance 10s so background poll runs once while post #9 turns terminal ("published")
    currentPosts = [makePost(9, { status: "published" })];
    await act(async () => {
      vi.advanceTimersByTime(10_500);
    });

    await waitFor(() => {
      expect(container.querySelector("[data-detached-notice]")?.textContent).toContain(
        "草稿已移出当前列表，本地修改可复制。"
      );
    });
    const detachedTextarea = container.querySelector("[data-dynamic-textarea]") as HTMLTextAreaElement;
    expect(detachedTextarea.readOnly).toBe(true);
    expect(detachedTextarea.disabled).toBe(false);
    expect(detachedTextarea.value).toContain("本地追加");
  });

  it("renders comment card participants, deduplicated links, card images with onError fallback, and inline edit apply", async () => {
    const user = userEvent.setup();

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method || "GET").toUpperCase();
        if (url === "/api/v1/operations/dynamics/policy") return jsonResponse({ enabled: true });
        if (url === "/api/v1/operations/dynamics/posts") {
          return jsonResponse([
            makePost(12, {
              comment_card: {
                page_count: 1,
                omitted: 2,
                missing: 1,
                comments: [
                  { author: "粉丝A", uid: 42, quote: "这条太有趣了", time_label: "今天 12:00" },
                ],
                links: [
                  { bvid: "BV1card1", url: "https://www.bilibili.com/video/BV1card1#reply1" },
                  { bvid: "BV1card1", url: "https://www.bilibili.com/video/BV1card1#reply2" },
                ],
              },
              sources: [
                {
                  type: "comment",
                  text: "同视频重复链接不应再展示",
                  source_url: "https://www.bilibili.com/video/BV1card1#reply9",
                },
                {
                  type: "video",
                  text: "另一个来源视频",
                  source_url: "https://www.bilibili.com/video/BV1other2",
                },
              ],
            }),
          ]);
        }
        if (url === "/api/v1/operations/dynamics/decisions") return jsonResponse([]);
        if (url === "/api/v1/operations/dynamics/materials") return jsonResponse({ sources: [] });
        if (url.startsWith("/api/v1/operations/dynamics/memories")) return jsonResponse([]);
        if (url === "/api/v1/operations/dynamics/edits/req-42/apply" && method === "POST") {
          return jsonResponse({ request_id: "req-42", status: "applied", platform_id: "dyn-999" });
        }
        return jsonResponse({});
      })
    );

    const { container } = render(<DynamicsPage route={parseHashRoute("#dynamics/edits/req-42")} />);

    await user.click(await screen.findByText(/#12 · 待审核/));

    // Participants, omitted/missing notices, and deduplicated BVID links
    expect(container.querySelector("[data-comment-card-participants]")?.textContent).toContain(
      "粉丝A"
    );
    expect(screen.getByText(/已略去 2 条未展示评论/)).toBeInTheDocument();
    expect(screen.getByText(/另有 1 条关联评论已缺失或不可读/)).toBeInTheDocument();
    const cardLinks = container.querySelectorAll("[data-comment-card-links] a");
    expect(cardLinks).toHaveLength(1);
    expect(cardLinks[0].textContent).toBe("打开原评论 · BV1card1");

    // Duplicate BV1card1 source_url suppressed inside sources list, while BV1other2 renders
    expect(container.querySelectorAll('[data-dynamic-sources] a[href*="BV1card1"]')).toHaveLength(0);
    expect(container.querySelectorAll('[data-dynamic-sources] a[href*="BV1other2"]')).toHaveLength(1);

    // Comment card image error displays accessible fallback
    const cardImg = container.querySelector("img.commentCardImage, img[src*='comment-card']") as HTMLImageElement;
    expect(cardImg?.src).toContain("/api/v1/operations/dynamics/posts/12/comment-card?page=1");
    fireEvent.error(cardImg);
    expect(await screen.findByText(/评论引用图第 1 页加载失败/)).toBeInTheDocument();

    // Inline dynamic edit apply from route #dynamics/edits/req-42
    const applyBtn = container.querySelector("[data-apply-dynamic-edit]") as HTMLButtonElement;
    expect((container.querySelector("[data-dynamic-edit-request-id]") as HTMLInputElement).value).toBe(
      "req-42"
    );
    await user.click(applyBtn);
    await waitFor(() => {
      expect(container.querySelector("[data-dynamic-edit-status]")?.textContent).toContain(
        "已核对原动态更新"
      );
      expect(applyBtn).toBeDisabled();
    });
  });
});
