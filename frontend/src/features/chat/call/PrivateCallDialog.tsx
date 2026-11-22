import { useEffect, useRef, type RefObject } from "react";
import { studioPath } from "@/shared/api/client";
import { AssistantAnswer } from "../AssistantAnswer";
import styles from "../ChatPage.module.css";
import type { ChatTurn, PrivateCallAvailability, PrivateCallWorkTask, VoiceOrigin } from "../types";
import type { PortraitUiState } from "./PortraitPlayback";
import {
  TranscriptReviewPanel,
  type TranscriptRecord,
  type TranscriptReviewConfig,
} from "./TranscriptReviewPanel";
import { CALL_WORK_STATUS_LABELS } from "./useCallWork";

export interface PrivateCallDialogProps {
  open: boolean;
  availability: PrivateCallAvailability | null;
  temporary: boolean;
  onTemporaryChange: (checked: boolean) => void;
  topic: string;
  onTopicChange: (topic: string) => void;
  optionsOpen: boolean;
  onOptionsToggle: (open: boolean) => void;
  inCall: boolean;
  callReady: boolean;
  callEphemeral: boolean;
  statusText: string;
  muted: boolean;
  showMute: boolean;
  avatarProfilesOffered: boolean;
  selectedProfile: string;
  onSelectedProfileChange: (profile: string) => void;
  portraitActive: boolean;
  portraitUi: PortraitUiState;
  videoElRef: RefObject<HTMLVideoElement | null>;
  transcriptRecords: TranscriptRecord[];
  reviewConfig: TranscriptReviewConfig | null;
  confirmHandoff?: (origin: VoiceOrigin, goal: string) => Promise<{ message?: string }>;
  workInput: string;
  onWorkInputChange: (val: string) => void;
  workSending: boolean;
  workAnswer: ChatTurn | null;
  workError: string;
  onSubmitWork: () => void;
  workTasks: PrivateCallWorkTask[];
  taskNote: string;
  onReadTask: (task: PrivateCallWorkTask) => void;
  onStartCall: () => void;
  onToggleMute: () => void;
  onSmoothingChange: (checked: boolean) => void;
  onInterruptAnswer: () => void;
  onEndOrClose: () => void;
  onNativeClose: () => void;
  onPetSlotChange: (el: HTMLDivElement | null) => void;
}

