import { $ } from "./core.js";

export function voiceAvailability(voice) {
  if (!voice) return "请选择可用的音色。";
  if (voice.provider === "prerecorded") return "";
  if (voice.id === "external_api") {
    return "该角色保留外部 API 音色选择。要用 Omni 试听，请选择参考音色。";
  }
  if (voice.provider === "omni_reference" && voice.reference_asset) return "";
  return "该音色缺少可用的参考录音。";
}

export function renderVoicePreview(model, selectDefault = false) {
  const select = $("preview-voice");
  const active = model.status?.character ? model.status.character.voice_profile || model.settings?.voice_id || "" : "";
  if (selectDefault || select.dataset.activeVoice !== active) {
    select.value = active;
    select.dataset.activeVoice = active;
    delete select.dataset.selectedVoice;
  }
  const voice = model.voices.find(item => item.id === select.value);
  const reason = voiceAvailability(voice);
  const fixed = voice?.provider === "prerecorded";
  $("preview-text").disabled = fixed || Boolean(reason);
  $("preview-text").closest("label").hidden = fixed;
  const submit = $("preview-submit");
  if (submit.getAttribute("aria-busy") !== "true") submit.disabled = Boolean(reason);
  submit.textContent = fixed ? "试听固定样本" : "生成试听音频";
  $("voice-preview-help").textContent = reason || (fixed ? "固定录音：" + (voice.sample_text || voice.name) : "");
}
