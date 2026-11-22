import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { apiGet, apiPost, apiPut } from "@/shared/api/client";
import { useAppSession } from "@/shared/hooks/useAppSession";
import { useUnsavedGuard } from "@/shared/hooks/useUnsavedGuard";
import { errorMessage, safeMessage } from "@/shared/lib/errors";
import styles from "./SettingsPage.module.css";
import {
  buildOmniSettingsPayload,
  type OmniProviderConfig,
  type ProviderCheckResponse,
  type ProviderCredentialsState,
  type ProviderModelsResponse,
  type ProviderSettingsPayload,
} from "./types";

interface ProviderFormState {
  baseUrl: string;
  model: string;
  responsesModel: string;
  operationsMaxTokens: string;
  enableThinking: boolean;
  maxFrames: string;
  parallelReads: string;
  maxOutputTokens: string;
  key: string;
  clearKey: boolean;
}

const EMPTY_FORM: ProviderFormState = {
  baseUrl: "",
  model: "",
  responsesModel: "",
  operationsMaxTokens: "2048",
  enableThinking: true,
  maxFrames: "5",
  parallelReads: "2",
  maxOutputTokens: "4096",
  key: "",
  clearKey: false,
};

function formStateFromOmni(omni: OmniProviderConfig): ProviderFormState {
  return {
    baseUrl: omni.base_url ?? "",
    model: omni.model ?? "",
    responsesModel: omni.responses_model ?? "",
    operationsMaxTokens: String(omni.operations_max_tokens ?? 2048),
    enableThinking: Boolean(omni.enable_thinking),
    maxFrames: String(omni.video_options?.max_frames ?? 5),
    parallelReads: String(omni.responses_options?.parallel_reads ?? 2),
    maxOutputTokens: String(omni.responses_options?.max_output_tokens ?? 4096),
    key: "",
    clearKey: false,
  };
}

