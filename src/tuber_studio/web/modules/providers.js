import { $, node, safeMessage, notice, busy, tokenCount } from "./core.js";

let dafeiyuModelTimer = null;

export function syncModelSelectWithInput() {
  const select = $("llm-model-select");
  const input = $("llm-model");
  if (!select || !input) return;
  const val = input.value.trim();
  let found = false;
  for (const opt of select.options) {
    if (opt.value === val) {
      select.value = val;
      found = true;
      break;
    }
  }
  if (!found && val) {
    select.value = "__custom__";
  }
}

export async function loadLocalModelsDropdown(api) {
  try {
    const data = await api("/api/providers/local-models");
    const ftGroup = $("fine-tuned-models-group");
    const baseGroup = $("base-models-group");
    if (ftGroup && Array.isArray(data.fine_tuned) && data.fine_tuned.length) {
      ftGroup.replaceChildren();
      data.fine_tuned.forEach(m => {
        const opt = node("option", m.name || m.id);
        opt.value = m.id;
        ftGroup.append(opt);
      });
    }
    if (baseGroup && Array.isArray(data.base) && data.base.length) {
      baseGroup.replaceChildren();
      data.base.forEach(m => {
        const opt = node("option", m.name || m.id);
        opt.value = m.id;
        baseGroup.append(opt);
      });
    }
    syncModelSelectWithInput();
  } catch (err) {
    console.warn("Failed to load local models dropdown:", err);
  }
}

export async function checkDafeiyuModelStatus(api) {
  const card = $("dafeiyu-model-card");
  const badge = $("dafeiyu-model-badge");
  clearTimeout(dafeiyuModelTimer);
  try {
    const status = await api("/api/models/dafeiyu/status");
    const downloading = status.download_status === "downloading";
    card.hidden = !status.is_full_local && !downloading;
    badge.textContent = downloading ? "下载中" : status.installed ? "已安装" : "未安装";
    $("dafeiyu-download").hidden = downloading || status.installed;
    $("btn-cancel-dafeiyu-download").hidden = !downloading;
    $("dafeiyu-progress").hidden = !downloading;
    $("dafeiyu-progress").value = status.download_percent || 0;
    $("dafeiyu-missing-hint").textContent = status.download_status === "failed" ? "下载失败，可以重试。" : "";
    if (downloading) dafeiyuModelTimer = setTimeout(() => checkDafeiyuModelStatus(api), 1500);
  } catch (error) {
    badge.textContent = "状态读取失败";
  }
}

export function renderTTSFields(model) {
  const ttsKind = $("tts-kind") ? $("tts-kind").value : "gpt_sovits";
  const isPrerecorded = ttsKind === "prerecorded";
  const isGptSovits = ttsKind === "gpt_sovits";
  const isOpenAi = ttsKind === "openai_compatible";

  const urlContainer = $("tts-base-url-container");
  const extFields = $("tts-external-api-fields");
  const credFields = $("tts-credential-fields");
  const baseUrlInput = $("tts-base-url");

  if (urlContainer) urlContainer.hidden = isPrerecorded;
  if (extFields) extFields.hidden = !isOpenAi;
  if (credFields) credFields.hidden = isPrerecorded;

  if (baseUrlInput) {
    baseUrlInput.disabled = isPrerecorded || Boolean(model.providersSaving);
    baseUrlInput.required = !isPrerecorded;
    if (isGptSovits) {
      baseUrlInput.placeholder = "http://127.0.0.1:9012/tts 或 http://<jetson-ip>:9012/tts";
    } else if (isOpenAi) {
      baseUrlInput.placeholder = "服务提供的 Base URL（通常包含 /v1）";
    }
  }

  const modelInput = $("tts-model");
  const voiceInput = $("tts-voice");
  if (modelInput) {
    modelInput.required = isOpenAi;
    modelInput.disabled = !isOpenAi || Boolean(model.providersSaving);
  }
  if (voiceInput) {
    voiceInput.required = isOpenAi;
    voiceInput.disabled = !isOpenAi || Boolean(model.providersSaving);
  }

  const keyInput = $("tts-key");
  if (keyInput) keyInput.disabled = isPrerecorded || Boolean(model.providersSaving);

  const ttsKeyStatus = $("tts-key-state");
  if (ttsKeyStatus) {
    ttsKeyStatus.textContent = model.settings?.provider_credentials?.tts ? "已有保存密钥（不回显）" : "未保存密钥";
  }
}

