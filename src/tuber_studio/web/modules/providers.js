import { $, node, safeMessage, notice, tokenCount } from "./core.js";

let dafeiyuModelTimer = null;

function syncModelSelectWithInput() {
  const select = $("llm-model-select");
  const value = $("llm-model").value.trim();
  if (value) select.value = [...select.options].some(option => option.value === value) ? value : "__custom__";
}

async function loadLocalModelsDropdown(api) {
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

async function checkDafeiyuModelStatus(api) {
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

function readProviderPayload() {
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
    vision: {
      kind: isLlmApi ? "openai_compatible" : "disabled",
      location: "local",
      base_url: isLlmApi ? llmUrl : "",
      model: isLlmApi ? modelId : "",
    },
    credentials: {},
    clear_credentials: [],
  };

  const key = $("llm-key");
  const clear = $("llm-clear-key").checked;
  if (!key.disabled && key.value && clear) throw new Error("请在填写新密钥和清除旧密钥之间选一项。");
  if (clear) payload.clear_credentials.push("llm");
  else if (!key.disabled && key.value) payload.credentials.llm = key.value;

  return payload;
}

export function initInferenceProvidersUI(model, { api, busy, apply, refreshStatus }) {
  let dirty = false;
  let saving = false;
  const render = () => renderProviderForm(model.settings, dirty, saving);
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
      const payload = readProviderPayload();
      saving = true;
      render();
      feedback("正在保存并应用，请等待服务返回…");
      try {
        const result = await api("/api/settings/providers", { method: "PUT", body: payload });
        dirty = false;
        apply(result);
        let message = result.restart_required === true ? "配置已保存；服务要求重启后生效，请按服务提示处理。" : "配置已保存并应用，无需手动重启。已有角色音色保持原选择。";
        if (result.auto_download_triggered) {
          message += " 检测到为全套本地部署且本地缺少微调权重，已自动在后台启动从 Hugging Face 镜像下载。";
        }
        feedback(message);
        checkDafeiyuModelStatus(api);
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
    checkDafeiyuModelStatus(api);
    feedback("已恢复当前已保存配置；未提交的密钥输入已清除。");
  });
  $("check-providers-settings").addEventListener("click", event => {
    busy(event.currentTarget, () => checkProviderConnections(api));
  });

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
    refresh: () => Promise.all([loadLocalModelsDropdown(api), checkDafeiyuModelStatus(api)]),
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
  const fields = {
    "llm-kind": llm.kind || "llama_cpp",
    "llm-base-url": llm.base_url || "",
    "llm-model": llm.model || "",
    "llm-context-tokens": llm.context_tokens ?? 131072,
    "llm-native-tokens": llm.native_context_tokens ?? status?.context?.native_tokens ?? 131072,
    "llm-operations-tokens": llm.operations_max_tokens ?? 8192,
  };
  Object.entries(fields).forEach(([id, value]) => {
    const el = $(id);
    if (el) el.value = String(value);
  });
  if ($("llm-live-output-budget")) {
    $("llm-live-output-budget").textContent = `直播台词使用独立额度：${tokenCount(llm.max_tokens)}。`;
  }
  $("llm-key").value = "";
  $("llm-clear-key").checked = false;
  syncModelSelectWithInput();
}

function renderProviderForm(settings, dirty, saving) {
  $("provider-fields").disabled = saving;
  $("save-providers").disabled = saving;
  $("save-providers").setAttribute("aria-busy", String(saving));
  $("reset-providers").disabled = saving || !dirty;
  $("check-providers-settings").disabled = saving;
  $("providers-dirty").textContent = saving ? "正在保存并应用…" : dirty ? "有未保存修改" :
    settings ? "已保存配置" : "正在读取已保存配置…";
  $("llm-key-state").textContent = settings?.provider_credentials?.llm ? "已保存密钥" : "未保存密钥";
  const external = $("llm-kind").value === "openai_compatible", demo = $("llm-kind").value === "demo";
  ["llm-base-url", "llm-model", "llm-key", "llm-context-tokens", "llm-native-tokens", "llm-operations-tokens"].forEach(id => {
    $(id).disabled = demo || saving;
  });
  $("llm-base-url").required = !demo;
  $("llm-model").required = !demo;
  $("llm-probe-help").hidden = !external;
  $("llm-capacity-help").textContent = external ?
    "默认窗口 131072（128K），可填写不小于 4096 的整数。服务上限由操作者依据 API 提供方能力声明；连接成功或用量计数不能证明服务实际支持该上限，也不等同于本机验证。" :
    "默认窗口 131072（128K），可填写不小于 4096 的整数。当前 MiniCPM5-2B 原生上限 131072；增大必须有实际服务支持，不能通过 RoPE 参数伪扩展。";
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

