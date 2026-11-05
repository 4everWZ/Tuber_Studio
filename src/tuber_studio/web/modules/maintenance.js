"use strict";

import { $ } from "./core.js";

export function renderRetentionControls(model) {
  const loaded = !!model.retention, pending = !!model.retentionPending;
  if ($("retention-never")) $("retention-never").disabled = !loaded || pending;
  if ($("retention-days")) {
    $("retention-days").disabled = !loaded || pending || $("retention-never")?.checked;
    $("retention-days").required = !$("retention-never")?.checked;
  }
  if ($("retention-before")) $("retention-before").disabled = !loaded || pending;
  if ($("retention-save")) $("retention-save").disabled = !loaded || pending || !model.retentionDirty;
  if ($("retention-preview")) $("retention-preview").disabled = !loaded || pending || model.retentionDirty;
  if ($("retention-confirm")) $("retention-confirm").disabled = pending || !model.retentionPreview;
  if ($("retention-cleanup")) $("retention-cleanup").disabled = pending || !model.retentionPreview || !$("retention-confirm")?.checked;
  for (const kind of ["save", "preview", "cleanup"]) {
    const el = $("retention-" + kind);
    if (el) el.setAttribute("aria-busy", String(model.retentionPending === kind));
  }
}

export function invalidateRetentionPreview(model, message = "请先预览待清理范围，再明确确认。", error = false) {
  model.retentionPreview = null;
  if ($("retention-confirm")) $("retention-confirm").checked = false;
  if ($("retention-preview-panel")) $("retention-preview-panel").hidden = true;
  if ($("retention-cleanup-state")) {
    $("retention-cleanup-state").textContent = message;
    $("retention-cleanup-state").classList.toggle("error-text", error);
  }
  renderRetentionControls(model);
}

export function retentionRequest(model) {
  if (model.retentionDirty) throw new Error("请先保存留存策略，再预览清理范围。");
  const selectedDate = $("retention-before")?.value;
  if (!selectedDate) {
    if (!model.retention || model.retention.days == null || model.retention.enabled === false) {
      throw new Error("当前不自动清理；手动清理请指定截至日期。");
    }
    return {};
  }
  const cutoff = new Date(selectedDate + "T00:00:00.000Z");
  if (Number.isNaN(cutoff.getTime())) throw new Error("请选择有效的截至日期。");
  cutoff.setUTCDate(cutoff.getUTCDate() + 1);
  if (cutoff.getTime() > Date.now()) throw new Error("截至日期必须是已结束的 UTC 日期；未来截止时间不能清理。");
  return { before: cutoff.toISOString() };
}

export async function retentionAction(kind, action, model, notice) {
  if (model.retentionPending) return;
  model.retentionPending = kind;
  renderRetentionControls(model);
  try {
    await action();
  } catch (error) {
    if (kind !== "save") {
      invalidateRetentionPreview(model, `操作未完成：${error.message} 请重新预览后再清理。`, true);
    } else {
      if ($("retention-policy-state")) {
        $("retention-policy-state").textContent = `策略保存失败：${error.message}`;
        $("retention-policy-state").classList.add("error-text");
      }
    }
    notice(error.message, true);
  } finally {
    model.retentionPending = null;
    renderRetentionControls(model);
  }
}

export async function loadRetention(model, { api, describe, dateTime }) {
  try {
    model.retention = await api("/api/retention");
    if (!model.retentionDirty) {
      if ($("retention-never")) $("retention-never").checked = model.retention.days == null;
      if ($("retention-days")) $("retention-days").value = model.retention.days ?? 30;
    }
    const lastCompleteDay = new Date();
    lastCompleteDay.setUTCDate(lastCompleteDay.getUTCDate() - 1);
    if ($("retention-before")) $("retention-before").max = lastCompleteDay.toISOString().slice(0, 10);
    const policy = model.retention;
    if ($("retention-status")) {
      describe($("retention-status"), [
        ["已保存策略", policy.days == null ? "永不自动清理" : `保留 ${policy.days} 天`],
        ["自动清理", policy.enabled ? "已启用" : "已关闭"],
        ["最近执行（UTC）", dateTime(policy.last_run)],
        ["下次执行（UTC）", dateTime(policy.next_run)]
      ]);
    }
    if ($("retention-last-result")) {
      $("retention-last-result").hidden = policy.last_result == null;
      $("retention-last-result").textContent = policy.last_result == null ? "" : JSON.stringify(policy.last_result, null, 2);
    }
    if ($("retention-policy-state")) {
      $("retention-policy-state").textContent = model.retentionDirty ? "有未保存修改，请保存后预览。" : "显示服务已保存策略。";
      $("retention-policy-state").classList.remove("error-text");
    }
  } catch (error) {
    if ($("retention-policy-state")) {
      $("retention-policy-state").textContent = `留存策略读取失败：${error.message}`;
      $("retention-policy-state").classList.add("error-text");
    }
    throw error;
  } finally {
    renderRetentionControls(model);
  }
}

