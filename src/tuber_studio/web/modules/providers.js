/**
 * Providers & Inference Settings Controller
 * Manages LLM endpoints, TTS configuration, Local vs Remote toggle, clean model selection, and ping diagnostics.
 */

import { $, node, safeMessage, notice, busy, tokenCount, formatBytes } from "./core.js";

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

        notice("端点测通诊断完成！详细延迟已展示在下方卡片。");
      } catch (err) {
        notice(`端点诊断执行失败：${safeMessage(err.message)}`, true);
      }
    });
  });

  loadLocalModelsDropdown(api);
  checkDafeiyuModelStatus(api);
}
