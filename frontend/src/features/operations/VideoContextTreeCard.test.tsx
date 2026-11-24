import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AppSessionProvider, type AppSessionValue } from "@/shared/hooks/useAppSession";
import { parseHashRoute } from "@/shell/navigation";
import { AutomationDashboardPage } from "./AutomationDashboardPage";
import { CommentQueueSection } from "./CommentQueueSection";
import { OperationsWorkbench } from "./OperationsWorkbench";
import { VideoContextTreeCard, buildVideoContextTree } from "./VideoContextTreeCard";
import type {
  ActivityItem,
  BrowsingIntent,
  BrowsingTask,
  CommentRecord,
  QueueDetail,
  QueueRow,
} from "./types";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function makeRow(id: string, overrides: Partial<QueueRow> = {}): QueueRow {
  return {
    channel: "browsing",
    id,
    status: "pending_review",
    source: "discovery",
    video_id: "BV1tree888",
    comment_id: 900 + (Number(id) || 1),
    excerpt: `草稿 ${id}`,
    updated_at: 1730000100,
    can_publish: true,
    ...overrides,
  };
}

describe("VideoContextTreeCard & Operations Phase B Enhancements", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("builds Video Base -> Floor Branch tree with coverage badges, first impression, owner takeover turns, and orphan fallback", () => {
    const rows: QueueRow[] = [
      makeRow("10", {
        channel: "browsing",
        video_id: "BV1tree888",
        comment_id: 7002,
        account_id: "123456",
        excerpt: "我也是这么觉得，水母片段很惊艳",
      }),
      makeRow("11", {
        channel: "jobs",
        video_id: undefined,
        excerpt: "独立任务草稿",
      }),
    ];

    const tasks: BrowsingTask[] = [
      {
        id: "bt-1",
        status: "completed",
        bvid: "BV1tree888",
        title: "深海摄影：发光水母的夜晚",
        metadata: {
          owner: { name: "珊瑚摄影师", mid: 888999 },
          owner_uid: 888999,
        },
        material_mode: "subtitle_visual",
        coverage: "full",
        explanation: "整片节奏克制，后半段蓝光水母镜头最抓人。",
        evidence: [{ id: "frame:12" }],
      },
    ];

    const intents: BrowsingIntent[] = [
      {
        id: "10",
        kind: "reply",
        status: "pending_review",
        bvid: "BV1tree888",
        target: {
          root: "7001",
          parent: "7001",
          title: "深海摄影：发光水母的夜晚",
          source_discussion: {
            parent: {
              comment_id: "7001",
              author_uid: "123456",
              author_name: "号主本号",
              text: "这段夜潜打光是我人工补充说明的细节。",
            },
            source: {
              comment_id: "7002",
              author_uid: "555",
              author_name: "路人观众",
              text: "原来夜潜光线这么暗也能拍清楚！",
            },
          },
        },
      },
    ];

    const activities: ActivityItem[] = [
      {
        id: "act-1",
        kind: "video",
        bvid: "BV1tree888",
        title: "深海摄影：发光水母的夜晚",
        fully_read: true,
      },
    ];

    const detailsByKey: Record<string, QueueDetail> = {
      "browsing/10": {
        ...rows[0]!,
        text: "夜潜低噪感光加上水母自带荧光确实很容易出片～",
        author: "路人观众",
        thread_id: "7001",
      },
    };

    const comments: CommentRecord[] = [];
    const tree = buildVideoContextTree({
      rows,
      tasks,
      intents,
      activities,
      comments,
      detailsByKey,
    });

    expect(tree.videos).toHaveLength(1);
    expect(tree.orphans).toHaveLength(1);
    expect(tree.videos[0]!.video.title).toBe("深海摄影：发光水母的夜晚");
    expect(tree.videos[0]!.video.uploaderName).toBe("珊瑚摄影师");
    expect(tree.videos[0]!.video.coverageBadges).toContain("字幕 + 画面帧");
    expect(tree.videos[0]!.video.coverageBadges).toContain("完整阅读");
    expect(tree.videos[0]!.video.firstImpression).toContain("蓝光水母镜头最抓人");

    const branch = tree.videos[0]!.branches[0]!;
    expect(branch.hasOwnerTakeover).toBe(true);
    expect(branch.turns.map((t) => t.roleLabel)).toEqual([
      "人工接管",
      "待回复",
      "Aqua 草稿",
    ]);

    const onSelectRow = vi.fn();
    const onToggleCheck = vi.fn();
    const onQuickApprove = vi.fn();
    const onQuickReject = vi.fn();

    render(
      <VideoContextTreeCard
        tree={tree}
        view="review"
        selectedKey={null}
        checkedDrafts={new Map()}
        busy={false}
        selectionLoading={false}
        onSelectRow={onSelectRow}
        onToggleCheck={onToggleCheck}
        onQuickApprove={onQuickApprove}
        onQuickReject={onQuickReject}
      />
    );

    const baseCard = document.querySelector("[data-video-base='BV1tree888']") as HTMLElement;
    expect(within(baseCard).getByText("深海摄影：发光水母的夜晚")).toBeTruthy();
    expect(within(baseCard).getByRole("link", { name: "珊瑚摄影师" }).getAttribute("href")).toBe(
      "https://space.bilibili.com/888999"
    );
    expect(within(baseCard).getByText("含人工接管")).toBeTruthy();
    expect(within(baseCard).getByText(/这段夜潜打光是我人工补充说明的细节/)).toBeTruthy();
    expect(within(baseCard).getByText(/夜潜低噪感光加上水母自带荧光/)).toBeTruthy();
    expect(screen.getByRole("region", { name: "其他与独立记录" })).toBeTruthy();
  });

  it("toggles between tree and flat queue views, supports errored-only batch reject, and invokes quick actions", async () => {
    const user = userEvent.setup();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const calls: Array<{ url: string; method: string; body?: unknown }> = [];
    const row = makeRow("42", {
      channel: "browsing",
      video_id: "BV1flat42",
      excerpt: "树节点待审回复",
    });

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? "GET").toUpperCase();
        const body = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
        calls.push({ url, method, body });

        if (url.includes("/api/v1/operations/comments/workbench/selection")) {
          return jsonResponse({ items: [row], total: 1 });
        }
        if (url.includes("/api/v1/operations/comments/workbench/batch-review/reject")) {
          return jsonResponse({ items: [{ channel: "browsing", id: "42", status: "rejected" }] });
        }
        if (url.includes("/api/v1/operations/comments/workbench/browsing/42")) {
          return jsonResponse({
            ...row,
            text: "树节点详细草稿正文",
            source_text: "观众提问原文",
            author: "观众小蓝",
            thread_id: "8801",
            can_publish: true,
          });
        }
        if (url.includes("/api/v1/operations/comments/workbench")) {
          return jsonResponse({ total: 1, offset: 0, limit: 20, items: [row] });
        }
        if (url.endsWith("/api/v1/operations/tasks")) {
          return jsonResponse({
            items: [
              {
                id: "task-42",
                status: "completed",
                bvid: "BV1flat42",
                title: "测试视频 Base",
                material_mode: "subtitle",
                explanation: "看完字幕后的总结印象",
              },
            ],
          });
        }
        if (url.includes("/api/v1/operations/intents/batch-reject")) {
          return jsonResponse({ ok: true, rejected: 2 });
        }
        if (url.includes("/api/v1/operations/intents/42/review")) {
          return jsonResponse({ ok: true, id: "42", status: "approved" });
        }
        return jsonResponse({ items: [] });
      })
    );

    render(<CommentQueueSection initialView="review" />);

    await screen.findByText("测试视频 Base");
    const branchCard = document.querySelector("[data-video-base='BV1flat42']") as HTMLElement;
    expect(within(branchCard).getByText("看完字幕后的总结印象")).toBeTruthy();

    // Quick approve inside branch
    await user.click(within(branchCard).getByRole("button", { name: "通过" }));
    await waitFor(() => {
      expect(
        calls.some(
          (c) =>
            c.url.includes("/api/v1/operations/intents/42/review") &&
            c.method === "POST" &&
            (c.body as { action?: string })?.action === "approve"
        )
      ).toBe(true);
    });

    // Switch to flat view and back to tree view
    await user.click(screen.getByRole("button", { name: "平铺列表" }));
    expect(document.querySelector("[data-video-base='BV1flat42']")).toBeNull();
    await user.click(screen.getByRole("button", { name: "树形视图" }));
    await screen.findByText("测试视频 Base");

    // Clean errored drafts scans selection?errored_only=true and batch-rejects
    await user.click(document.getElementById("queue-reject-errored")!);
    await waitFor(() => {
      expect(
        calls.some(
          (c) =>
            c.url.includes("/api/v1/operations/comments/workbench/selection") &&
            c.url.includes("errored_only=true")
        )
      ).toBe(true);
      expect(
        calls.some(
          (c) =>
            c.url.includes("/api/v1/operations/comments/workbench/batch-review/reject") &&
            c.method === "POST"
        )
      ).toBe(true);
    });
  });

  it("calls POST /api/v1/operations/intents/batch-reject with errored_only: true from OperationsWorkbench", async () => {
    const user = userEvent.setup();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const calls: Array<{ url: string; method: string; body?: unknown }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? "GET").toUpperCase();
        const body = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
        calls.push({ url, method, body });
        return jsonResponse({ ok: true, rejected: 1 });
      })
    );

    render(
      <OperationsWorkbench
        tasks={[]}
        rounds={[]}
        intents={[
          {
            id: "err-1",
            kind: "reply",
            status: "pending_review",
            bvid: "BV1flat42",
            error: "源楼已删除",
          },
        ]}
        inbox={[]}
        activeTab="intents"
        onTabChange={vi.fn()}
        roundFilter={null}
        selectedTaskId={null}
        onSelectTaskId={vi.fn()}
        onRefresh={vi.fn()}
      />
    );

    await user.click(document.getElementById("op-btn-reject-errored-intents")!);
    await waitFor(() => {
      const rejectCall = calls.find((c) =>
        c.url.includes("/api/v1/operations/intents/batch-reject")
      );
      expect(rejectCall?.body).toEqual({ errored_only: true });
    });
  });

  it("syncs AutomationDashboardPage status pill smoothly with chat-triggered starting/running state", async () => {
    let mockAutoState = "starting";
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/operations/automation")) {
          return jsonResponse({
            state: mockAutoState,
            policy: { enabled: true, emergency: false },
            character: { id: "aqua", name: "Aqua", display_name: "Aqua" },
          });
        }
        if (url.endsWith("/api/v1/operations/account/bilibili")) {
          return jsonResponse({ state: "logged_in", profile: { name: "Aqua官号" } });
        }
        return jsonResponse({ items: [] });
      })
    );

    const makeSession = (autoState: string): AppSessionValue => ({
      authenticated: true,
      localAccess: true,
      wsConnected: true,
      status: {
        state: "running",
        character: { id: "aqua", name: "Aqua", display_name: "Aqua", version: 1 },
        automation: {
          state: autoState,
          enabled: true,
          emergency: false,
          epoch: 1,
        },
      },
      settings: { local_access: true },
      refreshStatus: vi.fn(async () => null),
      refreshSettings: vi.fn(async () => null),
      navigate: vi.fn(),
      logout: vi.fn(async () => {}),
      onUnauthorized: vi.fn(),
    });

    const route = parseHashRoute("#automation");
    const { rerender } = render(
      <AppSessionProvider value={makeSession("starting")}>
        <AutomationDashboardPage route={route} />
      </AppSessionProvider>
    );

    await screen.findByText("当前角色：Aqua");
    expect(screen.getAllByText("正在检查启动条件").length).toBeGreaterThan(0);

    mockAutoState = "running";
    rerender(
      <AppSessionProvider value={makeSession("running")}>
        <AutomationDashboardPage route={route} />
      </AppSessionProvider>
    );

    await waitFor(() => {
      expect(screen.getAllByText("自主运营中").length).toBeGreaterThan(0);
    });
  });
});
