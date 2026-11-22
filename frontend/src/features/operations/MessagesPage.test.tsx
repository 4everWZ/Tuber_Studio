import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearDirtyRegistry } from "@/shared/lib/dirtyRegistry";
import { parseHashRoute } from "@/shell/navigation";
import { contactLabel, contactReason, contactStatus } from "./messageLabels";
import {
  messageDraftState,
  messagePolicyStatus,
  MessagesPage,
  TriggerProvenanceDetails,
} from "./MessagesPage";
import type { MessageDraft, MessagePolicyState, MessagesListResponse } from "./types";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function makeDraft(id: number, overrides: Partial<MessageDraft> = {}): MessageDraft {
  return {
    id,
    account_id: "acc-1",
    character_id: "feiyu",
    platform: "bilibili",
    platform_user_id: "u1",
    display_name: "测试粉丝",
    status: "pending_review",
    text: `私信草稿 ${id}`,
    updated_at: 1700000100,
    ...overrides,
  };
}

describe("messageLabels & messagePolicyStatus & TriggerProvenanceDetails", () => {
  it("formats outreach vs reply labels, stopped parent policy, and provenance details", () => {
    expect(contactLabel({ direction: "outbound", contact_type: "outreach" })).toBe("主动私信");
    expect(contactLabel({ direction: "inbound", type: "reply" })).toBe("回复来信");
    expect(contactLabel({ direction: "incoming" })).toBe("收到私信");
    expect(contactStatus({ status: "waiting", outreach_status: "waiting_agreement" })).toBe(
      "等待约定或对方回应"
    );
    expect(contactReason("user_requested")).toBe("对方请求私下交流");
    expect(contactReason("custom_reason")).toBe("custom_reason");

    const stoppedPolicy: MessagePolicyState = {
      enabled: true,
      automation_override: false,
      auto_poll: false,
      auto_send: false,
      base: { auto_poll: true, auto_send: false },
    };
    expect(messagePolicyStatus(stoppedPolicy)).toContain("自主运营已停止");

    const draft = makeDraft(7, {
      direction: "outbound",
      contact_type: "outreach",
      purpose: "回复明确邀约",
      trigger_source: {
        kind: "comment",
        bvid: "BV1xx411c7mD",
        root: 500,
        comment_id: 7001,
      },
      outreach_check: {
        assessment: { reason: "观众明确请求私信教程" },
      },
    });
    const { container, rerender } = render(<TriggerProvenanceDetails draft={draft} />);
    expect(container.textContent).toContain("回复明确邀约");
    expect(container.textContent).toContain("观众明确请求私信教程");
    expect(container.querySelector('a[href*="BV1xx411c7mD#reply7001"]')).toBeInTheDocument();

    rerender(<TriggerProvenanceDetails draft={null} />);
    expect(container.textContent).toBe("");
  });

  it("labels waiting agreement continuation state distinctly", () => {
    expect(
      messageDraftState(
        makeDraft(1, {
          status: "waiting",
          contact_state: "awaiting_completion",
        })
      )
    ).toEqual({
      label: "等待约定完成",
      history: false,
      progress: false,
      waiting: true,
      continueAgreement: true,
    });
  });
});

