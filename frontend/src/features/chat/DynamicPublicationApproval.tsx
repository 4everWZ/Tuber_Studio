import { useEffect, useMemo, useState, type FormEvent } from "react";
import { apiPatch, studioPath } from "@/shared/api/client";
import { errorMessage } from "@/shared/lib/errors";
import { MentionLinks } from "@/shared/ui/MentionLinksAndEditor";
import type { AgentTask, DynamicPublicationApprovalData } from "./types";

export interface DynamicPublicationApprovalProps {
  taskId: string;
  stepId: string;
  revision: number;
  approval: DynamicPublicationApprovalData;
  onUpdated: (task: AgentTask) => void;
}

export function DynamicPublicationApproval({
  taskId,
  stepId,
  revision,
  approval,
  onUpdated,
}: DynamicPublicationApprovalProps) {
  const carriedIds = useMemo(() => approval.carried_ids || [], [approval.carried_ids]);
  const carriedSet = useMemo(() => new Set(carriedIds), [carriedIds]);
  const drafts = useMemo(
    () => approval.posts.filter((item) => !carriedSet.has(item.post_id)),
    [approval.posts, carriedSet]
  );

  const [selected, setSelected] = useState<Set<number>>(() => new Set(carriedIds));
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");

  useEffect(() => {
    setSelected(new Set(carriedIds));
    setNote("");
  }, [taskId, stepId, revision, carriedIds]);

  const expiryMs = Number(approval.expires_at) * 1000;
  const expired = Date.now() >= expiryMs;
  const statusNote = note || (expired ? "确认已过期，请重新选择草稿。" : "");

  function togglePost(postId: number, checked: boolean) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (checked) next.add(postId);
      else next.delete(postId);
      return next;
    });
  }

  function selectAll() {
    setSelected((prev) => {
      const next = new Set(prev);
      for (const item of drafts) {
        next.add(item.post_id);
      }
      return next;
    });
  }

  async function apply(action: "approve" | "cancel") {
    if (action === "approve" && (Date.now() >= expiryMs || selected.size === 0)) {
      if (Date.now() >= expiryMs) {
        setNote("确认已过期，请重新选择草稿。");
      }
      return;
    }
    setBusy(true);
    setNote("");
    try {
      const result = await apiPatch<
        "/api/v1/agent/tasks/{identity}",
        AgentTask,
        {
          action: "approve" | "cancel";
          step_id: string;
          revision: number;
          selected_ids?: number[];
        }
      >("/api/v1/agent/tasks/{identity}", {
        pathParams: { identity: taskId },
        body: {
          action,
          step_id: stepId,
          revision,
          ...(action === "approve" ? { selected_ids: [...selected] } : {}),
        },
      });
      onUpdated(result);
    } catch (error) {
      setNote(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    void apply("approve");
  }

  return (
    <section>
      <h2>确认公开发布动态</h2>
      <form onSubmit={handleSubmit}>
        <fieldset disabled={busy}>
          <legend>{approval.target}</legend>
          {carriedSet.size > 0 && (
            <p className="hint">{carriedSet.size} 条内容未变，已保留确认。请核对有变化的草稿。</p>
          )}
          {drafts.length > 1 && (
            <button type="button" onClick={selectAll}>
              全选
            </button>
          )}
          {drafts.map((item) => {
            const caption = item.text.replace(/\s+/g, " ").slice(0, 72);
            const imageBase = `/api/v1/agent/tasks/${encodeURIComponent(taskId)}/approval/${encodeURIComponent(stepId)}/dynamics/${encodeURIComponent(String(item.post_id))}/images`;
            const hashes = item.image_hashes || [];
            return (
              <div key={item.post_id}>
                <label className="check-field">
                  <input
                    type="checkbox"
                    value={item.post_id}
                    checked={selected.has(item.post_id)}
                    onChange={(event) => togglePost(item.post_id, event.target.checked)}
                  />
                  <span>
                    草稿 #{item.post_id} · {caption}
                  </span>
                </label>
                <details>
                  <summary>{hashes.length ? "查看原文和配图" : "查看原文"}</summary>
                  <blockquote className="quoted-text">{item.text}</blockquote>
                  {item.payload?.mentions?.length ? (
                    <MentionLinks mentions={item.payload.mentions} />
                  ) : null}
                  {hashes.map((_, index) => (
                    <img
                      key={`${item.post_id}-img-${index}`}
                      className="dynamic-comment-card"
                      loading="lazy"
                      alt={`随草稿 #${item.post_id} 发布的配图 ${index + 1}`}
                      src={studioPath(`${imageBase}/${index}`)}
                    />
                  ))}
                </details>
              </div>
            );
          })}
          {(approval.excluded || []).map((item) => (
            <p key={`excl-${item.post_id}`} className="hint">
              草稿 #{item.post_id} 暂不可发布 · {item.reason}
            </p>
          ))}
        </fieldset>
        <div className="actions">
          {approval.posts.length > 0 && (
            <button
              type="submit"
              className="primary"
              disabled={busy || expired || selected.size === 0}
            >
              {selected.size > 0 ? `确认发布 ${selected.size} 条` : "确认发布"}
            </button>
          )}
          <button type="button" disabled={busy} onClick={() => void apply("cancel")}>
            取消
          </button>
        </div>
        <p className="hint" role="status">
          {statusNote}
        </p>
      </form>
    </section>
  );
}
