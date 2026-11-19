import { $, safeMessage } from "./core.js";

export function readOmniSettings() {
  const omni = {
    base_url: $("omni-base-url").value.trim(),
    model: $("omni-model").value.trim(),
    responses_model: $("omni-responses-model").value.trim(),
    operations_max_tokens: $("llm-operations-tokens").valueAsNumber,
    enable_thinking: $("llm-enable-thinking").checked,
    video_options: { max_frames: $("video-max-frames").valueAsNumber },
    responses_options: {
      parallel_reads: $("background-parallel").valueAsNumber,
      max_output_tokens: $("background-budget").valueAsNumber,
    },
  };
  const key = $("omni-key").value;
  const clear = $("omni-clear-key").checked;
  if (key && clear) throw new Error("填写新凭据或清除已有凭据，请选择一项。");
  if (key) return { omni, credentials: { omni: key } };
  if (clear) return { omni, clear_credentials: ["omni"] };
  return { omni };
}

export function initInferenceProvidersUI(model, { api, busy, apply }) {
  let dirty = false;
  let saving = false;
  const state = $("providers-save-state");
  const render = () => {
    $("provider-fields").disabled = saving || !model.settings?.omni;
    $("reset-providers").disabled = $("save-providers").disabled = saving || !dirty;
  };
  const sync = () => {
    if (!dirty && model.settings?.omni) {
      const { omni, provider_credentials } = model.settings;
      $("omni-base-url").value = omni.base_url;
      $("omni-model").value = omni.model;
      $("omni-responses-model").value = omni.responses_model;
      $("omni-key").value = "";
      $("omni-clear-key").checked = false;
      $("omni-key-state").textContent = provider_credentials?.omni ? "已配置凭据" : "未保存凭据";
      $("llm-operations-tokens").value = omni.operations_max_tokens;
      $("llm-enable-thinking").checked = omni.enable_thinking;
      $("background-parallel").value = omni.responses_options?.parallel_reads ?? 2;
      $("background-budget").value = omni.responses_options?.max_output_tokens ?? 4096;
      $("video-max-frames").value = omni.video_options?.max_frames ?? 5;
      $("video-max-frames").max = omni.video_options?.frame_strategy === "joint" ? 8 : 16;
    }
    render();
  };
  $("providers-form").addEventListener("input", () => {
    dirty = true;
    state.textContent = "有未保存的修改";
    render();
  });
  $("providers-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (!dirty || saving) return;
    saving = true;
    render();
    await busy(event.submitter, async () => {
      try {
        const result = await api("/api/settings/providers", { method: "PUT", body: readOmniSettings() });
        dirty = false;
        apply(result);
        state.textContent = "推理设置已保存。";
        $("providers-check-result").textContent = "";
      } catch (error) {
        state.textContent = "保存失败：" + safeMessage(error.message);
      }
    });
    saving = false;
    render();
  });
  $("reset-providers").addEventListener("click", () => {
    dirty = false;
    sync();
    state.textContent = "已恢复保存的设置。";
  });
  $("check-providers-settings").addEventListener("click", event => {
    busy(event.currentTarget, () => checkProviderConnections(api));
  });
  $("omni-discover-models").addEventListener("click", event => {
    busy(event.currentTarget, async () => {
      const options = $("omni-model-options");
      const note = $("omni-discovery-state");
      options.replaceChildren();
      note.textContent = "正在读取…";
      try {
        const result = await api("/api/providers/models");
        for (const alias of result.models || []) options.append(new Option(alias, alias));
        note.textContent = result.error ? safeMessage(result.error) : `前台报告了 ${options.options.length} 个别名。`;
      } catch (error) {
        note.textContent = safeMessage(error.message);
      }
    });
  });
  return { sync, reset() { dirty = false; $("omni-key").value = ""; }, get dirty() { return dirty; } };
}

export function publicSettings(value, key = "") {
  if (key === "credentials" && value != null) return "[已隐藏]";
  if (typeof value === "string") {
    if (/api[_-]?key|secret|password|authorization|(?:^|_)token$/i.test(key)) return "[已隐藏]";
    return safeMessage(value).replace(/([#?&](?:token|key)=)[^&#\s]+/gi, "$1[已隐藏]");
  }
  if (Array.isArray(value)) return value.map(item => publicSettings(item, key));
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([name, item]) => [name, publicSettings(item, name)]));
  }
  return value;
}

async function checkProviderConnections(api) {
  const el = $("providers-check-result");
  el.hidden = false;
  el.textContent = "正在读取服务模型卡…";
  try {
    const result = await api("/api/providers/check", { method: "POST" });
    el.textContent = [["前台模型卡", result.foreground], ["后台模型卡", result.background]]
      .map(([label, status]) => {
        if (!status?.available) return `${label}：${safeMessage(status?.error || "读取失败")}`;
        const capacity = Number.isSafeInteger(status.context_tokens) ? `（${status.context_tokens} tokens）` : "";
        return `${label}：已读取${capacity}`;
      })
      .join(" · ");
  } catch (error) {
    el.textContent = safeMessage(error.message);
  }
}

