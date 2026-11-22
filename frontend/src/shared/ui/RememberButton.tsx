import { useState } from "react";
import { apiPost } from "../api/client";
import { errorMessage } from "../lib/errors";

export interface MemoryProposalPayload {
  source: string;
  text: string;
  [key: string]: unknown;
}

interface MemoryProposalResponse {
  text: string;
  stamp: string;
  machine_text?: string;
  replaces?: {
    note?: string;
  };
}

interface MemoryGetResponse {
  source_key?: string;
  quote?: string;
  voice_origin?: {
    machine_text?: string;
  } | null;
}

export interface RememberButtonProps {
  label: string;
  propose: () => MemoryProposalPayload;
  disabled?: boolean;
  onConfirmed?: () => void;
}

export function RememberButton({ label, propose, disabled = false, onConfirmed }: RememberButtonProps) {
  const [pending, setPending] = useState(false);
  const [feedback, setFeedback] = useState("");

  async function handleClick() {
    if (pending || disabled) return;
    setPending(true);
    try {
      const body = propose();
      const preview = await apiPost<"/api/v1/agent/memory/propose", MemoryProposalResponse, MemoryProposalPayload>(
        "/api/v1/agent/memory/propose",
        { body }
      );
      const question = preview.replaces
        ? `更正这条偏好？\n\n原内容：${preview.replaces.note ?? ""}\n新原话：${preview.text}`
        : `保存为号主私有记忆？\n\n${preview.text}`;
      const evidence = preview.machine_text
        ? `\n\n机器转写：${preview.machine_text}\n号主确认：${preview.text}`
        : "";

      if (typeof window !== "undefined" && !window.confirm(question + evidence)) {
        return;
      }

      await apiPost<"/api/v1/agent/memory/confirm", unknown, MemoryProposalPayload & { stamp: string }>(
        "/api/v1/agent/memory/confirm",
        { body: { ...body, stamp: preview.stamp } }
      );
      setFeedback("已保存");
      if (typeof document !== "undefined") {
        document.dispatchEvent(new Event("owner-memory-changed"));
      }
      onConfirmed?.();
    } catch (error) {
      setFeedback(errorMessage(error));
    } finally {
      setPending(false);
    }
  }

  return (
    <span>
      <button type="button" disabled={disabled || pending} onClick={() => void handleClick()}>
        {label}
      </button>
      <span role="status">{feedback}</span>
    </span>
  );
}

export function VoiceSourceButton({ id, sourceKey }: { id: string | number; sourceKey: string }) {
  const [resultText, setResultText] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function inspect() {
    if (pending) return;
    setPending(true);
    try {
      const row = await apiPost<"/api/v1/agent/memory/get", MemoryGetResponse, { id: string | number; source_key: string }>(
        "/api/v1/agent/memory/get",
        { body: { id, source_key: sourceKey } }
      );
      const origin = row.source_key === sourceKey && row.voice_origin;
      setResultText(
        origin
          ? `机器转写：${origin.machine_text ?? ""}\n号主确认：${row.quote ?? ""}`
          : "这条来源没有录音转写回执。"
      );
    } catch {
      setResultText("当前无法读取这条来源。");
    } finally {
      setPending(false);
    }
  }

  if (resultText !== null) {
    return <p>{resultText}</p>;
  }

  return (
    <button type="button" disabled={pending} onClick={() => void inspect()}>
      核对语音来源
    </button>
  );
}
