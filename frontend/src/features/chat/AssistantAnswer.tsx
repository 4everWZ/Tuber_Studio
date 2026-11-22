import { MarkdownText } from "@/shared/ui/MarkdownText";
import { isPlatformLink, safeHttpsUrl, type ChatSourceGroup, type ChatTurn } from "./types";

const SOURCE_LABELS: Record<string, string> = {
  comment: "原评论",
  subtitle: "字幕",
  visual: "画面观察",
  reply: "回复记录",
};

const REPLY_STATE_LABELS: Record<string, string> = {
  confirmed: "已送达的回复",
  uncertain: "送达待核对",
  owner_replied: "号主代回",
  previously_replied: "此前已回",
  same_account_replied: "同账号已回，归属未确认",
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

export interface AssistantAnswerProps {
  turn: Pick<ChatTurn, "reply_text" | "reply_format" | "reply_sections" | "sources">;
}

export function AssistantAnswer({ turn }: AssistantAnswerProps) {
  const sources = turn.sources || [];
  const sections = turn.reply_sections || [];

  return (
    <>
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
    </>
  );
}
