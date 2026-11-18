import { $, node, safeMessage, notice, tokenCount } from "./core.js";

function syncModelSelectWithInput() {
  const select = $("llm-model-select");
  const value = $("llm-model").value.trim();
  if (value) select.value = [...select.options].some(option => option.value === value) ? value : "__custom__";
}

function formUsesModelCard(settings) {
  if ($("omni-video-service")?.checked && $("llm-kind").value === "openai_compatible") return true;
  const llm = settings?.llm;
  return llm?.context_source === "model_card"
    && $("llm-kind").value === llm.kind
    && $("llm-base-url").value.trim() === llm.base_url
    && $("llm-model").value.trim() === llm.model;
}

export function readProviderPayload(settings) {
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

  const modelId = ($("llm-model")?.value || "").trim();
  const llmKind = $("llm-kind")?.value || "llama_cpp";
  const llmUrl = ($("llm-base-url")?.value || "").trim();
  const modelCard = formUsesModelCard(settings);

  const payload = {
    llm: {
      kind: llmKind,
      base_url: llmUrl,
      model: modelId,
      operations_max_tokens: boundedInteger("llm-operations-tokens", "运营输出额度", 1, 65536),
    },
    credentials: {},
    clear_credentials: [],
  };
  if (!modelCard) {
    const native = integer("llm-native-tokens", "模型原生 / 服务声明上限");
    const context = $("llm-context-tokens").value.trim() ? integer("llm-context-tokens", "上下文窗口") : native;
    if (context > native) throw new Error("上下文窗口超过模型原生 / 服务声明上限。");
    Object.assign(payload.llm, { context_tokens: context, native_context_tokens: native });
  }
  if (llmKind === "openai_compatible" && $("omni-video-service")?.checked) payload.use_omni_video = true;
  if ((llmKind === "llama_cpp" || modelCard) && $("llm-enable-thinking")) {
    payload.llm.enable_thinking = $("llm-enable-thinking").checked;
    if (llmKind === "llama_cpp" && $("llm-live-thinking-tokens")) {
      payload.llm.live_thinking_tokens = boundedInteger("llm-live-thinking-tokens", "直播思考额度", 0, 8192);
    }
  }

  const key = $("llm-key");
  const clear = $("llm-clear-key").checked;
  if (!key.disabled && key.value && clear) throw new Error("请在填写新密钥和清除旧密钥之间选一项。");
  if (clear) payload.clear_credentials.push("llm");
  else if (!key.disabled && key.value) payload.credentials.llm = key.value;

  const backgroundBase = $("background-base")?.value?.trim();
  const backgroundModel = $("background-model")?.value?.trim();
  const backgroundKey = $("background-key")?.value;
  const clearBackground = $("background-clear")?.checked;
  if (backgroundBase || backgroundModel || backgroundKey || clearBackground || settings?.responses) {
    if (!backgroundBase || !backgroundModel) throw new Error("请完整填写后台服务地址和模型别名。");
    if (backgroundKey && clearBackground) throw new Error("后台凭据不能同时填写和清除。");
    payload.responses = { base_url: backgroundBase, model: backgroundModel };
    if ($("background-parallel")) {
      payload.responses.parallel_reads = boundedInteger("background-parallel", "并行取材数", 1, Number.MAX_SAFE_INTEGER);
    }
    if ($("background-budget")) {
      payload.responses.max_output_tokens = boundedInteger("background-budget", "后台思考与输出额度", 1, Number.MAX_SAFE_INTEGER);
    }
    if (backgroundKey) payload.credentials.responses = backgroundKey;
    if (clearBackground) payload.clear_credentials.push("responses");
  }

  return payload;
}

export function initInferenceProvidersUI(model, { api, busy, apply, refreshStatus }) {
  let dirty = false;
  let saving = false;
  const render = () => renderProviderForm(model.settings, model.status, dirty, saving);
  const sync = () => {
    if (!dirty) providerFormFromSettings(model.settings, model.status);
    render();
  };
  const saveState = $("providers-save-state");
  const feedback = (message, error = false) => {
    saveState.textContent = message;
    saveState.classList.toggle("error-text", error);
  };

  for (const event of ["input", "change"]) {
    $("providers-form").addEventListener(event, () => {
      dirty = true;
      render();
    });
  }

  $("providers-form").addEventListener("submit", event => {
    event.preventDefault();
    busy(event.submitter, async () => {
      const payload = readProviderPayload(model.settings);
      saving = true;
      render();
      feedback("正在保存并应用，请等待服务返回…");
      try {
        const result = await api("/api/settings/providers", { method: "PUT", body: payload });
        dirty = false;
        apply(result);
        let message = result.restart_required === true ? "配置已保存；服务要求重启后生效，请按服务提示处理。" : "配置已保存并应用，无需手动重启。已有角色音色保持原选择。";
        feedback(message);
        await refreshStatus();
      } catch (error) {
        const retry = error.status === 409 ? " 请先暂停或等待正在生成 / 发布的任务结束，再重试。" : "";
        feedback(`保存失败：${safeMessage(error.message)}${retry} 当前输入已保留。`, true);
      } finally {
        saving = false;
        render();
      }
    });
  });

  $("reset-providers").addEventListener("click", () => {
    dirty = false;
    sync();
    feedback("已恢复当前已保存配置；未提交的密钥输入已清除。");
  });
  $("check-providers-settings").addEventListener("click", event => {
    busy(event.currentTarget, async () => {
      await checkProviderConnections(api);
      await refreshStatus();
    });
  });

  const select = $("llm-model-select");
  const input = $("llm-model");
  if (select && input) {
    select.addEventListener("change", () => {
      if (select.value === "__custom__") {
        input.focus();
      } else if (select.value) {
        input.value = select.value;
      }
    });
    input.addEventListener("input", syncModelSelectWithInput);
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

    const output = $("ping-provider-result");
    output.textContent = "正在检查…";
    await busy(btn, async () => {
      try {
        const result = await api("/api/providers/ping", {
          method: "POST",
          body: { llm_url: llmUrl, llm_key: llmKey },
        });
        output.textContent = result.llm.detail;
      } catch (error) {
        output.textContent = safeMessage(error.message);
      }
    });
  });

  return {
    get dirty() { return dirty; },
    sync,
  };
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