export function ProvidersPanel() {
  const { settings, refreshSettings, refreshStatus } = useAppSession();
  const omni = (settings?.omni as unknown as OmniProviderConfig | null | undefined) ?? null;
  const providerCreds =
    (settings?.provider_credentials as ProviderCredentialsState | null | undefined) ?? null;

  const [form, setForm] = useState<ProviderFormState>(() => (omni ? formStateFromOmni(omni) : EMPTY_FORM));
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [checking, setChecking] = useState(false);
  const [discovering, setDiscovering] = useState(false);
  const [saveState, setSaveState] = useState("");
  const [checkResult, setCheckResult] = useState("");
  const [discoveryState, setDiscoveryState] = useState("");
  const [discoveredModels, setDiscoveredModels] = useState<string[]>([]);

  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;

  useUnsavedGuard("providers-panel", dirty);

  const syncFromSettings = useCallback((nextOmni: OmniProviderConfig | null) => {
    if (!dirtyRef.current && nextOmni) {
      setForm(formStateFromOmni(nextOmni));
    }
  }, []);

  useEffect(() => {
    syncFromSettings(omni);
  }, [omni, syncFromSettings]);

  useEffect(() => {
    if (!dirtyRef.current) {
      void refreshSettings().catch(() => {});
    }
  }, [refreshSettings]);

  function updateField<K extends keyof ProviderFormState>(key: K, value: ProviderFormState[K]) {
    dirtyRef.current = true;
    setDirty(true);
    setSaveState("有未保存的修改");
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  async function handleSave(event: FormEvent) {
    event.preventDefault();
    if (!dirty || saving) return;
    setSaving(true);
    try {
      const payload = buildOmniSettingsPayload({
        baseUrl: form.baseUrl,
        model: form.model,
        responsesModel: form.responsesModel,
        operationsMaxTokens: Number(form.operationsMaxTokens),
        enableThinking: form.enableThinking,
        maxFrames: Number(form.maxFrames),
        parallelReads: Number(form.parallelReads),
        maxOutputTokens: Number(form.maxOutputTokens),
        key: form.key,
        clearKey: form.clearKey,
      });

      await apiPut<"/api/v1/system/settings/providers", unknown, ProviderSettingsPayload>(
        "/api/v1/system/settings/providers",
        { body: payload }
      );
      dirtyRef.current = false;
      setDirty(false);
      setSaveState("推理设置已保存。");
      setCheckResult("");
      const updated = await refreshSettings();
      if (updated?.omni) {
        setForm(formStateFromOmni(updated.omni as unknown as OmniProviderConfig));
      } else {
        setForm((prev) => ({ ...prev, key: "", clearKey: false }));
      }
      await refreshStatus().catch(() => {});
    } catch (error) {
      setSaveState(`保存失败：${safeMessage(errorMessage(error))}`);
    } finally {
      setSaving(false);
    }
  }

  function handleReset() {
    dirtyRef.current = false;
    setDirty(false);
    if (omni) {
      setForm(formStateFromOmni(omni));
    }
    setSaveState("已恢复保存的设置。");
  }

  async function handleCheckConnections() {
    if (checking) return;
    setChecking(true);
    setCheckResult("正在读取服务模型卡…");
    try {
      const result = await apiPost<"/api/v1/system/providers/check", ProviderCheckResponse>(
        "/api/v1/system/providers/check"
      );
      const summary = (
        [
          ["前台模型卡", result.foreground],
          ["后台模型卡", result.background],
        ] as const
      )
        .map(([labelText, card]) => {
          if (!card?.available) {
            return `${labelText}：${safeMessage(card?.error || "读取失败")}`;
          }
          const capacity = Number.isSafeInteger(card.context_tokens)
            ? `（${card.context_tokens} tokens）`
            : "";
          return `${labelText}：已读取${capacity}`;
        })
        .join(" · ");
      setCheckResult(summary);
      await refreshStatus().catch(() => {});
    } catch (error) {
      setCheckResult(safeMessage(errorMessage(error)));
    } finally {
      setChecking(false);
    }
  }

  async function handleDiscoverModels() {
    if (discovering) return;
    setDiscovering(true);
    setDiscoveredModels([]);
    setDiscoveryState("正在读取…");
    try {
      const result = await apiGet<"/api/v1/system/providers/models", ProviderModelsResponse>(
        "/api/v1/system/providers/models"
      );
      const models = Array.isArray(result.models) ? result.models : [];
      setDiscoveredModels(models);
      setDiscoveryState(
        result.error ? safeMessage(result.error) : `前台报告了 ${models.length} 个别名。`
      );
    } catch (error) {
      setDiscoveryState(safeMessage(errorMessage(error)));
    } finally {
      setDiscovering(false);
    }
  }

  const maxFramesCap = omni?.video_options?.frame_strategy === "joint" ? 8 : 24;

  return (
    <section
      className={`panel ${styles.providersPanel}`}
      id="providers-panel"
      data-workspace-anchor=""
      aria-labelledby="providers-heading"
    >
      <div className="panel-heading">
        <h2 id="providers-heading" className="win-group-title">
          推理与任务
        </h2>
        <button
          type="button"
          id="check-providers-settings"
          className="win-btn"
          disabled={checking}
          aria-busy={checking ? "true" : undefined}
          onClick={() => void handleCheckConnections()}
        >
          检查已保存的连接
        </button>
      </div>
      <p id="providers-check-result" className="hint" role="status">
        {checkResult}
      </p>

      <form id="providers-form" onSubmit={(event) => void handleSave(event)}>
        <fieldset
          id="provider-fields"
          className={styles.providerFields}
          aria-label="推理与任务设置"
          disabled={saving || !omni}
        >
          <section className={`provider-section ${styles.providerSection}`} aria-labelledby="foreground-settings-heading">
            <h3 id="foreground-settings-heading">前台 Omni</h3>
            <p className="hint">用于对话、私有通话和直播即时回应。</p>
            <label>
              前台模型别名
              <input
                id="omni-model"
                list="omni-model-options"
                required
                value={form.model}
                onChange={(event) => updateField("model", event.target.value)}
              />
            </label>
            <datalist id="omni-model-options">
              {discoveredModels.map((alias) => (
                <option key={alias} value={alias}>
                  {alias}
                </option>
              ))}
            </datalist>
            <div className="actions">
              <button
                type="button"
                className="win-btn"
                id="omni-discover-models"
                disabled={discovering}
                aria-busy={discovering ? "true" : undefined}
                onClick={() => void handleDiscoverModels()}
              >
                读取已保存的前台模型别名
              </button>
              <span id="omni-discovery-state" className="hint" role="status">
                {discoveryState}
              </span>
            </div>
            <label className="check-field">
              <input
                id="llm-enable-thinking"
                type="checkbox"
                checked={form.enableThinking}
                onChange={(event) => updateField("enableThinking", event.target.checked)}
              />
              {" 启用前台思考"}
            </label>
          </section>

          <section className={`provider-section ${styles.providerSection}`} aria-labelledby="background-settings-heading">
            <h3 id="background-settings-heading">后台 Agent</h3>
            <p className="hint">用于复杂交办、自主运营、视频读帧和记忆整理。</p>
            <label>
              后台模型别名
              <input
                id="omni-responses-model"
                required
                value={form.responsesModel}
                onChange={(event) => updateField("responsesModel", event.target.value)}
              />
            </label>
            <div className="form-grid">
              <div>
                <label>
                  评论、私信与动态 · 单次输出上限（tokens）
                  <input
                    id="llm-operations-tokens"
                    type="number"
                    min={1}
                    max={65536}
                    required
                    aria-describedby="operations-budget-hint"
                    value={form.operationsMaxTokens}
                    onChange={(event) => updateField("operationsMaxTokens", event.target.value)}
                  />
                </label>
                <p className="hint" id="operations-budget-hint">
                  每次生成运营决定或草稿的上限，含思考。刷视频的选片决策在运营设置里调整。
                </p>
              </div>
              <div>
                <label>
                  委派任务 · 单次输出上限（tokens）
                  <input
                    id="background-budget"
                    type="number"
                    min={1}
                    required
                    aria-describedby="agent-budget-hint"
                    value={form.maxOutputTokens}
                    onChange={(event) => updateField("maxOutputTokens", event.target.value)}
                  />
                </label>
                <p className="hint" id="agent-budget-hint">
                  每轮生成计划、工具请求或成果文本的上限，含思考。一个任务可以有多轮。
                </p>
              </div>
              <label>
                单项任务并行取材数
                <input
                  id="background-parallel"
                  type="number"
                  min={1}
                  required
                  value={form.parallelReads}
                  onChange={(event) => updateField("parallelReads", event.target.value)}
                />
              </label>
              <div>
                <label>
                  后台视觉 · 每批处理帧数上限
                  <input
                    id="video-max-frames"
                    type="number"
                    min={1}
                    max={maxFramesCap}
                    step={1}
                    required
                    aria-describedby="video-max-frames-hint"
                    value={form.maxFrames}
                    onChange={(event) => updateField("maxFrames", event.target.value)}
                  />
                </label>
                <p className="hint" id="video-max-frames-hint">
                  逐帧模式最多 24 帧，联合模式最多 8 帧。运营故事板另外设有取帧上限，实际批量取两者较小值。
                </p>
              </div>
            </div>
          </section>

          <details className={`provider-section ${styles.providerSection}`}>
            <summary>共用连接与凭据</summary>
            <label>
              网关 API 地址
              <input
                id="omni-base-url"
                type="url"
                required
                value={form.baseUrl}
                onChange={(event) => updateField("baseUrl", event.target.value)}
              />
            </label>
            <label>
              应用凭据
              <input
                id="omni-key"
                type="password"
                autoComplete="new-password"
                placeholder="留空保留当前凭据"
                value={form.key}
                onChange={(event) => updateField("key", event.target.value)}
              />
            </label>
            <span id="omni-key-state" className="hint">
              {providerCreds?.omni ? "已配置凭据" : "未保存凭据"}
            </span>
            <label className="check-field">
              <input
                id="omni-clear-key"
                type="checkbox"
                checked={form.clearKey}
                onChange={(event) => updateField("clearKey", event.target.checked)}
              />
              {" 清除已保存的应用凭据"}
            </label>
          </details>
        </fieldset>

        <div className="actions">
          <button
            className="primary win-btn"
            id="save-providers"
            type="submit"
            disabled={saving || !dirty}
            aria-busy={saving ? "true" : undefined}
          >
            保存推理设置
          </button>
          <button
            className="win-btn"
            id="reset-providers"
            type="button"
            disabled={saving || !dirty}
            onClick={handleReset}
          >
            放弃修改
          </button>
        </div>
        <p id="providers-save-state" className="hint" role="status">
          {saveState}
        </p>
      </form>
    </section>
  );
}
