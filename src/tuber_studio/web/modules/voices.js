import { $, safeMessage } from "./core.js";

const sourceNames = {
  gpt_sovits: "本地 Jetson",
  openai_compatible: "兼容 API",
  prerecorded: "预录配置",
};

export function syncVoiceSettings(model) {
  if (model.voiceSettingsDirty) return;
  const tts = model.settings?.tts || {};
  $("tts-kind").value = tts.kind || "gpt_sovits";
  $("tts-base-url").value = tts.kind === "openai_compatible" ? tts.base_url : "";
  $("tts-model").value = tts.model || "";
  $("tts-voice").value = tts.voice || "";
  $("tts-key").value = "";
  $("tts-clear-key").checked = false;
  renderVoiceSettings(model);
}

function renderVoiceSettings(model) {
  const selection = $("tts-kind").value;
  const saved = model.settings?.tts?.kind;
  const external = selection === "openai_compatible";
  $("tts-api-fields").hidden = !external;
  $("tts-api-fields").disabled = !external || model.voiceSettingsSaving;
  $("tts-kind").disabled = model.voiceSettingsSaving;
  $("tts-source-state").textContent = sourceNames[saved] ? "当前 · " + sourceNames[saved] : "待读取";
  $("tts-key-state").textContent = model.settings?.provider_credentials?.tts ?
    "已保存密钥。同一地址留空可保留。" : "服务需要认证时填写。";
  $("tts-source-help").textContent = external ? "保存后用于试听和直播台词。" :
    selection === "prerecorded" ? "当前使用预录音色。" :
      "默认跟随 Jetson，直播模式就绪后可试听角色的本地音色。";
}

export function voiceAvailability(voice, model) {
  const kind = model.settings?.tts?.kind;
  if (voice?.provider === "prerecorded") return "";
  if (voice?.provider !== kind) return "请选择当前语音来源的音色";
  if (kind === "gpt_sovits") {
    if (model.modeSwitch) return "正在切换推理模式";
    if (!model.settings?.features?.live_enabled) return "当前推理配置未启用本地语音";
    if (model.settings?.inference_broker?.enabled && model.status?.inference?.mode !== "live") {
      return "切换到直播模式后可试听本地语音";
    }
  }
  return voice ? "" : "暂无可用音色";
}

export function renderVoicePreview(model, selectDefault = false) {
  const select = $("preview-voice");
  const kind = model.settings?.tts?.kind;
  const active = model.status?.character?.voice_profile || model.settings?.tts?.voice_id;
  const preferred = model.voices.find(voice => voice.provider === kind && voice.id === active) ||
    model.voices.find(voice => voice.provider === kind);
  for (const option of select.options) {
    const voice = model.voices.find(item => item.id === option.value);
    option.hidden = voice?.provider !== kind && voice?.provider !== "prerecorded";
    option.disabled = option.hidden;
  }
  if (selectDefault || !select.selectedOptions[0] || select.selectedOptions[0].hidden) {
    select.value = preferred?.id || [...select.options].find(option => !option.hidden)?.value || "";
  }
  const voice = model.voices.find(item => item.id === select.value);
  const reason = voiceAvailability(voice, model);
  if ($("preview-submit").getAttribute("aria-busy") !== "true") $("preview-submit").disabled = Boolean(reason);
  $("voice-preview-help").textContent = reason || "";
}

export function initVoiceSettings(model, api, busy, apply) {
  $("tts-form").addEventListener("input", () => {
    model.voiceSettingsDirty = true;
    renderVoiceSettings(model);
  });
  $("tts-form").addEventListener("submit", event => {
    event.preventDefault();
    busy(event.submitter, async () => {
      const kind = $("tts-kind").value;
      const state = $("tts-save-state");
      const body = { tts: { kind } };
      if (kind === "openai_compatible") {
        Object.assign(body.tts, {
          base_url: $("tts-base-url").value.trim(),
          model: $("tts-model").value.trim(),
          voice: $("tts-voice").value.trim(),
        });
        const key = $("tts-key").value.trim();
        if (key && $("tts-clear-key").checked) {
          state.textContent = "请在填写新密钥和清除旧密钥之间选一项。";
          state.classList.add("error-text");
          return;
        }
        if (key) body.credentials = { tts: key };
        if ($("tts-clear-key").checked) body.clear_credentials = ["tts"];
      } else if (kind === "gpt_sovits") {
        body.clear_credentials = ["tts"];
      }
      model.voiceSettingsSaving = true;
      renderVoiceSettings(model);
      state.classList.remove("error-text");
      state.textContent = "正在保存…";
      try {
        const result = await api("/api/settings/providers", { method: "PUT", body });
        model.voiceSettingsDirty = false;
        apply(result);
        syncVoiceSettings(model);
        $("preview-audio").pause();
        $("preview-audio").removeAttribute("src");
        $("preview-audio").hidden = true;
        $("preview-state").textContent = "";
        renderVoicePreview(model, true);
        state.textContent = "已保存直播语音来源。";
      } catch (error) {
        state.classList.add("error-text");
        state.textContent = `保存失败：${safeMessage(error.message)}`;
      } finally {
        model.voiceSettingsSaving = false;
        renderVoiceSettings(model);
      }
    });
  });
}