describe("MessagesPage", () => {
  beforeEach(() => {
    clearDirtyRegistry();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    clearDirtyRegistry();
  });

  it("maintains scoped (accountId, draftId) dirty edits, sends edited drafts, and loads policy cleanly", async () => {
    const user = userEvent.setup();
    const putCalls: Array<{ url: string; body: unknown }> = [];
    const postCalls: Array<{ url: string; body: unknown }> = [];

    const listData: MessagesListResponse = {
      account_id: "acc-1",
      character_id: "feiyu",
      policy: {
        enabled: true,
        auto_poll: true,
        auto_send: false,
        poll_interval_seconds: 60,
        max_per_hour: 10,
        max_per_day: 30,
      },
      users: [{ platform: "bilibili", platform_user_id: "u1", display_name: "测试粉丝" }],
      drafts: [
        makeDraft(11, { text: "草稿十一原文" }),
        makeDraft(12, {
          text: "草稿十二原文",
          status: "waiting",
          contact_state: "awaiting_completion",
        }),
        makeDraft(13, { text: "草稿十三原文" }),
      ],
    };

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method || "GET").toUpperCase();
        const body = init?.body ? JSON.parse(String(init.body)) : undefined;

        if (url === "/api/v1/operations/automation") {
          return jsonResponse({ state: "off", character: { id: "feiyu", name: "大肥鱼" } });
        }
        if (url === "/api/v1/operations/messages") return jsonResponse(listData);
        if (url.startsWith("/api/v1/operations/messages/conversation")) {
          return jsonResponse({
            account_id: "acc-1",
            character_id: "feiyu",
            relationship: { affinity: 80, stage: "熟识" },
            messages: [{ id: 1, content: "你好呀", sent_at: 1700000000 }],
            drafts: listData.drafts,
          });
        }
        if (url === "/api/v1/operations/messages/policy" && method === "PUT") {
          putCalls.push({ url, body });
          return jsonResponse({ ...listData.policy, ...body });
        }
        if (method === "POST") {
          postCalls.push({ url, body });
          if (url === "/api/v1/operations/messages/drafts/11/review") {
            return jsonResponse(makeDraft(11, { status: "approved", text: body.text }));
          }
          if (url === "/api/v1/operations/messages/drafts/12/retry") {
            return jsonResponse({ ok: true });
          }
        }
        return jsonResponse({});
      })
    );

    const { container } = render(<MessagesPage route={parseHashRoute("#messages")} />);

    const draft11Btn = await screen.findByRole("button", { name: /#11/ });
    const draft12Btn = screen.getByRole("button", { name: /#12/ });
    const draft13Btn = screen.getByRole("button", { name: /#13/ });

    // Edit draft 11 without saving, switch to draft 13, check continuation #12, then back to #11 -> edit preserved!
    await user.click(draft11Btn);
    const textarea = container.querySelector("[data-text]") as HTMLTextAreaElement;
    expect(textarea.value).toBe("草稿十一原文");
    await user.clear(textarea);
    await user.type(textarea, "改过的私信十一");

    await user.click(draft13Btn);
    expect(container.querySelector("[data-text]")).toHaveValue("草稿十三原文");

    await user.click(draft12Btn);
    expect(container.querySelector("[data-retry]")?.textContent).toBe("继续完成约定");
    await user.click(container.querySelector("[data-retry]") as HTMLElement);
    await waitFor(() => {
      expect(postCalls.some((c) => c.url === "/api/v1/operations/messages/drafts/12/retry")).toBe(
        true
      );
    });

    await user.click(draft11Btn);
    expect(container.querySelector("[data-text]")).toHaveValue("改过的私信十一");

    // Approve & save clears edit key for #11
    await user.click(container.querySelector('[data-review="approve"]') as HTMLElement);
    await waitFor(() => {
      expect(
        postCalls.some(
          (c) =>
            c.url === "/api/v1/operations/messages/drafts/11/review" &&
            (c.body as { action: string; text: string }).text === "改过的私信十一"
        )
      ).toBe(true);
    });
  });

  it("ignores stale conversation responses and focuses #messages/inbox/:id deep links", async () => {
    const staleConvGate = deferred<Response>();
    let convCalls = 0;

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/v1/operations/automation") {
          return jsonResponse({ state: "off", character: { id: "feiyu", name: "大肥鱼" } });
        }
        if (url === "/api/v1/operations/messages") {
          return jsonResponse({
            account_id: "acc-1",
            character_id: "feiyu",
            policy: { enabled: false },
            users: [{ platform: "bilibili", platform_user_id: "u1", display_name: "粉丝1" }],
            drafts: [],
          });
        }
        if (url === "/api/v1/operations/messages/inbox/55/conversation") {
          return jsonResponse({
            account_id: "acc-1",
            character_id: "feiyu",
            messages: [
              {
                id: 55,
                platform: "bilibili",
                platform_user_id: "u1",
                display_name: "粉丝1",
                content: "定位到的原私信",
                sent_at: 1700000000,
              },
            ],
            drafts: [],
          });
        }
        if (url.startsWith("/api/v1/operations/messages/conversation")) {
          convCalls += 1;
          if (convCalls === 1) return staleConvGate.promise;
          return jsonResponse({
            account_id: "acc-1",
            character_id: "feiyu",
            messages: [{ id: 99, content: "最新会话消息", sent_at: 1700000005 }],
            drafts: [],
          });
        }
        return jsonResponse({});
      })
    );

    const { container } = render(
      <MessagesPage route={parseHashRoute("#messages/inbox/55")} />
    );

    const targetArticle = await waitFor(() => {
      const el = container.querySelector('[data-message-id="55"]');
      expect(el).not.toBeNull();
      return el as HTMLElement;
    });
    expect(targetArticle).toHaveAttribute("aria-current", "true");
    expect(screen.getByText("定位到的原私信")).toBeInTheDocument();

    // Resolving a stale earlier conversation promise doesn't overwrite active view
    staleConvGate.resolve(
      jsonResponse({
        account_id: "acc-1",
        character_id: "feiyu",
        messages: [{ id: 1, content: "过期会话消息", sent_at: 1600000000 }],
        drafts: [],
      })
    );
    expect(screen.queryByText("过期会话消息")).toBeNull();
  });
});
