import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearDirtyRegistry } from "@/shared/lib/dirtyRegistry";
import { parseHashRoute } from "@/shell/navigation";
import { AutomationDashboardPage } from "./AutomationDashboardPage";
import { OperationsFeature } from "./index";
import { operationsStore } from "@/shared/operations/operationsStore";
import { Stage2PipelineCards } from "./Stage2PipelineCards";
import type {
  AutomationStatus,
  BilibiliAccountSnapshot,
  DiscoveryStatus,
  InboxRecordsResponse,
  OperationClaim,
  QuotaSnapshot,
} from "./types";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function makeStatus(overrides: Partial<AutomationStatus> = {}): AutomationStatus {
  return {
    state: "running",
    account_id: "10086",
    character: { id: "feiyu", name: "大肥鱼" },
    policy: {
      enabled: true,
      emergency: false,
      direct_quota_reserve: 2,
      interval_seconds: 300,
    },
    settings: {
      inbox_enabled: true,
      discovery_enabled: false,
      mentions_enabled: true,
      replies_enabled: true,
      mention_reply_enabled: true,
      notification_interval_seconds: 90,
      daily_watch_limit: 0,
      top_level_enabled: true,
      top_level_auto_publish: false,
      reply_enabled: true,
      reply_auto_publish: false,
      comment_like_enabled: false,
      comment_like_auto_publish: false,
      video_like_enabled: false,
      video_like_auto_publish: false,
      dynamic_like_enabled: false,
      dynamic_like_auto_publish: false,
      video_fav_enabled: false,
      video_fav_auto_publish: false,
    },
    inbox: {
      enabled: true,
      state: "running",
      pending_events: 3,
      dm_enabled: true,
      counts: {
        comment_reply: 4,
        mention: 2,
        own_content_comment: 1,
        dm: 5,
      },
    },
    discovery: {
      enabled: false,
      state: "off",
      daily_watch: { started: 1, limit: 0 },
    },
    backlog: {
      total_heads: 2,
      ready_heads: 1,
      waiting_comments: 0,
      oldest_seconds: 15,
      sends: { queued: 0, deferred: 0, reactions: 0, rejected: 0 },
      review: { comments: 2, messages: 1, dynamics: 0 },
      uncertain: {},
    },
    ...overrides,
  };
}

function makeQuotas(overrides: Partial<QuotaSnapshot> = {}): QuotaSnapshot {
  return {
    direct_quota_reserve: 2,
    max_per_hour: 10,
    max_per_day: 40,
    recipient_cooldown_seconds: 120,
    used_per_hour: 3,
    used_per_day: 8,
    remaining_per_hour: 7,
    remaining_per_day: 32,
    discovery_deferred: false,
    ...overrides,
  };
}