function providerFormFromSettings(settings, status) {
  const llm = settings.llm || {};
  const modelCard = llm.context_source === "model_card";
  const observed = status?.context?.capacity_source === "model_card" ? status.context.server_tokens : null;
  const fields = {
    "llm-kind": llm.kind || "llama_cpp",
    "llm-base-url": llm.base_url || "",
    "llm-model": llm.model || "",
    "llm-context-tokens": modelCard ? observed ?? "" : llm.context_tokens ?? "",
    "llm-native-tokens": modelCard ? observed ?? "" : llm.native_context_tokens ?? status?.context?.native_tokens ?? 131072,
    "llm-operations-tokens": llm.operations_max_tokens ?? 8192,
    "llm-live-thinking-tokens": llm.live_thinking_tokens ?? 1024,
  };
  Object.entries(fields).forEach(([id, value]) => {
    const el = $(id);
    if (el) el.value = String(value);
  });
  if ($("llm-enable-thinking")) $("llm-enable-thinking").checked = llm.enable_thinking !== false;
  if ($("omni-video-service")) {
    const visual = settings.video_vision;
    $("omni-video-service").checked = visual?.adapter === "omni" && visual.enabled &&
      visual.base_url === llm.base_url && visual.model === llm.model;
  }
  $("llm-key").value = "";
  if ($("background-base")) {
    $("background-base").value = settings?.responses?.base_url || "";
    $("background-model").value = settings?.responses?.model || "";
    if ($("background-parallel")) $("background-parallel").value = settings?.responses?.parallel_reads ?? 2;
    if ($("background-budget")) $("background-budget").value = settings?.responses?.max_output_tokens ?? 4096;
    $("background-key").value = "";
    $("background-clear").checked = false;
  }
  $("llm-clear-key").checked = false;
  syncModelSelectWithInput();
}

function renderProviderForm(settings, status, dirty, saving) {
  $("provider-fields").disabled = saving;
  $("save-providers").disabled = saving;
  $("save-providers").setAttribute("aria-busy", String(saving));
  $("reset-providers").disabled = saving || !dirty;
  $("check-providers-settings").disabled = saving;
  $("providers-dirty").textContent = saving ? "正在保存并应用…" : dirty ? "有未保存修改" :
    settings ? "已保存配置" : "正在读取已保存配置…";
  $("llm-key-state").textContent = settings?.provider_credentials?.llm ? "已保存密钥" : "未保存密钥";
  const external = $("llm-kind").value === "openai_compatible", demo = $("llm-kind").value === "demo";
  const modelCard = formUsesModelCard(settings);
  if ($("llm-enable-thinking")) $("llm-enable-thinking").disabled = (external && !modelCard) || demo || saving;
  if ($("llm-live-thinking-tokens")) $("llm-live-thinking-tokens").disabled = external || demo || saving;
  const outputBudget = $("llm-live-output-budget");
  if (outputBudget) {
    const visible = settings?.llm?.max_tokens ?? 160;
    const thinking = Number($("llm-live-thinking-tokens")?.value ?? 1024);
    const supported = status?.providers?.live_thinking_budget_supported;
    const thinkingEnabled = $("llm-enable-thinking")?.checked !== false;
    outputBudget.textContent = external || demo || supported === false
      ? `当前接口未提供可核验的独立思考额度，输出上限 ${tokenCount(visible)}。`
      : !thinkingEnabled ? `思考已关闭，台词额度 ${tokenCount(visible)}。`
      : supported === true ? `台词额度 ${tokenCount(visible)}，总输出上限 ${tokenCount(visible + thinking)}。`
      : "等待当前模型报告思考额度支持状态。";
  }
  ["llm-base-url", "llm-model", "llm-key", "llm-context-tokens", "llm-native-tokens", "llm-operations-tokens"].forEach(id => {
    $(id).disabled = demo || saving;
  });
  if (modelCard) {
    $("llm-context-tokens").disabled = true;
    $("llm-native-tokens").disabled = true;
  }
  $("llm-base-url").required = !demo;
  $("llm-model").required = !demo;
  const context = status?.context;
  const native = context?.native_tokens;
  const capacity = context?.server_tokens;
  $("llm-capacity-help").textContent = modelCard
    ? capacity ? `推理服务实报窗口 ${tokenCount(capacity)}` : "等待推理服务报告窗口。"
    : external ?
    "外部服务窗口按提供方的容量填写；保存后用于上下文与输出预算。" :
    [native ? `当前节点报告模型原生容量 ${tokenCount(native)}` : "模型原生容量等待节点报告",
      capacity ? `当前服务窗口 ${tokenCount(capacity)}` : "服务窗口等待节点报告"].join(" · ");

}

async function checkProviderConnections(api) {
  const el = $("providers-check-result");
  el.hidden = false;
  el.textContent = "检查已保存配置的连接中…";
  try {
    const result = await api("/api/providers/check", { method: "POST" });
    el.textContent = [["文本", result.llm], ["语音", result.tts], ["视觉", result.vision]]
      .map(([label, status]) => `${label}：${status?.available ? "可用" : "未就绪"}`)
      .join(" · ");
  } catch (error) {
    el.textContent = safeMessage(error.message);
  }
}

