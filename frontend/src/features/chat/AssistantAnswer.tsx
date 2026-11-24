import { MarkdownText } from "@/shared/ui/MarkdownText";
import {
  calmChatMessage,
  dispatchOpenManageMemories,
  isPlatformLink,
  safeHttpsUrl,
  type ChatSourceGroup,
  type ChatTurn,
  type MemoryUpdatedItem,
  type ToolActivityItem,
} from "./types";

const SOURCE_LABELS: Record<string, string> = {
  comment: "原评论",
  subtitle: "字幕",
  visual: "画面观察",
  reply: "回复记录",
};

const REPLY_STATE_LABELS: Record<string, string> = {
  confirmed: "已送达的回复",
  uncertain: "送达待核对",
  owner_replied: "本人代回",
  previously_replied: "此前已回",
  same_account_replied: "同账号已回，归属未确认",
};

const TOOL_STATUS_LABELS: Record<string, { text: string; tone: string }> = {
  running: { text: "进行中", tone: "running" },
  completed: { text: "已完成", tone: "completed" },
  constrained: { text: "已暂缓", tone: "constrained" },
};

const MEMORY_ACTION_LABELS: Record<string, string> = {
  created: "已记住",
  updated: "已更新",
  deleted: "已遗忘",
  deduplicated: "已保留",
};

function formatSourceTime(part: number | null | undefined, start: number | null | undefined): string {
  if (start == null) return "";
  const mins = Math.floor(start / 60);
  const secs = (start % 60).toFixed(3).padStart(6, "0");
  return `P${part ?? 1} · ${mins}:${secs}`;
}

export function SourcePreview({ group }: { group: ChatSourceGroup }) {
  return (
    <div className="chat-source-preview">
      <strong>{group.title}</strong>
      {group.entries.map((entry, idx) => {
        const typeLabel =
          (entry.state && REPLY_STATE_LABELS[entry.state]) ||
          (entry.kind && SOURCE_LABELS[entry.kind]) ||
          "";
        const timeText = formatSourceTime(entry.part, entry.start);
        const header = [
          typeLabel,
          entry.author || "",
          entry.uid ? `UID ${entry.uid}` : "",
          timeText,
        ]
          .filter(Boolean)
          .join(" · ");
        const safeLink = isPlatformLink(entry.href)
          ? entry.href
          : entry.kind === "web"
            ? safeHttpsUrl(entry.href)
            : null;
        return (
          <section key={`${entry.kind || "src"}-${idx}`}>
            {header && <small>{header}</small>}
            {entry.text && <p>{entry.text}</p>}
            {entry.original_text && <small>B 站原字幕「{entry.original_text}」，已校正</small>}
            {entry.kind === "web" && entry.published_at && <small>{entry.published_at}</small>}
            {safeLink && (
              <a href={safeLink} target="_blank" rel="noopener noreferrer">
                打开原始来源
              </a>
            )}
          </section>
        );
      })}
    </div>
  );
}

export function SourceChip({ group, index }: { group: ChatSourceGroup; index: number }) {
  const firstKind = group.entries[0]?.kind;
  const title = firstKind === "comment" ? "原评论" : (firstKind && SOURCE_LABELS[firstKind]) || "来源";
  return (
    <details className="chat-source-chip">
      <summary aria-label={`来源 ${index + 1} · ${group.title}`}>{title}</summary>
      <SourcePreview group={group} />
    </details>
  );
}

export function ToolActivityDisclosure({
  activities,
  defaultOpen = false,
}: {
  activities: ToolActivityItem[];
  defaultOpen?: boolean;
}) {
  if (!activities.length) return null;

  const runningItem = activities.find((item) => item.status === "running");
  const summaryCaption = runningItem
    ? `思考与工具 · 正在${runningItem.label}…`
    : activities.length === 1
      ? `思考与工具 · ${activities[0].label}`
      : `思考与工具 · ${activities[0].label} 等 ${activities.length} 项`;

  return (
    <details className="chat-tool-disclosure" open={defaultOpen || undefined}>
      <summary>
        <span
          className={`chat-tool-summary-dot${runningItem ? " is-running" : ""}`}
          aria-hidden="true"
        />
        <span>{summaryCaption}</span>
      </summary>
      <ul className="chat-tool-list">
        {activities.map((item) => {
          const meta = TOOL_STATUS_LABELS[item.status] || { text: "已完成", tone: "completed" };
          const text = item.summary || item.detail;
          return (
            <li key={item.id} className="chat-tool-capsule" data-status={meta.tone}>
              <div className="chat-tool-capsule-head">
                <span className="chat-tool-capsule-label">{item.label}</span>
                <span className="chat-tool-capsule-status">{meta.text}</span>
              </div>
              {text && <p className="chat-tool-capsule-summary">{calmChatMessage(text)}</p>}
            </li>
          );
        })}
      </ul>
    </details>
  );
}

