/**
 * Providers & Inference Settings Controller
 * Manages LLM endpoints, TTS configuration, Local vs Remote toggle, clean model selection, and ping diagnostics.
 */

import { $, node, safeMessage, notice, busy, tokenCount, formatBytes } from "./core.js";

let dafeiyuModelTimer = null;

export function updateMultimodalBadge() {
  const input = $("llm-model");
  const badge = $("model-multimodal-badge");
  const pingVisionBadge = $("ping-vision-badge");
  const pingVisionDetail = $("ping-vision-detail");
  if (!badge) return;

  const modelName = (input ? input.value : "").trim().toLowerCase();
  const isMultimodal = Boolean(
    modelName.includes("minicpm-v") ||
    modelName.includes("-v-") ||
    modelName.includes("vision") ||
    modelName.includes("vl") ||
    modelName.includes("4o") ||
    modelName.includes("gemini")
  );

  if (isMultimodal) {
    badge.className = "badge good";
    badge.textContent = "👁️ 支持多模态视觉";
    badge.title = "已检测到视觉能力：系统将支持视频抽帧与视觉关键帧分析";
    if (pingVisionBadge) {
      pingVisionBadge.className = "ping-badge ok";
      pingVisionBadge.textContent = "多模态就绪";
    }
    if (pingVisionDetail) {
      pingVisionDetail.textContent = "原生支持视觉抽帧分析；视频关键帧送入模型综合决策。";
    }
  } else {
    badge.className = "badge muted";
    badge.textContent = "📝 纯文本模式 (自动回退)";
    badge.title = "纯文本模型：无需多模态，系统自动平滑回退至字幕与文本决策模式";
    if (pingVisionBadge) {
      pingVisionBadge.className = "ping-badge muted";
      pingVisionBadge.textContent = "纯文本回退";
    }
    if (pingVisionDetail) {
      pingVisionDetail.textContent = "平滑回退模式：自动使用字幕与文本决策，不影响日常运营。";
    }
  }
}

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
  updateMultimodalBadge();
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
  try {
    const kind = $("llm-kind") ? $("llm-kind").value : "llama_cpp";
    const baseUrl = ($("llm-base-url")?.value || "").trim();
    const query = new URLSearchParams();
    if (baseUrl) query.set("base_url", baseUrl);
    if (kind) query.set("kind", kind);
    const qs = query.toString();
    const status = await api(`/api/models/dafeiyu/status${qs ? `?${qs}` : ""}`);
    renderDafeiyuModelCard(status);
    if (status.download_status === "downloading") {
      if (!dafeiyuModelTimer) {
        dafeiyuModelTimer = setInterval(() => checkDafeiyuModelStatus(api), 1500);
      }
    } else {
      if (dafeiyuModelTimer) {
        clearInterval(dafeiyuModelTimer);
        dafeiyuModelTimer = null;
      }
      if (status.installed) {
        loadLocalModelsDropdown(api);
      }
    }
  } catch (err) {
    console.warn("Failed to check Dafeiyu model status:", err);
  }
}

