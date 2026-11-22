import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { OperationContinuationCard, type ContinuationRecord } from "./OperationContinuationCard";

describe("OperationContinuationCard", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("displays zero token and call caps as unlimited without changing recovery count", () => {
    const value: ContinuationRecord = {
      id: "one",
      state: "checkpoint",
      recoveries: 0,
      max_recoveries: 0,
      budget: { model_calls: 65, input_tokens: 187468, output_tokens: 8363 },
      limits: { max_model_calls: 0, max_input_tokens: 0, max_output_tokens: 16384 },
      revisions: [],
    };

    const { container } = render(
      <OperationContinuationCard value={value} channel="comments" localId="one" />
    );

    expect(container.querySelector("[data-continuation-budget]")?.textContent).toBe(
      "调用 65 / 不限 · 输入 tokens 187468 / 不限 · 输出 tokens 8363 / 16384"
    );
    expect(container.querySelector("[data-continuation-summary]")?.textContent).toMatch(
      /恢复 0 \/ 0 次/
    );
  });

  it("requires backend permission for terminal generation states and posts resume on click", async () => {
    const user = userEvent.setup();
    for (const state of ["invalid", "awaiting_review", "deferred"]) {
      const value: ContinuationRecord = {
        id: state,
        phase: "generation",
        state,
        can_resume: false,
        revisions: [],
        resume_target: { channel: "comments", local_id: "generation:one" },
      };

      const { container, rerender, unmount } = render(
        <OperationContinuationCard value={value} channel="comments" localId={1} />
      );

      const resumeBtn = container.querySelector("[data-continuation-resume]") as HTMLButtonElement;
      expect(resumeBtn.hidden).toBe(true);
      expect(container.querySelector("[data-continuation-summary]")?.textContent).not.toMatch(
        /恢复|累计/
      );
      expect(container.querySelector("[data-continuation-note]")?.textContent).not.toMatch(
        /Agent 正在|下一轮继续/
      );

      rerender(
        <OperationContinuationCard
          value={{ ...value, can_resume: true }}
          channel="comments"
          localId={1}
        />
      );
      expect(resumeBtn.hidden).toBe(false);
      expect(resumeBtn.disabled).toBe(false);
      expect(resumeBtn.textContent).toBe("重新生成");
      unmount();
    }

    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      new Response(JSON.stringify({ continuation: { id: "g1", state: "completed", revisions: [] } }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })
    );
    vi.stubGlobal("fetch", fetchMock);
    const onRefresh = vi.fn();

    render(
      <OperationContinuationCard
        value={{
          id: "g1",
          phase: "generation",
          state: "awaiting_review",
          can_resume: true,
          revisions: [],
          resume_target: { channel: "comments", local_id: "generation:one" },
        }}
        channel="comments"
        localId={1}
        onRefresh={onRefresh}
      />
    );

    await user.click(screen.getByRole("button", { name: "重新生成" }));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/v1/operations/continuations/comments/generation%3Aone/resume"
    );
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it("routes private handoffs to messages workspace and supports publish_uncertain reconciliation", async () => {
    const { container, rerender } = render(
      <OperationContinuationCard
        value={{
          id: "priv-1",
          state: "awaiting_review",
          private_handoff: true,
          revisions: [],
        }}
        channel="comments"
        localId="c1"
      />
    );

    expect(container.querySelector("[data-continuation-state]")?.textContent).toBe(
      "生成原稿在私信工作台"
    );
    expect(container.querySelector("[data-continuation-private]")?.hasAttribute("hidden")).toBe(
      false
    );

    rerender(
      <OperationContinuationCard
        value={{
          id: "rec-1",
          state: "needs_reconciliation",
          can_resume: true,
          revisions: [],
        }}
        channel="comments"
        localId="c1"
        entityStatus="publish_uncertain"
      />
    );

    const reconcileBtn = container.querySelector(
      "[data-continuation-reconcile]"
    ) as HTMLButtonElement;
    expect(reconcileBtn.hidden).toBe(false);
    expect(reconcileBtn.textContent).toBe("核对发送结果");
  });
});
