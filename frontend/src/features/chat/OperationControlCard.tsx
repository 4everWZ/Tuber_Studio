import { useEffect, useState } from "react";
import { ApiError, apiGet, apiPost } from "@/shared/api/client";
import { useAppSession } from "@/shared/hooks/useAppSession";
import { errorMessage } from "@/shared/lib/errors";
import { StatusPill, type StatusPillTone } from "@/shared/ui/StatusPill";
import {
  calmChatMessage,
  dispatchAutomationSync,
  type AutomationControlStatus,
} from "./types";

interface PolicyFieldSpec {
  key: string;
  title: string;
  group: "browsing" | "messages" | "dynamics";
  enabled: string;
  automatic?: string;
}

const POLICY_FIELDS: readonly PolicyFieldSpec[] = [
  { key: "browsing.discovery_enabled", title: "观看视频", group: "browsing", enabled: "discovery_enabled" },
  {
    key: "browsing.top_level_enabled",
    title: "视频主评",
    group: "browsing",
    enabled: "top_level_enabled",
    automatic: "top_level_auto_publish",
  },
  {
    key: "browsing.reply_enabled",
    title: "公开跟帖",
    group: "browsing",
    enabled: "reply_enabled",
    automatic: "reply_auto_publish",
  },
  {
    key: "browsing.comment_like_enabled",
    title: "评论点赞",
    group: "browsing",
    enabled: "comment_like_enabled",
    automatic: "comment_like_auto_publish",
  },
  {
    key: "browsing.video_like_enabled",
    title: "视频点赞",
    group: "browsing",
    enabled: "video_like_enabled",
    automatic: "video_like_auto_publish",
  },
  {
    key: "browsing.video_fav_enabled",
    title: "视频收藏",
    group: "browsing",
    enabled: "video_fav_enabled",
    automatic: "video_fav_auto_publish",
  },
  {
    key: "messages.enabled",
    title: "私信发送",
    group: "messages",
    enabled: "enabled",
    automatic: "auto_send",
  },
  { key: "messages.auto_poll", title: "自动读取来信", group: "messages", enabled: "auto_poll" },
  {
    key: "messages.allow_comment_handoff",
    title: "接受评论转私聊",
    group: "messages",
    enabled: "allow_comment_handoff",
  },
  {
    key: "dynamics.enabled",
    title: "动态",
    group: "dynamics",
    enabled: "enabled",
    automatic: "auto_publish",
  },
];

const STATE_LABELS: Record<string, string> = {
  starting: "正在检查启动条件",
  running: "自主运营中",
  draining: "正在收尾当前批次",
  stopping: "正在停止自主运营",
  blocked: "暂需关注条件",
  off: "自主运营已停止",
};

function stateTone(state: string | null | undefined, enabled: boolean, emergency: boolean): StatusPillTone {
  if (emergency || state === "blocked") return "amber";
  if (state === "running" || (enabled && !state)) return "good";
  if (state === "starting" || state === "draining" || state === "stopping") return "blue";
  return "neutral";
}

function readGroup(status: AutomationControlStatus, group: PolicyFieldSpec["group"]): Record<string, unknown> {
  if (group === "browsing") return status.settings || {};
  return status.base?.[group] || {};
}

function scopeOf(status: AutomationControlStatus | null | undefined): string {
  return [status?.account_id || "", status?.character?.id, status?.character?.version].join("/");
}

function currentMode(status: AutomationControlStatus, field: PolicyFieldSpec): string {
  const values = readGroup(status, field.group);
  if (!values[field.enabled]) return "0";
  if (field.automatic && values[field.automatic]) return "2";
  return "1";
}

export interface OperationControlCardProps {
  requestId: string;
  initialStatus: AutomationControlStatus;
  sharedStatus: AutomationControlStatus | null;
  controlsPending: boolean;
  onControlsPendingChange: (pending: boolean) => void;
  onReflectAutomation: (status: AutomationControlStatus) => void;
}