export function renderDafeiyuModelCard(status = {}) {
  const cardIcon = $("dafeiyu-card-icon");
  const cardTitle = $("dafeiyu-card-title");
  const cardDesc = $("dafeiyu-card-desc");
  const badge = $("dafeiyu-model-badge");
  const installedView = $("dafeiyu-model-installed-view");
  const missingView = $("dafeiyu-model-missing-view");
  const btnDownload = $("btn-download-dafeiyu-model");
  const btnCancel = $("btn-cancel-dafeiyu-download");
  const progressBox = $("dafeiyu-progress-container");
  const progressFill = $("dafeiyu-progress-fill");
  const progressText = $("dafeiyu-progress-text");
  const speedText = $("dafeiyu-speed-text");

  if (!badge) return;

  const currentKind = $("llm-kind") ? $("llm-kind").value : "llama_cpp";
  const baseUrl = ($("llm-base-url")?.value || "").trim();
  const modelId = ($("llm-model")?.value || "").trim() || "默认模型";
  const isRemoteTab = $("btn-mode-remote")?.classList.contains("active");

  // Determine scenario category
  let category = "local_file";
  if (isRemoteTab || currentKind === "openai_compatible") {
    const isLocalAddress = (
      baseUrl.includes("9012") ||
      baseUrl.includes("localhost") ||
      baseUrl.includes("127.0.0.1") ||
      baseUrl.includes("100.") ||
      baseUrl.includes("192.168.") ||
      baseUrl.includes("10.") ||
      baseUrl.includes("172.")
    );
    category = (!baseUrl || isLocalAddress) ? "self_hosted" : "cloud_api";
  } else {
    // Local deployment tab
    category = (baseUrl.includes(":9012") || baseUrl.includes(":8000") || baseUrl.includes(":11434")) ? "self_hosted" : "local_file";
  }

  if (category === "self_hosted") {
    if (cardIcon) cardIcon.textContent = "🚀";
    if (cardTitle) cardTitle.textContent = "Jetson Orin / 独立推理节点 (Broker 统一直通分发)";
    if (cardDesc) cardDesc.textContent = "端点自动按模型名分发与装载；非直播时段卸载 TTS 节省显存。无需在当前电脑重复下载权重。";

    const isOnline = status?.node_online !== false;
    badge.className = `badge ${isOnline ? "good" : "active"}`;
    badge.textContent = isOnline ? "✔ 服务节点已就绪" : "⚡ 独立服务端点";
    if (installedView) installedView.hidden = false;
    if (missingView) missingView.hidden = true;
    if (progressBox) progressBox.style.display = "none";
    if (btnDownload) btnDownload.style.display = "none";
    if (btnCancel) btnCancel.style.display = "none";

    const pathEl = installedView.querySelector("p");
    if (pathEl) {
      pathEl.style.color = "var(--text, #374151)";
      pathEl.innerHTML = `✔ 独立服务节点：<code>${baseUrl || "http://127.0.0.1:9012/llm"}</code> · 模式: <strong>统一直通分发</strong>`;
    }
    const hintEl = installedView.querySelector("small");
    if (hintEl) {
      hintEl.textContent = `请求直接发往节点端点，Broker 自动路由与装载对应模型；当前电脑无需重复校验或下载 .gguf 权重。`;
    }
    return;
  }

  if (category === "cloud_api") {
    if (cardIcon) cardIcon.textContent = "🌐";
    if (cardTitle) cardTitle.textContent = "云端商业 API 接入 (OpenAI 兼容协议)";
    if (cardDesc) cardDesc.textContent = "直接调用云端大语言模型与语音 API；无需本地显存与权重文件。";

    badge.className = "badge muted";
    badge.textContent = "🌐 云端 API 接入";
    if (installedView) installedView.hidden = false;
    if (missingView) missingView.hidden = true;
    if (progressBox) progressBox.style.display = "none";
    if (btnDownload) btnDownload.style.display = "none";
    if (btnCancel) btnCancel.style.display = "none";

    const pathEl = installedView.querySelector("p");
    if (pathEl) {
      pathEl.style.color = "var(--text, #374151)";
      pathEl.innerHTML = `🌐 云端 API 接口：<code>${baseUrl || "未配置 Base URL"}</code> (模型: <strong>${modelId}</strong>)`;
    }
    const hintEl = installedView.querySelector("small");
    if (hintEl) {
      hintEl.textContent = `由第三方云服务进行模型计算，请核对下方 API Key 与模型 ID。`;
    }
    return;
  }

  // Pure local_file
  if (cardIcon) cardIcon.textContent = "🐳";
  if (cardTitle) cardTitle.textContent = "大肥鱼专属微调模型 (MiniCPM-V-4.7-1B Q4_K_M)";
  if (cardDesc) cardDesc.textContent = "DoRA + DARE-TIES 权重融合，已内化大肥鱼人设与白饭口癖；托管于 Hugging Face。";

  if (status.installed) {
    badge.className = "badge good";
    badge.textContent = "✔ 本地权重已就绪";
    if (installedView) installedView.hidden = false;
    if (missingView) missingView.hidden = true;
    if (progressBox) progressBox.style.display = "none";
    if (btnDownload) btnDownload.style.display = "none";
    if (btnCancel) btnCancel.style.display = "none";

    const pathEl = installedView.querySelector("p");
    if (pathEl) {
      pathEl.style.color = "var(--text-success, #2e7d32)";
      pathEl.innerHTML = `✔ 本地模型已就绪：<code id="dafeiyu-model-path">${status.file_path || "runtime/models/MiniCPM-V-4_7-1B-dafeiyu-Q4_K_M.gguf"}</code> (<span id="dafeiyu-model-size">${formatBytes(status.file_size)}</span>)`;
    }
    const hintEl = installedView.querySelector("small");
    if (hintEl) {
      hintEl.textContent = `权重文件校验通过。可在下方模型下拉列表直接选用，并配合极简锚点 Prompt 运行。`;
    }
  } else {
    if (installedView) installedView.hidden = true;
    if (missingView) missingView.hidden = false;
    if ($("dafeiyu-missing-hint")) {
      $("dafeiyu-missing-hint").textContent = "全套本地部署检测：未在本地目录检测到微调权重。在下方选择模型并保存配置确认后，系统将自动拉取；也可点击下方按钮立即下载。";
    }

    if (status.download_status === "downloading") {
      badge.className = "badge active";
      badge.textContent = "正在下载…";
      if (progressBox) progressBox.style.display = "block";
      if (btnDownload) btnDownload.style.display = "none";
      if (btnCancel) btnCancel.style.display = "inline-block";
      const pct = (status.download_percent || 0).toFixed(1);
      if (progressFill) progressFill.style.width = `${pct}%`;
      if (progressText) {
        progressText.textContent = `${pct}% (${formatBytes(status.downloaded_bytes)} / ${formatBytes(status.expected_size)})`;
      }
      if (speedText) {
        speedText.textContent = `${status.speed_mb_s || 0} MB/s`;
      }
    } else {
      badge.className = "badge";
      badge.textContent = "未安装";
      if (progressBox) progressBox.style.display = "none";
      if (btnDownload) {
        btnDownload.style.display = "inline-block";
        btnDownload.disabled = false;
        btnDownload.textContent = "📥 立即下载大肥鱼微调模型 (~640MB)";
      }
      if (btnCancel) btnCancel.style.display = "none";
    }
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
  const btnModeLocal = $("btn-mode-local");
  const btnModeRemote = $("btn-mode-remote");

  function setDeployMode(mode) {
    const isLocal = mode === "local";
    btnModeLocal?.classList.toggle("active", isLocal);
    btnModeRemote?.classList.toggle("active", !isLocal);
    if (isLocal) {
      $("llm-kind").value = "llama_cpp";
      if (!$("llm-base-url").value || $("llm-base-url").value.startsWith("https://") || $("llm-base-url").value.includes("openai") || $("llm-base-url").value.includes("100.")) {
        $("llm-base-url").value = "http://127.0.0.1:9012/llm";
      }
      if (!$("llm-model").value || $("llm-model").value === "__custom__") {
        $("llm-model").value = "MiniCPM-V-4.7-1B";
      }
      $("tts-kind").value = "gpt_sovits";
      if (!$("tts-base-url").value || $("tts-base-url").value.startsWith("https://") || $("tts-base-url").value.includes("100.")) {
        $("tts-base-url").value = "http://127.0.0.1:9012/tts";
      }
      notice("已切换为【本地部署】模式（默认直通本机 Broker: 9012）。");
    } else {
      $("llm-kind").value = "openai_compatible";
      if (!$("llm-base-url").value || $("llm-base-url").value === "http://127.0.0.1:9012/llm" || $("llm-base-url").value === "http://127.0.0.1:9012/llm/2b") {
        $("llm-base-url").value = "http://100.95.31.18:9012/llm";
      }
      if (!$("tts-base-url").value || $("tts-base-url").value === "http://127.0.0.1:9012/tts") {
        $("tts-base-url").value = "http://100.95.31.18:9012/tts";
      }
      notice("已切换为【远程 / 独立节点】模式（默认预填远端 Jetson Broker: 9012，亦可自定义填写外部端点）。");
    }
    syncModelSelectWithInput();
    model.providersDirty = true;
    renderTTSFields(model);
    renderDafeiyuModelCard({});
    checkDafeiyuModelStatus(api);
    if (typeof onSettingsChanged === "function") onSettingsChanged();
  }

  btnModeLocal?.addEventListener("click", () => setDeployMode("local"));
  btnModeRemote?.addEventListener("click", () => setDeployMode("remote"));

  $("llm-kind")?.addEventListener("change", () => {
    const isLocal = $("llm-kind").value === "llama_cpp";
    btnModeLocal?.classList.toggle("active", isLocal);
    btnModeRemote?.classList.toggle("active", !isLocal);
    model.providersDirty = true;
    renderTTSFields(model);
    checkDafeiyuModelStatus(api);
    if (typeof onSettingsChanged === "function") onSettingsChanged();
  });

  $("tts-kind")?.addEventListener("change", () => {
    model.providersDirty = true;
    renderTTSFields(model);
    if (typeof onSettingsChanged === "function") onSettingsChanged();
  });

  $("llm-base-url")?.addEventListener("input", () => {
    model.providersDirty = true;
    checkDafeiyuModelStatus(api);
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
        const data = await api("/api/providers/remote-models", {
          method: "POST",
          body: { base_url: baseUrl, api_key: apiKey || undefined },
        });
        const models = Array.isArray(data.models) ? data.models : [];
        if (!models.length) {
          notice("未获取到模型列表，请确认地址与密钥是否正确，或在下方输入框手动填写模型 ID。");
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
        notice(`成功拉取 ${models.length} 个远端模型！可在下拉列表中直接选择。`);
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

    const llmBadge = $("ping-llm-badge");
    const llmDetail = $("ping-llm-detail");
    const ttsBadge = $("ping-tts-badge");
    const ttsDetail = $("ping-tts-detail");

    if (llmBadge) { llmBadge.className = "badge"; llmBadge.textContent = "测速中…"; }
    if (ttsBadge) { ttsBadge.className = "badge"; ttsBadge.textContent = "测速中…"; }

    await busy(btn, async () => {
      try {
        const res = await api("/api/providers/ping", {
          method: "POST",
          body: {
            llm_url: llmUrl || undefined,
            llm_key: llmKey || undefined,
            tts_url: ttsUrl || undefined,
            tts_key: ttsKey || undefined,
          }
        });

        if (res.llm) {
          if (res.llm.ok) {
            llmBadge.className = "badge good";
            llmBadge.textContent = `${res.llm.latency_ms} ms (正常)`;
            llmDetail.textContent = `${res.llm.url} · 连接成功 (HTTP ${res.llm.status_code})`;
            const dot = $("capsule-llm-dot");
            const lbl = $("capsule-llm-label");
            if (dot) dot.className = "status-dot green";
            if (lbl) lbl.textContent = `LLM: 正常 (${res.llm.latency_ms}ms)`;
          } else {
            llmBadge.className = "badge bad";
            llmBadge.textContent = "连接失败";
            llmDetail.textContent = `${res.llm.url || "未配置"} · ${res.llm.detail || "不可达"}`;
            const dot = $("capsule-llm-dot");
            const lbl = $("capsule-llm-label");
            if (dot) dot.className = "status-dot red";
            if (lbl) lbl.textContent = "LLM: 不可达";
          }
        }

        if (res.tts) {
          if (res.tts.ok) {
            ttsBadge.className = "badge good";
            ttsBadge.textContent = `${res.tts.latency_ms} ms (正常)`;
            ttsDetail.textContent = `${res.tts.url} · 连接成功 (HTTP ${res.tts.status_code})`;
            const dot = $("capsule-tts-dot");
            const lbl = $("capsule-tts-label");
            if (dot) dot.className = "status-dot green";
            if (lbl) lbl.textContent = `TTS: 正常 (${res.tts.latency_ms}ms)`;
          } else {
            ttsBadge.className = "badge bad";
            ttsBadge.textContent = "连接失败";
            ttsDetail.textContent = `${res.tts.url || "未配置"} · ${res.tts.detail || "不可达"}`;
            const dot = $("capsule-tts-dot");
            const lbl = $("capsule-tts-label");
            if (dot) dot.className = "status-dot amber";
            if (lbl) lbl.textContent = "TTS: 未联通";
          }
        }

        updateMultimodalBadge();
        notice("端点测通诊断完成！详细延迟与多模态状态已展示在下方卡片。");
      } catch (err) {
        notice(`端点诊断执行失败：${safeMessage(err.message)}`, true);
      }
    });
  });

  loadLocalModelsDropdown(api);
  checkDafeiyuModelStatus(api);
  updateMultimodalBadge();
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
  const isLocal = (llm.kind || "llama_cpp") === "llama_cpp" && (!llm.base_url || llm.base_url.includes("127.0.0.1") || llm.base_url.includes("localhost"));
  const btnLocal = $("btn-mode-local"), btnRemote = $("btn-mode-remote");
  if (btnLocal && btnRemote) {
    btnLocal.classList.toggle("active", isLocal);
    btnRemote.classList.toggle("active", !isLocal);
  }
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
  updateMultimodalBadge();
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
    el.textContent = JSON.stringify(publicSettings(await api("/api/providers/check", { method: "POST" })), null, 2);
  } catch (error) {
    el.textContent = safeMessage(error.message);
    throw error;
  }
}

