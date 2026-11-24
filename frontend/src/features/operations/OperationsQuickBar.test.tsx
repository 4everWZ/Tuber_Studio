import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearDirtyRegistry } from "@/shared/lib/dirtyRegistry";
import { parseHashRoute } from "@/shell/navigation";
import { AutomationRecordsPage } from "./AutomationRecordsPage";
import { issueLogStore } from "./IssueLogStore";
import { IssuesPage } from "./IssuesPage";
import { OperationsQuickBar } from "./OperationsQuickBar";
import { operationsStore } from "@/shared/operations/operationsStore";
import type { AutomationStatus } from "./types";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function makeStatus(overrides: Partial<AutomationStatus> = {}): AutomationStatus {
  return {
    state: "off",
    started_at: 100,
    status_sequence: 1,
    policy: { enabled: false, emergency: false },
    settings: {
      top_level_enabled: true,
      reply_enabled: false,
      daily_watch_limit: 0,
    },
    backlog: {
      total_heads: 2,
      ready_heads: 1,
      waiting_comments: 1,
      oldest_seconds: 95,
      sends: { queued: 1, deferred: 0, reactions: 0, rejected: 0 },
      review: { comments: 1, messages: 0, dynamics: 0 },
      uncertain: {},
    },
    ...overrides,
  };
}

describe("OperationsQuickBar & operationsStore", () => {
  beforeEach(() => {
    operationsStore.reset();
    issueLogStore.reset();
    clearDirtyRegistry();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    operationsStore.reset();
    issueLogStore.reset();
    clearDirtyRegistry();
  });

  it("renders stopping state, active rounds vs waiting rounds, and detailed backlog summary", () => {
    const status = makeStatus({
      state: "stopping",
      policy: { enabled: true, emergency: false },
      waiting_round: { round_id: "rnd-w", remaining: 4 },
    });

    const { container } = render(
      <OperationsQuickBar status={status} onToggleMaster={vi.fn()} />
    );

    expect(container.querySelector("#op-home-state")?.textContent).toBe("正在停止自主运营");
    expect(container.querySelector("#op-toggle-auto")).toBeDisabled();
    expect(container.querySelector("#op-active-rounds")?.textContent).toBe(
      "视频轮次已保存 · 待续跑 · 剩余 4 项"
    );
    expect(container.querySelector("#op-backlog-heading")?.textContent).toContain(
      "待回访 2 楼 · 可执行 1 楼 · 最老等待 1 分 35 秒"
    );
    expect(container.querySelector("#op-backlog-detail")?.textContent).toContain(
      "已授权待发 1（等待 0）"
    );
  });

  it("starts with saved permissions without mutating them and blocks duplicate clicks while pending", async () => {
    const postCalls: Array<{ url: string; body: unknown }> = [];
    const gate = deferred<Response>();

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method || "GET").toUpperCase();
        const body = init?.body ? JSON.parse(String(init.body)) : undefined;
        if (method === "POST") {
          postCalls.push({ url, body });
          return gate.promise;
        }
        return jsonResponse(makeStatus());
      })
    );

    await operationsStore.refreshStatus();
    expect(operationsStore.getSnapshot().status?.settings?.reply_enabled).toBe(false);

    const first = operationsStore.toggleMasterAutomation(true);
    const second = operationsStore.toggleMasterAutomation(true);
    expect(postCalls).toHaveLength(1);
    expect(postCalls[0].url).toBe("/api/v1/operations/automation/enable");
    expect(postCalls[0].body).toEqual({ enabled: true });

    gate.resolve(
      jsonResponse(
        makeStatus({
          state: "running",
          started_at: 200,
          status_sequence: 2,
          policy: { enabled: true, emergency: false },
        })
      )
    );
    await Promise.all([first, second]);

    expect(operationsStore.getSnapshot().status?.policy?.enabled).toBe(true);
    expect(operationsStore.getSnapshot().status?.settings?.reply_enabled).toBe(false);
  });

  it("ignores delayed stop/poll status snapshots older than current (started_at, status_sequence)", async () => {
    const staleGate = deferred<Response>();
    let callIdx = 0;

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method || "GET").toUpperCase();
        if (url === "/api/v1/operations/automation" && method === "GET") {
          callIdx += 1;
          if (callIdx === 1) {
            return staleGate.promise;
          }
          return jsonResponse(
            makeStatus({
              state: "running",
              started_at: 300,
              status_sequence: 5,
              policy: { enabled: true, emergency: false },
            })
          );
        }
        return jsonResponse({});
      })
    );

    // Start slow poll first, then force a newer poll that returns running (300, 5)
    const slowPoll = operationsStore.refreshStatus();
    const freshPoll = operationsStore.refreshStatus({ background: false });
    await freshPoll;
    expect(operationsStore.getSnapshot().status?.state).toBe("running");

    // Stale poll resolves later with older (100, 1) stopping/off snapshot
    staleGate.resolve(
      jsonResponse(
        makeStatus({
          state: "off",
          started_at: 100,
          status_sequence: 1,
          policy: { enabled: false, emergency: false },
        })
      )
    );
    await slowPoll;

    // Monotonic guard must preserve newer running snapshot
    expect(operationsStore.getSnapshot().status?.state).toBe("running");
    expect(operationsStore.getSnapshot().status?.status_sequence).toBe(5);
  });
});

