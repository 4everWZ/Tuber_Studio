import { $, safeMessage } from "./core.js";

const actions = {
  matrix_top_level: ["top_level_enabled", "top_level_auto_publish"],
  matrix_reply: ["reply_enabled", "reply_auto_publish"],
  matrix_comment_like: ["comment_like_enabled", "comment_like_auto_publish"],
  matrix_video_like: ["video_like_enabled", "video_like_auto_publish"],
  matrix_video_fav: ["video_fav_enabled", "video_fav_auto_publish"],
};

let saving = false;

export function renderPermissionStatus(status, account) {
  const settings = status.settings;
  const fields = $("matrix-fields");
  fields.disabled = saving || !settings;
  if (settings) {
    $("matrix-video-enabled").checked = settings.discovery_enabled === true;
    for (const [name, [enabled, automatic]] of Object.entries(actions)) {
      const mode = !settings[enabled] ? "0" : settings[automatic] ? "2" : "1";
      const option = document.querySelector(`input[name="${name}"][value="${mode}"]`);
      if (option) option.checked = true;
    }
  }

  const hasAutomatic = Object.values(actions).some(([enabled, automatic]) => settings?.[enabled] && settings[automatic])
    || status.permissions?.private_messages === "auto" || status.permissions?.dynamics === "auto";
  const sendingState = $("automatic-sending-state");
  sendingState.hidden = !hasAutomatic || status.automatic_sending_ready !== false;
  const blockers = (status.blockers || []).map(value => typeof value === "string" ? value : value.message).filter(Boolean);
  const configured = account ? account.configured?.sessdata && account.configured?.csrf : status.effective?.comments?.credentials_configured;
  sendingState.textContent = sendingState.hidden ? "" : blockers.length ? `尚未就绪：${blockers.join("；")}` :
    configured === false ? "请先登录 B 站账号。" : "等待账号或模型连接。";

  const visual = $("matrix-visual-status");
  visual.hidden = typeof status.vision?.images !== "boolean";
  visual.textContent = status.vision?.images ? "观看时读取字幕并抽样画面" : "当前视觉服务未就绪";
}

export function initPermissionMatrix(save, refresh) {
  const fields = $("matrix-fields");
  const feedback = $("matrix-save-feedback");
  fields.addEventListener("change", async ({ target }) => {
    if (fields.disabled || saving) return;
    let change;
    if (target.id === "matrix-video-enabled") {
      change = { discovery_enabled: target.checked };
    } else if (Object.hasOwn(actions, target.name) && ["0", "1", "2"].includes(target.value)) {
      const [enabled, automatic] = actions[target.name];
      change = { [enabled]: target.value !== "0", [automatic]: target.value === "2" };
    } else return;
    saving = fields.disabled = true;
    feedback.textContent = "正在保存…";
    try {
      await save(change);
      feedback.textContent = "已保存";
    } catch (error) {
      feedback.textContent = `保存未确认，请核对当前选项。 ${safeMessage(error.message)}`;
    } finally {
      saving = false;
      await refresh();
    }
  });
}
