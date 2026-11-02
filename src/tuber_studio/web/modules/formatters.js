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
        return { label: "🚀 已发布到B站", className: "status-blue", priority: 3 };
      case "rejected":
        return { label: "✕ 已放弃", className: "status-slate", priority: 4 };
      case "publish_uncertain":
        return { label: "⚠️ 发布核验中", className: "status-amber", priority: 5 };
      case "running":
        return { label: "⚡ 正在巡航", className: "status-emerald", priority: 0 };
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
        return { label: "👀 已阅不发", className: "status-slate", priority: 8 };
      case "checkpoint":
        return { label: "💾 进度已保存", className: "status-slate", priority: 9 };
      case "no_subtitles":
        return { label: "🔇 无可用字幕", className: "status-slate", priority: 14 };
      default:
        return { label: status || "未知状态", className: "status-slate", priority: 99 };
    }
  }

  function humanizeReason(reason) {
    if (!reason) return "常规内容运营互动";
    const map = {
      "light_interaction": "优质讨论引发共鸣，大肥鱼参与活跃氛围",
      "specific_detail": "深入呼应视频中的核心观点与亮点细节",
      "private_details": "涉及更深话题，引导至私信交流",
      "no_subtitles": "视频缺少可用字幕或音轨无法识别，已安全跳过",
      "visual_unavailable": "视频多帧视觉提取未就绪，按策略跳过",
      "platform_error": "B站接口网络波动或频控限制",
      "spam": "过滤掉广告营销或低俗刷屏发言",
      "short_comment": "评论内容过短，缺少展开互动的价值",
      "duplicate_intent": "同源互动已有草稿或已发表，绝不重复发言",
      "already_commented": "大肥鱼已在此视频留过言，避免频繁刷屏",
    };
    return map[reason] || reason;
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
    const topLevels = budget.top_levels ?? 0;

    let narrative = "";
    const keywords = (config.keywords && config.keywords.length) ? config.keywords.slice(0, 3).join("、") : "热门关注内容";

    if (round.status === "completed") {
      narrative = `本轮围绕「${keywords}」自主巡航，共检索浏览了 ${reads} 篇视频，深度调用模型分析 ${modelCalls} 次，成功沉淀了 ${topLevels || (modelCalls > 0 ? "1" : "0")} 条高质量互动草稿。`;
    } else if (round.status === "partial") {
      narrative = `本轮检索浏览了 ${reads} 篇视频，部分视频因缺少可用字幕或未触达兴趣点跳过，已完成分析并保留相关进度。`;
    } else if (round.status === "cancelled") {
      narrative = `本轮巡航被手动中止，已阅览的 ${reads} 篇视频进度与已生成的草稿已妥善留存。`;
    } else if (round.status === "failed") {
      narrative = `本轮巡航遇到异常中断：${round.error || "网络或平台限制"}，系统已自动熔断保护。`;
    } else if (round.status === "running") {
      narrative = `当前正在对「${keywords}」进行实时巡航，已阅 ${reads} 篇视频，正在进行深度理解……`;
    } else {
      narrative = `本轮巡航共处理 ${reads} 篇视频，消耗 ${totalTokens} Tokens。`;
    }

    const tokenFormatted = totalTokens > 1000 ? `${(totalTokens / 1000).toFixed(1)}k` : `${totalTokens}`;

    return {
      id: round.id,
      title,
      dateStr,
      status: round.status,
      statusInfo,
      narrative,
      metrics: {
        reads,
        modelCalls,
        tokens: tokenFormatted,
        topLevels,
      },
      raw: round,
    };
  }

  return {
    formatRelativeTime,
    humanizeKind,
    humanizeStatus,
    humanizeReason,
    humanizeRoundReport,
  };
});