describe("AutomationRecordsPage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("refreshes records without calling /api/v1/operations/automation and supports lazy evidence & intents", async () => {
    const user = userEvent.setup();
    const seenUrls: string[] = [];

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method || "GET").toUpperCase();
        seenUrls.push(`${method} ${url}`);

        if (url.startsWith("/api/v1/operations/rounds")) {
          return jsonResponse([
            { id: "round-1", status: "completed", public_drafts: 2, public_published: 1 },
          ]);
        }
        if (url.startsWith("/api/v1/operations/tasks")) {
          return jsonResponse([
            {
              id: "task-1",
              round_id: "round-1",
              bvid: "BV1rec1",
              title: "记录测试视频",
              status: "completed",
            },
          ]);
        }
        if (url.startsWith("/api/v1/operations/intents")) {
          return jsonResponse([
            {
              id: "intent-1",
              kind: "video_like",
              status: "pending_review",
              target: { bvid: "BV1rec1", text: "想点赞这个视频" },
            },
          ]);
        }
        if (url.startsWith("/api/v1/operations/inbox")) {
          return jsonResponse({ items: [] });
        }
        if (url.startsWith("/api/v1/operations/discovery/records")) {
          return jsonResponse({
            sent: [{ id: "sent-1", bvid: "BV1rec1", text: "已发出的主评", updated_at: 1700000000 }],
            issues: [],
            issue_history: [],
          });
        }
        if (url.startsWith("/api/v1/operations/activity")) {
          return jsonResponse({
            day: "2026-11-22",
            page: 1,
            pages: 1,
            total: 0,
            counts: { total: 0, sent: 0, pending: 0 },
            items: [],
          });
        }
        if (url === "/api/v1/operations/comments/workbench/browsing/sent-1") {
          return jsonResponse({
            channel: "browsing",
            id: "sent-1",
            status: "published",
            evidence_segments: [{ start: 12, observation: "视频第 12 秒提到主题" }],
          });
        }
        if (url === "/api/v1/operations/intents/intent-1/review" && method === "POST") {
          return jsonResponse({ id: "intent-1", status: "approved" });
        }
        return jsonResponse({});
      })
    );

    render(<AutomationRecordsPage route={parseHashRoute("#automation-records")} />);

    // Open diagnostics details, switch to tasks tab -> verifies tasks loaded without calling /automation
    await user.click(screen.getByText("任务与诊断"));
    await user.click(screen.getByRole("tab", { name: "浏览任务" }));
    expect(await screen.findByText(/记录测试视频/)).toBeInTheDocument();
    expect(seenUrls.some((u) => u.includes("/api/v1/operations/automation"))).toBe(false);

    // Open published section and expand BV1rec1 <details> for lazy evidence load
    await user.click(screen.getByText("近期公开发布"));
    const evidenceSummary = await screen.findByText(/跟帖 · BV1rec1/);
    await user.click(evidenceSummary);
    await waitFor(() => {
      expect(seenUrls).toContain("GET /api/v1/operations/comments/workbench/browsing/sent-1");
      expect(screen.getByText(/视频第 12 秒提到主题/)).toBeInTheDocument();
    });

    // Switch to intents tab and approve
    await user.click(screen.getByRole("tab", { name: /点赞与收藏/ }));
    await user.click(await screen.findByRole("button", { name: "审核通过" }));
    await waitFor(() => {
      expect(seenUrls).toContain("POST /api/v1/operations/intents/intent-1/review");
    });
  });
});

describe("IssuesPage", () => {
  beforeEach(() => {
    issueLogStore.reset();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    issueLogStore.reset();
  });

  it("acknowledges unread issues and clears issue history", async () => {
    const user = userEvent.setup();
    const calls: string[] = [];

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method || "GET").toUpperCase();
        calls.push(`${method} ${url}`);
        if (url.startsWith("/api/v1/operations/discovery/records")) {
          if (calls.some((c) => c.includes("/issues/clear"))) {
            return jsonResponse({ issues: [], issue_history: [] });
          }
          if (calls.some((c) => c.includes("/issues/acknowledge"))) {
            return jsonResponse({
              issues: [{ id: "iss-1", error: "接口超时", unread_occurrences: 0, updated_at: 1700000000 }],
              issue_history: [],
            });
          }
          return jsonResponse({
            issues: [{ id: "iss-1", error: "接口超时", unread_occurrences: 2, updated_at: 1700000000 }],
            issue_history: [],
          });
        }
        return jsonResponse({});
      })
    );

    render(<IssuesPage />);
    expect(await screen.findByText(/接口超时/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "标为已读" }));
    await waitFor(() => {
      expect(calls).toContain("POST /api/v1/operations/issues/acknowledge");
    });

    await user.click(document.getElementById("clear-workspace-issues") as HTMLElement);
    await waitFor(() => {
      expect(calls).toContain("POST /api/v1/operations/issues/clear");
      expect(screen.getByText("暂无匹配的告警或错误。")).toBeInTheDocument();
    });
  });
});
