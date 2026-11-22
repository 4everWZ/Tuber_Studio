import { useEffect, useRef, useState, type FormEvent } from "react";
import { apiDelete, apiGet, apiPut } from "@/shared/api/client";
import { errorMessage } from "@/shared/lib/errors";
import type { CreativeGoal, GoalCommentEventState, GoalEventState } from "./types";

export interface GoalEventsListProps {
  goal: CreativeGoal | null | undefined;
  onEditingChange: (editing: boolean) => void;
}

type ActivePanel = "none" | "inbox" | "comments";

export function GoalEventsList({ goal, onEditingChange }: GoalEventsListProps) {
  const [panel, setPanel] = useState<ActivePanel>("none");

  // Inbox event watch state
  const [inboxState, setInboxState] = useState<GoalEventState | null>(null);
  const [senderUid, setSenderUid] = useState("");
  const [afterReply, setAfterReply] = useState("");
  const [inboxNotice, setInboxNotice] = useState("");
  const [inboxBusy, setInboxBusy] = useState(false);

  // Comment event watch state
  const [commentState, setCommentState] = useState<GoalCommentEventState | null>(null);
  const [resource, setResource] = useState("");
  const [rootId, setRootId] = useState("");
  const [commentNotice, setCommentNotice] = useState("");
  const [commentBusy, setCommentBusy] = useState(false);

  const inboxUidRef = useRef<HTMLInputElement | null>(null);
  const commentResRef = useRef<HTMLInputElement | null>(null);
  const inboxBtnRef = useRef<HTMLButtonElement | null>(null);
  const commentBtnRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    setPanel("none");
    setInboxState(null);
    setCommentState(null);
  }, [goal?.id]);

  if (!goal || goal.closed) {
    return null;
  }

  const goalId = goal.id;

  async function openInbox() {
    setPanel("inbox");
    onEditingChange(true);
    setInboxNotice("正在读取跟踪授权…");
    setInboxState(null);
    try {
      const state = await apiGet<"/api/v1/agent/tasks/{identity}/goal/events", GoalEventState>(
        "/api/v1/agent/tasks/{identity}/goal/events",
        { pathParams: { identity: goalId } }
      );
      setInboxState(state);
      setSenderUid(state.sender_uid || "");
      setAfterReply(state.after_reply || "");
      const binding = state.account ? `接收账号 ${state.account} · 角色 ${state.character}` : "尚未绑定接收账号";
      setInboxNotice(
        state.enabled ? `${binding} · 已跟踪 UID ${state.sender_uid}` : `${binding} · 未启用`
      );
      setTimeout(() => inboxUidRef.current?.focus(), 0);
    } catch (error) {
      setInboxNotice(errorMessage(error));
    }
  }

  async function openComments() {
    setPanel("comments");
    onEditingChange(true);
    setCommentNotice("正在读取评论跟踪授权…");
    setCommentState(null);
    try {
      const state = await apiGet<
        "/api/v1/agent/tasks/{identity}/goal/comment-events",
        GoalCommentEventState
      >("/api/v1/agent/tasks/{identity}/goal/comment-events", {
        pathParams: { identity: goalId },
      });
      setCommentState(state);
      setResource(state.resource || "");
      setRootId(state.root_id || "");
      const binding = state.account ? `接收账号 ${state.account} · 角色 ${state.character}` : "尚未绑定接收账号";
      setCommentNotice(
        state.enabled
          ? `${binding} · 已跟踪 ${state.resource} 楼 ${state.root_id}`
          : `${binding} · 未启用`
      );
      setTimeout(() => commentResRef.current?.focus(), 0);
    } catch (error) {
      setCommentNotice(errorMessage(error));
    }
  }

  function closePanel(returnTo: "inbox" | "comments") {
    setPanel("none");
    onEditingChange(false);
    setTimeout(() => {
      if (returnTo === "inbox") inboxBtnRef.current?.focus();
      else commentBtnRef.current?.focus();
    }, 0);
  }

  async function updateInbox(method: "PUT" | "DELETE") {
    if (!inboxState) return;
    setInboxBusy(true);
    try {
      const trimmedAfter = afterReply.trim();
      const next =
        method === "PUT"
          ? await apiPut<
              "/api/v1/agent/tasks/{identity}/goal/events",
              GoalEventState,
              {
                source: "bilibili_inbox";
                sender_uid: string;
                revision: number;
                epoch: number;
                after_reply?: string;
              }
            >("/api/v1/agent/tasks/{identity}/goal/events", {
              pathParams: { identity: goalId },
              body: {
                source: "bilibili_inbox",
                sender_uid: senderUid.trim(),
                revision: inboxState.revision,
                epoch: inboxState.epoch || 0,
                ...(trimmedAfter ? { after_reply: trimmedAfter } : {}),
              },
            })
          : await apiDelete<"/api/v1/agent/tasks/{identity}/goal/events", GoalEventState>(
              "/api/v1/agent/tasks/{identity}/goal/events",
              {
                pathParams: { identity: goalId },
                query: { revision: inboxState.revision },
              }
            );
      setInboxState(next);
      setSenderUid(next.sender_uid || "");
      setAfterReply(next.after_reply || "");
      const binding = next.account ? `接收账号 ${next.account} · 角色 ${next.character}` : "尚未绑定接收账号";
      setInboxNotice(
        next.enabled ? `${binding} · 已跟踪 UID ${next.sender_uid}` : `${binding} · 未启用`
      );
    } catch (error) {
      setInboxNotice(`${errorMessage(error)}；可返回目标并重新打开设置。`);
    } finally {
      setInboxBusy(false);
    }
  }

  async function updateComments(method: "PUT" | "DELETE") {
    if (!commentState) return;
    setCommentBusy(true);
    try {
      const next =
        method === "PUT"
          ? await apiPut<
              "/api/v1/agent/tasks/{identity}/goal/comment-events",
              GoalCommentEventState,
              {
                resource: string;
                root_id: string;
                revision: number;
                epoch: number;
              }
            >("/api/v1/agent/tasks/{identity}/goal/comment-events", {
              pathParams: { identity: goalId },
              body: {
                resource: resource.trim(),
                root_id: rootId.trim(),
                revision: commentState.revision,
                epoch: commentState.epoch || 0,
              },
            })
          : await apiDelete<"/api/v1/agent/tasks/{identity}/goal/comment-events", GoalCommentEventState>(
              "/api/v1/agent/tasks/{identity}/goal/comment-events",
              {
                pathParams: { identity: goalId },
                query: { revision: commentState.revision },
              }
            );
      setCommentState(next);
      setResource(next.resource || "");
      setRootId(next.root_id || "");
      const binding = next.account ? `接收账号 ${next.account} · 角色 ${next.character}` : "尚未绑定接收账号";
      setCommentNotice(
        next.enabled
          ? `${binding} · 已跟踪 ${next.resource} 楼 ${next.root_id}`
          : `${binding} · 未启用`
      );
    } catch (error) {
      setCommentNotice(`${errorMessage(error)}；可返回目标并重新打开设置。`);
    } finally {
      setCommentBusy(false);
    }
  }

  function handleInboxSubmit(event: FormEvent) {
    event.preventDefault();
    void updateInbox("PUT");
  }

  function handleCommentsSubmit(event: FormEvent) {
    event.preventDefault();
    void updateComments("PUT");
  }

  if (panel === "inbox") {
    const canSave = Boolean(inboxState?.account && inboxState?.epoch && !inboxState?.closed && !inboxBusy);
    return (
      <form className="chat-task-steer" onSubmit={handleInboxSubmit}>
        {inboxState && (
          <p className="hint">
            仅跟踪授权后新入库的 B 站来信（含延迟同步）。只记消息标识，不读取正文，不开启轮询或自动回复。
          </p>
        )}
        <p className="hint" role="status">
          {inboxNotice}
        </p>
        {inboxState && (
          <>
            <label>
              发送者 UID
              <input
                ref={inboxUidRef}
                inputMode="numeric"
                pattern="[1-9][0-9]{0,19}"
                maxLength={20}
                required
                value={senderUid}
                onChange={(event) => setSenderUid(event.target.value)}
              />
            </label>
            <label>
              来信后交办的私有文本工作（可选）
              <textarea
                rows={2}
                maxLength={400}
                value={afterReply}
                onChange={(event) => setAfterReply(event.target.value)}
              />
            </label>
            <button type="submit" disabled={!canSave}>
              授权跟踪此 UID
            </button>
            <button
              type="button"
              hidden={!inboxState.enabled}
              disabled={inboxBusy}
              onClick={() => void updateInbox("DELETE")}
            >
              停止事件跟踪
            </button>
          </>
        )}
        <button type="button" onClick={() => closePanel("inbox")}>
          返回目标
        </button>
      </form>
    );
  }

  if (panel === "comments") {
    const canSave = Boolean(
      commentState?.account && commentState?.epoch && !commentState?.closed && !commentBusy
    );
    return (
      <form className="chat-task-steer" onSubmit={handleCommentsSubmit}>
        {commentState && (
          <p className="hint">
            仅跟踪授权后新观察到的指定评论楼回复。只记评论标识，不开启轮询或自动回复。
          </p>
        )}
        <p className="hint" role="status">
          {commentNotice}
        </p>
        {commentState && (
          <>
            <label>
              视频 BV 号或动态目标
              <input
                ref={commentResRef}
                placeholder="BV… 或 dynamic:12345"
                pattern="(BV[0-9A-Za-z]{10}|dynamic:[1-9][0-9]{0,19})"
                required
                value={resource}
                onChange={(event) => setResource(event.target.value)}
              />
            </label>
            <label>
              评论根 ID
              <input
                inputMode="numeric"
                pattern="[1-9][0-9]{0,19}"
                maxLength={20}
                required
                value={rootId}
                onChange={(event) => setRootId(event.target.value)}
              />
            </label>
            <button type="submit" disabled={!canSave}>
              授权跟踪此评论楼
            </button>
            <button
              type="button"
              hidden={!commentState.enabled}
              disabled={commentBusy}
              onClick={() => void updateComments("DELETE")}
            >
              停止评论事件跟踪
            </button>
          </>
        )}
        <button type="button" onClick={() => closePanel("comments")}>
          返回目标
        </button>
      </form>
    );
  }

  return (
    <>
      <button ref={inboxBtnRef} type="button" onClick={() => void openInbox()}>
        来信事件跟踪
      </button>
      <button ref={commentBtnRef} type="button" onClick={() => void openComments()}>
        评论事件跟踪
      </button>
    </>
  );
}