describe("Stage2PipelineCards & AutomationDashboardPage", () => {
  beforeEach(() => {
    operationsStore.reset();
    clearDirtyRegistry();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    operationsStore.reset();
    clearDirtyRegistry();
  });

  it("renders dual cards, inbound counts, deferral/backoff notices, and active operation_claims", () => {
    const status = makeStatus({
      inbox: {
        enabled: true,
        state: "running",
        pending_events: 2,
        dm_enabled: true,
        counts: {
          comment_reply: 7,
          mention: 3,
          own_content_comment: 2,
          dm: 4,
        },
      },
      read_gate: {
        discovery_paused: true,
        discovery_backoff: { reason: "WBI 412 风控退避", until: 1_900_000_000, strikes: 2 },
      },
      backlog: {
        total_heads: 1,
        ready_heads: 1,
        waiting_comments: 0,
        oldest_seconds: 10,
        sends: { queued: 0, deferred: 0, reactions: 0, rejected: 0 },
        uncertain: { comments: 1 },
      },
    });
    const account: BilibiliAccountSnapshot = {
      state: "expired",
      profile: { name: "大肥鱼号", uid: "10086" },
    };
    const quotas = makeQuotas({
      discovery_deferred: true,
      discovery_deferral_reason: "剩余 2 条仅够直连互动预留",
      discovery_retry_at: 1_900_000_500,
    });
    const discovery: DiscoveryStatus = {
      enabled: true,
      state: "waiting",
      active_round: { id: "round-12345678", status: "running" },
      current_video: { bvid: "BV1xx411c7mD", title: "测试视频" },
      daily_watch: { started: 3, limit: 0 },
    };
    const claims: OperationClaim[] = [
      {
        claim_key: "floor:BV1xx411c7mD:root-9",
        claim_kind: "floor",
        account_id: "10086",
        character_id: "feiyu",
        resource_type: "video",
        resource_id: "BV1xx411c7mD",
        root_id: "root-9",
        target_comment_id: "cmt-42",
        owner_pipeline: "inbox",
        lease_until: 1_900_000_600,
      },
    ];
    const inboxRecords: InboxRecordsResponse = { items: [] };

    const { container } = render(
      <Stage2PipelineCards
        status={status}
        account={account}
        discovery={discovery}
        quotas={quotas}
        inboxRecords={inboxRecords}
        claims={claims}
        controlPending={false}
        onToggleInbox={vi.fn()}
        onToggleDiscovery={vi.fn()}
        onSaveInboxSettings={vi.fn()}
        onSaveDiscoverySettings={vi.fn()}
        onSaveQuotas={vi.fn()}
      />
    );

    expect(container.querySelector('[data-inbound-count="comment_reply"]')?.textContent).toBe("7");
    expect(container.querySelector('[data-inbound-count="mention"]')?.textContent).toBe("3");
    expect(container.querySelector('[data-inbound-count="own_content_comment"]')?.textContent).toBe(
      "2"
    );
    expect(container.querySelector('[data-inbound-count="dm"]')?.textContent).toBe("4");

    expect(container.querySelector("[data-discovery-deferral]")?.textContent).toContain(
      "剩余 2 条仅够直连互动预留"
    );
    expect(container.querySelector("[data-discovery-backoff]")?.textContent).toContain(
      "WBI 412 风控退避"
    );
    expect(screen.getAllByText("401 登录失效").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("待核对冻结 (1)")).toBeInTheDocument();
    expect(screen.getByText(/楼租约 · BV1xx411c7mD \/ root-9 · #cmt-42/)).toBeInTheDocument();
  });

  it("toggles Inbox and Discovery independently and updates direct_quota_reserve (0 vs >0)", async () => {
    const user = userEvent.setup();
    const calls: Array<{ url: string; method: string; body: unknown }> = [];
    let currentStatus = makeStatus({
      inbox: { enabled: false, state: "off", pending_events: 0, dm_enabled: false },
      discovery: { enabled: false, state: "off" },
      settings: { inbox_enabled: false, discovery_enabled: false, daily_watch_limit: 0 },
    });
    let currentQuotas = makeQuotas({ direct_quota_reserve: 2 });

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method || "GET").toUpperCase();
        const body = init?.body ? JSON.parse(String(init.body)) : undefined;
        calls.push({ url, method, body });

        if (url === "/api/v1/operations/automation") return jsonResponse(currentStatus);
        if (url === "/api/v1/operations/account/bilibili")
          return jsonResponse({ state: "logged_in", profile: { name: "鱼宝" } });
        if (url === "/api/v1/operations/discovery")
          return jsonResponse(currentStatus.discovery || { state: "off" });
        if (url === "/api/v1/operations/control/quotas" && method === "GET")
          return jsonResponse(currentQuotas);
        if (url === "/api/v1/operations/control/quotas" && method === "PUT") {
          currentQuotas = { ...currentQuotas, direct_quota_reserve: body.direct_quota_reserve };
          return jsonResponse(currentQuotas);
        }
        if (url === "/api/v1/operations/inbox") return jsonResponse({ items: [], messages: 1 });
        if (url === "/api/v1/operations/claims") return jsonResponse([]);
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
        if (url === "/api/v1/operations/inbox/enable" && method === "POST") {
          currentStatus = {
            ...currentStatus,
            inbox: { ...currentStatus.inbox!, enabled: body.enabled, state: body.enabled ? "running" : "off" },
          };
          return jsonResponse(currentStatus);
        }
        if (url === "/api/v1/operations/discovery/enable" && method === "POST") {
          currentStatus = {
            ...currentStatus,
            discovery: {
              enabled: body.enabled,
              state: body.enabled ? "waiting" : "off",
            },
          };
          return jsonResponse(currentStatus);
        }
        return jsonResponse({});
      })
    );

    render(<AutomationDashboardPage route={parseHashRoute("#automation")} />);

    const inboxSwitch = await screen.findByRole("switch", { name: "消息响应（Inbox）开关" });
    const discoverySwitch = screen.getByRole("switch", { name: "主动探索（Discovery）开关" });
    expect(inboxSwitch).toHaveAttribute("aria-checked", "false");
    expect(discoverySwitch).toHaveAttribute("aria-checked", "false");

    // Enabling Inbox must NOT enable Discovery
    await user.click(inboxSwitch);
    await waitFor(() => {
      expect(inboxSwitch).toHaveAttribute("aria-checked", "true");
    });
    expect(discoverySwitch).toHaveAttribute("aria-checked", "false");
    expect(calls.some((c) => c.url === "/api/v1/operations/inbox/enable" && (c.body as { enabled: boolean }).enabled)).toBe(true);
    expect(calls.some((c) => c.url === "/api/v1/operations/discovery/enable")).toBe(false);

    // Enabling Discovery independently
    await user.click(discoverySwitch);
    await waitFor(() => {
      expect(discoverySwitch).toHaveAttribute("aria-checked", "true");
    });
    expect(calls.some((c) => c.url === "/api/v1/operations/discovery/enable" && (c.body as { enabled: boolean }).enabled)).toBe(true);

    // Reserve = 0 saves and shows unreserved / shared quota
    const reserveInput = screen.getByLabelText(/direct_quota_reserve/);
    await user.clear(reserveInput);
    await user.type(reserveInput, "0");
    await user.click(screen.getByRole("button", { name: "保存预留额度" }));
    await waitFor(() => {
      expect(screen.getByText("0 · 不预留 / 共享额度")).toBeInTheDocument();
    });
    expect(
      calls.some(
        (c) =>
          c.url === "/api/v1/operations/control/quotas" &&
          c.method === "PUT" &&
          (c.body as { direct_quota_reserve: number }).direct_quota_reserve === 0
      )
    ).toBe(true);
  });

  it("freezes both Inbox and Discovery under Global Emergency Stop and restores them on reset", async () => {
    const user = userEvent.setup();
    let statusState = makeStatus({
      policy: { enabled: true, emergency: false },
      inbox: { enabled: true, state: "running", pending_events: 0, dm_enabled: true },
      discovery: { enabled: true, state: "discovering" },
    });

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method || "GET").toUpperCase();
        if (url === "/api/v1/operations/automation") return jsonResponse(statusState);
        if (url === "/api/v1/operations/account/bilibili")
          return jsonResponse({ state: "logged_in", profile: { name: "鱼宝" } });
        if (url === "/api/v1/operations/discovery")
          return jsonResponse(statusState.discovery || { state: "off" });
        if (url === "/api/v1/operations/control/quotas") return jsonResponse(makeQuotas());
        if (url === "/api/v1/operations/inbox") return jsonResponse({ items: [], messages: 0 });
        if (url === "/api/v1/operations/claims") return jsonResponse([]);
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
        if (url === "/api/v1/operations/automation/stop" && method === "POST") {
          statusState = makeStatus({
            state: "emergency",
            policy: { enabled: false, emergency: true },
            inbox: { enabled: false, state: "emergency", pending_events: 0, dm_enabled: false },
            discovery: { enabled: false, state: "emergency" },
          });
          return jsonResponse(statusState);
        }
        if (url === "/api/v1/operations/automation/reset-stop" && method === "POST") {
          statusState = makeStatus({
            state: "off",
            policy: { enabled: false, emergency: false },
            inbox: { enabled: false, state: "off", pending_events: 0, dm_enabled: false },
            discovery: { enabled: false, state: "off" },
          });
          return jsonResponse(statusState);
        }
        return jsonResponse({});
      })
    );

    render(<AutomationDashboardPage route={parseHashRoute("#automation")} />);

    const eStopBtn = await screen.findByRole("button", { name: "紧急停止" });
    await user.click(eStopBtn);

    const resetBtn = await screen.findByRole("button", { name: "恢复急停" });
    expect(resetBtn).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "消息响应（Inbox）开关" })).toBeDisabled();
    expect(screen.getByRole("switch", { name: "主动探索（Discovery）开关" })).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent("全局紧急停止已生效");

    await user.click(resetBtn);
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "紧急停止" })).toBeInTheDocument();
    });
    expect(screen.getByRole("switch", { name: "消息响应（Inbox）开关" })).toBeEnabled();
    expect(screen.getByRole("switch", { name: "主动探索（Discovery）开关" })).toBeEnabled();
  });

  it("saves strictly scoped Inbox vs Discovery inline settings and routes all 7 OperationsFeature views", async () => {
    const user = userEvent.setup();
    const inboxSaveCalls: Array<Record<string, unknown>> = [];
    const discoverySaveCalls: Array<Record<string, unknown>> = [];

    const onSaveInbox = vi.fn(async (patch: Record<string, unknown>) => {
      inboxSaveCalls.push(patch);
      return { enabled: true, state: "running" };
    });
    const onSaveDiscovery = vi.fn(async (patch: Record<string, unknown>) => {
      discoverySaveCalls.push(patch);
      return { enabled: true, state: "waiting" };
    });

    const { container } = render(
      <Stage2PipelineCards
        status={makeStatus()}
        account={null}
        discovery={null}
        quotas={makeQuotas()}
        inboxRecords={{ items: [] }}
        claims={[]}
        controlPending={false}
        onToggleInbox={vi.fn()}
        onToggleDiscovery={vi.fn()}
        onSaveInboxSettings={onSaveInbox}
        onSaveDiscoverySettings={onSaveDiscovery}
        onSaveQuotas={vi.fn()}
      />
    );

    await user.click(screen.getByText("收件箱响应设置"));
    await user.click(screen.getByRole("button", { name: "保存收件箱设置" }));
    await waitFor(() => expect(inboxSaveCalls).toHaveLength(1));
    expect(inboxSaveCalls[0]).toMatchObject({
      mentions_enabled: true,
      replies_enabled: true,
      mention_reply_enabled: true,
      notification_interval_seconds: 90,
    });
    expect(inboxSaveCalls[0].daily_watch_limit).toBeUndefined();

    await user.click(screen.getByText("探索管道参数"));
    await user.click(screen.getByRole("button", { name: "保存探索参数" }));
    await waitFor(() => expect(discoverySaveCalls).toHaveLength(1));
    expect(discoverySaveCalls[0]).toEqual({ daily_watch_limit: 0 });
    expect(discoverySaveCalls[0].mentions_enabled).toBeUndefined();

    // OperationsFeature routes all 7 section pages
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ items: [], sources: [], users: [], drafts: [], policy: {} })));
    const hashes = [
      ["#automation", "automation"],
      ["#automation-records", "automation-records"],
      ["#automation-settings", "automation-settings"],
      ["#comments", "comments"],
      ["#messages", "messages"],
      ["#dynamics", "dynamics"],
      ["#issues", "issues"],
    ] as const;
    for (const [hash, expectedSection] of hashes) {
      const view = render(<OperationsFeature route={parseHashRoute(hash)} />);
      expect(view.container.querySelector(`[data-section="${expectedSection}"]`)).not.toBeNull();
      view.unmount();
    }
    expect(container).toBeInTheDocument();
  });

  it("renders visibility_risk pause alert and comment_cooldown_until on pipeline cards", () => {
    const futureCooldown = Math.floor(Date.now() / 1000) + 1800;
    const status = makeStatus({
      comment_cooldown_until: futureCooldown,
      visibility_risk: {
        streak: 2,
        threshold: 2,
        active: true,
        cooldown_until: futureCooldown,
        downgraded: true,
        last_status: "shadow_deleted",
        reason: "连续 2 条评论未公开可见（最近：疑似被吞），已暂停评论发送 30 分钟并降级为人工审核",
      },
    });

    const { container } = render(
      <Stage2PipelineCards
        status={status}
        account={null}
        discovery={null}
        quotas={makeQuotas()}
        inboxRecords={{ items: [] }}
        claims={[]}
        controlPending={false}
        onToggleInbox={vi.fn()}
        onToggleDiscovery={vi.fn()}
        onSaveInboxSettings={vi.fn()}
        onSaveDiscoverySettings={vi.fn()}
        onSaveQuotas={vi.fn()}
      />
    );

    const alert = container.querySelector('[data-visibility-risk-alert="active"]');
    expect(alert).not.toBeNull();
    expect(alert?.textContent).toContain("发评风控保护已触发");
    expect(alert?.textContent).toContain("已自动降级为人工审核模式");
    expect(container.querySelector('[data-comment-cooldown="active"]')).not.toBeNull();
    expect(screen.getAllByText("发评风控暂停").length).toBeGreaterThanOrEqual(2);
  });

  it("shows expired visibility history without an active comment pause", () => {
    const { container } = render(
      <Stage2PipelineCards
        status={makeStatus({ visibility_risk: {
          streak: 4, threshold: 2, active: false, cooldown_until: null, downgraded: false,
          last_status: "self_only", reason: "此前 4 条评论未公开可见，评论冷却已结束",
        } })}
        account={null} discovery={null} quotas={makeQuotas()} inboxRecords={{ items: [] }} claims={[]}
        controlPending={false} onToggleInbox={vi.fn()} onToggleDiscovery={vi.fn()}
        onSaveInboxSettings={vi.fn()} onSaveDiscoverySettings={vi.fn()} onSaveQuotas={vi.fn()}
      />
    );
    expect(screen.getByText("最近评论可见性记录")).toBeInTheDocument();
    expect(container.querySelector('[data-visibility-risk-alert="active"]')).toBeNull();
    expect(screen.queryByText("发评风控暂停")).not.toBeInTheDocument();
  });
});
