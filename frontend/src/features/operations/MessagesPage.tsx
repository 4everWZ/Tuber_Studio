import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  MemePickerModal,
  OperationContinuationCard,
  useUnsavedGuard,
  type MemeAsset,
} from "@/shared";
import { apiGet, apiPost, apiPut } from "@/shared/api/client";
import { useAppSession } from "@/shared/hooks/useAppSession";
import { emitNotice, errorMessage, webCharacterName } from "@/shared/lib/errors";
import { illustrationStatus, illustrationUrl } from "@/shared/lib/operationImage";
import type { ParsedRoute } from "@/shell/navigation";
import { contactLabel, contactReason, contactStatus } from "./messageLabels";
import { MessagePolicyForm } from "./PolicyForms";
import { createReplyHandoff } from "./replyHandoff";
import styles from "./operations.module.css";
import type {
  MessageConversationResponse,
  MessageDraft,
  MessagePolicyState,
  MessagesListResponse,
} from "./types";
import { UserMemoryModal, type UserMemoryTarget } from "./UserMemoryModal";

const DRAFT_STATUS_LABELS: Record<string, string> = {
  queued: "等待生成",
  generating: "生成中",
  pending_review: "待审核",
  approved: "已审核",
  rejected: "已退回",
  copied: "已复制",
  published: "已发送",
  publishing: "发送中",
  publish_uncertain: "发送结果待核对",
  failed: "生成失败",
  cancelled: "已取消",
  waiting: "等待约定或对方回应",
  not_initiated: "未发起私信",
};

const HISTORY_STATUSES = new Set(["published", "rejected", "copied", "not_initiated"]);
const ACCEPT_ALL_MEMES = () => true;

export function messagePolicyStatus(policy: MessagePolicyState | null | undefined): string {
  if (!policy) return "";
  const base = policy.base || policy;
  const saved = `已配置${base.auto_poll ? "自动读取" : "手动读取"} · ${base.auto_send ? "自动回复" : "审核后发送"}`;
  if (policy.emergency) return `紧急停止；${saved}`;
  if (policy.automation_override === false) return `自主运营已停止；${saved}`;
  return `${policy.auto_poll ? "自动读取" : "手动读取"} · ${policy.auto_send ? "自动回复" : "审核后发送"}`;
}

export function messageDraftState(row: MessageDraft | null | undefined): {
  history: boolean;
  progress: boolean;
  waiting: boolean;
  continueAgreement: boolean;
  label: string;
} {
  const status = row?.status || "";
  const history = HISTORY_STATUSES.has(status);
  const label = contactStatus(row) || DRAFT_STATUS_LABELS[status] || status;
  return {
    history,
    progress: status === "queued" || status === "generating",
    waiting: status === "waiting",
    continueAgreement: status === "waiting" && row?.contact_state === "awaiting_completion",
    label,
  };
}

interface ParsedTriggerSource {
  kind?: string;
  bvid?: string;
  root?: string | number;
  comment_id?: string | number;
  ids?: number[];
  message_id?: string;
}

export function parseTriggerSource(raw: MessageDraft["trigger_source"]): ParsedTriggerSource | null {
  if (!raw) return null;
  if (typeof raw === "string") {
    try {
      const parsed = JSON.parse(raw);
      return typeof parsed === "object" && parsed ? (parsed as ParsedTriggerSource) : null;
    } catch {
      return null;
    }
  }
  return raw as ParsedTriggerSource;
}

