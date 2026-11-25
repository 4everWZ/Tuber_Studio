import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mainReplySummary, OperationsActivityFeed } from "./OperationsActivityFeed";
import type { ActivityResponse } from "./types";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("mainReplySummary", () => {
  it("summarizes top-level reply status across counts, drafts, decisions, and read states", () => {
    expect(mainReplySummary({ main_replies: 2 })).toBe("今日已发主评 2 条");
    expect(mainReplySummary({ has_main_reply: true })).toBe("已有主评");
    expect(mainReplySummary({ replies: [{ kind: "top_level", status: "pending_review" }] })).toBe(
      "主评待审核"
    );
    expect(mainReplySummary({ main_decision: { action: "skip" } })).toBe("本次不发主评");
    expect(mainReplySummary({ main_decision: { action: "wait" } })).toBe("主评等待继续");
    expect(mainReplySummary({ main_decision: { action: "reply_public" } })).toBe(
      "已决定主评，等待生成"
    );
    expect(mainReplySummary({ read_state: "comments_only" })).toBe("只逛评论，未生成主评");
    expect(mainReplySummary({})).toBe("主评尚未决定");
  });
});

describe("OperationsActivityFeed", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("filters by Stage 2 source (all/discovery/inbox/manual) and displays source badges", async () => {
    const user = userEvent.setup();
    const seenSources: string[] = [];

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = new URL(String(input), "http://localhost");
        const source = url.searchParams.get("source") || "all";
        seenSources.push(source);
        const payload: ActivityResponse = {
          day: "2026-11-22",
          page: 1,
          pages: 1,
          total: 3,
          counts: { total: 3, sent: 2, pending: 1 },
          items: [
            {
              id: "act-1",
              kind: "comment",
              source_pipeline: "inbox",
              status: "published",
              recipient_name: "路人甲",
              text: "收到，感谢提醒",
            },
            {
              id: "act-2",
              kind: "video",
              source_pipeline: "discovery",
              bvid: "BV1discovery1",
              title: "主动探索视频",
              read_state: "complete",
              fully_read: true,
              main_replies: 1,
            },
            {
              id: "act-3",
              kind: "dynamic",
              source_pipeline: "manual",
              status: "published",
              text: "手动发布的动态",
            },
          ].filter((item) => source === "all" || item.source_pipeline === source),
        };
        return jsonResponse(payload);
      })
    );

    const { container } = render(<OperationsActivityFeed />);

    await waitFor(() => {
      expect(container.querySelector('[data-source-badge="inbox"]')).toHaveTextContent(
        "收件箱消息 inbox"
      );
    });
    expect(container.querySelector('[data-source-badge="discovery"]')).toHaveTextContent(
      "主动探索 discovery"
    );
    expect(container.querySelector('[data-source-badge="manual"]')).toHaveTextContent(
      "手动 manual"
    );

    await user.click(container.querySelector('[data-activity-source="inbox"]') as HTMLElement);
    await waitFor(() => {
      expect(seenSources).toContain("inbox");
      expect(container.querySelector('[data-source-badge="discovery"]')).toBeNull();
      expect(container.querySelector('[data-source-badge="inbox"]')).toBeInTheDocument();
    });
  });

  it("renders daily videos with preference, full-read clarity, pagination, and date controls", async () => {
    const user = userEvent.setup();
    const seenQueries: Array<{ page: string; day: string | null; kind: string }> = [];

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = new URL(String(input), "http://localhost");
        const page = Number(url.searchParams.get("page") || "1");
        const day = url.searchParams.get("day");
        const kind = url.searchParams.get("kind") || "all";
        seenQueries.push({ page: String(page), day, kind });
        const payload: ActivityResponse = {
          day: day || "2026-11-22",
          page,
          pages: 2,
          total: 12,
          counts: { videos: 12, watched: 10, replies: 2, skipped: 2 },
          items: [
            {
              id: `vid-p${page}`,
              kind: "video",
              bvid: `BV1page${page}`,
              title: page === 1 ? "第1页视频" : "第2页视频",
              read_state: "complete",
              fully_read: page === 1,
              video_preference: "like",
              preference_reason: "剪辑很有节奏",
              main_replies: 1,
              comment_replies: 2,
              replies: [
                {
                  id: "r-1",
                  kind: "top_level",
                  status: "published",
                  text: "好听！",
                },
              ],
            },
          ],
        };
        return jsonResponse(payload);
      })
    );

    render(<OperationsActivityFeed initialKind="videos" />);

    expect(await screen.findByText("第1页视频")).toBeInTheDocument();
    expect(screen.getByText("完整阅读")).toBeInTheDocument();
    expect(screen.getAllByText(/偏好：喜欢 · 剪辑很有节奏/).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/今日已发主评 1 条 · 今日跟帖 2 条/)).toBeInTheDocument();
    expect(screen.getByText("好听！")).toBeInTheDocument();

    // Pagination next page
    await user.click(screen.getByRole("button", { name: "下一页" }));
    expect(await screen.findByText("第2页视频")).toBeInTheDocument();
    expect(screen.getByText("已看过")).toBeInTheDocument(); // fully_read: false with complete

    // Date change resets page to 1 & Today button clears day
    const dayInput = document.getElementById("discovery-history-day") as HTMLInputElement;
    fireEvent.change(dayInput, { target: { value: "2026-11-20" } });
    await waitFor(() => {
      expect(seenQueries.some((q) => q.day === "2026-11-20" && q.page === "1")).toBe(true);
    });
  });

  it("distinguishes outreach vs reply messages and resolves comment/dynamic links + filter check", async () => {
    const user = userEvent.setup();

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = new URL(String(input), "http://localhost");
        const kind = url.searchParams.get("kind") || "all";
        const commentFilter = url.searchParams.get("comment_filter") || "replied";

        if (kind === "comments" && commentFilter === "read") {
          // Simulate stale/missing backend counts rejection
          return jsonResponse({
            day: "2026-11-22",
            page: 1,
            pages: 1,
            total: 0,
            comment_filter: "replied",
            counts: {},
            items: [],
          });
        }

        return jsonResponse({
          day: "2026-11-22",
          page: 1,
          pages: 1,
          total: 2,
          comment_filter: commentFilter,
          counts:
            kind === "comments"
              ? { sent_top_level: 1, sent_replies: 1 }
              : { total: 2, sent: 1, pending: 0 },
          items: [
            {
              id: "msg-outreach",
              kind: "message",
              direction: "outbound",
              contact_type: "outreach",
              outreach_status: "sent",
              recipient_name: "B站观众A",
              source_text: "原来问的问题",
              account_reply_text: "私信主动问候",
              reason: "agreement_met",
            },
            {
              id: "cmt-dyn",
              kind: "comment",
              reply_kind: "reply",
              comment_outcome: "new_reply",
              recipient_name: "动态网友",
              thread: { resource: "dynamic:987654321", root_id: "r1" },
              source_href: "https://t.bilibili.com/987654321#reply12",
              own_resource: true,
              own_thread: false,
              text: "谢谢来互动",
            },
          ],
        } satisfies ActivityResponse);
      })
    );

    const { container } = render(<OperationsActivityFeed />);

    expect(await screen.findByText("私信主动问候")).toBeInTheDocument();
    const titleLink = container.querySelector(
      '[data-activity-control="title"][href*="987654321"]'
    ) as HTMLAnchorElement | null;
    expect(titleLink?.href).toContain("https://t.bilibili.com/987654321#reply12");

    // Switch to comments filter -> reject mismatched comment_filter payload
    await user.click(container.querySelector('[data-activity-kind="comments"]') as HTMLElement);
    await waitFor(() => {
      expect(screen.getByText(/主评 1 · 楼内回复 1/)).toBeInTheDocument();
    });

    const select = document.getElementById("comment-activity-filter") as HTMLSelectElement;
    await user.selectOptions(select, "read");
    await waitFor(() => {
      expect(document.getElementById("discovery-history-feedback")?.textContent).toContain(
        "当前工作台后端未应用评论筛选"
      );
    });
  });

  it("preserves open <details> and focused controls across background refresh polls", async () => {
    let pollCount = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        pollCount += 1;
        return jsonResponse({
          day: "2026-11-22",
          page: 1,
          pages: 1,
          total: 1,
          counts: { total: 1, sent: 1, pending: 0 },
          items: [
            {
              id: "act-keep",
              kind: "comment",
              source_pipeline: "inbox",
              status: "published",
              bvid: "BV1keepopen",
              video_title: `保持展开 (${pollCount})`,
              round_id: "rnd-abc",
              text: `第 ${pollCount} 次轮询文本`,
            },
          ],
        } satisfies ActivityResponse);
      })
    );

    const { rerender, container } = render(<OperationsActivityFeed refreshTick={0} />);

    const summary = (await screen.findByText("处理详情")) as HTMLElement;
    const details = summary.closest("details") as HTMLDetailsElement;
    expect(details.open).toBe(false);

    act(() => {
      summary.click();
      summary.focus();
    });
    expect(details.open).toBe(true);
    expect(document.activeElement).toBe(summary);

    // Re-render with updated refreshTick simulating 5s dashboard poll
    rerender(<OperationsActivityFeed refreshTick={1} />);

    await waitFor(() => {
      expect(screen.getByText("第 2 次轮询文本")).toBeInTheDocument();
    });

    const detailsAfter = container.querySelector("details") as HTMLDetailsElement;
    expect(detailsAfter.open).toBe(true);
    expect(document.activeElement).toBe(summary);
  });

  it("renders distinct visibility badges/link labels and triggers manual visibility recheck", async () => {
    const user = userEvent.setup();
    let rechecked = false;
    const verifyCalls: string[] = [];

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/operations/intents/intent-self/verify-visibility") && init?.method === "POST") {
          verifyCalls.push(url);
          rechecked = true;
          return jsonResponse({
            id: "intent-self",
            status: "published",
            visibility: "public_visible",
            visibility_checked_at: 1700000200,
          });
        }
        return jsonResponse({
          day: "2026-11-22",
          page: 1,
          pages: 1,
          total: 4,
          counts: { total: 4, sent: 4, pending: 0, visibility_issues: rechecked ? 1 : 2 },
          items: [
            {
              id: "comment/browsing/intent-pub",
              intent_id: "intent-pub",
              kind: "comment",
              status: "published",
              comment_outcome: "new_reply",
              visibility: "public_visible",
              sent_href: "https://www.bilibili.com/video/BV1vis#reply1",
              text: "公开评论",
            },
            {
              id: "comment/browsing/intent-self",
              intent_id: "intent-self",
              kind: "comment",
              status: "published",
              comment_outcome: "new_reply",
              visibility: rechecked ? "public_visible" : "self_only",
              visibility_checked_at: 1700000100,
              visibility_detail: { auth_visible: true, anon_visible: rechecked },
              sent_href: "https://www.bilibili.com/video/BV1vis#reply2",
              text: "仅自己可见评论",
            },
            {
              id: "comment/browsing/intent-gone",
              intent_id: "intent-gone",
              kind: "comment",
              status: "published",
              comment_outcome: "new_reply",
              visibility: "shadow_deleted",
              sent_href: "https://www.bilibili.com/video/BV1vis#reply3",
              text: "被吞评论",
            },
            {
              id: "comment/browsing/intent-wait",
              intent_id: "intent-wait",
              kind: "comment",
              status: "published",
              comment_outcome: "new_reply",
              visibility: "pending_recheck",
              sent_href: "https://www.bilibili.com/video/BV1vis#reply4",
              text: "待复查评论",
            },
          ],
        } satisfies ActivityResponse);
      })
    );

    const { container } = render(<OperationsActivityFeed />);

    expect(await screen.findByText("查看公开回复")).toBeInTheDocument();
    expect(screen.getByText("查看回复（仅自己可见）")).toBeInTheDocument();
    expect(screen.getByText("原回复链接（疑似被吞）")).toBeInTheDocument();
    expect(screen.getByText("回执链接（待复查）")).toBeInTheDocument();
    expect(screen.queryByText("已确认回复")).toBeNull();
    expect(screen.getByText(/可见异常 2/)).toBeInTheDocument();

    const selfArticle = container.querySelector('[data-activity-key*="intent-self"]') as HTMLElement;
    const recheckBtn = selfArticle.querySelector(
      '[data-activity-control="recheck-visibility"]'
    ) as HTMLButtonElement;
    await user.click(recheckBtn);

    await waitFor(() => {
      expect(verifyCalls.length).toBe(1);
      expect(screen.getByText(/可见异常 1/)).toBeInTheDocument();
    });
  });
});
