import { useEffect, useState, type FormEvent } from "react";
import { useUnsavedGuard } from "@/shared";
import { apiPut } from "@/shared/api/client";
import { emitNotice, errorMessage, webCharacterName, type CharacterLike } from "@/shared/lib/errors";
import styles from "./operations.module.css";
import type { DynamicPolicy, MessagePolicyState } from "./types";

function parseStrictInt(raw: string, title: string, min: number): number {
  const num = Number(raw);
  if (!raw.trim() || !Number.isSafeInteger(num) || num < min) {
    throw new Error(`${title}须为不小于 ${min} 的有效整数。`);
  }
  return num;
}

function formatLocalDateTime(value: number | string | null | undefined): string {
  if (value == null) return "—";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime())
    ? String(value)
    : date.toLocaleString("zh-CN", { hour12: false });
}

export interface DynamicPolicyFormProps {
  policy: DynamicPolicy | null;
  character?: CharacterLike | null;
  onSaved: (next: DynamicPolicy) => void;
  onDirtyChange?: (dirty: boolean) => void;
}

export function DynamicPolicyForm({
  policy,
  character,
  onSaved,
  onDirtyChange,
}: DynamicPolicyFormProps) {
  const [enabled, setEnabled] = useState(false);
  const [autoPublish, setAutoPublish] = useState(true);
  const [evaluateInterval, setEvaluateInterval] = useState("1800");
  const [minPostInterval, setMinPostInterval] = useState("21600");
  const [maxPosts, setMaxPosts] = useState("2");
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [stateText, setStateText] = useState("");
  const [stateError, setStateError] = useState(false);

  useUnsavedGuard("dynamic-policy-form", dirty);

  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);

  useEffect(() => {
    if (dirty || !policy) return;
    const saved = policy.base || policy;
    setEnabled(Boolean(saved.enabled));
    setAutoPublish(saved.auto_publish !== false);
    setEvaluateInterval(String(saved.evaluate_interval_seconds ?? 1800));
    setMinPostInterval(String(saved.min_post_interval_seconds ?? 21600));
    setMaxPosts(String(saved.max_posts_per_day ?? 2));
  }, [policy, dirty]);

  const saved = policy?.base || policy;
  const modeLabel = !policy
    ? "正在读取"
    : !saved?.enabled
      ? "未允许动态"
      : saved.auto_publish
        ? "已允许动态 · 自动发布"
        : "已允许动态 · 人工审核";

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saving || !policy) return;
    setSaving(true);
    setStateError(false);
    try {
      const payload = {
        enabled,
        auto_publish: autoPublish,
        evaluate_interval_seconds: parseStrictInt(evaluateInterval, "动态评估间隔", 1),
        min_post_interval_seconds: parseStrictInt(minPostInterval, "最短发布间隔", 0),
        max_posts_per_day: parseStrictInt(maxPosts, "每日动态数", 0),
      };
      const updated = await apiPut<
        "/api/v1/operations/dynamics/policy",
        DynamicPolicy,
        typeof payload
      >("/api/v1/operations/dynamics/policy", { body: payload });
      setDirty(false);
      onSaved(updated);
      const updatedBase = updated.base || updated;
      const msg = !updatedBase.enabled
        ? "已关闭动态权限。"
        : updatedBase.auto_publish
          ? "已允许自动发布动态，开始自主运营后生效。"
          : "已允许生成动态草稿，开始自主运营后生效。";
      setStateText(msg);
      emitNotice(msg);
    } catch (err) {
      setStateError(true);
      setStateText(`策略保存失败：${errorMessage(err)}。输入已保留。`);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="win-group-box" id="dynamic-policy-settings" data-workspace-anchor="">
      <div className="panel-heading">
        <div className="win-group-title" style={{ margin: 0 }}>
          动态生成策略与发布频次
        </div>
        <span id="dynamic-mode" className={`badge ${saved?.enabled ? "good" : ""}`.trim()}>
          {modeLabel}
        </span>
      </div>

      <form id="dynamic-policy-form" onSubmit={handleSubmit}>
        <fieldset id="dynamic-policy-fields" disabled={!policy || saving}>
          <div className="win-checks-bar">
            <label>
              <input
                id="dynamic-enabled"
                type="checkbox"
                checked={enabled}
                onChange={(e) => {
                  setEnabled(e.target.checked);
                  setDirty(true);
                }}
              />{" "}
              允许动态
            </label>
            <label>
              <input
                id="dynamic-auto-publish"
                type="checkbox"
                checked={autoPublish}
                onChange={(e) => {
                  setAutoPublish(e.target.checked);
                  setDirty(true);
                }}
              />{" "}
              自动发布（关闭则存为草稿待人工审核）
            </label>
          </div>

          <div className="form-grid" style={{ marginTop: 12 }}>
            <label>
              评估间隔（秒）
              <input
                id="dynamic-evaluate-interval"
                type="number"
                min={1}
                step={1}
                required
                value={evaluateInterval}
                onChange={(e) => {
                  setEvaluateInterval(e.target.value);
                  setDirty(true);
                }}
              />
            </label>
            <label>
              最短发布间隔（秒，0 表示不限）
              <input
                id="dynamic-min-post-interval"
                type="number"
                min={0}
                step={1}
                required
                value={minPostInterval}
                onChange={(e) => {
                  setMinPostInterval(e.target.value);
                  setDirty(true);
                }}
              />
            </label>
            <label>
              24 小时最大尝试次数（0 表示不限）
              <input
                id="dynamic-max-posts"
                type="number"
                min={0}
                step={1}
                required
                value={maxPosts}
                onChange={(e) => {
                  setMaxPosts(e.target.value);
                  setDirty(true);
                }}
              />
            </label>
          </div>
        </fieldset>

        <div className="actions" style={{ marginTop: 14 }}>
          <button
            id="save-dynamic-policy"
            type="submit"
            className="primary win-btn"
            disabled={!policy || saving || !dirty}
            aria-busy={saving}
          >
            保存动态设置
          </button>
          <a href="#accounts" className="win-link-btn">
            配置 B站共用账号 →
          </a>
        </div>
      </form>

      <p
        id="dynamic-policy-state"
        className={stateError ? "error-text" : "hint"}
        role="status"
      >
        {stateText}
      </p>

      {policy && (
        <dl id="dynamic-policy-status" className="description-list">
          <dt>当前角色</dt>
          <dd>{webCharacterName(character) || "未报告"}</dd>
          <dt>共用B站账号</dt>
          <dd>{policy.credentials_configured ? "已配置（不回显）" : "未配置，可手动生成草稿"}</dd>
          <dt>下次评估（当地时间）</dt>
          <dd>{formatLocalDateTime(policy.next_evaluation_at)}</dd>
          <dt>退避至（当地时间）</dt>
          <dd>{formatLocalDateTime(policy.backoff_until)}</dd>
          <dt>最近错误</dt>
          <dd>{policy.last_error || "无"}</dd>
          <dt>表单</dt>
          <dd>{dirty ? "有未保存修改" : "显示已保存策略"}</dd>
        </dl>
      )}
    </div>
  );
}