export function readProviderPayload() {
  const integer = (id, title) => {
    const value = Number($(id).value);
    if (!$(id).value.trim() || !Number.isSafeInteger(value) || value < 4096) {
      throw new Error(`${title}须为不小于 4096 的整数 tokens。`);
    }
    return value;
  };
  const boundedInteger = (id, title, min, max) => {
    const value = Number($(id).value);
    if (!$(id).value.trim() || !Number.isSafeInteger(value) || value < min || value > max) {
      throw new Error(`${title}须在 ${min} 到 ${max} 之间。`);
    }
    return value;
  };

  const context = integer("llm-context-tokens", "上下文窗口");
  const native = integer("llm-native-tokens", "模型原生 / 服务声明上限");
  if (context > native) {
    throw new Error("上下文窗口不能超过模型原生 / 服务声明上限。请按实际服务能力填写。");
  }

  const modelId = ($("llm-model")?.value || "").trim();
  const llmKind = $("llm-kind")?.value || "llama_cpp";
  const isLlmApi = llmKind === "openai_compatible";
  const llmUrl = ($("llm-base-url")?.value || "").trim();

  const payload = {
    llm: {
      kind: llmKind,
      base_url: llmUrl,
      model: modelId,
      context_tokens: context,
      native_context_tokens: native,
      operations_max_tokens: boundedInteger("llm-operations-tokens", "运营输出额度", 1, 65536),
    },
    tts: {
      kind: $("tts-kind")?.value || "gpt_sovits",
      base_url: ($("tts-base-url")?.value || "").trim(),
      model: ($("tts-model")?.value || "").trim(),
      voice: ($("tts-voice")?.value || "").trim(),
    },
    vision: {
      kind: isLlmApi ? "openai_compatible" : "disabled",
      location: "local",
      base_url: isLlmApi ? llmUrl : "",
      model: isLlmApi ? modelId : "",
    },
    credentials: {},
    clear_credentials: [],
  };

  ["llm", "tts"].forEach(kind => {
    const key = $(`${kind}-key`);
    const clearEl = $(`${kind}-clear-key`);
    const clear = clearEl ? clearEl.checked : false;
    if (key && !key.disabled && key.value && clear) {
      throw new Error(`${kind.toUpperCase()} 请分别选择输入新密钥或清除已有密钥，不能同时操作。`);
    }
    if (clear) payload.clear_credentials.push(kind);
    else if (key && !key.disabled && key.value) payload.credentials[kind] = key.value;
  });

  return payload;
}