export function readImportPayload() {
  const raw = $("import-comments")?.value.trim(), payload = { video_id: $("import-video")?.value.trim() };
  if (!payload.video_id || !raw) throw new Error("请填写视频标识和评论数据");
  if (raw.startsWith("[") || raw.startsWith("{")) {
    let value;
    try {
      value = JSON.parse(raw);
    } catch (error) {
      throw new Error(`评论 JSON 格式错误：${error.message}`);
    }
    payload.items = Array.isArray(value) ? value : value.items;
    if (!Array.isArray(payload.items)) throw new Error("JSON 必须是评论数组，或含 items 数组的对象");
  } else {
    payload.csv_text = raw;
  }
  const mapping = $("import-mapping")?.value.trim();
  if (mapping) {
    try {
      payload.field_mapping = JSON.parse(mapping);
    } catch (error) {
      throw new Error(`字段映射 JSON 格式错误：${error.message}`);
    }
    if (!payload.field_mapping || typeof payload.field_mapping !== "object" || Array.isArray(payload.field_mapping)) {
      throw new Error("字段映射必须是 JSON 对象");
    }
    const targets = ["comment_id", "platform_user_id", "display_name", "content", "like_count", "created_at", "thread_id", "parent_comment_id"];
    Object.entries(payload.field_mapping).forEach(([target, source]) => {
      if (!targets.includes(target)) throw new Error(`不支持的目标字段：${target}`);
      if (typeof source !== "string" || !source.trim()) throw new Error(`${target} 的来源字段名必须是非空字符串`);
    });
  }
  return payload;
}

export function invalidateImportPreview(model) {
  model.importPreviewSignature = null;
  if ($("submit-import")) $("submit-import").disabled = true;
  if ($("import-preview")) $("import-preview").hidden = true;
  if ($("import-preview-state")) $("import-preview-state").textContent = "输入已变更，请重新预览。";
}

