import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useUnsavedGuard } from "@/shared";
import { apiGet, apiPost, apiPut } from "@/shared/api/client";
import { emitNotice, errorMessage, safeMessage } from "@/shared/lib/errors";
import { DiscoverySourceForm } from "./DiscoveryPanel";
import { OperationRecoveryPolicyForm } from "./OperationRecoveryPolicyForm";
import { OperationSchedulePanel } from "./OperationSchedulePanel";
import { useOperationsControl } from "@/shared/operations/operationsStore";
import { deriveAutomaticSendingWarning, PermissionMatrix } from "./PermissionMatrix";
import { DynamicPolicyForm, MessagePolicyForm } from "./PolicyForms";
import styles from "./operations.module.css";
import type { DynamicPolicy, MessagesListResponse, MessagePolicyState } from "./types";

interface RoundFieldSpec {
  key: keyof typeof ROUND_DEFAULTS;
  id: string;
}

const ROUND_DEFAULTS = {
  daily_watch_limit: 0,
  max_replies_per_video: 2,
  max_per_hour: 5,
  max_per_day: 20,
  interval_seconds: 300,
  workers: 4,
  candidate_limit: 20,
  decision_max_tokens: 8192,
  deadline_seconds: 1800,
} as const;

const ROUND_FIELD_SPECS: RoundFieldSpec[] = [
  { key: "daily_watch_limit", id: "op-daily-watch" },
  { key: "max_replies_per_video", id: "op-max-replies" },
  { key: "max_per_hour", id: "op-max-per-hour" },
  { key: "max_per_day", id: "matrix-max-per-day" },
  { key: "interval_seconds", id: "op-interval" },
  { key: "workers", id: "op-workers" },
  { key: "candidate_limit", id: "exp-candidate-limit" },
  { key: "decision_max_tokens", id: "exp-decision-max-tokens" },
  { key: "deadline_seconds", id: "exp-deadline-seconds" },
];

