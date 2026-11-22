import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { apiGet, apiPatch, apiPost, apiPut } from "@/shared/api/client";
import { errorMessage } from "@/shared/lib/errors";
import styles from "./ChatPage.module.css";
import { createRequestId, type AgentTask, type BrowserSessionState } from "./types";

function parseSources(text: string): string[] {
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
}

export interface BrowserTaskCreateFormProps {
  onCreated: (taskId: string) => void;
  onCancel: () => void;
}

export function BrowserTaskCreateForm({ onCreated, onCancel }: BrowserTaskCreateFormProps) {
  const [goal, setGoal] = useState("");
  const [sourcesText, setSourcesText] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const requestIdRef = useRef<string>(createRequestId());
  const goalInputRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    goalInputRef.current?.focus();
  }, []);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setNote("");
    try {
      const task = await apiPost<
        "/api/v1/agent/tasks",
        AgentTask,
        { request_id: string; goal: string; browser_sources: string[] }
      >("/api/v1/agent/tasks", {
        body: {
          request_id: requestIdRef.current,
          goal,
          browser_sources: parseSources(sourcesText),
        },
      });
      onCreated(task.id);
    } catch (error) {
      setNote(errorMessage(error));
      setBusy(false);
    }
  }

  return (
    <form className="chat-task-steer" onSubmit={(event) => void handleSubmit(event)}>
      <label>
        希望阅读这些来源做什么？
        <textarea
          ref={goalInputRef}
          required
          maxLength={2000}
          value={goal}
          onChange={(event) => setGoal(event.target.value)}
        />
      </label>
      <label>
        允许读取的公开来源
        <textarea
          required
          rows={3}
          placeholder="每行一个完整的 HTTPS 文档地址"
          value={sourcesText}
          onChange={(event) => setSourcesText(event.target.value)}
        />
      </label>
      <button type="submit" disabled={busy}>
        交办阅读
      </button>
      <button type="button" onClick={onCancel}>
        返回活动
      </button>
      <p className="hint">{note}</p>
    </form>
  );
}

export interface TaskBrowserPanelProps {
  task: AgentTask;
  isOpen: boolean;
  onOpen: () => void;
  onClose: () => void;
}