export interface MessagePolicyFormProps {
  policy: MessagePolicyState | null;
  defaultOpen?: boolean;
  onSavePolicy: (value: {
    enabled: boolean;
    auto_poll: boolean;
    auto_send: boolean;
    allow_comment_handoff: boolean;
    poll_interval_seconds: number;
    max_per_hour: number;
    max_per_day: number;
    recipient_cooldown_seconds: number;
  }) => Promise<unknown>;
  onDirtyChange?: (dirty: boolean) => void;
}

export function MessagePolicyForm({
  policy,
  defaultOpen = false,
  onSavePolicy,
  onDirtyChange,
}: MessagePolicyFormProps) {
  const [enabled, setEnabled] = useState(false);
  const [autoPoll, setAutoPoll] = useState(false);
  const [autoSend, setAutoSend] = useState(false);
  const [allowHandoff, setAllowHandoff] = useState(true);
  const [pollInterval, setPollInterval] = useState("60");
  const [maxPerHour, setMaxPerHour] = useState("5");
  const [maxPerDay, setMaxPerDay] = useState("20");
  const [cooldown, setCooldown] = useState("120");
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [feedback, setFeedback] = useState("");

  useUnsavedGuard("message-policy-form", dirty);

  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);

  useEffect(() => {
    if (dirty || !policy) return;
    const base = policy.base || policy;
    setEnabled(Boolean(base.enabled));
    setAutoPoll(Boolean(base.auto_poll));
    setAutoSend(Boolean(base.auto_send));
    setAllowHandoff(base.allow_comment_handoff !== false);
    setPollInterval(String(base.poll_interval_seconds ?? 60));
    setMaxPerHour(String(base.max_per_hour ?? 5));
    setMaxPerDay(String(base.max_per_day ?? 20));
    setCooldown(String(base.recipient_cooldown_seconds ?? 120));
  }, [policy, dirty]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saving) return;
    setSaving(true);
    setFeedback("");
    try {
      await onSavePolicy({
        enabled,
        auto_poll: autoPoll,
        auto_send: autoSend,
        allow_comment_handoff: allowHandoff,
        poll_interval_seconds: Number(pollInterval),
        max_per_hour: Number(maxPerHour),
        max_per_day: Number(maxPerDay),
        recipient_cooldown_seconds: Number(cooldown),
      });
      setDirty(false);
      setFeedback("私信策略已保存。");
    } catch (err) {
      setFeedback(errorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <details className="panel" open={defaultOpen}>
      <summary>私信策略与发送限额</summary>
      <form data-policy="" onSubmit={handleSubmit}>
        <div className="checks">
          <label>
            <input
              type="checkbox"
              name="enabled"
              checked={enabled}
              onChange={(e) => {
                setEnabled(e.target.checked);
                setDirty(true);
                setFeedback("策略有未保存修改。");
              }}
            />
            启用私信
          </label>
          <label>
            <input
              type="checkbox"
              name="auto_poll"
              checked={autoPoll}
              onChange={(e) => {
                setAutoPoll(e.target.checked);
                setDirty(true);
                setFeedback("策略有未保存修改。");
              }}
            />
            自动读取来信
          </label>
          <label>
            <input
              type="checkbox"
              name="auto_send"
              checked={autoSend}
              onChange={(e) => {
                setAutoSend(e.target.checked);
                setDirty(true);
                setFeedback("策略有未保存修改。");
              }}
            />
            自动发送有来源触发的回复
          </label>
          <label>
            <input
              type="checkbox"
              name="allow_comment_handoff"
              checked={allowHandoff}
              onChange={(e) => {
                setAllowHandoff(e.target.checked);
                setDirty(true);
                setFeedback("策略有未保存修改。");
              }}
            />
            收到私聊邀请或完成约定后联系
          </label>
        </div>

        <div className={styles.policyFieldsGrid}>
          <label>
            读取间隔（秒）
            <input
              name="poll_interval_seconds"
              type="number"
              min={20}
              max={86400}
              value={pollInterval}
              onChange={(e) => {
                setPollInterval(e.target.value);
                setDirty(true);
                setFeedback("策略有未保存修改。");
              }}
            />
          </label>
          <label>
            每小时最多发送
            <input
              name="max_per_hour"
              type="number"
              min={0}
              required
              value={maxPerHour}
              onChange={(e) => {
                setMaxPerHour(e.target.value);
                setDirty(true);
                setFeedback("策略有未保存修改。");
              }}
            />
          </label>
          <label>
            每天最多发送
            <input
              name="max_per_day"
              type="number"
              min={0}
              required
              value={maxPerDay}
              onChange={(e) => {
                setMaxPerDay(e.target.value);
                setDirty(true);
                setFeedback("策略有未保存修改。");
              }}
            />
          </label>
          <label>
            同一收件人冷却（秒）
            <input
              name="recipient_cooldown_seconds"
              type="number"
              min={10}
              max={86400}
              value={cooldown}
              onChange={(e) => {
                setCooldown(e.target.value);
                setDirty(true);
                setFeedback("策略有未保存修改。");
              }}
            />
          </label>
        </div>

        <div className="actions" style={{ marginTop: 12 }}>
          <button type="submit" disabled={!policy || saving}>
            保存私信策略
          </button>
          <span className="hint" role="status">
            {feedback}
          </span>
        </div>
        <p className="hint">
          发送限额填 0 表示不限。启用自动读取和发送后，新收到的连续来信会合并回复。手工生成的草稿和关闭自动发送时的草稿进入审核。
        </p>
      </form>
    </details>
  );
}