export function MemoryUpdatedPill({ updates }: { updates: MemoryUpdatedItem[] }) {
  if (!updates.length) return null;
  const primary = updates[updates.length - 1];
  const primaryAction = MEMORY_ACTION_LABELS[primary.action] || "已更新";
  const tooltip = `${primaryAction}：${primary.snippet}`;
  const nearLimit = updates.find((item) => item.soft_limit_reached);

  return (
    <div className="chat-memory-bar">
      <button
        type="button"
        className="chat-memory-pill"
        title={tooltip}
        onClick={() =>
          dispatchOpenManageMemories({
            memoryId: primary.memory_id ?? null,
            snippet: primary.snippet,
            scope: primary.scope || "owner",
          })
        }
      >
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path
            d="M7 4.5h10A1.5 1.5 0 0 1 18.5 6v14l-6.5-3.8L5.5 20V6A1.5 1.5 0 0 1 7 4.5Z"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinejoin="round"
          />
        </svg>
        <span>{updates.length > 1 ? `已更新记忆 · ${updates.length}` : "已更新记忆"}</span>
      </button>

      <details className="chat-memory-details">
        <summary aria-label="查看记忆变更详情">详情</summary>
        <div className="chat-memory-popover">
          {updates.map((item, idx) => {
            const actionText = MEMORY_ACTION_LABELS[item.action] || "已更新";
            return (
              <div key={`${item.memory_id ?? item.snippet}-${idx}`} className="chat-memory-entry">
                <span className="chat-memory-action">{actionText}</span>
                <span className="chat-memory-snippet">{item.snippet}</span>
                <button
                  type="button"
                  className="quiet chat-memory-jump"
                  onClick={() =>
                    dispatchOpenManageMemories({
                      memoryId: item.memory_id ?? null,
                      snippet: item.snippet,
                      scope: item.scope || "owner",
                    })
                  }
                >
                  管理这条记忆
                </button>
              </div>
            );
          })}
          {nearLimit && (
            <p className="hint chat-memory-limit-note">
              已保存记忆接近容量上限（{nearLimit.count ?? 0} / {nearLimit.limit ?? 100}），可在记忆弹窗中整理。
            </p>
          )}
          <a className="chat-memory-fallback-link" href="#personalization">
            前往个性化管理
          </a>
        </div>
      </details>
    </div>
  );
}

export interface AssistantAnswerProps {
  turn: Pick<
    ChatTurn,
    "reply_text" | "reply_format" | "reply_sections" | "sources" | "tool_activities" | "memory_updates"
  >;
}

export function AssistantAnswer({ turn }: AssistantAnswerProps) {
  const sources = turn.sources || [];
  const sections = turn.reply_sections || [];
  const activities = turn.tool_activities || [];
  const memoryUpdates = turn.memory_updates || [];

  return (
    <>
      {activities.length > 0 && <ToolActivityDisclosure activities={activities} />}
      {sections.length > 0 ? (
        <ol className="chat-stories">
          {sections.map((text, index) => (
            <li key={`${index}-${text.slice(0, 16)}`}>
              <p>{text}</p>
              {sources[index] && <SourceChip group={sources[index]} index={index} />}
            </li>
          ))}
        </ol>
      ) : turn.reply_format === "markdown" ? (
        <MarkdownText text={turn.reply_text || ""} />
      ) : (
        <p>{turn.reply_text || ""}</p>
      )}
      {!sections.length && sources.length === 1 && <SourceChip group={sources[0]} index={0} />}
      {sources.length > 1 && (
        <details className="chat-sources">
          <summary>来源 · {sources.length}</summary>
          {sources.map((group, idx) => (
            <SourcePreview key={`${group.title}-${idx}`} group={group} />
          ))}
        </details>
      )}
      {memoryUpdates.length > 0 && <MemoryUpdatedPill updates={memoryUpdates} />}
    </>
  );
}