export function initMaintenanceModule(model, {
  api,
  busy,
  notice,
  node,
  cell,
  list,
  describe,
  dateTime,
  refreshCommentsFn,
}) {
  const doLoadRetention = () => loadRetention(model, { api, describe, dateTime });

  // Retention event bindings
  $("retention-form")?.addEventListener("submit", event => {
    event.preventDefault();
    retentionAction("save", async () => {
      const days = $("retention-never")?.checked ? null : Number($("retention-days")?.value);
      const payload = { enabled: !$("retention-never")?.checked, days };
      await api("/api/retention", { method: "PUT", body: payload });
      model.retentionDirty = false;
      await doLoadRetention();
      notice("留存策略已保存。");
    }, model, notice);
  });

  $("retention-never")?.addEventListener("change", () => {
    model.retentionDirty = true;
    renderRetentionControls(model);
  });
  $("retention-days")?.addEventListener("input", () => {
    model.retentionDirty = true;
    renderRetentionControls(model);
  });
  $("retention-before")?.addEventListener("change", () => invalidateRetentionPreview(model));
  $("retention-confirm")?.addEventListener("change", () => renderRetentionControls(model));

  $("retention-preview")?.addEventListener("click", () => retentionAction("preview", async () => {
    const payload = retentionRequest(model), signature = JSON.stringify(payload);
    invalidateRetentionPreview(model, "正在统计待清理记录；尚未删除…");
    const result = await api("/api/retention/preview", { method: "POST", body: payload });
    if (JSON.stringify(retentionRequest(model)) !== signature) return;
    if (result.cutoff == null) throw new Error("服务未返回清理截止时间，请指定日期后重新预览。");
    model.retentionPreview = { signature, cutoff: result.cutoff };
    if ($("retention-preview-cutoff")) {
      $("retention-preview-cutoff").textContent = `删除时间早于 ${result.cutoff} 的适用记录（UTC，边界不包含）。`;
    }
    const countLabels = {
      context_windows: "上下文窗口",
      grounded_memory: "来源检索索引",
      conversations: "对话记录",
      jobs: "直播任务",
      events: "直播事件",
      sessions: "直播会话",
      dynamic_memory_summaries: "运营记忆摘要"
    };
    if ($("retention-preview-counts")) {
      describe($("retention-preview-counts"), Object.entries(result.counts || {}).map(([key, count]) => [countLabels[key] || key, count]));
    }
    if ($("retention-preview-detail")) $("retention-preview-detail").textContent = JSON.stringify(result, null, 2);
    if ($("retention-preview-panel")) $("retention-preview-panel").hidden = false;
    if ($("retention-cleanup-state")) $("retention-cleanup-state").textContent = "预览完成，尚未删除。请核对数量和范围后确认。";
  }, model, notice));

  $("retention-cleanup")?.addEventListener("click", () => retentionAction("cleanup", async () => {
    const preview = model.retentionPreview;
    if (!preview || !$("retention-confirm")?.checked || JSON.stringify(retentionRequest(model)) !== preview.signature) {
      throw new Error("请先预览当前范围并明确确认清理。");
    }
    const result = await api("/api/retention/cleanup", { method: "POST", body: { before: preview.cutoff, confirm: true } });
    invalidateRetentionPreview(model);
    if ($("retention-cleanup-state")) $("retention-cleanup-state").textContent = `清理已完成，实际结果：${JSON.stringify(result.counts || result)}`;
    await doLoadRetention();
    if (model.page === "comments" && typeof refreshCommentsFn === "function") {
      await refreshCommentsFn();
    }
  }, model, notice));

  // Import event bindings
  ["import-video", "import-comments", "import-mapping"].forEach(id => {
    $(id)?.addEventListener("input", () => invalidateImportPreview(model));
  });

  $("preview-import")?.addEventListener("click", () => busy($("preview-import"), async () => {
    if (!$("import-comments-form")?.reportValidity()) return;
    invalidateImportPreview(model);
    if ($("import-preview-state")) $("import-preview-state").textContent = "正在规范化并预览数据…";
    try {
      const payload = readImportPayload(), signature = JSON.stringify(payload);
      const result = await api("/api/comments/import/preview", { method: "POST", body: payload });
      let stillCurrent = false;
      try {
        stillCurrent = JSON.stringify(readImportPayload()) === signature;
      } catch {
        /* Input changed while previewing. */
      }
      if (!stillCurrent) {
        invalidateImportPreview(model);
        return;
      }
      const items = list(result);
      const tbody = $("import-preview-rows");
      if (tbody) {
        tbody.replaceChildren();
        if (!items.length) {
          tbody.append(node("tr", null, "").append(node("td", "没有可预览的数据。")));
        } else {
          items.slice(0, 10).forEach(item => {
            const row = node("tr");
            cell(row, item.comment_id);
            cell(row, item.thread_id || item.comment_id);
            cell(row, item.parent_comment_id || "—");
            cell(row, item.platform_user_id || "未知 ID", item.display_name || "匿名观众");
            cell(row, item.content);
            cell(row, item.like_count ?? 0);
            cell(row, item.created_at || "—");
            tbody.append(row);
          });
        }
      }
      if ($("import-preview")) $("import-preview").hidden = false;
      if ($("import-preview-state")) {
        $("import-preview-state").textContent = `共 ${result.total ?? items.length} 条，预览前 ${Math.min(items.length, 10)} 条；尚未写入。`;
      }
      model.importPreviewSignature = signature;
      if ($("submit-import")) $("submit-import").disabled = !items.length;
    } catch (error) {
      if ($("import-preview-state")) $("import-preview-state").textContent = `预览失败：${error.message}`;
      throw error;
    }
  }));

  $("import-comments-form")?.addEventListener("submit", event => {
    event.preventDefault();
    busy(event.submitter || $("submit-import"), async () => {
      const payload = readImportPayload();
      if (!model.importPreviewSignature || JSON.stringify(payload) !== model.importPreviewSignature) {
        throw new Error("请先预览当前数据，再提交导入");
      }
      const result = await api("/api/comments/import", { method: "POST", body: payload });
      model.importPreviewSignature = null;
      if ($("submit-import")) $("submit-import").disabled = true;
      if ($("import-preview-state")) {
        $("import-preview-state").textContent = `已导入 ${result.imported ?? 0} 条${result.duplicates != null ? `，重复 ${result.duplicates} 条` : ""}。修改数据后可再次预览。`;
      }
      if ($("filter-video")) $("filter-video").value = result.video_id || payload.video_id;
      notice(`导入 ${result.imported ?? 0} 条评论。`);
      if (typeof refreshCommentsFn === "function") await refreshCommentsFn();
    });
  });

  return {
    loadRetention: doLoadRetention,
    renderRetentionControls: () => renderRetentionControls(model),
  };
}