export function TaskBrowserPanel({ task, isOpen, onOpen, onClose }: TaskBrowserPanelProps) {
  const granted = task.steps.some((step) => step.tool === "owner_browser_grant");
  const isTerminal = ["completed", "failed", "cancelled"].includes(task.status);
  const canMount = task.can_control !== false && (granted || !isTerminal);

  const [state, setState] = useState<BrowserSessionState | null>(null);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [grantSources, setGrantSources] = useState("");
  const [findText, setFindText] = useState("");
  const [selectedSource, setSelectedSource] = useState("1");

  const busyRef = useRef(false);
  const stateRef = useRef<BrowserSessionState | null>(null);
  stateRef.current = state;

  const refreshBrowser = useCallback(async () => {
    if (busyRef.current) return;
    const before = stateRef.current;
    try {
      const next = await apiGet<"/api/v1/agent/tasks/{identity}/browser", BrowserSessionState>(
        "/api/v1/agent/tasks/{identity}/browser",
        { pathParams: { identity: task.id } }
      );
      if (busyRef.current || before !== stateRef.current) return;
      if (
        !stateRef.current ||
        stateRef.current.version !== next.version ||
        stateRef.current.mode !== next.mode ||
        stateRef.current.view?.image !== next.view?.image
      ) {
        setState(next);
        setSelectedSource(String(next.position || 1));
      }
      setNotice("");
    } catch (error) {
      setState(null);
      setNotice(errorMessage(error));
    }
  }, [task.id]);

  useEffect(() => {
    if (!isOpen) {
      setState(null);
      setNotice("");
      return;
    }
    void refreshBrowser();
    const timer = setInterval(() => {
      void refreshBrowser();
    }, 1300);
    return () => clearInterval(timer);
  }, [isOpen, refreshBrowser]);

  if (!canMount) {
    return null;
  }

  if (!isOpen) {
    return (
      <button type="button" onClick={onOpen}>
        {granted ? "查看阅读浏览器" : "授权阅读浏览器"}
      </button>
    );
  }

  async function runUpdate(fn: () => Promise<BrowserSessionState>) {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    try {
      const next = await fn();
      setState(next);
      setSelectedSource(String(next.position || 1));
      setNotice("");
    } catch (error) {
      setState(null);
      setNotice(errorMessage(error));
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }

  const version = state?.version || 0;
  const isOwner = state?.mode === "owner";

  return (
    <section className="chat-task-steer">
      <h3>阅读浏览器</h3>
      <div>
        {state && (
          <>
            {state.mode === "ended" && <p>浏览会话已结束。</p>}
            {(state.mode === "ungranted" || state.mode === "ended") && !isTerminal && (
              <>
                <label>
                  允许读取的公开来源
                  <textarea
                    required
                    rows={3}
                    placeholder="每行一个完整的 HTTPS 文档地址"
                    value={grantSources}
                    onChange={(event) => setGrantSources(event.target.value)}
                  />
                </label>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    void runUpdate(() =>
                      apiPut<
                        "/api/v1/agent/tasks/{identity}/browser",
                        BrowserSessionState,
                        { revision: number; version: number; sources: string[] }
                      >("/api/v1/agent/tasks/{identity}/browser", {
                        pathParams: { identity: task.id },
                        body: {
                          revision: state.revision,
                          version: state.version || 0,
                          sources: parseSources(grantSources),
                        },
                      })
                    )
                  }
                >
                  授权本次阅读
                </button>
              </>
            )}
            {(state.mode === "owner" || state.mode === "agent") && (
              <>
                <p>{isOwner ? "由你控制" : "由角色控制"}</p>
                <p className="hint">{state.sources[state.position - 1]}</p>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    void runUpdate(() =>
                      apiPatch<
                        "/api/v1/agent/tasks/{identity}/browser",
                        BrowserSessionState,
                        { version: number; action: "handback" | "takeover" }
                      >("/api/v1/agent/tasks/{identity}/browser", {
                        pathParams: { identity: task.id },
                        body: { version, action: isOwner ? "handback" : "takeover" },
                      })
                    )
                  }
                >
                  {isOwner ? "交回角色" : "接管浏览器"}
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    void runUpdate(() =>
                      apiPatch<
                        "/api/v1/agent/tasks/{identity}/browser",
                        BrowserSessionState,
                        { version: number; action: "end" }
                      >("/api/v1/agent/tasks/{identity}/browser", {
                        pathParams: { identity: task.id },
                        body: { version, action: "end" },
                      })
                    )
                  }
                >
                  终止浏览
                </button>
                {state.view?.image && (
                  <>
                    <img
                      className={styles.browserViewportImg}
                      alt="当前 Chromium 阅读视口"
                      src={`data:image/jpeg;base64,${state.view.image}`}
                    />
                    <details>
                      <summary>视口文字</summary>
                      <p>{state.view.text}</p>
                    </details>
                  </>
                )}
                {isOwner && (
                  <>
                    <label>
                      已授权来源
                      <select
                        className={styles.browserSelect}
                        value={selectedSource}
                        onChange={(event) => setSelectedSource(event.target.value)}
                      >
                        {state.sources.map((url, idx) => (
                          <option key={`${url}-${idx}`} value={String(idx + 1)}>
                            {url}
                          </option>
                        ))}
                      </select>
                    </label>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() =>
                        void runUpdate(() =>
                          apiPost<
                            "/api/v1/agent/tasks/{identity}/browser/actions",
                            BrowserSessionState,
                            { version: number; kind: string; index: number; text: string }
                          >("/api/v1/agent/tasks/{identity}/browser/actions", {
                            pathParams: { identity: task.id },
                            body: { version, kind: "open", index: Number(selectedSource), text: "" },
                          })
                        )
                      }
                    >
                      打开来源
                    </button>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() =>
                        void runUpdate(() =>
                          apiPost<
                            "/api/v1/agent/tasks/{identity}/browser/actions",
                            BrowserSessionState,
                            { version: number; kind: string; index: number; text: string }
                          >("/api/v1/agent/tasks/{identity}/browser/actions", {
                            pathParams: { identity: task.id },
                            body: { version, kind: "previous", index: 0, text: "" },
                          })
                        )
                      }
                    >
                      上一屏
                    </button>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() =>
                        void runUpdate(() =>
                          apiPost<
                            "/api/v1/agent/tasks/{identity}/browser/actions",
                            BrowserSessionState,
                            { version: number; kind: string; index: number; text: string }
                          >("/api/v1/agent/tasks/{identity}/browser/actions", {
                            pathParams: { identity: task.id },
                            body: { version, kind: "next", index: 0, text: "" },
                          })
                        )
                      }
                    >
                      下一屏
                    </button>
                    <label>
                      页内文字
                      <input
                        maxLength={160}
                        value={findText}
                        onChange={(event) => setFindText(event.target.value)}
                      />
                    </label>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() =>
                        void runUpdate(() =>
                          apiPost<
                            "/api/v1/agent/tasks/{identity}/browser/actions",
                            BrowserSessionState,
                            { version: number; kind: string; index: number; text: string }
                          >("/api/v1/agent/tasks/{identity}/browser/actions", {
                            pathParams: { identity: task.id },
                            body: { version, kind: "find", index: 0, text: findText },
                          })
                        )
                      }
                    >
                      查找
                    </button>
                    {(state.view?.controls || []).map((ctrl) => (
                      <button
                        key={ctrl.index}
                        type="button"
                        disabled={busy}
                        onClick={() =>
                          void runUpdate(() =>
                            apiPost<
                              "/api/v1/agent/tasks/{identity}/browser/actions",
                              BrowserSessionState,
                              { version: number; kind: string; index: number; text: string }
                            >("/api/v1/agent/tasks/{identity}/browser/actions", {
                              pathParams: { identity: task.id },
                              body: { version, kind: "activate", index: ctrl.index, text: "" },
                            })
                          )
                        }
                      >
                        {`${ctrl.kind === "link" ? "打开" : ctrl.kind === "choice" ? "切换选项" : "展开或收起"} · ${
                          ctrl.label || ctrl.index
                        }`}
                      </button>
                    ))}
                  </>
                )}
              </>
            )}
          </>
        )}
      </div>
      <p className="hint" role="status">
        {notice}
      </p>
      <button type="button" onClick={onClose}>
        返回任务
      </button>
    </section>
  );
}
