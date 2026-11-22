import { useEffect, useRef, useState } from "react";
import { apiPost } from "@/shared/api/client";
import { RememberButton, type MemoryProposalPayload } from "@/shared/ui/RememberButton";
import styles from "../ChatPage.module.css";
import { createRequestId, type AgentTask, type MemoryCitationItem, type VoiceOrigin } from "../types";

export interface TranscriptRecord {
  id: number;
  text: string;
  origin: VoiceOrigin | null;
}

export interface TranscriptReviewConfig {
  memory_citations?: MemoryCitationItem[];
  correction_targets?: Array<number | string>;
  memory_use?: Record<string, unknown> | null;
}

export interface TranscriptReviewPanelProps {
  records: TranscriptRecord[];
  config: TranscriptReviewConfig | null;
  confirmHandoff?: (origin: VoiceOrigin, goal: string) => Promise<{ message?: string }>;
}

function UtteranceHandoffRow({
  record,
  config,
  confirmHandoff,
}: {
  record: TranscriptRecord;
  config: TranscriptReviewConfig | null;
  confirmHandoff?: (origin: VoiceOrigin, goal: string) => Promise<{ message?: string }>;
}) {
  const { text, origin } = record;
  const [editedText, setEditedText] = useState(text);
  const [selectedTargetId, setSelectedTargetId] = useState("");
  const [feedback, setFeedback] = useState("");
  const [handoffDone, setHandoffDone] = useState(false);
  const [handoffPending, setHandoffPending] = useState(false);

  const requestIdRef = useRef<string>(createRequestId());

  const targets = (config?.memory_citations || []).filter((item) =>
    config?.correction_targets?.includes(item.id)
  );
  const effectiveTargetId =
    targets.length === 1 ? String(targets[0].id) : selectedTargetId;

  useEffect(() => {
    setEditedText(text);
    setFeedback("");
    setHandoffDone(false);
    setHandoffPending(false);
    setSelectedTargetId("");
    requestIdRef.current = createRequestId();
  }, [record.id, text]);

  const wording = () => (origin ? editedText.trim() : text);

  const memoryBody = (): MemoryProposalPayload => {
    const value = wording();
    if (!value || [...value].length > 150) {
      throw new Error("记忆原话须在 1 至 150 字之间");
    }
    return {
      source: "confirmed_voice",
      text: value,
      ...(origin ? { voice_origin: origin } : {}),
    };
  };

  async function handleHandoff() {
    if (handoffPending || handoffDone) return;
    setHandoffPending(true);
    try {
      const goal = wording();
      if (!goal) {
        setFeedback("请填写要交办的原话。");
        setHandoffPending(false);
        return;
      }
      if (origin && confirmHandoff) {
        const receipt = await confirmHandoff(origin, goal);
        setFeedback(receipt.message || "已交办，可在活动中查看。");
      } else {
        await apiPost<"/api/v1/agent/tasks", AgentTask, { request_id: string; goal: string }>(
          "/api/v1/agent/tasks",
          { body: { request_id: requestIdRef.current, goal } }
        );
        setFeedback("已交办，可在活动中查看。");
      }
      setHandoffDone(true);
    } catch {
      setFeedback("交办未确认，可以重试。");
      setHandoffPending(false);
    }
  }

  const overLimit = [...text].length > 2000;
  const allowRemember = Boolean(origin || [...text].length <= 150);

  return (
    <div>
      {origin && "所选录音的转写（机器原稿）"}
      <blockquote>{text}</blockquote>
      {origin && (
        <label>
          号主确认的文字（可改正误听）
          <textarea
            maxLength={2000}
            value={editedText}
            onChange={(event) => setEditedText(event.target.value)}
          />
        </label>
      )}
      {overLimit ? null : (
        <button
          type="button"
          disabled={handoffPending || handoffDone}
          onClick={() => void handleHandoff()}
        >
          将这句话交办
        </button>
      )}
      <span role="status">
        {overLimit ? "超过任务字数上限，请在文字对话中整理后交办。" : feedback}
      </span>
      {allowRemember && <RememberButton label="确认并记住原话" propose={memoryBody} />}
      {allowRemember && config?.memory_use && targets.length > 0 && (
        <>
          <label>
            更正本次引用的偏好{" "}
            <select
              value={effectiveTargetId}
              onChange={(event) => setSelectedTargetId(event.target.value)}
            >
              {targets.length > 1 && <option value="">选择要更正的偏好</option>}
              {targets.map((item) => (
                <option key={String(item.id)} value={String(item.id)}>
                  {item.note}
                </option>
              ))}
            </select>
          </label>
          <RememberButton
            label="确认用这句话更正"
            propose={() => {
              const target = targets.find((t) => String(t.id) === effectiveTargetId);
              if (!target) throw new Error("请选择要更正的原偏好");
              return {
                ...memoryBody(),
                correction: {
                  id: target.id,
                  revision: target.revision,
                  use: config.memory_use,
                },
              };
            }}
          />
        </>
      )}
    </div>
  );
}

export function TranscriptReviewPanel({
  records,
  config,
  confirmHandoff,
}: TranscriptReviewPanelProps) {
  const [selectedValue, setSelectedValue] = useState("");
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (records.length === 0) {
      setSelectedValue("");
      setOpen(false);
    }
  }, [records.length]);

  const selectedIndex = Number(selectedValue) - 1;
  const activeRecord =
    selectedValue && Number.isInteger(selectedIndex) && selectedIndex >= 0
      ? records[selectedIndex] || null
      : null;

  return (
    <details
      id="private-call-review"
      hidden={records.length === 0}
      open={open}
      onToggle={(event) => setOpen((event.currentTarget as HTMLDetailsElement).open)}
    >
      <summary>查看通话转写</summary>
      <label>
        选择要处理的转写
        <select
          id="private-call-transcript"
          value={selectedValue}
          onChange={(event) => setSelectedValue(event.target.value)}
        >
          <option value="">请选择一句</option>
          {records.map((record, idx) => (
            <option key={record.id} value={String(idx + 1)}>
              {(record.origin ? "所选录音 · " : "") + record.text}
            </option>
          ))}
        </select>
      </label>
      <div id="private-call-utterances" className={styles.callUtterances}>
        {activeRecord && (
          <UtteranceHandoffRow
            record={activeRecord}
            config={config}
            confirmHandoff={confirmHandoff}
          />
        )}
      </div>
    </details>
  );
}
