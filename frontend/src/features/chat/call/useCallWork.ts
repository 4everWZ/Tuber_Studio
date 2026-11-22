import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, apiPost } from "@/shared/api/client";
import { errorMessage } from "@/shared/lib/errors";
import type { CallOrigin, PrivateCallWorkTask, VoiceOrigin } from "../types";
import type { CallPlaybackController } from "./PrivatePlayback";

export const CALL_WORK_STATUS_LABELS: Record<string, string> = {
  queued: "已交办",
  running: "处理中",
  waiting: "等待工具结果",
  scheduled: "已预约",
  awaiting_approval: "等待确认",
  needs_input: "等待你的答复",
  cancelled: "已停止",
  completed: "交办完成",
  failed: "交办遇到问题",
  paused: "已暂停",
};

interface ReadbackState {
  task: PrivateCallWorkTask;
  request: AbortController;
  buffer?: AudioBuffer;
  node?: AudioBufferSourceNode;
  checking?: boolean;
}

export interface CallWorkSession {
  readonly context: CallOrigin;
  readonly audioContext: AudioContext;
  readonly playback: CallPlaybackController;
  readonly abort: AbortController;
  readonly speaking: boolean;
  readonly lastVoice: number;
  readonly pendingAudio: number;
  readonly transcriptionPending: boolean;
}