export function TriggerProvenanceDetails({ draft }: { draft: MessageDraft | null | undefined }) {
  if (!draft) return null;
  const source = parseTriggerSource(draft.trigger_source);
  const check = draft.outreach_check;
  const assessment = check?.assessment;

  const commentHref =
    source?.kind === "comment" &&
    source.bvid &&
    /^BV[0-9A-Za-z]{10}$/.test(source.bvid) &&
    source.comment_id &&
    /^[1-9][0-9]*$/.test(String(source.comment_id))
      ? `https://www.bilibili.com/video/${source.bvid}#reply${source.comment_id}`
      : null;

  return (
    <details data-trigger="" style={{ marginBlock: 8 }}>
      <summary style={{ cursor: "pointer", fontWeight: 600 }}>触发来源与联系目的</summary>
      <div>
        <p>
          {contactLabel(draft)} · {draft.purpose || "未记录联系目的"}
        </p>
        {source?.kind === "comment" ? (
          commentHref ? (
            <a href={commentHref} target="_blank" rel="noopener noreferrer">
              公开评论 {source.bvid || ""} · 楼 {source.root || "未知"} · 评论{" "}
              {source.comment_id || "未知"}
            </a>
          ) : (
            <p>
              公开评论 {source.bvid || ""} · 楼 {source.root || "未知"} · 评论{" "}
              {source.comment_id || "未知"}
            </p>
          )
        ) : source?.kind === "message" ? (
          <p>
            对方来信 · 本地编号 {(source.ids || [draft.source_id]).join("、")} · 平台消息{" "}
            {source.message_id || "未记录"}
          </p>
        ) : (
          <p className="hint">旧记录未保存结构化触发来源，以上方原来源为准。</p>
        )}
        {assessment?.reason && <p>联系判断：{assessment.reason}</p>}
        {assessment?.invitation_quote && <p>邀请或约定原话：{assessment.invitation_quote}</p>}
        {check?.previous_draft_id && <p>关联先前私信 #{check.previous_draft_id}</p>}
      </div>
    </details>
  );
}

export function MessagesPage({ route }: { route: ParsedRoute }) {
  const { status: sysStatus } = useAppSession();
  const routeInboxId = route.routeKind === "inbox" && route.routeId ? route.routeId : null;

  const [value, setValue] = useState<MessagesListResponse | null>(null);
  const [selectedUser, setSelectedUser] = useState<string>("");
  const [selectedDraftId, setSelectedDraftId] = useState<number | null>(null);
  const [draftView, setDraftView] = useState<"pending" | "history">("pending");
  const [inboxId, setInboxId] = useState<string | null>(routeInboxId);
  const [conversation, setConversation] = useState<MessageConversationResponse | null>(null);
  const [conversationStateText, setConversationStateText] = useState("");
  const [errorText, setErrorText] = useState("");
  const [busy, setBusy] = useState(false);

  // Scoped edits map keyed by `${accountId}/${draftId}`
  const [edits, setEdits] = useState<Map<string, string>>(() => new Map());
  const [unsavedByAccount, setUnsavedByAccount] = useState<
    Map<string, { selected: number; user: string; text: string }>
  >(() => new Map());
  const [policyDirty, setPolicyDirty] = useState(false);

  // Reply handoff checks
  const [replyChecks, setReplyChecks] = useState<Map<number, number | undefined>>(() => new Map());

  // Private continuations
  const [privateSelectedId, setPrivateSelectedId] = useState<string | null>(null);

  // Meme Picker & User Memory Modal
  const [memePickerMode, setMemePickerMode] = useState<{
    attach: boolean;
    draftId: number;
    account: string;
    character?: string;
    updatedAt?: number;
    selectedAssetId?: string | null;
  } | null>(null);
  const [memeCache, setMemeCache] = useState<Map<string, MemeAsset>>(() => new Map());
  const [imagePreviewError, setImagePreviewError] = useState(false);
  const [userMemoryTarget, setUserMemoryTarget] = useState<UserMemoryTarget | null>(null);

  const refreshSeqRef = useRef(0);
  const conversationSeqRef = useRef(0);
  const refreshedAtRef = useRef(0);
  const backgroundPendingRef = useRef(false);
  const prevScopeRef = useRef<{ account: string; character: string } | null>(null);
  const delegateRef = useRef(createReplyHandoff());
  const focusedInboxRef = useRef<string | null>(null);
  const targetMessageRefs = useRef<Record<number, HTMLElement | null>>({});

  const selectedDraft = useMemo(
    () => value?.drafts.find((row) => row.id === selectedDraftId) ?? null,
    [value, selectedDraftId]
  );

  const editKey = useCallback(
    (id: number | null = selectedDraftId, accountId: string = value?.account_id || "") =>
      id != null ? `${accountId}/${id}` : "",
    [selectedDraftId, value?.account_id]
  );

  const currentEditKey = editKey();
  const isSelectedDirty = Boolean(currentEditKey && edits.has(currentEditKey));
  const editorText = isSelectedDirty
    ? edits.get(currentEditKey) || ""
    : selectedDraft?.text || "";

  useUnsavedGuard(
    "messages-workbench",
    () => edits.size > 0 || policyDirty || unsavedByAccount.size > 0
  );

  const loadConversation = useCallback(
    async (
      currentValue: MessagesListResponse | null,
      targetUser: string,
      targetInboxId: string | null
    ) => {
      const seq = ++conversationSeqRef.current;
      if (!currentValue || (!targetUser && !targetInboxId)) {
        setConversation(null);
        setConversationStateText("");
        return;
      }

      const acct = currentValue.account_id;
      const charId = currentValue.character_id;
      const [platform, userId] = targetUser ? targetUser.split("|") : ["", ""];

      try {
        const res = targetInboxId
          ? await apiGet<
              "/api/v1/operations/messages/inbox/{identity}/conversation",
              MessageConversationResponse
            >("/api/v1/operations/messages/inbox/{identity}/conversation", {
              pathParams: { identity: Number(targetInboxId) },
            })
          : await apiGet<
              "/api/v1/operations/messages/conversation",
              MessageConversationResponse
            >("/api/v1/operations/messages/conversation", {
              query: { platform, user_id: userId },
            });

        if (seq !== conversationSeqRef.current) return;
        if (
          ("account_id" in res && res.account_id !== acct) ||
          ("character_id" in res && res.character_id !== charId)
        ) {
          throw new Error("账号或角色已变化，请刷新私信工作台。");
        }

        if (targetInboxId) {
          const targetMsg = res.messages.find((m) => String(m.id) === targetInboxId);
          if (!targetMsg) throw new Error("当前账号和角色下找不到这条来信");
          const resolvedUser = `${targetMsg.platform}|${targetMsg.platform_user_id}`;
          setSelectedUser(resolvedUser);
          setValue((prev) => {
            if (!prev) return prev;
            if (
              prev.users.some(
                (u) => `${u.platform}|${u.platform_user_id}` === resolvedUser
              )
            ) {
              return prev;
            }
            return {
              ...prev,
              users: [
                ...prev.users,
                {
                  platform: targetMsg.platform || "bilibili",
                  platform_user_id: targetMsg.platform_user_id || "",
                  display_name: targetMsg.display_name,
                },
              ],
            };
          });
        }

        setConversation(res);
        setConversationStateText(res.messages.length ? "" : "此会话暂无来信。");

        if (targetInboxId && focusedInboxRef.current !== targetInboxId) {
          focusedInboxRef.current = targetInboxId;
          setTimeout(() => {
            const el = targetMessageRefs.current[Number(targetInboxId)];
            if (el) {
              el.focus({ preventScroll: true });
              el.scrollIntoView?.({ block: "nearest" });
            }
          }, 0);
        }
      } catch (err) {
        if (seq !== conversationSeqRef.current) return;
        if (targetInboxId) {
          setSelectedUser("");
          setSelectedDraftId(null);
          setConversation(null);
          setConversationStateText("来信无法定位，可选择当前账号和角色的其他会话。");
        } else {
          setConversationStateText("会话读取失败，可点击刷新重试。");
        }
        setErrorText(errorMessage(err));
      }
    },
    []
  );

  const refreshMessages = useCallback(
    async ({ background = false }: { background?: boolean } = {}) => {
      if (
        background &&
        (busy || backgroundPendingRef.current || Date.now() - refreshedAtRef.current < 8000)
      ) {
        return;
      }
      if (background) backgroundPendingRef.current = true;
      refreshedAtRef.current = Date.now();
      const seq = ++refreshSeqRef.current;

      try {
        const nextVal = await apiGet<"/api/v1/operations/messages", MessagesListResponse>(
          "/api/v1/operations/messages"
        );
        if (seq !== refreshSeqRef.current) return;

        let activeUser = selectedUser;
        let activeDraftId = selectedDraftId;

        const prevScope = prevScopeRef.current;
        if (
          prevScope &&
          (prevScope.account !== nextVal.account_id ||
            prevScope.character !== nextVal.character_id)
        ) {
          setPrivateSelectedId(null);
          setMemePickerMode(null);
          const oldScopeKey = JSON.stringify([prevScope.account, prevScope.character]);
          const newScopeKey = JSON.stringify([nextVal.account_id, nextVal.character_id]);
          const prevDraftKey = activeDraftId != null ? `${prevScope.account}/${activeDraftId}` : "";
          if (prevDraftKey && edits.has(prevDraftKey) && activeDraftId != null) {
            setUnsavedByAccount((prev) =>
              new Map(prev).set(oldScopeKey, {
                selected: activeDraftId!,
                user: activeUser,
                text: edits.get(prevDraftKey) || "",
              })
            );
          }
          conversationSeqRef.current += 1;
          activeUser = "";
          activeDraftId = null;
          setConversation(null);

          const saved = unsavedByAccount.get(newScopeKey);
          if (
            !inboxId &&
            saved &&
            nextVal.drafts.some((row) => row.id === saved.selected)
          ) {
            activeDraftId = saved.selected;
            activeUser = saved.user;
            setEdits((prev) =>
              new Map(prev).set(`${nextVal.account_id}/${saved.selected}`, saved.text)
            );
            setUnsavedByAccount((prev) => {
              const next = new Map(prev);
              next.delete(newScopeKey);
              return next;
            });
          }
          setSelectedUser(activeUser);
          setSelectedDraftId(activeDraftId);
          setErrorText("账号或角色已变化，已隐藏旧会话。未保存编辑仍留在原账号和角色。");
        }

        prevScopeRef.current = {
          account: nextVal.account_id,
          character: nextVal.character_id,
        };

        const foundDraft = nextVal.drafts.find((row) => row.id === activeDraftId);
        const activeKey =
          activeDraftId != null ? `${nextVal.account_id}/${activeDraftId}` : "";
        const isActiveDirty = Boolean(activeKey && edits.has(activeKey));
        if (
          activeDraftId != null &&
          !isActiveDirty &&
          (!foundDraft ||
            (draftView !== "history" && messageDraftState(foundDraft).history))
        ) {
          setSelectedDraftId(null);
          setMemePickerMode(null);
        }

        setValue(nextVal);
        await loadConversation(nextVal, activeUser, inboxId);
      } catch (err) {
        if (seq === refreshSeqRef.current) {
          setErrorText(errorMessage(err));
        }
      } finally {
        if (background) backgroundPendingRef.current = false;
      }
    },
    [busy, selectedUser, selectedDraftId, edits, unsavedByAccount, inboxId, draftView, loadConversation]
  );

  useEffect(() => {
    setInboxId(routeInboxId);
    if (routeInboxId) {
      focusedInboxRef.current = null;
      conversationSeqRef.current += 1;
      setConversation(null);
      setConversationStateText("正在定位来信…");
    }
    void refreshMessages();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routeInboxId]);

  useEffect(() => {
    const timer = setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      void refreshMessages({ background: true });
    }, 5000);
    return () => clearInterval(timer);
  }, [refreshMessages]);

  const drafts = useMemo(() => {
    if (!value) return [];
    return value.drafts.filter(
      (row) =>
        (!selectedUser || `${row.platform}|${row.platform_user_id}` === selectedUser) &&
        messageDraftState(row).history === (draftView === "history")
    );
  }, [value, selectedUser, draftView]);

  // Prune reply checks when scope or draft timestamps change
  const replyScopeKey = JSON.stringify([
    value?.account_id,
    value?.character_id,
    selectedUser,
    draftView,
  ]);
  const prevReplyScopeRef = useRef(replyScopeKey);
  useEffect(() => {
    if (prevReplyScopeRef.current !== replyScopeKey) {
      prevReplyScopeRef.current = replyScopeKey;
      setReplyChecks(new Map());
      return;
    }
    setReplyChecks((prev) => {
      let changed = false;
      const next = new Map(prev);
      for (const [id, stamp] of next) {
        if (!drafts.some((r) => r.id === id && r.updated_at === stamp)) {
          next.delete(id);
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [replyScopeKey, drafts]);

  function applyDraftResult(result: { status?: string } | null | undefined, id: number) {
    if (!result?.status) return;
    const key = editKey(id);
    if (key) {
      setEdits((prev) => {
        const next = new Map(prev);
        next.delete(key);
        return next;
      });
    }
    if (selectedDraftId === id && messageDraftState(result as MessageDraft).history) {
      setSelectedDraftId(null);
      setMemePickerMode(null);
    }
  }

  async function runAction(work: () => Promise<void>) {
    if (busy) return;
    setBusy(true);
    setErrorText("");
    try {
      await work();
    } catch (err) {
      setErrorText(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  const draftState = messageDraftState(selectedDraft);
  const terminalInPending = draftView !== "history" && draftState.history;
  const editable = Boolean(
    selectedDraft &&
      !terminalInPending &&
      ["pending_review", "approved", "rejected", "copied"].includes(selectedDraft.status)
  );
  const hideEditor = !isSelectedDirty && (draftState.progress || draftState.waiting);
  const ref = selectedDraft?.illustration;
  const legacyImage = selectedDraft?.message_kind === "image";
  const hasImage = legacyImage || Boolean(ref);
  const asset =
    selectedDraft?.meme ||
    (selectedDraft?.character_id && selectedDraft?.meme_id
      ? memeCache.get(`${selectedDraft.character_id}:${selectedDraft.meme_id}`)
      : undefined);
  const assetUnavailable = Boolean(hasImage && asset && (!asset.enabled || asset.missing));
  const awaitingChoice = selectedDraft?.illustration_choice === "awaiting_choice";
  const textOnly = selectedDraft?.illustration_choice === "text_only";
  const needsImagePreview =
    awaitingChoice || (isSelectedDirty && Boolean(selectedDraft?.image_ref?.job_id));
  const canContinueOrRetry =
    draftState.continueAgreement ||
    Boolean(selectedDraft && ["failed", "cancelled"].includes(selectedDraft.status));
  const hideRetryForGeneration =
    !draftState.continueAgreement && selectedDraft?.continuation?.phase === "generation";

  const previewUrl = ref
    ? illustrationUrl("messages", selectedDraft!.id, ref)
    : legacyImage && selectedDraft?.meme_id
      ? illustrationUrl("messages", selectedDraft.id, { asset_id: selectedDraft.meme_id })
      : "";

  const policy = value?.policy;
  const privateContinuations = value?.private_continuations || [];
  const activePrivateContinuation =
    privateContinuations.find((c) => c.id === privateSelectedId) ||
    privateContinuations[0] ||
    null;

  const relationship = conversation?.relationship;
  const statusLine = value
    ? `${value.account_id ? `当前账号 ${value.account_id}` : "当前账号尚未核验"} · ${policy?.enabled ? "私信已启用" : "私信已关闭"} · ${messagePolicyStatus(policy)}${!policy?.credentials_configured ? " · 尚未配置账号凭证" : ""}${policyDirty ? " · 基础策略有未保存修改" : ""}`
    : "正在读取私信工作台…";

  return (
    <section data-section="messages" className="win-page-view">
      <div id="messages-panel">
        <div className="page-heading win-heading">
          <div>
            <h1>私信工作台</h1>
          </div>
          <div className="actions">
            <button
              type="button"
              data-sync=""
              disabled={!policy?.enabled || busy}
              onClick={() =>
                void runAction(async () => {
                  const result = await apiPost<
                    "/api/v1/operations/messages/sync",
                    { imported?: number; errors?: unknown[] },
                    never
                  >("/api/v1/operations/messages/sync");
                  emitNotice(
                    `读取完成，新增 ${result.imported ?? 0} 条私信${result.errors?.length ? "，部分会话读取失败" : ""}`
                  );
                  await refreshMessages();
                })
              }
            >
              读取最新私信
            </button>
            <button
              type="button"
              data-refresh=""
              disabled={busy}
              onClick={() => void refreshMessages()}
            >
              刷新
            </button>
            <a href="#message-policy-settings">私信设置 →</a>
          </div>
        </div>

        <p className="hint">
          从来信选择回复对象。收到私聊邀请或完成约定后，可向原发言人联系一次。
        </p>

        {(policy?.automation_override || policy?.emergency) && (
          <p data-override="" className="feature-note">
            <span data-override-text="">
              {policy.emergency
                ? "紧急停止已生效，所有自动收发被阻止。这里编辑个人基础设置；解除紧停前不会恢复自动业务。"
                : "自主运营正在运行，按已保存的私信权限处理。设置保存后生效；统一停止请回到活动页。"}
            </span>{" "}
            <a href="#automation">自主浏览与互动 →</a>
          </p>
        )}

        <MessagePolicyForm
          policy={policy ?? null}
          onDirtyChange={setPolicyDirty}
          onSavePolicy={async (nextPolicy) => {
            await apiPut("/api/v1/operations/messages/policy", { body: nextPolicy });
            setPolicyDirty(false);
            await refreshMessages();
          }}
        />

        <p data-status="" role="status">
          {statusLine}
        </p>
        <p data-error="" className="error-text" role="alert">
          {errorText || policy?.last_error || ""}
        </p>

        <div className={styles.messageLayout}>
          {/* Left: Incoming Conversations */}
          <section className="panel">
            <h2>来信会话</h2>
            <label>
              会话
              <select
                data-users=""
                disabled={busy}
                value={selectedUser}
                onChange={(e) => {
                  const nextUser = e.target.value;
                  if (inboxId && typeof window !== "undefined" && typeof window.history !== "undefined") {
                    window.history.replaceState(null, "", "#messages");
                  }
                  setInboxId(null);
                  setSelectedUser(nextUser);
                  setSelectedDraftId(null);
                  setErrorText("");
                  setConversation(null);
                  setConversationStateText(nextUser ? "正在读取会话…" : "");
                  void loadConversation(value, nextUser, null);
                }}
              >
                <option value="">选择会话</option>
                {(value?.users || []).map((user) => {
                  const key = `${user.platform}|${user.platform_user_id}`;
                  return (
                    <option key={key} value={key}>
                      {user.display_name || "来信人"} · {user.platform_user_id}
                    </option>
                  );
                })}
              </select>
            </label>

            <p data-relation="" className="hint">
              {relationship
                ? typeof relationship.affinity === "number"
                  ? `与当前OC的好感度 ${relationship.affinity} / 100 · ${relationship.stage}（评论／私信共享）`
                  : "个性化未启用"
                : ""}
            </p>

            {selectedUser && value?.account_id && (
              <button
                type="button"
                data-user-profile=""
                onClick={() => {
                  const userObj = value.users.find(
                    (u) => `${u.platform}|${u.platform_user_id}` === selectedUser
                  );
                  const characterId =
                    value.character_id || sysStatus?.character?.id || "";
                  if (!userObj || !characterId) return;
                  setUserMemoryTarget({
                    platform: "bilibili",
                    userId: userObj.platform_user_id,
                    accountId: value.account_id,
                    characterId,
                    characterName: webCharacterName(sysStatus?.character),
                    name: userObj.display_name,
                    onChange: () => refreshMessages(),
                  });
                }}
              >
                查看档案
              </button>
            )}

            <p data-conversation-state="" className="hint" role="status">
              {conversationStateText}
            </p>

            <div data-inbox="">
              {(conversation?.messages || []).map((msg) => {
                const isTarget = String(msg.id) === inboxId;
                const ts = new Date(msg.sent_at * 1000).toLocaleString("zh-CN");
                const known = conversation?.drafts.find(
                  (d) => d.source_kind === "message" && d.source_id === msg.id
                );
                return (
                  <article
                    key={msg.id}
                    ref={(el) => {
                      targetMessageRefs.current[msg.id] = el;
                    }}
                    className={styles.messageIncoming}
                    data-message-id={String(msg.id)}
                    data-platform-user-id={String(
                      msg.platform_user_id || selectedUser.split("|")[1] || ""
                    )}
                    tabIndex={isTarget ? -1 : undefined}
                    aria-current={isTarget ? "true" : undefined}
                  >
                    <small>{isTarget ? `来源来信 · ${ts}` : ts}</small>
                    <p>{msg.content}</p>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() =>
                        void runAction(async () => {
                          const res = known
                            ? { draft_id: known.id }
                            : await apiPost<
                                "/api/v1/operations/messages/inbox/{identity}/reply",
                                { draft_id: number },
                                never
                              >("/api/v1/operations/messages/inbox/{identity}/reply", {
                                pathParams: { identity: msg.id },
                              });
                          setDraftView(
                            known && messageDraftState(known).history ? "history" : "pending"
                          );
                          await refreshMessages();
                          setSelectedDraftId(res.draft_id);
                        })
                      }
                    >
                      {known ? "查看已有回复" : "生成私信回复"}
                    </button>
                  </article>
                );
              })}
            </div>
          </section>

          {/* Right: Drafts & Provenance */}
          <section className="panel">
            <h2>私信草稿与回执</h2>
            <fieldset data-draft-views="" className={styles.queueStatusOptions}>
              <legend>私信记录</legend>
              {(
                [
                  ["pending", "待处理"],
                  ["history", "历史"],
                ] as const
              ).map(([val, label]) => (
                <label key={val}>
                  <input
                    type="radio"
                    name="message-draft-view"
                    value={val}
                    checked={draftView === val}
                    disabled={busy}
                    onChange={() => {
                      setDraftView(val);
                      setSelectedDraftId(null);
                      setErrorText("");
                    }}
                  />
                  <span>{label}</span>
                </label>
              ))}
            </fieldset>

            <div data-drafts="" className={styles.messageDrafts} style={{ marginTop: 10 }}>
              {drafts.length === 0 ? (
                <p className="hint">
                  {draftView === "history"
                    ? "暂无历史私信记录。"
                    : "暂无待处理私信。新来信按当前自动读取与发送设置处理。"}
                </p>
              ) : (
                drafts.map((row) => {
                  const label = `#${row.id} ${contactLabel(row)} · ${row.display_name || row.platform_user_id} · ${messageDraftState(row).label}`;
                  const canDelegate =
                    Boolean(row.reply_source_ids?.length) &&
                    ["pending_review", "approved", "copied"].includes(row.status);
                  const buttonEl = (
                    <button
                      key={row.id}
                      type="button"
                      disabled={busy}
                      aria-pressed={row.id === selectedDraftId}
                      onClick={() => {
                        setSelectedDraftId(row.id);
                        setImagePreviewError(false);
                        setErrorText("");
                      }}
                    >
                      {label}
                    </button>
                  );
                  if (!canDelegate) return buttonEl;
                  return (
                    <div key={row.id} className={styles.queueCheckRow}>
                      <label>
                        <input
                          type="checkbox"
                          disabled={busy}
                          aria-label={`选择私信草稿 ${label}`}
                          checked={replyChecks.has(row.id)}
                          onChange={(e) => {
                            setReplyChecks((prev) => {
                              const next = new Map(prev);
                              if (e.target.checked) next.set(row.id, row.updated_at);
                              else next.delete(row.id);
                              return next;
                            });
                          }}
                        />
                      </label>
                      {buttonEl}
                    </div>
                  );
                })
              )}
            </div>

            {replyChecks.size > 0 && (
              <button
                type="button"
                disabled={busy || replyChecks.size > 100}
                style={{ marginBottom: 10 }}
                onClick={() =>
                  void runAction(async () => {
                    const items = [...replyChecks.keys()].map((id) => {
                      const row = value?.drafts.find((d) => d.id === id);
                      const rowKey = editKey(id);
                      if (
                        !row ||
                        (rowKey && edits.has(rowKey)) ||
                        replyChecks.get(id) !== row.updated_at
                      ) {
                        throw new Error("草稿已变化，请保存后重新选择。");
                      }
                      return {
                        channel: "messages" as const,
                        draft_id: id,
                        source_ids: row.reply_source_ids || [],
                      };
                    });
                    await delegateRef.current(items);
                    setReplyChecks(new Map());
                    emitNotice(`已交办 ${items.length} 份私信草稿，可在对话的活动中逐条审批。`);
                  })
                }
              >
                交办逐条送审（{replyChecks.size}）
              </button>
            )}

            <p data-source="" className="quoted-text">
              {selectedDraft
                ? `${contactLabel(selectedDraft)} · ${selectedDraft.display_name || selectedDraft.platform_user_id} · UID ${selectedDraft.platform_user_id} · ${selectedDraft.source_kind === "comment" ? "评论转入" : selectedDraft.source_kind === "message" ? "对方来信" : "来源记录"} #${selectedDraft.source_id} · ${contactReason(selectedDraft.reason)} · ${selectedDraft.character_id}`
                : "选择草稿查看来源和回复。"}
            </p>

            <TriggerProvenanceDetails draft={selectedDraft} />

            <p data-draft-status="">
              {isSelectedDirty && !editable
                ? "草稿已移出当前列表，本地修改可复制。"
                : selectedDraft
                  ? `${draftState.label}${selectedDraft.purpose && draftState.waiting ? ` · ${selectedDraft.purpose}` : ""}${selectedDraft.error && selectedDraft.status !== "publish_uncertain" ? ` · ${selectedDraft.error}` : ""}${isSelectedDirty ? " · 有未保存修改" : ""}`
                  : ""}
            </p>

            {editable && (
              <div className="actions" style={{ marginBottom: 10 }}>
                <button
                  type="button"
                  data-pick-meme=""
                  disabled={!editable || busy}
                  onClick={() => {
                    if (!selectedDraft || !value) return;
                    setMemePickerMode({
                      attach: false,
                      draftId: selectedDraft.id,
                      account: value.account_id,
                      character: selectedDraft.character_id,
                      updatedAt: selectedDraft.updated_at,
                      selectedAssetId: selectedDraft.meme_id,
                    });
                  }}
                >
                  选择表情包
                </button>
                <button
                  type="button"
                  data-attach-image=""
                  disabled={!editable || busy || !selectedDraft?.account_id}
                  onClick={() => {
                    if (!selectedDraft || !value) return;
                    setMemePickerMode({
                      attach: true,
                      draftId: selectedDraft.id,
                      account: value.account_id,
                      character: selectedDraft.character_id,
                      updatedAt: selectedDraft.updated_at,
                      selectedAssetId: selectedDraft.illustration?.asset_id,
                    });
                  }}
                >
                  附图并保留正文
                </button>
                {Boolean(selectedDraft?.image_ref?.job_id) && (
                  <button
                    type="button"
                    data-reselect-image=""
                    disabled={busy || isSelectedDirty || !awaitingChoice}
                    onClick={() =>
                      void runAction(async () => {
                        if (!selectedDraft) return;
                        await apiPost(
                          "/api/v1/operations/messages/drafts/{identity}/review",
                          {
                            pathParams: { identity: selectedDraft.id },
                            body: {
                              action: "reselect_image",
                              updated_at: selectedDraft.updated_at,
                              expected_image: selectedDraft.illustration ?? null,
                            },
                          }
                        );
                        await refreshMessages();
                      })
                    }
                  >
                    重新选图
                  </button>
                )}
                <button
                  type="button"
                  data-clear-meme=""
                  disabled={!editable || busy || !hasImage}
                  onClick={() =>
                    void runAction(async () => {
                      if (!selectedDraft) return;
                      await apiPost(
                        "/api/v1/operations/messages/drafts/{identity}/review",
                        {
                          pathParams: { identity: selectedDraft.id },
                          body: {
                            action: "edit",
                            updated_at: selectedDraft.updated_at,
                            meme_id: null,
                            ...(editorText.trim() ? { text: editorText } : {}),
                          },
                        }
                      );
                      await refreshMessages();
                    })
                  }
                >
                  使用纯文字
                </button>
                <a href="#assets">管理表情包图库 →</a>
              </div>
            )}

            {(hasImage || textOnly) && (
              <div data-meme-preview="" className={styles.memeAttachmentPreview}>
                <strong data-meme-name="">
                  {ref
                    ? ref.asset_id
                      ? "图库配图"
                      : ref.artifact_id
                        ? "生成配图"
                        : "私信配图"
                    : legacyImage
                      ? asset?.title || selectedDraft?.meme_id || "已选图片"
                      : textOnly
                        ? "纯文字"
                        : ""}
                </strong>
                {previewUrl && !imagePreviewError && (
                  <figure>
                    <img
                      data-meme-image=""
                      src={previewUrl}
                      alt="已选私信配图"
                      onError={() => setImagePreviewError(true)}
                    />
                  </figure>
                )}
                {imagePreviewError && (
                  <span data-meme-fallback="" className="hint">
                    图片暂时无法预览。
                  </span>
                )}
                <p data-meme-availability="" className="hint">
                  {ref
                    ? illustrationStatus(ref)
                    : legacyImage && assetUnavailable
                      ? "当前图片已禁用或缺失，请更换图片或改用文字。"
                      : ""}
                </p>
                <p data-image-mode="" className="hint">
                  {legacyImage
                    ? "只发一条图片消息，正文保留作备选。"
                    : hasImage && !awaitingChoice
                      ? "先发图片，再向同一 UID 发送正文，各自取得平台回执。"
                      : ""}
                </p>
              </div>
            )}

            {!hideEditor && (
              <label>
                <span data-text-label="">
                  {legacyImage ? "审阅文字 / 文字备选（不会随图片发送）" : "私信内容"}
                </span>
                <textarea
                  data-text=""
                  rows={6}
                  disabled={busy || (!editable && !isSelectedDirty)}
                  readOnly={!editable}
                  value={editorText}
                  onChange={(e) => {
                    if (!selectedDraft || !currentEditKey) return;
                    const nextText = e.target.value;
                    setEdits((prev) => {
                      const next = new Map(prev);
                      if (nextText === (selectedDraft.text || "")) next.delete(currentEditKey);
                      else next.set(currentEditKey, nextText);
                      return next;
                    });
                  }}
                />
              </label>
            )}

            <div className="actions" style={{ marginTop: 10 }}>
              {editable && (
                <>
                  <button
                    type="button"
                    data-review="edit"
                    disabled={busy}
                    onClick={() =>
                      void runAction(async () => {
                        if (!selectedDraft) return;
                        const result = await apiPost<
                          "/api/v1/operations/messages/drafts/{identity}/review",
                          MessageDraft
                        >("/api/v1/operations/messages/drafts/{identity}/review", {
                          pathParams: { identity: selectedDraft.id },
                          body: {
                            action: "edit",
                            text: editorText,
                            updated_at: selectedDraft.updated_at,
                            expected_image: selectedDraft.illustration ?? null,
                          },
                        });
                        applyDraftResult(result, selectedDraft.id);
                        await refreshMessages();
                      })
                    }
                  >
                    保存草稿
                  </button>
                  <button
                    type="button"
                    data-review="approve"
                    className="primary"
                    disabled={busy || needsImagePreview}
                    onClick={() =>
                      void runAction(async () => {
                        if (!selectedDraft) return;
                        const result = await apiPost<
                          "/api/v1/operations/messages/drafts/{identity}/review",
                          MessageDraft
                        >("/api/v1/operations/messages/drafts/{identity}/review", {
                          pathParams: { identity: selectedDraft.id },
                          body: {
                            action: "approve",
                            text: editorText,
                            updated_at: selectedDraft.updated_at,
                            expected_image: selectedDraft.illustration ?? null,
                          },
                        });
                        applyDraftResult(result, selectedDraft.id);
                        await refreshMessages();
                      })
                    }
                  >
                    审核通过
                  </button>
                  <button
                    type="button"
                    data-review="reject"
                    disabled={busy}
                    onClick={() =>
                      void runAction(async () => {
                        if (!selectedDraft) return;
                        const result = await apiPost<
                          "/api/v1/operations/messages/drafts/{identity}/review",
                          MessageDraft
                        >("/api/v1/operations/messages/drafts/{identity}/review", {
                          pathParams: { identity: selectedDraft.id },
                          body: {
                            action: "reject",
                            text: editorText,
                            updated_at: selectedDraft.updated_at,
                            expected_image: selectedDraft.illustration ?? null,
                          },
                        });
                        applyDraftResult(result, selectedDraft.id);
                        await refreshMessages();
                      })
                    }
                  >
                    拒绝
                  </button>
                  <button
                    type="button"
                    data-copy=""
                    disabled={
                      hasImage ||
                      !selectedDraft ||
                      !["approved", "copied"].includes(selectedDraft.status) ||
                      isSelectedDirty ||
                      busy
                    }
                    onClick={() =>
                      void runAction(async () => {
                        if (!selectedDraft) return;
                        await navigator.clipboard.writeText(editorText);
                        const result = await apiPost<
                          "/api/v1/operations/messages/drafts/{identity}/review",
                          MessageDraft
                        >("/api/v1/operations/messages/drafts/{identity}/review", {
                          pathParams: { identity: selectedDraft.id },
                          body: { action: "copy" },
                        });
                        applyDraftResult(result, selectedDraft.id);
                        await refreshMessages();
                      })
                    }
                  >
                    复制已审核内容
                  </button>
                  <button
                    type="button"
                    data-send=""
                    className="primary"
                    disabled={
                      !selectedDraft ||
                      awaitingChoice ||
                      !(legacyImage
                        ? ["pending_review", "approved", "copied"]
                        : ["approved", "copied"]
                      ).includes(selectedDraft.status) ||
                      isSelectedDirty ||
                      busy ||
                      assetUnavailable ||
                      !policy?.enabled ||
                      !policy?.credentials_configured
                    }
                    onClick={() =>
                      void runAction(async () => {
                        if (!selectedDraft) return;
                        try {
                          if (selectedDraft.message_kind === "image") {
                            await apiPost(
                              "/api/v1/operations/messages/drafts/{identity}/review",
                              {
                                pathParams: { identity: selectedDraft.id },
                                body: {
                                  action: "approve",
                                  updated_at: selectedDraft.updated_at,
                                  expected_image: selectedDraft.illustration ?? null,
                                },
                              }
                            );
                          }
                          const result = await apiPost<
                            "/api/v1/operations/messages/drafts/{identity}/send",
                            MessageDraft
                          >("/api/v1/operations/messages/drafts/{identity}/send", {
                            pathParams: { identity: selectedDraft.id },
                          });
                          applyDraftResult(result, selectedDraft.id);
                          if (result.status === "published") {
                            emitNotice(
                              selectedDraft.message_kind === "image"
                                ? "单张图片已发送，平台回执已保存。"
                                : "私信已发送，平台回执已保存。"
                            );
                          }
                        } finally {
                          await refreshMessages();
                        }
                      })
                    }
                  >
                    {legacyImage ? "批准并发送图片" : "发送已审核私信"}
                  </button>
                </>
              )}

              {canContinueOrRetry && !hideRetryForGeneration && (
                <button
                  type="button"
                  data-retry=""
                  disabled={busy}
                  onClick={() =>
                    void runAction(async () => {
                      if (!selectedDraft) return;
                      await apiPost(
                        "/api/v1/operations/messages/drafts/{identity}/retry",
                        { pathParams: { identity: selectedDraft.id } }
                      );
                      await refreshMessages();
                    })
                  }
                >
                  {draftState.continueAgreement ? "继续完成约定" : "重试生成"}
                </button>
              )}
            </div>

            <OperationContinuationCard
              value={selectedDraft?.continuation}
              channel="messages"
              localId={selectedDraft?.id}
              dirty={isSelectedDirty}
              entityStatus={selectedDraft?.status}
              onRefresh={() => refreshMessages()}
            />

            <pre data-receipt="" className="code-small" style={{ marginTop: 10 }}>
              {selectedDraft?.image_uploads?.length || selectedDraft?.delivery_steps?.length
                ? JSON.stringify(
                    {
                      图片上传: selectedDraft.image_uploads,
                      分步送达: selectedDraft.delivery_steps,
                      最终回执: selectedDraft.receipt,
                    },
                    null,
                    2
                  )
                : typeof selectedDraft?.receipt === "string"
                  ? selectedDraft.receipt
                  : selectedDraft?.receipt
                    ? JSON.stringify(selectedDraft.receipt, null, 2)
                    : selectedDraft?.status === "publish_uncertain"
                      ? selectedDraft.error || ""
                      : ""}
            </pre>
          </section>
        </div>

        {/* Private Generation Continuations */}
        {privateContinuations.length > 0 && (
          <section className="panel" data-private-generations="" style={{ marginTop: 16 }}>
            <h2>私域生成进度</h2>
            <p className="hint">这里保留移交私聊及渠道尚待确认的生成原稿。</p>
            <div data-private-generation-list="" className={styles.messageDrafts}>
              {privateContinuations.map((row) => (
                <button
                  key={row.id}
                  type="button"
                  aria-pressed={row.id === activePrivateContinuation?.id}
                  onClick={() => setPrivateSelectedId(row.id)}
                >
                  {row.source_title || "私域生成记录"} #{String(row.id).slice(0, 8)} ·{" "}
                  {row.state}
                </button>
              ))}
            </div>
            <OperationContinuationCard
              value={activePrivateContinuation}
              channel="messages"
              localId={null}
              privateContext
              onRefresh={() => refreshMessages()}
            />
          </section>
        )}
      </div>

      <MemePickerModal
        open={Boolean(memePickerMode)}
        selectedAssetId={memePickerMode?.selectedAssetId}
        description="选择启用的图片，保存后重新审核。"
        accept={ACCEPT_ALL_MEMES}
        onClose={() => setMemePickerMode(null)}
        onSelect={async (assetId, assetObj) => {
          if (!memePickerMode || !value || !selectedDraft) return;
          if (
            memePickerMode.draftId !== selectedDraft.id ||
            memePickerMode.account !== value.account_id ||
            memePickerMode.character !== selectedDraft.character_id
          ) {
            throw new Error("账号或草稿已变化，请重新选图。");
          }
          const ctx = memePickerMode;
          await apiPost("/api/v1/operations/messages/drafts/{identity}/review", {
            pathParams: { identity: ctx.draftId },
            body: {
              action: "edit",
              updated_at: ctx.updatedAt ?? selectedDraft.updated_at,
              ...(ctx.attach && assetId && assetObj
                ? {
                    image_ref: {
                      asset_id: assetId,
                      sha256: (assetObj as { sha256?: string }).sha256,
                    },
                  }
                : { meme_id: assetId }),
              ...(editorText.trim() ? { text: editorText } : {}),
            },
          });
          if (assetObj && selectedDraft.character_id) {
            setMemeCache((prev) =>
              new Map(prev).set(
                `${selectedDraft.character_id}:${assetObj.asset_id}`,
                assetObj
              )
            );
          }
          if (currentEditKey) {
            setEdits((prev) => {
              const next = new Map(prev);
              next.delete(currentEditKey);
              return next;
            });
          }
          await refreshMessages();
        }}
      />

      <UserMemoryModal
        target={userMemoryTarget}
        onClose={() => setUserMemoryTarget(null)}
      />
    </section>
  );
}