export function OperationControlCard({
  requestId,
  initialStatus,
  sharedStatus,
  controlsPending,
  onControlsPendingChange,
  onReflectAutomation,
}: OperationControlCardProps) {
  const { refreshStatus } = useAppSession();
  const status = sharedStatus || initialStatus;
  const [draftModes, setDraftModes] = useState<Record<string, string>>({});
  const [lastScope, setLastScope] = useState(() => scopeOf(status));
  const [note, setNote] = useState("");
  const [showRefresh, setShowRefresh] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  const currentScope = scopeOf(status);
  useEffect(() => {
    if (currentScope !== lastScope) {
      setLastScope(currentScope);
      setDraftModes({});
    }
  }, [currentScope, lastScope]);

  const emergency = status.policy?.emergency === true;
  const isStarting = status.state === "starting";
  const isRunning = status.state === "running" && !emergency;
  const enabled = status.policy?.enabled === true || isRunning;
  const pending = controlsPending || ["starting", "draining", "stopping"].includes(status.state || "");
  const dirty = POLICY_FIELDS.some(
    (field) => field.key in draftModes && draftModes[field.key] !== currentMode(status, field)
  );

  const stateText = emergency
    ? "已紧急停止"
    : (status.state && STATE_LABELS[status.state]) || (enabled ? STATE_LABELS.running : STATE_LABELS.off);
  const pillTone = stateTone(status.state, enabled, emergency);

  const blockers = (status.blockers || [])
    .map((item) => (typeof item === "string" ? item : item?.message))
    .filter((item): item is string => Boolean(item));
  const blockerText = calmChatMessage(blockers.join("；"));

  async function apply(action: "save" | "start" | "stop") {
    if (controlsPending) return;
    const scope = scopeOf(status);
    onControlsPendingChange(true);
    onReflectAutomation(status);
    setNote(action === "stop" ? "正在请求收尾…" : "正在确认运营权限…");
    let result: AutomationControlStatus | undefined;
    try {
      if (action === "stop") {
        result = await apiPost<"/api/v1/operations/automation/stop", AutomationControlStatus, { emergency: boolean }>(
          "/api/v1/operations/automation/stop",
          { body: { emergency: false } }
        );
      } else {
        const fresh = await apiGet<"/api/v1/operations/automation", AutomationControlStatus>(
          "/api/v1/operations/automation"
        );
        if (scopeOf(fresh) !== scope) {
          throw new Error("运营账号或角色已变更，请按当前状态重新确认权限。");
        }
        const body: Record<string, Record<string, boolean>> = {};
        for (const group of ["browsing", "messages", "dynamics"] as const) {
          const changedFields = POLICY_FIELDS.filter(
            (field) =>
              field.group === group &&
              field.key in draftModes &&
              draftModes[field.key] !== currentMode(status, field)
          );
          if (!changedFields.length) continue;
          const patch: Record<string, boolean> = {};
          for (const field of changedFields) {
            const val = draftModes[field.key];
            patch[field.enabled] = val !== "0";
            if (field.automatic) {
              patch[field.automatic] = val === "2";
            }
          }
          body[group] = patch;
        }
        const response = await apiPost<
          "/api/v1/agent/chat/turns/{request_id}/confirm/{action}",
          { card: { data: AutomationControlStatus } },
          Record<string, Record<string, boolean>>
        >("/api/v1/agent/chat/turns/{request_id}/confirm/{action}", {
          pathParams: { request_id: requestId, action },
          body,
        });
        result = response.card.data;
        setDraftModes({});
      }
      setNote(
        action === "save"
          ? "权限已保存。"
          : result?.state === "draining"
            ? "本批收尾后停止。"
            : (result?.state && STATE_LABELS[result.state]) || "控制状态已更新。"
      );
    } catch (error) {
      setNote(calmChatMessage(errorMessage(error)));
      if (error instanceof ApiError && error.status === 409 && action !== "stop") {
        setShowRefresh(true);
      }
      try {
        result = await apiGet<"/api/v1/operations/automation", AutomationControlStatus>(
          "/api/v1/operations/automation"
        );
      } catch {
        // Next status refresh will reconcile the card.
      }
    } finally {
      const effective = result || status;
      onControlsPendingChange(false);
      onReflectAutomation(effective);
      dispatchAutomationSync({
        automation: {
          state: effective.state ?? undefined,
          enabled: effective.policy?.enabled,
          emergency: effective.policy?.emergency,
        },
        refresh: true,
      });
      void refreshStatus().catch(() => {});
    }
  }

  async function refreshConfirmation() {
    if (refreshing) return;
    setRefreshing(true);
    try {
      const result = await apiPost<
        "/api/v1/agent/chat/turns/{request_id}/refresh-confirmation",
        { card: { data: AutomationControlStatus } }
      >("/api/v1/agent/chat/turns/{request_id}/refresh-confirmation", {
        pathParams: { request_id: requestId },
      });
      setDraftModes({});
      onReflectAutomation(result.card.data);
      setNote("已更新当前权限，请核对后确认。");
      setShowRefresh(false);
    } catch (error) {
      setNote(calmChatMessage(errorMessage(error)));
    } finally {
      setRefreshing(false);
    }
  }

  return (
    <>
      <div className="chat-card-header">
        <h2>自主运营</h2>
        <StatusPill label={stateText} tone={pillTone} />
      </div>
      {status.active_schedule && (
        <p className="hint">
          定时计划：{status.active_schedule.label || "限时运行中"} · 预计{" "}
          {status.active_schedule.end_local_display || "到期"} 自动停止
        </p>
      )}
      {blockerText && <p className="hint">{blockerText}</p>}
      <details>
        <summary>查看与修改运营权限</summary>
        <div className="operator-chat-policy">
          {POLICY_FIELDS.map((field) => {
            const baseMode = currentMode(status, field);
            const value = draftModes[field.key] ?? baseMode;
            const modes = field.automatic
              ? [
                  ["0", "关闭"],
                  ["1", "人工审核"],
                  ["2", "自动执行"],
                ]
              : [
                  ["0", "关闭"],
                  ["1", "允许"],
                ];
            return (
              <label key={field.key}>
                {field.title}
                <select
                  value={value}
                  disabled={pending}
                  onChange={(event) => {
                    const nextVal = event.target.value;
                    setDraftModes((prev) => {
                      const next = { ...prev };
                      if (nextVal === baseMode) delete next[field.key];
                      else next[field.key] = nextVal;
                      return next;
                    });
                  }}
                >
                  {modes.map(([optVal, optLabel]) => (
                    <option key={optVal} value={optVal}>
                      {optLabel}
                    </option>
                  ))}
                </select>
              </label>
            );
          })}
        </div>
      </details>
      <div className="actions">
        <button type="button" disabled={pending || !dirty} onClick={() => void apply("save")}>
          保存权限
        </button>
        <button
          type="button"
          className="primary"
          hidden={enabled && !isStarting}
          disabled={pending || emergency}
          aria-busy={isStarting}
          onClick={() => void apply("start")}
        >
          {isStarting ? "正在启动运营…" : "开始自主运营"}
        </button>
        <button
          type="button"
          hidden={!enabled || isStarting}
          disabled={pending || emergency}
          onClick={() => void apply("stop")}
        >
          停止自主运营
        </button>
        <button
          type="button"
          hidden={!showRefresh}
          disabled={refreshing}
          onClick={() => void refreshConfirmation()}
        >
          重新核对权限
        </button>
        <a href="#automation-settings">打开运营设置</a>
      </div>
      <p className="hint" role="status">
        {note}
      </p>
    </>
  );
}