export function initInferenceProvidersUI(model, api, notice, busy, safeMessage, onSettingsChanged) {
  for (const event of ["input", "change"]) {
    $("providers-form").addEventListener(event, () => {
      model.providersDirty = true;
      onSettingsChanged();
    });
  }

  $("btn-download-dafeiyu-model")?.addEventListener("click", async () => {
    try {
      notice("已启动大肥鱼微调模型下载任务（从 Hugging Face 镜像加速拉取）…");
      await api("/api/models/dafeiyu/download", { method: "POST" });
      checkDafeiyuModelStatus(api);
    } catch (e) {
      notice(`启动下载失败: ${safeMessage(e.message)}`, true);
    }
  });

  $("btn-cancel-dafeiyu-download")?.addEventListener("click", async () => {
    try {
      await api("/api/models/dafeiyu/cancel", { method: "POST" });
      notice("已发送取消下载请求。");
      checkDafeiyuModelStatus(api);
    } catch (e) {
      notice(`取消失败: ${safeMessage(e.message)}`, true);
    }
  });

  const select = $("llm-model-select");
  const input = $("llm-model");
  if (select && input) {
    select.addEventListener("change", () => {
      if (select.value === "__custom__") {
        input.focus();
      } else if (select.value) {
        input.value = select.value;
        model.providersDirty = true;
        if (typeof onSettingsChanged === "function") onSettingsChanged();
      }
    });
    input.addEventListener("input", () => {
      syncModelSelectWithInput();
      model.providersDirty = true;
      if (typeof onSettingsChanged === "function") onSettingsChanged();
    });
  }

  $("btn-fetch-models")?.addEventListener("click", async () => {
    const btn = $("btn-fetch-models");
    const baseUrl = $("llm-base-url").value.trim();
    const apiKey = $("llm-key").value.trim();
    if (!baseUrl) {
      notice("请先填写 LLM 服务 Base URL，再拉取远端模型列表。", true);
      return;
    }
    await busy(btn, async () => {
      try {
        const data = await api("/api/providers/models", {
          method: "POST",
          body: { base_url: baseUrl, api_key: apiKey || undefined },
        });
        const models = Array.isArray(data.models) ? data.models : [];
        if (!models.length) {
          notice(data.error || "服务没有返回模型。可手动填写模型 ID。", true);
          return;
        }
        const remoteGroup = $("remote-models-group");
        if (remoteGroup) {
          remoteGroup.replaceChildren();
          models.forEach(m => {
            const opt = node("option", m);
            opt.value = m;
            remoteGroup.append(opt);
          });
          remoteGroup.hidden = false;
        }
        syncModelSelectWithInput();
        notice(`已读取 ${models.length} 个模型。`);
      } catch (err) {
        notice(`拉取远端模型失败：${safeMessage(err.message)}`, true);
      }
    });
  });

  $("btn-ping-providers")?.addEventListener("click", async () => {
    const btn = $("btn-ping-providers");
    const llmUrl = $("llm-base-url").value.trim();
    const llmKey = $("llm-key").value.trim();
    const ttsUrl = $("tts-base-url").value.trim();
    const ttsKey = $("tts-key").value.trim();

    const output = $("ping-provider-result");
    output.textContent = "正在检查…";
    await busy(btn, async () => {
      try {
        const result = await api("/api/providers/ping", {
          method: "POST",
          body: { llm_url: llmUrl, llm_key: llmKey, tts_url: ttsUrl, tts_key: ttsKey },
        });
        output.textContent = [
          ["文本", result.llm],
          ["语音", result.tts],
        ].map(([name, item]) => `${name}：${item.detail}${item.latency_ms == null ? "" : `（${item.latency_ms} ms）`}`).join(" · ");
      } catch (error) {
        output.textContent = safeMessage(error.message);
      }
    });
  });
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

export function clearProviderSecrets() {
  ["llm", "tts"].forEach(kind => {
    const el = $(`${kind}-key`), clear = $(`${kind}-clear-key`);
    if (el) el.value = "";
    if (clear) clear.checked = false;
  });
}

export function providerFormFromSettings(settings, model) {
  const llm = settings.llm || {}, tts = settings.tts || {};
  const fields = {
    "llm-kind": llm.kind || "llama_cpp",
    "llm-base-url": llm.base_url || "",
    "llm-model": llm.model || "",
    "llm-context-tokens": llm.context_tokens ?? 131072,
    "llm-native-tokens": llm.native_context_tokens ?? model?.status?.context?.native_tokens ?? 131072,
    "llm-operations-tokens": llm.operations_max_tokens ?? 8192,
    "tts-kind": tts.kind || "gpt_sovits",
    "tts-base-url": tts.base_url || "",
    "tts-model": tts.model || "",
    "tts-voice": tts.voice || "",
  };
  Object.entries(fields).forEach(([id, value]) => {
    const el = $(id);
    if (el) el.value = String(value);
  });
  if ($("llm-live-output-budget")) {
    $("llm-live-output-budget").textContent = `直播台词使用独立额度：${tokenCount(llm.max_tokens)}。`;
  }
  clearProviderSecrets();
  if (model) model.providersDirty = false;
  syncModelSelectWithInput();

}

export function renderProviderForm(model, renderLiveFeatures) {
  if ($("provider-fields")) $("provider-fields").disabled = !!model?.providersSaving;
  if ($("save-providers")) {
    $("save-providers").disabled = !!model?.providersSaving;
    $("save-providers").setAttribute("aria-busy", String(!!model?.providersSaving));
  }
  if ($("reset-providers")) $("reset-providers").disabled = !!model?.providersSaving || !model?.providersDirty;
  if ($("check-providers-settings")) $("check-providers-settings").disabled = !!model?.providersSaving;
  if ($("providers-dirty")) {
    $("providers-dirty").textContent = model?.providersSaving ? "正在保存并应用…" :
      model?.providersDirty ? "有未保存修改" :
      model?.settings ? "已保存配置" : "正在读取已保存配置…";
  }
  const external = $("llm-kind")?.value === "openai_compatible", demo = $("llm-kind")?.value === "demo";
  ["llm-base-url", "llm-model", "llm-key", "llm-context-tokens", "llm-native-tokens", "llm-operations-tokens"].forEach(id => {
    const el = $(id);
    if (el) el.disabled = demo || !!model?.providersSaving;
  });
  if ($("llm-base-url")) $("llm-base-url").required = !demo;
  if ($("llm-model")) $("llm-model").required = !demo;
  if ($("llm-probe-help")) $("llm-probe-help").hidden = !external;
  if ($("llm-capacity-help")) {
    $("llm-capacity-help").textContent = external ?
      "默认窗口 131072（128K），可填写不小于 4096 的整数。服务上限由操作者依据 API 提供方能力声明；连接成功或用量计数不能证明服务实际支持该上限，也不等同于本机验证。" :
      "默认窗口 131072（128K），可填写不小于 4096 的整数。当前 MiniCPM5-2B 原生上限 131072；增大必须有实际服务支持，不能通过 RoPE 参数伪扩展。";
  }
  renderTTSFields(model);
  if (typeof renderLiveFeatures === "function") renderLiveFeatures(model);
}

export function voiceAvailability(voice, model) {
  const provider = voice?.provider || voice?.kind;
  if (provider === "gpt_sovits") {
    const tts = model?.settings?.tts;
    if (tts?.kind !== "gpt_sovits") return "当前语音来源未选择 GPT-SoVITS / Genie";
    if (!tts?.base_url) return "请先配置语音服务 Base URL";
    return "";
  }
  if (provider === "openai_compatible" && (model?.settings?.tts?.kind !== "openai_compatible" || !model?.settings?.tts?.model || !model?.settings?.tts?.voice)) {
    return "请先配置外部语音 API 的模型和 Voice ID";
  }
  if (!["gpt_sovits", "openai_compatible", "prerecorded"].includes(provider)) return "此语音 Provider 尚未准入";
  return "";
}

export function renderVoicePreview(model, localEngineLabel = "GPT-SoVITS") {
  const select = $("preview-voice");
  if (!select) return;
  [...select.options].forEach(option => {
    const voice = model?.voices?.find(item => item.id === option.value);
    const reason = voiceAvailability(voice, model);
    option.disabled = !!reason;
    option.textContent = `${voice?.name || option.value}${reason ? "（暂不可用）" : ""}`;
  });
  const selected = model?.voices?.find(voice => voice.id === select.value);
  const reason = selected ? voiceAvailability(selected, model) : "暂无可用音色";
  const button = $("preview-submit");
  if (button && button.getAttribute("aria-busy") !== "true") button.disabled = !!reason;
  if ($("voice-preview-help")) {
    $("voice-preview-help").textContent = reason || ((selected?.provider || selected?.kind) === "gpt_sovits" ?
      `本地引擎：${localEngineLabel}，仅使用服务选定音色。点击生成后再播放；不会启动直播或切换资源模式。` :
      "预录和已配置的外部语音 API 可独立试听；不会启动直播、切换资源模式或替换角色音色。");
  }
}

export function applySettings(settings, model, { renderLiveFeatures, renderVoicePreviewFn, renderStatusFn } = {}) {
  model.settings = { ...model.settings, ...settings };
  settings = model.settings;
  if ($("settings-json")) $("settings-json").textContent = JSON.stringify(publicSettings(settings), null, 2);
  if (!model.providersDirty) providerFormFromSettings(settings, model);
  renderProviderForm(model, renderLiveFeatures);
  if (renderVoicePreviewFn) renderVoicePreviewFn();
  if (model.status && renderStatusFn) renderStatusFn(model.status);
  let url = model.settings.overlay_url || "/overlay";
  try { url = new URL(url, location.origin).href; } catch { url = `${location.origin}/overlay`; }
  if ($("overlay-url")) $("overlay-url").value = url;
  if ($("open-overlay")) $("open-overlay").href = url;
}

export async function loadSettings(model, api, deps = {}) {
  applySettings(await api("/api/settings"), model, deps);
}

export async function checkProviderConnections(output, api) {
  const el = $(output);
  if (!el) return;
  el.hidden = false;
  el.textContent = "检查已保存配置的连接中…";
  try {
    const result = await api("/api/providers/check", { method: "POST" });
    el.textContent = [["文本", result.llm], ["语音", result.tts], ["视觉", result.vision]]
      .map(([label, status]) => `${label}：${status?.available ? "可用" : "未就绪"}`)
      .join(" · ");
  } catch (error) {
    el.textContent = safeMessage(error.message);
    throw error;
  }
}