export function useCallWork() {
  const [tasks, setTasks] = useState<PrivateCallWorkTask[]>([]);
  const [taskNote, setTaskNote] = useState("");

  const sessionRef = useRef<CallWorkSession | null>(null);
  const readbackRef = useRef<ReadbackState | undefined>(undefined);
  const updatePromiseRef = useRef<Promise<void> | undefined>(undefined);
  const routingCountRef = useRef(0);
  const queueRef = useRef<Promise<void>>(Promise.resolve());
  const tasksCountRef = useRef(0);
  const signatureRef = useRef("");

  const notifyChatChanged = useCallback(() => {
    if (typeof document !== "undefined") {
      document.dispatchEvent(new CustomEvent("operator-chat-changed"));
    }
  }, []);

  const stopReading = useCallback(() => {
    const rb = readbackRef.current;
    if (!rb) return;
    rb.request.abort();
    if (rb.node) {
      try {
        rb.node.stop();
        rb.node.disconnect();
      } catch {
        // Ignore already-stopped AudioBufferSourceNode.
      }
    }
    readbackRef.current = undefined;
  }, []);

  const refresh = useCallback(async (): Promise<void> => {
    const session = sessionRef.current;
    if (!session || session.abort.signal.aborted) return;
    if (updatePromiseRef.current) return updatePromiseRef.current;

    const promise = (async () => {
      const { tasks: nextTasks } = await apiPost<
        "/api/v1/agent/private-call/work/status",
        { tasks: PrivateCallWorkTask[] },
        CallOrigin
      >("/api/v1/agent/private-call/work/status", {
        signal: session.abort.signal,
        body: session.context,
      });

      if (session.abort.signal.aborted || sessionRef.current !== session) return;

      const byId = new Map(nextTasks.map((t) => [t.id, t]));
      const rb = readbackRef.current;
      if (rb) {
        const revised = byId.get(rb.task.id);
        if (
          revised?.revision !== rb.task.revision ||
          revised.status !== "completed" ||
          revised.message !== rb.task.message
        ) {
          stopReading();
        }
      }

      const nextSig = JSON.stringify(
        nextTasks.map((t) => [t.id, t.updated, t.status, t.message])
      );
      tasksCountRef.current = nextTasks.length;
      if (nextSig !== signatureRef.current) {
        signatureRef.current = nextSig;
        setTasks(nextTasks);
        notifyChatChanged();
      }
    })();

    updatePromiseRef.current = promise;
    try {
      await promise;
    } catch (error) {
      stopReading();
      if (error instanceof ApiError && [401, 409].includes(error.status)) {
        tasksCountRef.current = 0;
        signatureRef.current = "";
        setTasks([]);
      }
      throw error;
    } finally {
      if (updatePromiseRef.current === promise) {
        updatePromiseRef.current = undefined;
      }
    }
  }, [notifyChatChanged, stopReading]);

  const readTask = useCallback(
    async (task: PrivateCallWorkTask) => {
      const session = sessionRef.current;
      if (!session || session.abort.signal.aborted) return;
      stopReading();
      const selected: ReadbackState = { task, request: new AbortController() };
      readbackRef.current = selected;
      try {
        const bufferData = await apiPost<
          "/api/v1/agent/private-call/work/{task_id}/read",
          ArrayBuffer,
          { call: CallOrigin; revision: number }
        >("/api/v1/agent/private-call/work/{task_id}/read", {
          pathParams: { task_id: task.id },
          signal: selected.request.signal,
          body: { call: session.context, revision: task.revision },
          responseType: "arrayBuffer",
        });
        if (readbackRef.current !== selected || session.abort.signal.aborted) return;
        selected.buffer = await session.audioContext.decodeAudioData(bufferData);
      } catch (error) {
        if (readbackRef.current !== selected || session.abort.signal.aborted) return;
        stopReading();
        setTaskNote(errorMessage(error));
      }
    },
    [stopReading]
  );

  const isIdle = useCallback((session: CallWorkSession) => {
    return (
      session.audioContext.state === "running" &&
      !session.playback.muted &&
      !session.speaking &&
      !session.pendingAudio &&
      !session.playback.queue.length &&
      performance.now() - session.lastVoice > 800 &&
      !session.transcriptionPending &&
      routingCountRef.current === 0
    );
  }, []);

  const playWhenIdle = useCallback(async () => {
    const session = sessionRef.current;
    const selected = readbackRef.current;
    if (!session || !selected?.buffer || selected.node || selected.checking || !isIdle(session)) {
      return;
    }
    selected.checking = true;
    try {
      if (updatePromiseRef.current) await updatePromiseRef.current;
      await refresh();
      if (
        readbackRef.current !== selected ||
        !isIdle(session) ||
        session.abort.signal.aborted
      ) {
        return;
      }
      const node = session.audioContext.createBufferSource();
      selected.node = node;
      node.buffer = selected.buffer;
      node.connect(session.audioContext.destination);
      node.onended = () => {
        node.disconnect();
        if (readbackRef.current === selected) {
          readbackRef.current = undefined;
        }
      };
      node.start();
    } catch {
      stopReading();
    } finally {
      selected.checking = false;
    }
  }, [isIdle, refresh, stopReading]);

  const handoff = useCallback(
    async (origin: VoiceOrigin, confirmation?: string) => {
      const session = sessionRef.current;
      if (!session) {
        throw new Error("当前通话未连接");
      }
      const receipt = await apiPost<
        "/api/v1/agent/private-call/utterances",
        { turn_id?: string | null; task_id?: string | null; message?: string },
        { origin: VoiceOrigin; confirmation?: string }
      >("/api/v1/agent/private-call/utterances", {
        signal: session.abort.signal,
        body: {
          origin,
          ...(confirmation === undefined ? {} : { confirmation }),
        },
      });
      if (receipt.turn_id) {
        notifyChatChanged();
        await refresh().catch(() => {});
        if (!receipt.task_id && receipt.message) {
          setTaskNote(receipt.message);
        }
      }
      return receipt;
    },
    [notifyChatChanged, refresh]
  );

  const acceptOrigin = useCallback(
    (origin: VoiceOrigin) => {
      const session = sessionRef.current;
      if (!session) return;
      routingCountRef.current += 1;
      queueRef.current = queueRef.current
        .then(async () => {
          if (!session.abort.signal.aborted) {
            await handoff(origin);
          }
        })
        .catch((error) => {
          if (!session.abort.signal.aborted) {
            setTaskNote(errorMessage(error));
          }
        })
        .finally(() => {
          routingCountRef.current -= 1;
        });
    },
    [handoff]
  );

  const attach = useCallback(
    (session: CallWorkSession) => {
      stopReading();
      sessionRef.current = session;
      tasksCountRef.current = 0;
      signatureRef.current = "";
      setTasks([]);
      setTaskNote("");

      const poll = setInterval(() => {
        if (tasksCountRef.current > 0) {
          void refresh().catch(() => {});
        }
      }, 1800);
      const breaks = setInterval(() => {
        void playWhenIdle();
      }, 250);

      session.abort.signal.addEventListener(
        "abort",
        () => {
          clearInterval(poll);
          clearInterval(breaks);
          stopReading();
          if (sessionRef.current === session) {
            sessionRef.current = null;
          }
          tasksCountRef.current = 0;
          signatureRef.current = "";
          setTasks([]);
          setTaskNote("");
        },
        { once: true }
      );
    },
    [playWhenIdle, refresh, stopReading]
  );

  const interrupt = useCallback(() => {
    if (readbackRef.current?.node) {
      stopReading();
    }
  }, [stopReading]);

  useEffect(() => {
    return () => {
      stopReading();
    };
  }, [stopReading]);

  return {
    tasks,
    taskNote,
    setTaskNote,
    attach,
    refresh: () => void refresh().catch(() => {}),
    readTask,
    handoff,
    acceptOrigin,
    interrupt,
    stopReading,
  };
}