export function AutomationSettingsPage() {
  const control = useOperationsControl();
  const [roundValues, setRoundValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(Object.entries(ROUND_DEFAULTS).map(([k, v]) => [k, String(v)]))
  );
  const [businessTimezone, setBusinessTimezone] = useState("Asia/Taipei");
  const [frameSampling, setFrameSampling] = useState("baseline");
  const [roundDirty, setRoundDirty] = useState(false);
  const [roundSaving, setRoundSaving] = useState(false);
  const [roundNote, setRoundNote] = useState("");
  const [previewPending, setPreviewPending] = useState(false);

  const [dynamicPolicy, setDynamicPolicy] = useState<DynamicPolicy | null>(null);
  const [messagePolicy, setMessagePolicy] = useState<MessagePolicyState | null>(null);

  useUnsavedGuard("automation-round-settings", roundDirty);

  const loadExtraPolicies = useCallback(async () => {
    const [dynRes, msgRes] = await Promise.allSettled([
      apiGet<"/api/v1/operations/dynamics/policy", DynamicPolicy>(
        "/api/v1/operations/dynamics/policy"
      ),
      apiGet<"/api/v1/operations/messages", MessagesListResponse>("/api/v1/operations/messages"),
    ]);
    if (dynRes.status === "fulfilled") setDynamicPolicy(dynRes.value);
    if (msgRes.status === "fulfilled") setMessagePolicy(msgRes.value.policy);
  }, []);

  const refreshAll = useCallback(async () => {
    await Promise.allSettled([
      control.refreshStatus(),
      control.refreshAccount(),
      control.refreshDiscovery(),
      loadExtraPolicies(),
    ]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadExtraPolicies]);

  useEffect(() => {
    void refreshAll();
  }, [refreshAll]);

  // Sync round settings from server when not dirty (preserving 0 via ??)
  useEffect(() => {
    if (roundDirty || !control.status?.settings) return;
    const settings = control.status.settings;
    const next: Record<string, string> = {};
    for (const { key } of ROUND_FIELD_SPECS) {
      const val = settings[key];
      next[key] = String(val ?? ROUND_DEFAULTS[key]);
    }
    setRoundValues(next);
    setBusinessTimezone(settings.business_timezone || "Asia/Taipei");
    setFrameSampling(settings.frame_sampling || "baseline");
  }, [control.status?.settings, roundDirty]);

  const status = control.status;
  const policyEnabled = status?.policy?.enabled === true;
  const emergency = Boolean(status?.policy?.emergency);
  const transitioning = Boolean(
    status?.state && ["starting", "draining", "stopping"].includes(status.state)
  );
  const activeRounds = status?.active_rounds ?? (status?.active_round ? [status.active_round] : []);
  const hasActiveRound = activeRounds.length > 0;
  const hasVideoRound = activeRounds.some((round) => !round.config?.interaction_only);

  const sendingWarning = deriveAutomaticSendingWarning(status, control.account);

  async function handlePreviewRound() {
    if (previewPending || hasVideoRound) return;
    setPreviewPending(true);
    try {
      await apiPost<"/api/v1/operations/automation/round", unknown, { preview: boolean }>(
        "/api/v1/operations/automation/round",
        { body: { preview: true } }
      );
      emitNotice("单轮运营预览已排队启动，候选将存为待审核草稿。");
      await control.refreshStatus();
    } catch (err) {
      emitNotice(errorMessage(err), true);
    } finally {
      setPreviewPending(false);
    }
  }

  async function handleSaveRoundSettings(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (roundSaving) return;
    const payload: Record<string, unknown> = {};
    for (const { key } of ROUND_FIELD_SPECS) {
      payload[key] = Number(roundValues[key]);
    }
    payload.business_timezone = businessTimezone.trim() || "Asia/Taipei";
    payload.frame_sampling = frameSampling;

    setRoundSaving(true);
    setRoundNote("正在保存轮次参数…");
    try {
      await control.saveAutomationSettings(payload);
      setRoundDirty(false);
      setRoundNote("轮次参数已保存。");
    } catch (err) {
      setRoundNote(`保存失败：${safeMessage(errorMessage(err))}`);
    } finally {
      setRoundSaving(false);
    }
  }

  function updateRoundField(key: string, value: string) {
    setRoundValues((prev) => ({ ...prev, [key]: value }));
    setRoundDirty(true);
    setRoundNote("有未保存的修改。");
  }

  return (
    <section
      data-section="automation-settings"
      className={`win-page-view ${styles.settingsStack}`}
    >
      <div className="page-heading win-heading">
        <h1>运营设置</h1>
        <div className="actions">
          <a href="#automation-records" className="button quiet">
            运行记录
          </a>
          <a href="#automation" className="button quiet">
            返回控制台
          </a>
        </div>
      </div>

      {/* Operations Controls */}
      <section className="win-group-box" id="operations-controls" data-workspace-anchor="">
        <h2 className="win-group-title">运营控制</h2>
        <div className="actions">
          <button
            id="op-preview-round"
            type="button"
            className="win-btn"
            disabled={!status || hasVideoRound || previewPending}
            onClick={() => void handlePreviewRound()}
          >
            只读单轮预览
          </button>
          <button
            id="op-stop-round"
            type="button"
            className="win-btn"
            disabled={
              !status ||
              emergency ||
              transitioning ||
              control.controlPending ||
              (!policyEnabled && !hasActiveRound)
            }
            onClick={() => void control.stopAutomation(false)}
          >
            {policyEnabled ? "停止自主运营" : "停止只读预览"}
          </button>
          <button
            id="refresh-operations"
            type="button"
            className="win-btn"
            onClick={async () => {
              await Promise.allSettled([control.refreshStatus(), control.refreshDiscovery()]);
              emitNotice("运营状态已刷新。");
            }}
          >
            刷新状态
          </button>
        </div>

        {sendingWarning && (
          <p id="automatic-sending-state" className="feature-note" role="status">
            {sendingWarning}
          </p>
        )}

        <div className={styles.operationStopControls}>
          <div className="actions">
            <button
              id="op-emergency"
              type="button"
              className="danger"
              disabled={emergency || control.controlPending}
              onClick={() => void control.stopAutomation(true)}
            >
              紧急停止所有自动收发
            </button>
            {emergency && (
              <button
                id="op-reset-stop"
                type="button"
                className="win-btn"
                disabled={control.controlPending}
                onClick={() => void control.resetEmergencyStop()}
              >
                解除紧急停止
              </button>
            )}
          </div>
          <p className="hint">紧急停止会阻止各渠道自动收发，重启后仍有效。</p>
        </div>
      </section>

      <OperationSchedulePanel
        status={status}
        controlPending={control.controlPending}
        onCreateSchedule={(input) => control.createOperationSchedule(input)}
        onUpdateSchedule={(id, patch) => control.updateOperationSchedule(id, patch)}
        onDeleteSchedule={(id) => control.deleteOperationSchedule(id)}
      />

      {/* Discovery Source */}
      <div className="win-group-box">
        <h2 className="win-group-title">自动刷视频来源</h2>
        <DiscoverySourceForm
          discovery={control.discovery ?? status?.discovery ?? null}
          onSaveSource={(selection) =>
            control.saveDiscoverySettings({ discovery_source: selection })
          }
        />
      </div>

      {/* Shared Round Settings */}
      <div
        className="win-group-box"
        id="operation-round-settings"
        data-workspace-anchor=""
      >
        <h2 className="win-group-title">共享轮次参数</h2>
        <p className="hint">自主运营与只读预览共用这些设置。</p>
        <form id="op-round-settings" onSubmit={handleSaveRoundSettings}>
          <fieldset className={styles.settingsFormGroup} disabled={roundSaving}>
            <legend>观看与回复数量</legend>
            <p className="hint">各项填 0 表示该项不限额。是否发出由各渠道的权限设置决定。</p>
            <div className={styles.settingsGrid3}>
              <label>
                每日观看视频数
                <input
                  id="op-daily-watch"
                  type="number"
                  min={0}
                  required
                  value={roundValues.daily_watch_limit}
                  onChange={(e) => updateRoundField("daily_watch_limit", e.target.value)}
                />
              </label>
              <label>
                每个视频每日回复数
                <input
                  id="op-max-replies"
                  type="number"
                  min={0}
                  required
                  value={roundValues.max_replies_per_video}
                  onChange={(e) => updateRoundField("max_replies_per_video", e.target.value)}
                />
                <small className="hint">主评与跟帖合计，只计发布成功的回复</small>
              </label>
              <label>
                评论每小时发布上限
                <input
                  id="op-max-per-hour"
                  type="number"
                  min={0}
                  required
                  value={roundValues.max_per_hour}
                  onChange={(e) => updateRoundField("max_per_hour", e.target.value)}
                />
              </label>
              <label>
                评论滚动 24 小时发布上限
                <input
                  id="matrix-max-per-day"
                  type="number"
                  min={0}
                  required
                  value={roundValues.max_per_day}
                  onChange={(e) => updateRoundField("max_per_day", e.target.value)}
                />
              </label>
              <label>
                每日计数时区
                <input
                  id="op-business-timezone"
                  required
                  value={businessTimezone}
                  onChange={(e) => {
                    setBusinessTimezone(e.target.value);
                    setRoundDirty(true);
                    setRoundNote("有未保存的修改。");
                  }}
                />
              </label>
            </div>
          </fieldset>

          <fieldset className={styles.settingsFormGroup} disabled={roundSaving}>
            <legend>轮次调度</legend>
            <p className="hint">新来信和直接互动优先。连续两次后，下一次模型机会让给正在等待的后台任务。</p>
            <div className="form-grid">
              <label>
                轮次间隔（秒）
                <input
                  type="number"
                  id="op-interval"
                  min={60}
                  max={86400}
                  step={1}
                  required
                  value={roundValues.interval_seconds}
                  onChange={(e) => updateRoundField("interval_seconds", e.target.value)}
                />
                <small className="hint">一轮结束到下一轮开始的等待时间</small>
              </label>
              <label>
                视频 Agent 并发数
                <input
                  type="number"
                  id="op-workers"
                  min={1}
                  max={8}
                  step={1}
                  required
                  value={roundValues.workers}
                  onChange={(e) => updateRoundField("workers", e.target.value)}
                />
                <small className="hint">单轮最多同时处理的视频数</small>
              </label>
            </div>
          </fieldset>

          <fieldset className={styles.settingsFormGroup} disabled={roundSaving}>
            <legend>取材与生成额度</legend>
            <div className="form-grid">
              <label>
                每轮候选视频上限
                <input
                  id="exp-candidate-limit"
                  type="number"
                  min={1}
                  max={20}
                  required
                  value={roundValues.candidate_limit}
                  onChange={(e) => updateRoundField("candidate_limit", e.target.value)}
                />
              </label>
              <label>
                单次决策最大输出 Token
                <input
                  id="exp-decision-max-tokens"
                  type="number"
                  min={512}
                  max={65536}
                  required
                  value={roundValues.decision_max_tokens}
                  onChange={(e) => updateRoundField("decision_max_tokens", e.target.value)}
                />
              </label>
              <label>
                只读预览运行超时（秒）
                <input
                  id="exp-deadline-seconds"
                  type="number"
                  min={10}
                  max={1800}
                  required
                  value={roundValues.deadline_seconds}
                  onChange={(e) => updateRoundField("deadline_seconds", e.target.value)}
                />
              </label>
              <label>
                故事板每批取帧上限
                <select
                  id="exp-frame-sampling"
                  value={frameSampling}
                  onChange={(e) => {
                    setFrameSampling(e.target.value);
                    setRoundDirty(true);
                    setRoundNote("有未保存的修改。");
                  }}
                >
                  <option value="baseline">每批最多三帧</option>
                  <option value="adaptive">每批最多六帧</option>
                </select>
              </label>
            </div>
          </fieldset>

          <div className="actions" style={{ marginTop: 12 }}>
            <button
              id="op-save-quick-config"
              type="submit"
              className="primary"
              disabled={!status || roundSaving}
            >
              保存轮次参数
            </button>
            <p id="op-round-settings-note" className="hint" role="status" style={{ margin: 0 }}>
              {roundNote}
            </p>
          </div>
        </form>
      </div>

      {/* Recovery Policy */}
      <div className="win-group-box">
        <div className="win-group-title">平台拒绝后的修订</div>
        <OperationRecoveryPolicyForm />
      </div>

      {/* Permission Matrix */}
      <PermissionMatrix
        status={status}
        account={control.account}
        onSaveChange={(change) => control.saveAutomationSettings(change)}
        onRefresh={() => control.refreshStatus()}
      />

      {/* Dynamic Policy */}
      <DynamicPolicyForm
        policy={dynamicPolicy}
        character={status?.character}
        onSaved={(next) => setDynamicPolicy(next)}
      />

      {/* Message Policy */}
      <section id="message-policy-settings" data-workspace-anchor="">
        <MessagePolicyForm
          policy={messagePolicy}
          defaultOpen
          onSavePolicy={async (value) => {
            const updated = await apiPut<
              "/api/v1/operations/messages/policy",
              MessagePolicyState,
              typeof value
            >("/api/v1/operations/messages/policy", { body: value });
            setMessagePolicy(updated);
            await control.refreshStatus();
          }}
        />
      </section>
    </section>
  );
}