export function PrivateCallDialog({
  open,
  availability,
  temporary,
  onTemporaryChange,
  topic,
  onTopicChange,
  optionsOpen,
  onOptionsToggle,
  inCall,
  callReady,
  callEphemeral,
  statusText,
  muted,
  showMute,
  avatarProfilesOffered,
  selectedProfile,
  onSelectedProfileChange,
  portraitActive,
  portraitUi,
  videoElRef,
  transcriptRecords,
  reviewConfig,
  confirmHandoff,
  workInput,
  onWorkInputChange,
  workSending,
  workAnswer,
  workError,
  onSubmitWork,
  workTasks,
  taskNote,
  onReadTask,
  onStartCall,
  onToggleMute,
  onSmoothingChange,
  onInterruptAnswer,
  onEndOrClose,
  onNativeClose,
  onPetSlotChange,
}: PrivateCallDialogProps) {
  const dialogRef = useRef<HTMLDialogElement | null>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open) {
      if (!dialog.open) {
        if (typeof dialog.showModal === "function") {
          dialog.showModal();
        } else {
          dialog.setAttribute("open", "");
        }
      }
    } else if (dialog.open) {
      if (typeof dialog.close === "function") {
        dialog.close();
      } else {
        dialog.removeAttribute("open");
      }
    }
  }, [open]);

  const recordings = availability?.recordings;
  const recordingLinks = [
    ["mature", "常规版通话实录"],
    ["q", "Q 版通话实录"],
  ] as const;
  const hasRecordings = Boolean(recordings?.mature || recordings?.q);

  return (
    <dialog
      ref={dialogRef}
      id="private-call-dialog"
      className={styles.callDialog}
      aria-labelledby="private-call-title"
      onCancel={(event) => {
        event.preventDefault();
        onEndOrClose();
      }}
      onClose={onNativeClose}
    >
      <h2 id="private-call-title">
        {availability?.character ? `与 ${availability.character} 通话` : "私有语音通话"}
      </h2>

      <div id="private-call-recordings" className="actions" hidden={!hasRecordings}>
        {recordingLinks.map(([profile, label]) => {
          const url = recordings?.[profile];
          if (!url) return null;
          return (
            <a key={profile} href={studioPath(url)} target="_blank" rel="noopener">
              {label}
            </a>
          );
        })}
      </div>

      {avatarProfilesOffered && (
        <label>
          通话形象{" "}
          <select
            disabled={inCall}
            value={selectedProfile}
            onChange={(event) => onSelectedProfileChange(event.target.value)}
          >
            <option value="">仅语音</option>
            <option value="original">常规版</option>
            <option value="q">Q 版</option>
          </select>
        </label>
      )}

      {portraitActive && (
        <>
          <video
            ref={videoElRef}
            playsInline
            autoPlay
            hidden={!portraitUi.videoVisible}
            width={240}
            aria-label="实时通话形象"
          />
          <div>{portraitUi.caption}</div>
          {portraitUi.smoothingSupported && (
            <label
              className="check-field"
              title="音视频同时增加接收缓冲，播放可能稍晚"
              hidden={!portraitUi.smoothingVisible}
            >
              <input
                type="checkbox"
                checked={portraitUi.smoothingChecked}
                onChange={(event) => onSmoothingChange(event.target.checked)}
              />
              <span>减少画面卡顿</span>
            </label>
          )}
          <button
            type="button"
            hidden={!portraitUi.interruptVisible}
            onClick={onInterruptAnswer}
          >
            打断回答
          </button>
        </>
      )}

      <label className="check-field">
        <input
          type="checkbox"
          id="private-call-temporary"
          checked={temporary}
          disabled={inCall}
          onChange={(event) => onTemporaryChange(event.target.checked)}
        />
        <span>临时通话</span>
      </label>

      <details
        id="private-call-options"
        open={optionsOpen}
        hidden={temporary}
        onToggle={(event) => onOptionsToggle((event.currentTarget as HTMLDetailsElement).open)}
      >
        <summary>指定通话话题</summary>
        <label>
          话题（可选）
          <input
            id="private-call-topic"
            maxLength={500}
            disabled={inCall}
            value={topic}
            onChange={(event) => onTopicChange(event.target.value)}
          />
        </label>
      </details>

      <div ref={onPetSlotChange} id="private-call-pet-slot" />

      <p id="private-call-summary">
        {temporary
          ? "临时通话在挂断后清空。建议佩戴耳机。"
          : "明确交办的原话和结果会回到对话。建议佩戴耳机。"}
      </p>

      <p id="private-call-state" role="status">
        {statusText}
      </p>

      <form
        id="private-call-work"
        hidden={!callReady || callEphemeral}
        onSubmit={(event) => {
          event.preventDefault();
          onSubmitWork();
        }}
      >
        <label>
          通话中的文字交办
          <textarea
            id="private-call-work-input"
            rows={2}
            required
            value={workInput}
            onChange={(event) => onWorkInputChange(event.target.value)}
          />
        </label>
        <button type="submit" disabled={workSending}>
          发送到 Chats
        </button>
        <div id="private-call-work-result" role="status">
          {workAnswer && <AssistantAnswer turn={workAnswer} />}
          {workError}
        </div>
      </form>

      <p id="private-call-task-note" role="status">
        {taskNote}
      </p>

      <div id="private-call-task-results" hidden={workTasks.length === 0}>
        {workTasks.map((task) => {
          const headline = `${CALL_WORK_STATUS_LABELS[task.status] ?? "交办"} · ${task.goal}`;
          return (
            <section key={task.id}>
              <p role="status">{headline}</p>
              {task.message && (
                <div>
                  <AssistantAnswer turn={{ reply_format: "markdown", reply_text: task.message }} />
                </div>
              )}
              <button
                type="button"
                hidden={task.status !== "completed" || !task.message}
                onClick={() => onReadTask(task)}
              >
                朗读结果
              </button>
            </section>
          );
        })}
      </div>

      <TranscriptReviewPanel
        records={transcriptRecords}
        config={reviewConfig}
        confirmHandoff={confirmHandoff}
      />

      <div className="actions">
        <button
          id="private-call-start"
          type="button"
          disabled={inCall}
          onClick={onStartCall}
        >
          开始通话
        </button>
        <button
          id="private-call-mute"
          type="button"
          hidden={!showMute}
          aria-pressed={muted}
          onClick={onToggleMute}
        >
          {muted ? "恢复播放" : "播放静音"}
        </button>
        <button id="private-call-end" type="button" onClick={onEndOrClose}>
          {inCall ? "挂断" : "关闭"}
        </button>
      </div>
    </dialog>
  );
}
