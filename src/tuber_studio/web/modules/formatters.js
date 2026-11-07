/**
 * Formatters and Humanizers for Tuber Studio Operations Workbench
 * Converts raw database structures, timestamps, and codes into human-readable Chinese.
 */

(function (root, factory) {
  if (typeof define === 'function' && define.amd) {
    define([], factory);
  } else if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.TuberFormatters = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {

  function parseEpoch(val) {
    if (!val) return 0;
    if (typeof val === 'number') {
      return val > 1e11 ? val : val * 1000;
    }
    const d = new Date(val);
    return isNaN(d.getTime()) ? 0 : d.getTime();
  }

  function formatRelativeTime(val) {
    const time = parseEpoch(val);
    if (!time) return "未知时间";
    const now = Date.now();
    const diff = Math.floor((now - time) / 1000);

    if (diff < 30) return "刚刚";
    if (diff < 60) return `${diff} 秒前`;
    if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
    if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;

    const d = new Date(time);
    const today = new Date();
    const isYesterday = (
      d.getDate() === today.getDate() - 1 &&
      d.getMonth() === today.getMonth() &&
      d.getFullYear() === today.getFullYear()
    );

    const pad = n => String(n).padStart(2, '0');
    const timeStr = `${pad(d.getHours())}:${pad(d.getMinutes())}`;

    if (isYesterday) return `昨天 ${timeStr}`;
    return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${timeStr}`;
  }

  function humanizeKind(kind) {
    switch (kind) {
      case "top_level":
        return { label: "💡 顶层主评", icon: "💡", type: "top_level" };
      case "reply_public":
        return { label: "💬 楼内跟帖", icon: "💬", type: "reply_public" };
      case "video_like":
        return { label: "👍 视频点赞", icon: "👍", type: "video_like" };
      case "video_fav":
        return { label: "⭐ 视频收藏", icon: "⭐", type: "video_fav" };
      case "private_handoff":
        return { label: "✉️ 深度转私信", icon: "✉️", type: "private_handoff" };
      case "mention":
        return { label: "🔔 艾特与提及", icon: "🔔", type: "mention" };
      default:
        return { label: "📝 互动草稿", icon: "📝", type: "draft" };
    }
  }

  function humanizeStatus(status) {
    switch (status) {
      case "pending_review":
        return { label: "⏳ 待审核", className: "status-amber", priority: 1 };
      case "approved":
        return { label: "✓ 已审核通过", className: "status-emerald", priority: 2 };
      case "published":
        return { label: "平台已受理", className: "status-blue", priority: 3 };
      case "rejected":
        return { label: "✕ 已放弃", className: "status-slate", priority: 4 };
      case "publish_uncertain":
        return { label: "⚠️ 发布核验中", className: "status-amber", priority: 5 };
      case "running":
        return { label: "运行中", className: "status-emerald", priority: 0 };
      case "queued":
      case "discovered":
        return { label: "排队中", className: "status-slate", priority: 0 };
      case "claimed":
      case "preparing":
        return { label: "取材中", className: "status-emerald", priority: 0 };
      case "evidence_ready":
        return { label: "材料就绪", className: "status-emerald", priority: 0 };
      case "completed":
        return { label: "🟢 正常完成", className: "status-emerald", priority: 10 };
      case "partial":
        return { label: "🟡 部分完成", className: "status-amber", priority: 11 };
      case "cancelled":
        return { label: "⚪ 已停止", className: "status-slate", priority: 12 };
      case "failed":
        return { label: "🔴 发生异常", className: "status-danger", priority: 13 };
      case "selected":
        return { label: "✨ 已生成互动", className: "status-blue", priority: 2 };
      case "viewed_without_comment":
        return { label: "看过未发", className: "status-slate", priority: 8 };
      case "comments_checked":
        return { label: "已检查评论", className: "status-slate", priority: 8 };
      case "paused":
        return { label: "已暂停，进度保留", className: "status-slate", priority: 9 };
      case "deferred_daily":
        return { label: "今日观看已用完", className: "status-slate", priority: 9 };
      case "recovered":
        return { label: "已恢复", className: "status-slate", priority: 10 };
      case "checkpoint":
        return { label: "💾 进度已保存", className: "status-slate", priority: 9 };
      case "retry_wait":
        return { label: "已记录失败", className: "status-amber", priority: 9 };
      case "no_subtitles":
        return { label: "无可用字幕", className: "status-slate", priority: 14 };
      case "skipped":
        return { label: "已跳过", className: "status-slate", priority: 14 };
      default:
        return { label: status || "未知状态", className: "status-slate", priority: 99 };
    }
  }

  function humanizeReason(reason) {
    if (!reason) return "暂无原因记录";
    const map = {
      "light_interaction": "优质讨论引发共鸣，大肥鱼参与活跃氛围",
      "specific_detail": "深入呼应视频中的核心观点与亮点细节",
      "private_details": "涉及更深话题，引导至私信交流",
      "no_subtitles": "视频无可用字幕，已跳过",
      "visual_unavailable": "视频暂无可用字幕且视觉未就绪，已安全跳过（待后续接入ASR）",
      "platform_error": "B站接口网络波动或频控限制",
      "spam": "过滤掉广告营销或低俗刷屏发言",
      "short_comment": "评论内容过短，缺少展开互动的价值",
      "duplicate_intent": "同源互动已有草稿或已发表，绝不重复发言",
      "already_commented": "大肥鱼已在此视频留过言，避免频繁刷屏",
      "round_budget": "达到本轮资源上限，进度已保存，下一轮继续",
      "daily_watch_limit": "今日观看次数已用完，继续处理评论",
      "daily_reply_limit": "该视频今日回复数已用完，草稿保留到次日",
    };
    return map[reason] || reason;
  }

  function humanizeTaskReason(task) {
    const payload = task.payload || {};
    const rawReason = task.reason || payload.reason;
    const scheduling = task.retry_kind || payload.retry_kind ||
      (["round_budget", "queued", "waiting", "checkpoint", "paused", "next_batch", "daily_watch_limit", "daily_reply_limit"].includes(rawReason) ? rawReason : null);
    const decisions = task.decisions || payload.decisions || [];
    const reasons = [task.explanation, task.reason, payload.explanation, payload.reason,
      ...decisions.map(value => value.explanation || value.reason)]
      .filter(value => typeof value === "string" && value.trim())
      .map(value => value.trim());
    const unique = [...new Set(reasons)].filter(value => value !== scheduling);
    const meaningful = unique.filter(value => !/^[a-z][a-z0-9_]*$/.test(value));
    let content = meaningful.join("；");
    if (!content && unique.length) {
      content = unique.map(value => `${humanizeReason(value)}（历史记录仅保存此标记）`).join("；");
    }
    if (scheduling) {
      const progress = `程序进度：${humanizeReason(scheduling)}`;
      return content ? `${content} · ${progress}` : progress;
    }
    if (content) return content;
    if (task.error || task.error_kind) return `处理未完成：${task.error || humanizeReason(task.error_kind)}`;
    const stages = {
      discovered: "视频已找到，等待读取字幕与画面。", claimed: "已分配处理任务，正在准备材料。",
      preparing: "正在读取字幕与画面，内容判断尚未完成。", evidence_ready: "材料已准备，等待生成内容判断。",
      checkpoint: "处理进度已保存，尚未生成完整的内容判断。", paused: "任务已暂停，保留进度等待继续。",
      queued: "任务已排队，尚未开始读取内容。", running: "任务正在处理，内容判断尚未完成。",
      cancelled: "任务已停止；此历史记录未保存内容判断。", selected: "已生成回应；此历史记录未保存具体原因。",
      viewed_without_comment: "已看过且未生成回复；此历史记录未保存具体原因。",
      comments_checked: "已检查这条视频的新增评论。", deferred_daily: "程序进度：今日观看次数已用完，继续处理评论。",
    };
    return stages[task.status] || "此历史记录未保存具体原因，可查看来源与处理详情。";
  }

  function humanizeRoundReport(round, roundIndex) {
    const statusInfo = humanizeStatus(round.status);
    const dateStr = formatRelativeTime(round.created_at);
    const title = `第 ${roundIndex || round.epoch || round.id?.slice(0, 6) || "1"} 轮巡航 · ${dateStr}`;

    let budget = {};
    if (typeof round.budget === 'string') {
      try { budget = JSON.parse(round.budget); } catch (e) {}
    } else if (typeof round.budget === 'object') {
      budget = round.budget || {};
    }

    let config = {};
    if (typeof round.config === 'string') {
      try { config = JSON.parse(round.config); } catch (e) {}
    } else if (typeof round.config === 'object') {
      config = round.config || {};
    }

    const reads = budget.reads ?? 0;
    const modelCalls = budget.model_calls ?? 0;
    const inputTokens = budget.input_tokens ?? 0;
    const outputTokens = budget.output_tokens ?? 0;
    const totalTokens = inputTokens + outputTokens;
    const interactionOnly = config.interaction_only === true;
    const kind = interactionOnly ? "互动收件箱" : config.discovery_id ? "自动刷视频" : round.preview ? "只读预览" : "全渠道运营";
    const viewed = round.metrics?.viewed;
    const drafts = round.public_drafts;
    const narrative = [kind, viewed == null ? "" : `已处理 ${viewed} 条`,
      drafts == null ? "" : `评论草稿 ${drafts} 条`, round.error || ""].filter(Boolean).join(" · ");

    const tokenFormatted = totalTokens > 1000 ? `${(totalTokens / 1000).toFixed(1)}k` : `${totalTokens}`;

    return {
      id: round.id,
      title,
      dateStr,
      status: round.status,
      statusInfo,
      interactionOnly,
      narrative,
      metrics: {
        reads,
        modelCalls,
        tokens: tokenFormatted,
        drafts,
      },
      raw: round,
    };
  }

  return {
    formatRelativeTime,
    humanizeKind,
    humanizeStatus,
    humanizeReason,
    humanizeTaskReason,
    humanizeRoundReport,
  };
});
